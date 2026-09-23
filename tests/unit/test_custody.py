"""VaultStore error mapping (design §5/§9) against a stubbed hvac client:
the CAS-conflict message becomes CasConflict, a missing path is None (never
an outage), and everything else is CustodyUnavailable (fail closed,
distinguishably). A change in hvac's error text would break these first."""
import pytest
from hvac import exceptions as hvac_exc
from unit_helpers import make_config

from token_broker.custody import CasConflict, CustodyTokenRejected, CustodyUnavailable, VaultStore

CAS_MESSAGE = "check-and-set parameter did not match the current version"


class FakeKV:
    def __init__(self, error=None, result=None):
        self.error, self.result, self.calls = error, result, []

    def _do(self, name, **kw):
        self.calls.append((name, kw))
        if self.error is not None:
            raise self.error
        return self.result

    def read_secret_version(self, **kw):
        return self._do("read", **kw)

    def create_or_update_secret(self, **kw):
        return self._do("write", **kw)

    def delete_metadata_and_all_versions(self, **kw):
        return self._do("delete", **kw)

    def list_secrets(self, **kw):
        return self._do("list", **kw)


def store_with(kv: FakeKV) -> VaultStore:
    store = VaultStore(make_config())

    class _Secrets:
        class kv:  # noqa: N801 — mirrors hvac's attribute path
            v2 = None
    _Secrets.kv.v2 = kv

    class _Client:
        secrets = _Secrets
    store._client = _Client()
    return store


async def test_read_returns_entry_and_version():
    kv = FakeKV(result={"data": {"data": {"state": "ACTIVE"},
                                 "metadata": {"version": 4}}})
    assert await store_with(kv).read("mockhub", "alice") == ({"state": "ACTIVE"}, 4)
    name, kw = kv.calls[0]
    assert kw["path"] == "mockhub/alice" and kw["mount_point"] == "vendor-tokens"


async def test_read_missing_path_is_none():
    assert await store_with(FakeKV(error=hvac_exc.InvalidPath("not found"))).read("v", "s") is None


@pytest.mark.parametrize("error", [
    hvac_exc.Forbidden("permission denied"),
    hvac_exc.VaultDown("sealed"),
    ConnectionError("refused"),
])
async def test_read_other_errors_fail_closed(error):
    with pytest.raises(CustodyUnavailable):
        await store_with(FakeKV(error=error)).read("v", "s")


async def test_write_returns_new_version_and_passes_cas():
    kv = FakeKV(result={"data": {"version": 5}})
    assert await store_with(kv).write("v", "s", {"state": "ACTIVE"}, cas=4) == 5
    assert kv.calls[0][1]["cas"] == 4


async def test_write_cas_mismatch_is_cas_conflict():
    with pytest.raises(CasConflict):
        store = store_with(FakeKV(error=hvac_exc.InvalidRequest(CAS_MESSAGE)))
        await store.write("v", "s", {}, cas=1)


async def test_write_other_invalid_request_is_unavailable():
    with pytest.raises(CustodyUnavailable):
        await store_with(FakeKV(error=hvac_exc.InvalidRequest("bad json"))).write("v", "s", {})


async def test_write_transport_error_is_unavailable():
    with pytest.raises(CustodyUnavailable):
        await store_with(FakeKV(error=ConnectionError("refused"))).write("v", "s", {})


async def test_delete_missing_is_silent_and_errors_fail_closed():
    await store_with(FakeKV(error=hvac_exc.InvalidPath("gone"))).delete("v", "s")
    with pytest.raises(CustodyUnavailable):
        await store_with(FakeKV(error=hvac_exc.Forbidden("denied"))).delete("v", "s")


async def test_list_subjects_drops_folders_and_handles_missing():
    kv = FakeKV(result={"data": {"keys": ["alice", "bob", "nested/"]}})
    assert await store_with(kv).list_subjects("mockhub") == ["alice", "bob"]
    assert await store_with(FakeKV(error=hvac_exc.InvalidPath("none"))).list_subjects("v") == []


async def test_read_client_uses_clients_mount():
    kv = FakeKV(result={"data": {"data": {"client_id": "cid"}}})
    assert await store_with(kv).read_client("mockhub") == {"client_id": "cid"}
    assert kv.calls[0][1]["mount_point"] == "vendor-clients"
    assert await store_with(FakeKV(error=hvac_exc.InvalidPath("none"))).read_client("v") is None


class FakeTokenAPI:
    def __init__(self, error=None, lookup=None, renew=None):
        self.error, self.lookup, self.renew = error, lookup, renew

    def lookup_self(self):
        if self.error is not None:
            raise self.error
        return self.lookup

    def renew_self(self):
        if self.error is not None:
            raise self.error
        return self.renew


def store_with_token(api: FakeTokenAPI) -> VaultStore:
    store = VaultStore(make_config())

    class _Auth:
        token = api

    class _Client:
        auth = _Auth
    store._client = _Client()
    return store


async def test_token_status_and_renewal():
    store = store_with_token(FakeTokenAPI(
        lookup={"data": {"ttl": 2763169, "renewable": True}},
        renew={"auth": {"lease_duration": 2763177, "renewable": True}}))
    assert await store.token_status() == (2763169, True)
    assert await store.renew_token() == 2763177


async def test_root_style_token_reports_no_expiry():
    store = store_with_token(FakeTokenAPI(lookup={"data": {"ttl": 0, "renewable": False}}))
    assert await store.token_status() == (0, False)


@pytest.mark.parametrize("error,expected", [
    (hvac_exc.Forbidden("permission denied"), CustodyTokenRejected),
    (hvac_exc.Unauthorized("bad token"), CustodyTokenRejected),
    (ConnectionError("refused"), CustodyUnavailable),
])
async def test_token_errors_distinguish_rejection_from_outage(error, expected):
    store = store_with_token(FakeTokenAPI(error=error))
    for call in (store.token_status, store.renew_token):
        with pytest.raises(expected) as exc:
            await call()
        assert type(exc.value) is expected
