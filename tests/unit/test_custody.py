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
    assert kw["path"] == "mockhub/sub-b64.YWxpY2U" and kw["mount_point"] == "vendor-tokens"


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


# ------------------------------------------------------------ path encoding (M8)

class KVStore:
    """Stateful KV v2 fake: versions, check-and-set, list, delete."""

    def __init__(self):
        self.data: dict[str, tuple[dict, int]] = {}

    def read_secret_version(self, path, mount_point, raise_on_deleted_version):
        if path not in self.data:
            raise hvac_exc.InvalidPath("no secret")
        entry, version = self.data[path]
        return {"data": {"data": dict(entry), "metadata": {"version": version}}}

    def create_or_update_secret(self, path, secret, cas, mount_point):
        current = self.data.get(path, ({}, 0))[1]
        if cas is not None and cas != current:
            raise hvac_exc.InvalidRequest(CAS_MESSAGE)
        self.data[path] = (dict(secret), current + 1)
        return {"data": {"version": current + 1}}

    def delete_metadata_and_all_versions(self, path, mount_point):
        if path not in self.data:
            raise hvac_exc.InvalidPath("no secret")
        del self.data[path]

    def list_secrets(self, path, mount_point):
        prefix = path.rstrip("/") + "/"
        keys = sorted({p[len(prefix):].split("/")[0] + ("/" if "/" in p[len(prefix):] else "")
                       for p in self.data if p.startswith(prefix)})
        if not keys:
            raise hvac_exc.InvalidPath("none")
        return {"data": {"keys": keys}}


@pytest.fixture
def kv_store():
    kv = KVStore()
    return store_with(kv), kv


@pytest.mark.parametrize("sub", ["alice", "https://idp.example/users/42", "a/b/../c",
                                 "user with spaces", "ünï-cødé", "sub-b64.lookalike"])
async def test_any_subject_is_one_key_and_round_trips(kv_store, sub):
    store, kv = kv_store
    await store.write("mockhub", sub, {"state": "ACTIVE"})
    [path] = kv.data
    assert path.count("/") == 1 and path.startswith("mockhub/sub-b64.")
    assert await store.read("mockhub", sub) == ({"state": "ACTIVE"}, 1)
    assert await store.list_subjects("mockhub") == [sub]
    await store.delete("mockhub", sub)
    assert kv.data == {}


async def test_legacy_entry_is_read_then_migrated_on_its_next_cas_write(kv_store):
    store, kv = kv_store
    kv.data["mockhub/alice"] = ({"state": "ACTIVE", "g": 1}, 5)       # pre-encoding entry
    entry, ver = await store.read("mockhub", "alice")
    assert (entry["g"], ver) == (1, 5)
    new_ver = await store.write("mockhub", "alice", {"state": "ACTIVE", "g": 2}, cas=ver)
    assert new_ver == 1
    assert list(kv.data) == ["mockhub/sub-b64.YWxpY2U"]              # legacy removed
    assert await store.read("mockhub", "alice") == ({"state": "ACTIVE", "g": 2}, 1)
    await store.write("mockhub", "alice", {"g": 3}, cas=1)          # normal CAS from here


async def test_stale_cas_against_a_legacy_entry_still_conflicts(kv_store):
    store, kv = kv_store
    kv.data["mockhub/alice"] = ({"g": 1}, 5)
    await store.read("mockhub", "alice")
    with pytest.raises(CasConflict):
        await store.write("mockhub", "alice", {"g": 2}, cas=4)
    assert "mockhub/alice" in kv.data


async def test_losing_the_migration_race_is_a_cas_conflict(kv_store):
    """Another replica migrated the entry after we read the legacy copy."""
    store, kv = kv_store
    kv.data["mockhub/alice"] = ({"g": 1}, 5)
    await store.read("mockhub", "alice")
    kv.data["mockhub/sub-b64.YWxpY2U"] = ({"g": 2}, 1)
    del kv.data["mockhub/alice"]
    with pytest.raises(CasConflict):
        await store.write("mockhub", "alice", {"g": 9}, cas=5)
    assert kv.data["mockhub/sub-b64.YWxpY2U"] == ({"g": 2}, 1)


async def test_consent_overwrite_removes_a_legacy_entry(kv_store):
    store, kv = kv_store
    kv.data["mockhub/alice"] = ({"g": 1}, 5)
    await store.write("mockhub", "alice", {"g": "fresh"}, cas=None)
    assert list(kv.data) == ["mockhub/sub-b64.YWxpY2U"]


async def test_listing_mixes_encoded_and_legacy_without_duplicates(kv_store):
    store, kv = kv_store
    kv.data["mockhub/alice"] = ({}, 1)                     # legacy, unmigrated
    kv.data["mockhub/sub-b64.YWxpY2U"] = ({}, 1)           # same subject, migrated
    kv.data["mockhub/bob"] = ({}, 1)
    await store.write("mockhub", "carol/x", {})
    assert sorted(await store.list_subjects("mockhub")) == ["alice", "bob", "carol/x"]


async def test_delete_removes_both_paths(kv_store):
    store, kv = kv_store
    kv.data["mockhub/alice"] = ({}, 1)
    kv.data["mockhub/sub-b64.YWxpY2U"] = ({}, 1)
    await store.delete("mockhub", "alice")
    assert kv.data == {}
