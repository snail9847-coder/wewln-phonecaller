"""Builds the configured provider and the call service from the settings."""

from __future__ import annotations

from ..config import Settings
from ..errors import ConfigError
from ..storage import Database
from ..storage.calls import CallStore
from ..storage.repository import Repository
from .asterisk import AriConfig, AsteriskAriProvider
from .base import TelephonyProvider
from .mock import MockTelephonyProvider
from .service import CallService


def create_provider(
    settings: Settings, *, scenario: str | None = None, time_scale: float = 1.0
) -> TelephonyProvider:
    if settings.telephony_provider == "mock":
        return MockTelephonyProvider(scenario or settings.mock_scenario, time_scale=time_scale)
    if settings.telephony_provider == "asterisk":
        if settings.provider_problems or settings.ari_password is None:
            raise ConfigError(settings.provider_problems)
        return AsteriskAriProvider(
            AriConfig(
                url=settings.ari_url,
                username=settings.ari_username,
                password=settings.ari_password.get_secret_value(),
                app=settings.ari_app,
                endpoint_template=settings.asterisk_endpoint,
                number_format=settings.asterisk_number_format,
                caller_id=settings.caller_id,
                answer_media=settings.asterisk_answer_media,
            )
        )
    raise ConfigError([f"unknown telephony provider {settings.telephony_provider!r}"])


def create_repository(settings: Settings, db: Database) -> Repository:
    return Repository(
        db,
        default_language=settings.default_language,
        default_region=settings.default_phone_region,
        allowed_regions=settings.allowed_call_regions,
    )


def create_call_store(settings: Settings, db: Database) -> CallStore:
    return CallStore(create_repository(settings, db), retry_delay_s=settings.call_retry_delay)


def create_call_service(
    settings: Settings,
    db: Database,
    *,
    provider: TelephonyProvider | None = None,
    scenario: str | None = None,
    time_scale: float = 1.0,
) -> CallService:
    return CallService(
        settings,
        create_call_store(settings, db),
        provider or create_provider(settings, scenario=scenario, time_scale=time_scale),
    )
