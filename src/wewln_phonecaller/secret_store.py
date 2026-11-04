"""Secrets (Notion token, ARI password, webhook secret) outside of files: the OS credential store.

The store is reached via `keyring`; on Windows this is the Credential Manager (protected by the
user's Windows login). The backend is injectable so tests never touch the real store. Values are
never logged or printed.
"""

from __future__ import annotations

from typing import Any, Protocol

SERVICE_NAME = "wewln-phonecaller"
NOTION_TOKEN_KEY = "notion_token"  # noqa: S105 (the entry name, not a secret)
ARI_PASSWORD_KEY = "ari_password"  # noqa: S105 (the entry name, not a secret)
WEBHOOK_SECRET_KEY = "webhook_secret"  # noqa: S105 (the entry name, not a secret)


class SecretStoreUnavailable(Exception):
    """No usable credential store on this machine (for example, Linux without a keyring)."""


class SecretStore(Protocol):
    name: str

    def get(self, key: str) -> str | None: ...

    def set(self, key: str, value: str) -> None: ...

    def delete(self, key: str) -> bool: ...


class KeyringStore:
    """The `keyring` library; on Windows it uses the Credential Manager."""

    def __init__(self, module: Any | None = None) -> None:
        if module is None:
            try:
                import keyring as module  # noqa: PLC0415  (optional dependency)
            except ImportError as exc:
                raise SecretStoreUnavailable("the 'keyring' package is not installed") from exc
        self._keyring = module
        backend = module.get_keyring()
        self.name = f"keyring ({type(backend).__name__})"
        if type(backend).__name__ == "Keyring" and "fail" in type(backend).__module__:
            raise SecretStoreUnavailable("no keyring backend is available on this system")

    def _call(self, func: str, *args: str) -> Any:
        try:
            return getattr(self._keyring, func)(*args)
        except Exception as exc:  # backends raise many different errors
            if type(exc).__name__ in {"PasswordDeleteError"}:
                raise
            raise SecretStoreUnavailable(f"credential store error ({type(exc).__name__})") from exc

    def get(self, key: str) -> str | None:
        value = self._call("get_password", SERVICE_NAME, key)
        return value or None

    def set(self, key: str, value: str) -> None:
        self._call("set_password", SERVICE_NAME, key, value)

    def delete(self, key: str) -> bool:
        try:
            self._call("delete_password", SERVICE_NAME, key)
        except Exception as exc:
            if type(exc).__name__ == "PasswordDeleteError":
                return False
            raise
        return True


class MemoryStore:
    """In-memory store for tests."""

    name = "memory"

    def __init__(self, values: dict[str, str] | None = None) -> None:
        self.values: dict[str, str] = dict(values or {})

    def get(self, key: str) -> str | None:
        return self.values.get(key)

    def set(self, key: str, value: str) -> None:
        self.values[key] = value

    def delete(self, key: str) -> bool:
        return self.values.pop(key, None) is not None


class _UnavailableStore:
    def __init__(self, reason: str) -> None:
        self.name = "unavailable"
        self.reason = reason

    def get(self, key: str) -> str | None:
        return None

    def set(self, key: str, value: str) -> None:
        raise SecretStoreUnavailable(self.reason)

    def delete(self, key: str) -> bool:
        raise SecretStoreUnavailable(self.reason)


_override: SecretStore | None = None


def set_default_store(store: SecretStore | None) -> None:
    """Replace the store used by default (tests use this)."""
    global _override
    _override = store


def default_store() -> SecretStore:
    if _override is not None:
        return _override
    try:
        return KeyringStore()
    except SecretStoreUnavailable as exc:
        return _UnavailableStore(str(exc))
