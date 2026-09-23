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
import logging
from typing import Protocol

import hvac
from hvac import exceptions as hvac_exc

from .config import Config

TOKENS_MOUNT = "vendor-tokens"
CLIENTS_MOUNT = "vendor-clients"
log = logging.getLogger(__name__)


class CustodyUnavailable(Exception):
    """Custody backend unreachable or refusing. The message is fixed: raw
    client errors carry hostnames and go to the debug log only."""

    def __init__(self, cause: Exception | None = None):
        super().__init__("custody backend unavailable")
        if cause is not None:
            log.debug("custody error: %r", cause)


class CasConflict(Exception):
    pass


class Custody(Protocol):
    async def read(self, vendor: str, sub: str) -> tuple[dict, int] | None: ...
    async def write(self, vendor: str, sub: str, entry: dict,
                    cas: int | None = None) -> int: ...
    async def delete(self, vendor: str, sub: str) -> None: ...
    async def list_subjects(self, vendor: str) -> list[str]: ...
    async def read_client(self, vendor: str) -> dict | None: ...


class VaultStore:
    """KV v2 binding (OpenBao or HashiCorp Vault)."""

    def __init__(self, cfg: Config):
        # Explicit timeout so an unreachable/frozen vault surfaces as
        # CustodyUnavailable (→ 503, §9 fail-closed) in bounded time
        # instead of hanging the resolve.
        self._client = hvac.Client(
            url=cfg.vault_addr, token=cfg.vault_token, timeout=cfg.vault_timeout_s)

    @staticmethod
    def _path(vendor: str, sub: str) -> str:
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

    # Blocking implementations (worker thread only).

    def _read(self, vendor: str, sub: str) -> tuple[dict, int] | None:
        try:
            resp = self._client.secrets.kv.v2.read_secret_version(
                path=self._path(vendor, sub), mount_point=TOKENS_MOUNT,
                raise_on_deleted_version=True)
        except hvac_exc.InvalidPath:
            return None
        except Exception as exc:
            raise CustodyUnavailable(exc) from exc
        return resp["data"]["data"], resp["data"]["metadata"]["version"]

    def _write(self, vendor: str, sub: str, entry: dict, cas: int | None) -> int:
        try:
            resp = self._client.secrets.kv.v2.create_or_update_secret(
                path=self._path(vendor, sub), secret=entry, cas=cas, mount_point=TOKENS_MOUNT)
        except hvac_exc.InvalidRequest as exc:
            if "check-and-set" in str(exc):
                raise CasConflict(str(exc)) from exc
            raise CustodyUnavailable(exc) from exc
        except Exception as exc:
            raise CustodyUnavailable(exc) from exc
        return resp["data"]["version"]

    def _delete(self, vendor: str, sub: str) -> None:
        try:
            self._client.secrets.kv.v2.delete_metadata_and_all_versions(
                path=self._path(vendor, sub), mount_point=TOKENS_MOUNT)
        except hvac_exc.InvalidPath:
            pass
        except Exception as exc:
            raise CustodyUnavailable(exc) from exc

    def _list_subjects(self, vendor: str) -> list[str]:
        try:
            resp = self._client.secrets.kv.v2.list_secrets(
                path=vendor, mount_point=TOKENS_MOUNT)
        except hvac_exc.InvalidPath:
            return []
        except Exception as exc:
            raise CustodyUnavailable(exc) from exc
        return [k for k in resp["data"]["keys"] if not k.endswith("/")]

    def _read_client(self, vendor: str) -> dict | None:
        try:
            resp = self._client.secrets.kv.v2.read_secret_version(
                path=vendor, mount_point=CLIENTS_MOUNT, raise_on_deleted_version=True)
        except hvac_exc.InvalidPath:
            return None
        except Exception as exc:
            raise CustodyUnavailable(exc) from exc
        return resp["data"]["data"]
