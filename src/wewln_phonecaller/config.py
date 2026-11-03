"""Configuration from environment variables (prefix WEWLN_) and an optional .env file.

Precedence: explicit arguments > environment variables > .env file > defaults.
Validation runs at startup; an invalid configuration stops the program (fail closed).
"""

from __future__ import annotations

import ipaddress
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, SecretStr, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .errors import ConfigError

ENV_PREFIX = "WEWLN_"
PRODUCTION_CONFIRM_PHRASE = "I_ACCEPT_REAL_CALLS"
MIN_API_TOKEN_LENGTH = 32
# Conversation languages. Russian is mandatory; more languages are added here later.
SUPPORTED_LANGUAGES: dict[str, str] = {"ru-RU": "Русский"}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX,
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        validate_default=True,
    )

    # Safety. Simulation is the default; production also needs the confirmation phrase.
    mode: Literal["simulation", "production"] = "simulation"
    production_confirm: str = ""
    kill_switch: bool = False
    # Only the simulation provider exists in this version.
    telephony_provider: Literal["mock"] = "mock"

    # Local HTTP API.
    http_host: str = "127.0.0.1"
    http_port: int = Field(default=8787, ge=1, le=65535)
    api_token: SecretStr | None = None

    # Storage and logs.
    data_dir: Path = Path("data")
    db_path: Path | None = None
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_format: Literal["json", "text"] = "json"
    log_to_file: bool = True
    log_file: Path | None = None

    # Conversations and scheduling.
    default_language: str = "ru-RU"
    timezone: str = "UTC"

    # Notion is optional: the backend works without it.
    notion_enabled: bool = False
    notion_token: SecretStr | None = None

    @field_validator("http_host")
    @classmethod
    def _check_host(cls, value: str) -> str:
        value = value.strip()
        if value == "localhost":
            return value
        try:
            ipaddress.ip_address(value)
        except ValueError as exc:
            raise ValueError("must be an IP address or 'localhost'") from exc
        return value

    @field_validator("api_token", "notion_token", mode="before")
    @classmethod
    def _blank_secret_is_unset(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("api_token")
    @classmethod
    def _check_api_token(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and len(value.get_secret_value()) < MIN_API_TOKEN_LENGTH:
            raise ValueError(f"must be at least {MIN_API_TOKEN_LENGTH} characters long")
        return value

    @field_validator("default_language")
    @classmethod
    def _check_language(cls, value: str) -> str:
        if value not in SUPPORTED_LANGUAGES:
            raise ValueError("unsupported language; supported: " + ", ".join(SUPPORTED_LANGUAGES))
        return value

    @field_validator("timezone")
    @classmethod
    def _check_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(
                "unknown IANA time zone (on Windows the 'tzdata' package provides the database)"
            ) from exc
        return value

    @model_validator(mode="after")
    def _cross_checks(self) -> Settings:
        problems: list[str] = []
        if self.mode == "production" and self.production_confirm != PRODUCTION_CONFIRM_PHRASE:
            problems.append(
                "WEWLN_MODE=production requires "
                f"WEWLN_PRODUCTION_CONFIRM={PRODUCTION_CONFIRM_PHRASE}"
            )
        if not self.is_loopback and self.api_token is None:
            problems.append("WEWLN_HTTP_HOST is not a loopback address: set WEWLN_API_TOKEN")
        if self.notion_enabled and self.notion_token is None:
            problems.append("WEWLN_NOTION_ENABLED=true requires WEWLN_NOTION_TOKEN")
        if problems:
            raise ValueError(" | ".join(problems))
        return self

    @property
    def is_loopback(self) -> bool:
        if self.http_host == "localhost":
            return True
        return ipaddress.ip_address(self.http_host).is_loopback

    @property
    def resolved_db_path(self) -> Path:
        return self.db_path or self.data_dir / "wewln.db"

    @property
    def resolved_log_file(self) -> Path | None:
        if not self.log_to_file:
            return None
        return self.log_file or self.data_dir / "logs" / "wewln.log"

    def secret_values(self) -> list[str]:
        """Plain secret values, only so that logging can mask them."""
        return [s.get_secret_value() for s in (self.api_token, self.notion_token) if s is not None]

    def redacted_summary(self) -> dict[str, Any]:
        """Settings safe to print or return over the API: secrets appear only as set / not set."""
        return {
            "mode": self.mode,
            "production_confirmed": self.mode == "production"
            and self.production_confirm == PRODUCTION_CONFIRM_PHRASE,
            "kill_switch": self.kill_switch,
            "telephony_provider": self.telephony_provider,
            "http_host": self.http_host,
            "http_port": self.http_port,
            "api_token": "set" if self.api_token else "not set",
            "data_dir": str(self.data_dir),
            "db_path": str(self.resolved_db_path),
            "log_level": self.log_level,
            "log_format": self.log_format,
            "log_file": str(self.resolved_log_file) if self.resolved_log_file else None,
            "default_language": self.default_language,
            "timezone": self.timezone,
            "notion_enabled": self.notion_enabled,
            "notion_token": "set" if self.notion_token else "not set",
        }


def _describe(exc: ValidationError) -> list[str]:
    problems: list[str] = []
    for error in exc.errors(include_url=False, include_input=False):
        loc = error.get("loc") or ()
        name = f"{ENV_PREFIX}{str(loc[0]).upper()}" if loc else None
        message = str(error.get("msg", "invalid value")).removeprefix("Value error, ")
        for part in message.split(" | "):
            problems.append(f"{name}: {part}" if name else part)
    return problems


def load_settings(env_file: str | Path | None = ".env", **overrides: Any) -> Settings:
    """Load and validate settings. Raises ConfigError with readable problems (no secret values)."""
    try:
        return Settings(_env_file=env_file, **overrides)
    except ValidationError as exc:
        raise ConfigError(_describe(exc)) from None


def find_unknown_variables(
    environ: Mapping[str, str] | None = None, env_file: str | Path | None = ".env"
) -> list[str]:
    """WEWLN_* names that match no setting (usually typos). They are ignored, so warn about them."""
    known = {ENV_PREFIX + name.upper() for name in Settings.model_fields}
    names = {k.upper() for k in (os.environ if environ is None else environ)}
    if env_file and Path(env_file).is_file():
        for raw in Path(env_file).read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key = line.split("=", 1)[0].strip().removeprefix("export ").strip()
            names.add(key.upper())
    return sorted(n for n in names if n.startswith(ENV_PREFIX) and n not in known)
