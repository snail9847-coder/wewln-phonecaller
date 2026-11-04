"""Configuration from environment variables (prefix WEWLN_) and an optional .env file.

Precedence: explicit arguments > environment variables > .env file > defaults.
Validation runs at startup; an invalid configuration stops the program (fail closed).
"""

from __future__ import annotations

import ipaddress
import os
import re
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
from .phones import SUPPORTED_REGIONS, normalize_phone
from .secret_store import (
    ARI_PASSWORD_KEY,
    NOTION_TOKEN_KEY,
    WEBHOOK_SECRET_KEY,
    SecretStore,
    SecretStoreUnavailable,
    default_store,
)

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
MIN_WEBHOOK_SECRET_LENGTH = 32
MAX_SECRET_LENGTH = 500
MOCK_SCENARIOS = (
    "answer",
    "busy",
    "no_answer",
    "reject",
    "unreachable",
    "fail",
    "silent",
    "answer_hold",
    "mixed",
)
_ARI_APP = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_DIAL_TEMPLATE = re.compile(r"^[A-Za-z]{2,16}/[A-Za-z0-9_.@/+{}-]{1,180}$")
_SOFTPHONE = re.compile(r"^PJSIP/[A-Za-z0-9_-]{1,64}$")
_MEDIA = re.compile(r"^(sound|recording|tone|digits|number):[A-Za-z0-9_/,.-]{1,100}$")
_TRUNK_OF = re.compile(r"^PJSIP/\{number\}@(?P<trunk>[A-Za-z0-9_.-]+)$")


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
    # mock: simulated calls only. asterisk: own Asterisk PBX over ARI (can place real calls,
    # only behind the real-call gate; see docs/ASTERISK.md).
    telephony_provider: Literal["mock", "asterisk"] = "mock"

    # Calls. Real calls go only to numbers of these countries (ISO codes, comma-separated).
    call_regions: str = ""
    call_ring_timeout: int = Field(default=30, ge=5, le=120)
    call_max_duration: int = Field(default=300, ge=10, le=3600)
    call_retry_delay: int = Field(default=900, ge=60, le=86400)
    mock_scenario: str = "answer"

    # Asterisk REST Interface (ARI). The password lives in the credential store or the env.
    ari_url: str = "http://127.0.0.1:8088"
    ari_username: str = "wewln"
    ari_password: SecretStr | None = None
    ari_app: str = "wewln"
    asterisk_endpoint: str = "PJSIP/{number}@wewln-trunk"
    asterisk_number_format: Literal["e164", "digits", "00"] = "e164"
    asterisk_softphone_endpoint: str = "PJSIP/softphone"
    asterisk_answer_media: str | None = "sound:hello-world"
    caller_id: str | None = None

    # Signed call-status webhooks (POST /api/v1/telephony/events). Closed while no secret is set.
    webhook_secret: SecretStr | None = None
    webhook_tolerance: int = Field(default=300, ge=30, le=900)

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
    _ari_origin: str = PrivateAttr(default="not set")
    _webhook_origin: str = PrivateAttr(default="not set")

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
        "ari_password",
        "webhook_secret",
        "caller_id",
        "asterisk_answer_media",
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

    @field_validator("call_regions")
    @classmethod
    def _check_call_regions(cls, value: str) -> str:
        codes = sorted({part.strip().upper() for part in value.split(",") if part.strip()})
        unknown = [code for code in codes if code not in SUPPORTED_REGIONS]
        if unknown:
            raise ValueError(
                "unknown country code(s) " + ", ".join(unknown) + "; use ISO codes such as AZ,RU"
            )
        return ",".join(codes)

    @field_validator("mock_scenario")
    @classmethod
    def _check_mock_scenario(cls, value: str) -> str:
        if value not in MOCK_SCENARIOS:
            raise ValueError("must be one of: " + ", ".join(MOCK_SCENARIOS))
        return value

    @field_validator("ari_url")
    @classmethod
    def _check_ari_url(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        parts = urlsplit(value)
        host = parts.hostname or ""
        if parts.scheme not in {"https", "http"} or not host:
            raise ValueError("must be an http:// or https:// URL such as http://127.0.0.1:8088")
        if parts.username or parts.password:
            raise ValueError("must not contain a user name or password (use WEWLN_ARI_PASSWORD)")
        if parts.path or parts.query or parts.fragment:
            raise ValueError("must contain only the scheme, host and port (no /ari)")
        if parts.scheme == "http" and not _is_loopback_host(host):
            raise ValueError(
                "plain http is allowed only on this computer (127.0.0.1; WSL2 forwards it); "
                "use https:// for Asterisk on another machine"
            )
        return value

    @field_validator("ari_username")
    @classmethod
    def _check_ari_username(cls, value: str) -> str:
        if not _ARI_APP.match(value):
            raise ValueError("must be 1-64 letters, digits, '_' or '-'")
        return value

    @field_validator("ari_password", "webhook_secret")
    @classmethod
    def _check_secret_shape(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None:
            check_secret_format(value.get_secret_value())
        return value

    @field_validator("webhook_secret")
    @classmethod
    def _check_webhook_secret(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and len(value.get_secret_value()) < MIN_WEBHOOK_SECRET_LENGTH:
            raise ValueError(f"must be at least {MIN_WEBHOOK_SECRET_LENGTH} characters long")
        return value

    @field_validator("ari_app")
    @classmethod
    def _check_ari_app(cls, value: str) -> str:
        if not _ARI_APP.match(value):
            raise ValueError("must be 1-64 letters, digits, '_' or '-'")
        return value

    @field_validator("asterisk_endpoint")
    @classmethod
    def _check_endpoint(cls, value: str) -> str:
        value = value.strip()
        if value.count("{number}") != 1 or not _DIAL_TEMPLATE.match(value):
            raise ValueError(
                "must look like PJSIP/{number}@wewln-trunk (one {number}, no spaces or '&')"
            )
        return value

    @field_validator("asterisk_softphone_endpoint")
    @classmethod
    def _check_softphone(cls, value: str) -> str:
        value = value.strip()
        if not _SOFTPHONE.match(value):
            raise ValueError("must be a local PJSIP endpoint such as PJSIP/softphone")
        return value

    @field_validator("asterisk_answer_media")
    @classmethod
    def _check_media(cls, value: str | None) -> str | None:
        if value is not None and not _MEDIA.match(value.strip()):
            raise ValueError("must be an Asterisk media URI such as sound:hello-world")
        return value.strip() if value is not None else None

    @field_validator("caller_id")
    @classmethod
    def _check_caller_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = normalize_phone(value.strip(), None)
        if normalized.e164 is None:
            raise ValueError("must be a phone number with the country code, e.g. +994121234567")
        return normalized.e164

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
        trunk = _TRUNK_OF.match(self.asterisk_endpoint)
        if trunk and self.asterisk_softphone_endpoint == f"PJSIP/{trunk['trunk']}":
            problems.append(
                "WEWLN_ASTERISK_SOFTPHONE_ENDPOINT must be a local softphone, not the SIP trunk"
            )
        if problems:
            raise ValueError(" | ".join(problems))
        return self

    @property
    def allowed_call_regions(self) -> frozenset[str]:
        """Countries real calls may go to. Empty: none chosen (real calls stay blocked)."""
        return frozenset(
            part.strip().upper() for part in (self.call_regions or "").split(",") if part.strip()
        )

    @property
    def ari_password_source(self) -> str:
        return self._ari_origin if self.ari_password is not None else "not set"

    @property
    def webhook_secret_source(self) -> str:
        return self._webhook_origin if self.webhook_secret is not None else "not set"

    @property
    def provider_problems(self) -> list[str]:
        """What is missing for the configured provider to work at all (no secret values)."""
        if self.telephony_provider == "asterisk" and self.ari_password is None:
            return [
                "WEWLN_TELEPHONY_PROVIDER=asterisk requires the ARI password: run "
                "`wewln telephony secret set ari-password` or set WEWLN_ARI_PASSWORD"
            ]
        return []

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
        return [
            s.get_secret_value()
            for s in (self.api_token, self.notion_token, self.ari_password, self.webhook_secret)
            if s is not None
        ]

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
            "call_regions": self.call_regions or "not set",
            "call_ring_timeout": self.call_ring_timeout,
            "call_max_duration": self.call_max_duration,
            "call_retry_delay": self.call_retry_delay,
            "mock_scenario": self.mock_scenario,
            "ari_url": self.ari_url,
            "ari_username": self.ari_username,
            "ari_password": (
                f"set ({self.ari_password_source})" if self.ari_password else "not set"
            ),
            "ari_app": self.ari_app,
            "asterisk_endpoint": self.asterisk_endpoint,
            "asterisk_number_format": self.asterisk_number_format,
            "asterisk_softphone_endpoint": self.asterisk_softphone_endpoint,
            "asterisk_answer_media": self.asterisk_answer_media,
            "caller_id": "set" if self.caller_id else "not set",
            "webhook_secret": (
                f"set ({self.webhook_secret_source})" if self.webhook_secret else "not set"
            ),
            "webhook_tolerance": self.webhook_tolerance,
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


def check_secret_format(value: str) -> None:
    """Shape check for passwords and shared secrets (the value itself is never shown)."""
    if any(ch.isspace() for ch in value):
        raise ValueError("must not contain spaces or line breaks")
    if not value or len(value) > MAX_SECRET_LENGTH:
        raise ValueError(f"must be 1-{MAX_SECRET_LENGTH} characters long")


def _origin(name: str, overrides: Mapping[str, Any]) -> str:
    if name in overrides:
        return "argument"
    if any(k.upper() == f"{ENV_PREFIX}{name.upper()}" for k in os.environ):
        return "environment variable"
    return ".env file"


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

    Secrets (Notion token, ARI password, webhook secret) are taken from (in order) explicit
    arguments, the environment, the .env file and finally the OS credential store
    (`wewln notion token set`, `wewln telephony secret set ...`).
    """
    try:
        settings = Settings(_env_file=env_file, **overrides)
    except ValidationError as exc:
        raise ConfigError(_describe(exc)) from None
    store = secret_store if secret_store is not None else default_store()

    def stored(key: str) -> str | None:
        try:
            return store.get(key)
        except SecretStoreUnavailable:
            return None

    if settings.notion_token is not None:
        settings._notion_origin = _origin("notion_token", overrides)
    elif token := stored(NOTION_TOKEN_KEY):
        try:
            check_notion_token_format(token)
        except ValueError as exc:
            raise ConfigError([f"Notion token in the credential store: {exc}"]) from None
        settings.notion_token = SecretStr(token)
        settings._notion_origin = "credential store"

    if settings.ari_password is not None:
        settings._ari_origin = _origin("ari_password", overrides)
    elif password := stored(ARI_PASSWORD_KEY):
        try:
            check_secret_format(password)
        except ValueError as exc:
            raise ConfigError([f"ARI password in the credential store: {exc}"]) from None
        settings.ari_password = SecretStr(password)
        settings._ari_origin = "credential store"

    if settings.webhook_secret is not None:
        settings._webhook_origin = _origin("webhook_secret", overrides)
    elif secret := stored(WEBHOOK_SECRET_KEY):
        try:
            check_secret_format(secret)
            if len(secret) < MIN_WEBHOOK_SECRET_LENGTH:
                raise ValueError(f"must be at least {MIN_WEBHOOK_SECRET_LENGTH} characters long")
        except ValueError as exc:
            raise ConfigError([f"Webhook secret in the credential store: {exc}"]) from None
        settings.webhook_secret = SecretStr(secret)
        settings._webhook_origin = "credential store"

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
