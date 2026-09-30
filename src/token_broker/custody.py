"""Token custody behind the design §5 custody contract.

The default backend is OpenBao / Vault KV v2: mounts vendor-tokens/ and
vendor-clients/. The entry's refresh_generation is the §8 monotonic
counter; the KV v2 version doubles as the compare-and-swap handle (cas= on
write fails if another writer moved the entry). Custody unavailability
fails CLOSED (§9): callers translate CustodyUnavailable into 503, and it
is never conflated with "entry absent" (which would turn an outage into a
mass re-consent stampede).

The interface is async. hvac is a blocking client, so every call runs in a
worker thread: a slow or frozen backend never stalls the event loop, and
cache hits keep serving during a custody outage (§9).
"""

import asyncio
import base64
import logging
from typing import Protocol

import hvac
from hvac import exceptions as hvac_exc

from .config import Config

TOKENS_MOUNT = "vendor-tokens"
CLIENTS_MOUNT = "vendor-clients"
log = logging.getLogger(__name__)


# Subjects are stored under an encoded path segment, so any `sub` the hub
# issues (URIs with "/", dots, spaces) is exactly one KV key: never a nested
# folder the sweeper cannot list (review M8). Entries written before this
# encoding sit at the raw path; they are still read, and move to the encoded
# path on their next write.
SUB_PREFIX = "sub-b64."


def encode_sub(sub: str) -> str:
    return SUB_PREFIX + base64.urlsafe_b64encode(sub.encode()).rstrip(b"=").decode()


def decode_sub(name: str) -> str:
    """KV key name back to the subject; a legacy raw name is the subject."""
    if not name.startswith(SUB_PREFIX):
        return name
    raw = name[len(SUB_PREFIX) :]
    return base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode()


class CustodyUnavailable(Exception):
    """Custody backend unreachable or refusing. The message is fixed: raw
    client errors carry hostnames and go to the debug log only."""

    MESSAGE = "custody backend unavailable"

    def __init__(self, cause: Exception | None = None):
        super().__init__(self.MESSAGE)
        if cause is not None:
            log.debug("custody error: %r", cause)


class CustodyTokenRejected(CustodyUnavailable):
    """The backend is reachable but refuses the broker's own token (expired,
    revoked, or wrong): a credential problem, not an outage."""

    MESSAGE = "custody token rejected"


class CasConflict(Exception):
    pass


class Custody(Protocol):
    async def read(self, vendor: str, sub: str) -> tuple[dict, int] | None: ...
    async def write(self, vendor: str, sub: str, entry: dict, cas: int | None = None) -> int: ...
    async def delete(self, vendor: str, sub: str) -> None: ...
    async def list_subjects(self, vendor: str) -> list[str]: ...
    async def read_client(self, vendor: str) -> dict | None: ...
    async def token_status(self) -> tuple[int, bool]: ...
    async def renew_token(self) -> int: ...


