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
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    Field,
    PrivateAttr,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

from .errors import ConfigError
from .notion.ids import normalize_notion_id
from .phones import SUPPORTED_REGIONS
from .secret_store import NOTION_TOKEN_KEY, SecretStore, SecretStoreUnavailable, default_store

ENV_PREFIX = "WEWLN_"
PRODUCTION_CONFIRM_PHRASE = "I_ACCEPT_REAL_CALLS"
MIN_API_TOKEN_LENGTH = 32
NOTION_TOKEN_MIN_LENGTH = 20
NOTION_TOKEN_MAX_LENGTH = 500
NOTION_DB_SETTINGS = {
    "notion_campaigns_db": "WEWLN_NOTION_CAMPAIGNS_DB",
    "notion_contacts_db": "WEWLN_NOTION_CONTACTS_DB",
    "notion_jobs_db": "WEWLN_NOTION_JOBS_DB",
    "notion_results_db": "WEWLN_NOTION_RESULTS_DB",
}
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

    # Phone numbers without a country code are read in this region (ISO 3166-1, e.g. AZ).
    default_phone_region: str | None = None

    # Notion is optional: the backend works without it.
    notion_enabled: bool = False
    notion_token: SecretStr | None = None
    notion_campaigns_db: str | None = None
    notion_contacts_db: str | None = None
    notion_jobs_db: str | None = None
    notion_results_db: str | None = None
    notion_sync_interval: int = Field(default=60, ge=15, le=86400)
    notion_api_base_url: str = "https://api.notion.com"
    notion_timeout: float = Field(default=30.0, ge=1, le=300)
    notion_max_rps: float = Field(default=2.5, gt=0, le=100)

    _notion_origin: str = PrivateAttr(default="not set")

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

    @field_validator(
        "api_token",
        "notion_token",
        "default_phone_region",
        *NOTION_DB_SETTINGS,
        mode="before",
    )
    @classmethod
    def _blank_is_unset(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("notion_token")
    @classmethod
    def _check_notion_token(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None:
            check_notion_token_format(value.get_secret_value())
        return value

    @field_validator(*NOTION_DB_SETTINGS)
    @classmethod
    def _check_notion_db(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            return normalize_notion_id(value)
        except ValueError as exc:
            raise ValueError("must be a Notion database ID or a link to the database") from exc

    @field_validator("notion_api_base_url")
    @classmethod
    def _check_notion_url(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        parts = urlsplit(value)
        host = parts.hostname or ""
        if parts.scheme not in {"https", "http"} or not host:
            raise ValueError("must be an https:// URL")
        if parts.path or parts.query or parts.fragment or parts.username or parts.password:
            raise ValueError("must contain only the scheme, host and port")
        if parts.scheme == "http" and not _is_loopback_host(host):
            raise ValueError("plain http is allowed only for a local mock server (127.0.0.1)")
        return value

    @field_validator("default_phone_region")
    @classmethod
    def _check_region(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip().upper()
        if value not in SUPPORTED_REGIONS:
            raise ValueError("must be a two-letter country code such as AZ, RU or KZ")
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
        if self.notion_enabled:
            missing = [env for name, env in NOTION_DB_SETTINGS.items() if not getattr(self, name)]
            if missing:
                problems.append(
                    "WEWLN_NOTION_ENABLED=true requires "
                    + ", ".join(missing)
                    + " (create the databases with: wewln notion bootstrap --parent <page>)"
                )
        if problems:
            raise ValueError(" | ".join(problems))
        return self

    @property
    def is_loopback(self) -> bool:
        return _is_loopback_host(self.http_host)

    @property
    def notion_token_source(self) -> str:
        """Where the Notion token came from: environment, .env file, keyring or not set."""
        return self._notion_origin if self.notion_token is not None else "not set"

    @property
    def notion_databases(self) -> dict[str, str | None]:
        return {
            "campaigns": self.notion_campaigns_db,
            "contacts": self.notion_contacts_db,
            "jobs": self.notion_jobs_db,
            "results": self.notion_results_db,
        }

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
            "default_phone_region": self.default_phone_region,
            "notion_enabled": self.notion_enabled,
            "notion_token": (
                f"set ({self.notion_token_source})" if self.notion_token else "not set"
            ),
            "notion_campaigns_db": self.notion_campaigns_db,
            "notion_contacts_db": self.notion_contacts_db,
            "notion_jobs_db": self.notion_jobs_db,
            "notion_results_db": self.notion_results_db,
            "notion_sync_interval": self.notion_sync_interval,
            "notion_api_base_url": self.notion_api_base_url,
            "notion_timeout": self.notion_timeout,
            "notion_max_rps": self.notion_max_rps,
        }


def _is_loopback_host(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def check_notion_token_format(token: str) -> None:
    """Shape check only (the value itself is never shown). Raises ValueError."""
    if any(ch.isspace() for ch in token):
        raise ValueError("must not contain spaces or line breaks (copy the token again)")
    if not NOTION_TOKEN_MIN_LENGTH <= len(token) <= NOTION_TOKEN_MAX_LENGTH:
        raise ValueError(
            "does not look like a Notion token (expected the value from the connection's "
            "Configuration tab, usually starting with ntn_)"
        )


def _describe(exc: ValidationError) -> list[str]:
    problems: list[str] = []
    for error in exc.errors(include_url=False, include_input=False):
        loc = error.get("loc") or ()
        name = f"{ENV_PREFIX}{str(loc[0]).upper()}" if loc else None
        message = str(error.get("msg", "invalid value")).removeprefix("Value error, ")
        for part in message.split(" | "):
            problems.append(f"{name}: {part}" if name else part)
    return problems


def load_settings(
    env_file: str | Path | None = ".env",
    *,
    secret_store: SecretStore | None = None,
    **overrides: Any,
) -> Settings:
    """Load and validate settings. Raises ConfigError with readable problems (no secret values).

    The Notion token is taken from (in order) explicit arguments, the environment, the .env file
    and finally the OS credential store (`wewln notion token set`).
    """
    try:
        settings = Settings(_env_file=env_file, **overrides)
    except ValidationError as exc:
        raise ConfigError(_describe(exc)) from None
    if settings.notion_token is not None:
        if "notion_token" in overrides:
            settings._notion_origin = "argument"
        elif any(k.upper() == f"{ENV_PREFIX}NOTION_TOKEN" for k in os.environ):
            settings._notion_origin = "environment variable"
        else:
            settings._notion_origin = ".env file"
    else:
        store = secret_store if secret_store is not None else default_store()
        try:
            stored = store.get(NOTION_TOKEN_KEY)
        except SecretStoreUnavailable:
            stored = None
        if stored:
            try:
                check_notion_token_format(stored)
            except ValueError as exc:
                raise ConfigError([f"Notion token in the credential store: {exc}"]) from None
            settings.notion_token = SecretStr(stored)
            settings._notion_origin = "credential store"
    if settings.notion_enabled and settings.notion_token is None:
        raise ConfigError(
            [
                "WEWLN_NOTION_ENABLED=true requires a Notion token: run `wewln notion token set` "
                "(Windows Credential Manager) or set WEWLN_NOTION_TOKEN"
            ]
        )
    return settings


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
