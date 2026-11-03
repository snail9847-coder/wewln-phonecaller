"""Application errors. Each one carries a stable machine-readable code and an HTTP status."""

from __future__ import annotations

from typing import Any


class WewlnError(Exception):
    """Base class for expected application errors."""

    code = "internal_error"
    http_status = 500

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        http_status: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        if http_status is not None:
            self.http_status = http_status
        self.details: dict[str, Any] = details or {}


class ConfigError(WewlnError):
    """The configuration is invalid. `problems` lists every issue, never secret values."""

    code = "config_invalid"

    def __init__(self, problems: list[str]) -> None:
        self.problems = list(problems)
        super().__init__(
            "Invalid configuration: " + "; ".join(self.problems),
            details={"problems": self.problems},
        )


class StorageError(WewlnError):
    code = "storage_error"
    http_status = 503


class AuthError(WewlnError):
    code = "unauthorized"
    http_status = 401


class RealCallsBlockedError(WewlnError):
    code = "real_calls_blocked"
    http_status = 409