class VaultStore:
    """KV v2 binding (OpenBao or HashiCorp Vault)."""

    def __init__(self, cfg: Config):
        # Explicit timeout so an unreachable/frozen vault surfaces as
        # CustodyUnavailable (→ 503, §9 fail-closed) in bounded time
        # instead of hanging the resolve.
        self._client = hvac.Client(
            url=cfg.vault_addr, token=cfg.vault_token, timeout=cfg.vault_timeout_s
        )
        # {(vendor, sub): KV version} for entries last read at their legacy
        # raw path and not yet migrated to the encoded path.
        self._legacy: dict[tuple[str, str], int] = {}

    @staticmethod
    def _path(vendor: str, sub: str) -> str:
        return f"{vendor}/{encode_sub(sub)}"

    @staticmethod
    def _legacy_path(vendor: str, sub: str) -> str:
        return f"{vendor}/{sub}"

    # Async interface: each call runs the blocking hvac method in a thread.

    async def read(self, vendor: str, sub: str) -> tuple[dict, int] | None:
        """Return (entry, kv_version) or None if absent."""
        return await asyncio.to_thread(self._read, vendor, sub)

    async def write(self, vendor: str, sub: str, entry: dict, cas: int | None = None) -> int:
        """Write the entry; cas=N fails with CasConflict if the version moved.
        cas=0 requires the entry not to exist; cas=None overwrites (re-consent)."""
        return await asyncio.to_thread(self._write, vendor, sub, entry, cas)

    async def delete(self, vendor: str, sub: str) -> None:
        await asyncio.to_thread(self._delete, vendor, sub)

    async def list_subjects(self, vendor: str) -> list[str]:
        """Subs with an entry under this vendor (KV v2 list). Empty if none."""
        return await asyncio.to_thread(self._list_subjects, vendor)

    async def read_client(self, vendor: str) -> dict | None:
        """Per-vendor confidential client credential (design §3/§5)."""
        return await asyncio.to_thread(self._read_client, vendor)

    async def token_status(self) -> tuple[int, bool]:
        """(seconds of TTL left, renewable) for the broker's own token.
        A TTL of 0 means the token never expires."""
        return await asyncio.to_thread(self._token_status)

    async def renew_token(self) -> int:
        """Renew the broker's own token; returns the new TTL in seconds."""
        return await asyncio.to_thread(self._renew_token)

    # Blocking implementations (worker thread only).

    def _token_status(self) -> tuple[int, bool]:
        try:
            data = self._client.auth.token.lookup_self()["data"]
        except (hvac_exc.Forbidden, hvac_exc.Unauthorized) as exc:
            raise CustodyTokenRejected(exc) from exc
        except Exception as exc:
            raise CustodyUnavailable(exc) from exc
        return int(data.get("ttl") or 0), bool(data.get("renewable"))

    def _renew_token(self) -> int:
        try:
            return int(self._client.auth.token.renew_self()["auth"]["lease_duration"])
        except (hvac_exc.Forbidden, hvac_exc.Unauthorized) as exc:
            raise CustodyTokenRejected(exc) from exc
        except Exception as exc:
            raise CustodyUnavailable(exc) from exc

    def _read(self, vendor: str, sub: str) -> tuple[dict, int] | None:
        key = (vendor, sub)
        found = self._read_at(self._path(vendor, sub))
        if found is not None:
            self._legacy.pop(key, None)
            return found
        found = self._read_at(self._legacy_path(vendor, sub))
        if found is not None:
            self._legacy[key] = found[1]
        return found

    def _read_at(self, path: str) -> tuple[dict, int] | None:
        try:
            resp = self._client.secrets.kv.v2.read_secret_version(
                path=path, mount_point=TOKENS_MOUNT, raise_on_deleted_version=True
            )
        except hvac_exc.InvalidPath:
            return None
        except Exception as exc:
            raise CustodyUnavailable(exc) from exc
        return resp["data"]["data"], resp["data"]["metadata"]["version"]

    def _write(self, vendor: str, sub: str, entry: dict, cas: int | None) -> int:
        key = (vendor, sub)
        if cas is not None and self._legacy.get(key) == cas:
            # CAS against a legacy entry we just read: migrate. Create the
            # encoded entry only if nobody else has (cas=0), then remove the
            # legacy one. A replica that migrated first makes this a
            # CasConflict, which is the right answer.
            version = self._write_at(self._path(vendor, sub), entry, cas=0)
            self._legacy.pop(key, None)
            self._delete_legacy(vendor, sub)
            return version
        version = self._write_at(self._path(vendor, sub), entry, cas)
        if cas is None:  # an overwrite (consent) supersedes any legacy entry
            self._legacy.pop(key, None)
            self._delete_legacy(vendor, sub)
        return version

    def _delete_legacy(self, vendor: str, sub: str) -> None:
        """Best effort: the new entry is already written; a leftover legacy
        entry is shadowed by it and removed on the next delete."""
        try:
            self._delete_at(self._legacy_path(vendor, sub))
        except CustodyUnavailable:
            log.debug("legacy custody entry not removed for %s", vendor)

    def _write_at(self, path: str, entry: dict, cas: int | None) -> int:
        try:
            resp = self._client.secrets.kv.v2.create_or_update_secret(
                path=path, secret=entry, cas=cas, mount_point=TOKENS_MOUNT
            )
        except hvac_exc.InvalidRequest as exc:
            if "check-and-set" in str(exc):
                raise CasConflict(str(exc)) from exc
            raise CustodyUnavailable(exc) from exc
        except Exception as exc:
            raise CustodyUnavailable(exc) from exc
        return resp["data"]["version"]

    def _delete(self, vendor: str, sub: str) -> None:
        self._delete_at(self._path(vendor, sub))
        self._delete_at(self._legacy_path(vendor, sub))
        self._legacy.pop((vendor, sub), None)

    def _delete_at(self, path: str) -> None:
        try:
            self._client.secrets.kv.v2.delete_metadata_and_all_versions(
                path=path, mount_point=TOKENS_MOUNT
            )
        except hvac_exc.InvalidPath:
            pass
        except Exception as exc:
            raise CustodyUnavailable(exc) from exc

    def _list_subjects(self, vendor: str) -> list[str]:
        try:
            resp = self._client.secrets.kv.v2.list_secrets(path=vendor, mount_point=TOKENS_MOUNT)
        except hvac_exc.InvalidPath:
            return []
        except Exception as exc:
            raise CustodyUnavailable(exc) from exc
        # Decoded, deduplicated (mid-migration a subject may exist at both
        # paths); legacy folder keys ("a/") are subjects with "/" that only a
        # direct read can reach, so they are skipped as before.
        subs = [decode_sub(k) for k in resp["data"]["keys"] if not k.endswith("/")]
        return list(dict.fromkeys(subs))

    def _read_client(self, vendor: str) -> dict | None:
        try:
            resp = self._client.secrets.kv.v2.read_secret_version(
                path=vendor, mount_point=CLIENTS_MOUNT, raise_on_deleted_version=True
            )
        except hvac_exc.InvalidPath:
            return None
        except Exception as exc:
            raise CustodyUnavailable(exc) from exc
        return resp["data"]["data"]
