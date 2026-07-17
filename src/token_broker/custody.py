"""Token custody behind the blueprint §2 protocol.

The default backend is OpenBao / Vault KV v2: mounts vendor-tokens/ and
vendor-clients/. The entry's refresh_generation is the §9 monotonic
counter; the KV v2 version doubles as the compare-and-swap handle (cas= on
write fails if another writer moved the entry). Custody unavailability
fails CLOSED (§10): callers translate CustodyUnavailable into 503, and it
is never conflated with "entry absent" (which would turn an outage into a
mass re-consent stampede).
"""
from typing import Protocol

import hvac
from hvac import exceptions as hvac_exc

from .config import Config

TOKENS_MOUNT = "vendor-tokens"
CLIENTS_MOUNT = "vendor-clients"


class CustodyUnavailable(Exception):
    pass


class CasConflict(Exception):
    pass


class Custody(Protocol):
    def read(self, vendor: str, sub: str) -> tuple[dict, int] | None: ...
    def write(self, vendor: str, sub: str, entry: dict, cas: int | None = None) -> int: ...
    def delete(self, vendor: str, sub: str) -> None: ...
    def list_subjects(self, vendor: str) -> list[str]: ...
    def read_client(self, vendor: str) -> dict | None: ...


class VaultStore:
    """KV v2 binding (OpenBao or HashiCorp Vault)."""

    def __init__(self, cfg: Config):
        # Explicit timeout so an unreachable/frozen vault surfaces as
        # CustodyUnavailable (→ 503, §10 fail-closed) in bounded time
        # instead of hanging the resolve.
        self._client = hvac.Client(
            url=cfg.vault_addr, token=cfg.vault_token, timeout=cfg.vault_timeout_s)

    @staticmethod
    def _path(vendor: str, sub: str) -> str:
        return f"{vendor}/{sub}"

    def read(self, vendor: str, sub: str) -> tuple[dict, int] | None:
        """Return (entry, kv_version) or None if absent."""
        try:
            resp = self._client.secrets.kv.v2.read_secret_version(
                path=self._path(vendor, sub), mount_point=TOKENS_MOUNT,
                raise_on_deleted_version=True)
        except hvac_exc.InvalidPath:
            return None
        except Exception as exc:
            raise CustodyUnavailable(str(exc)) from exc
        return resp["data"]["data"], resp["data"]["metadata"]["version"]

    def write(self, vendor: str, sub: str, entry: dict, cas: int | None = None) -> int:
        """Write the entry; cas=N fails with CasConflict if the version moved.
        cas=0 requires the entry not to exist; cas=None overwrites (re-consent)."""
        try:
            resp = self._client.secrets.kv.v2.create_or_update_secret(
                path=self._path(vendor, sub), secret=entry, cas=cas, mount_point=TOKENS_MOUNT)
        except hvac_exc.InvalidRequest as exc:
            if "check-and-set" in str(exc):
                raise CasConflict(str(exc)) from exc
            raise CustodyUnavailable(str(exc)) from exc
        except Exception as exc:
            raise CustodyUnavailable(str(exc)) from exc
        return resp["data"]["version"]

    def delete(self, vendor: str, sub: str) -> None:
        try:
            self._client.secrets.kv.v2.delete_metadata_and_all_versions(
                path=self._path(vendor, sub), mount_point=TOKENS_MOUNT)
        except hvac_exc.InvalidPath:
            pass
        except Exception as exc:
            raise CustodyUnavailable(str(exc)) from exc

    def list_subjects(self, vendor: str) -> list[str]:
        """Subs with an entry under this vendor (KV v2 list). Empty if none."""
        try:
            resp = self._client.secrets.kv.v2.list_secrets(
                path=vendor, mount_point=TOKENS_MOUNT)
        except hvac_exc.InvalidPath:
            return []
        except Exception as exc:
            raise CustodyUnavailable(str(exc)) from exc
        return [k for k in resp["data"]["keys"] if not k.endswith("/")]

    def read_client(self, vendor: str) -> dict | None:
        """Per-vendor confidential client credential (design §3/§5)."""
        try:
            resp = self._client.secrets.kv.v2.read_secret_version(
                path=vendor, mount_point=CLIENTS_MOUNT, raise_on_deleted_version=True)
        except hvac_exc.InvalidPath:
            return None
        except Exception as exc:
            raise CustodyUnavailable(str(exc)) from exc
        return resp["data"]["data"]
