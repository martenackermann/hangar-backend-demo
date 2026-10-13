"""Application settings (SPEC §2.7), read from environment variables / ``.env``.

Every setting maps to an upper-case env var (``database_url`` <- ``DATABASE_URL``); the
``HANGAR_*`` ones are spelled out with ``validation_alias``. Defaults are local-dev
placeholders that match the shared dev containers.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from pydantic import AliasChoices, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict
from sqlalchemy.engine import make_url

# Placeholders only (never real secrets). The JWT default is longer than the spec's
# "dev-only-change-me" because HS256 keys should be >= 32 bytes.
DEV_JWT_SECRET = "dev-only-change-me-0123456789abcdef"  # noqa: S105
DEV_SIGNING_KEY = "dev-only-signing-key"
DEFAULT_CORS = [f"http://localhost:{port}" for port in range(5173, 5179)]


def _env(*names: str) -> AliasChoices:
    return AliasChoices(*names)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        populate_by_name=True,
    )

    # --- runtime ------------------------------------------------------------------------
    environment: Literal["development", "test", "production"] = "development"
    host: str = "127.0.0.1"
    port: int = 8100
    log_level: str = "INFO"
    log_format: Literal["json", "console"] = "json"

    # --- database -----------------------------------------------------------------------
    database_url: str = "postgresql+psycopg://dev:dev@localhost:5432/hangar_main"
    database_echo: bool = False
    database_pool_size: int = 10

    # --- auth ---------------------------------------------------------------------------
    jwt_secret: SecretStr = SecretStr(DEV_JWT_SECRET)
    jwt_algorithm: str = "HS256"
    jwt_ttl_minutes: int = 720

    # --- demo ---------------------------------------------------------------------------
    demo_mode: bool = True
    demo_nightly_reset: bool = False

    # --- business calendar --------------------------------------------------------------
    business_tz: str = Field(
        default="Europe/Zurich", validation_alias=_env("HANGAR_BUSINESS_TZ", "business_tz")
    )

    # --- files --------------------------------------------------------------------------
    files_dir: Path = Field(
        default=Path("var/files"), validation_alias=_env("HANGAR_FILES_DIR", "files_dir")
    )
    public_api_url: str = Field(
        default="http://localhost:8100/api/v1",
        validation_alias=_env("HANGAR_PUBLIC_API_URL", "public_api_url"),
    )
    file_url_signing_key: SecretStr = SecretStr(DEV_SIGNING_KEY)
    file_url_ttl_seconds: int = 600
    import_max_bytes: int = 20 * 1024 * 1024
    import_max_rows: int = 50_000
    deactivation_max_rows: int = 10_000

    # --- http ---------------------------------------------------------------------------
    cors_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: list(DEFAULT_CORS),
        validation_alias=_env("HANGAR_CORS_ORIGINS", "cors_origins"),
    )
    frontend_url: str = "http://localhost:5173"
    mailpit_url: str | None = "http://localhost:8025"

    # --- mail ---------------------------------------------------------------------------
    smtp_host: str = "localhost"
    smtp_port: int = 1025
    smtp_username: str | None = None
    smtp_password: SecretStr | None = None
    smtp_starttls: bool = False
    mail_from: str = "Hangar Rollout Desk <rollout-desk@example.com>"
    mail_team_mailbox: str = "rollout-team@example.com"
    mail_escalation_cc: str = "fleet-escalations@example.com"
    mail_partner_orders: str = "partner-orders@example.com"

    # --- ITSM ---------------------------------------------------------------------------
    itsm_hold_assignment_group: str = "Rollout Hardware Hold"

    # --- gateways -----------------------------------------------------------------------
    gateway_booking: Literal["fake", "real"] = "fake"
    gateway_carrier: Literal["fake", "real"] = "fake"
    gateway_mailer: Literal["smtp", "memory", "console"] = "smtp"
    booking_base_url: str = "https://booking.example.com"
    carrier_base_url: str = "https://carrier.example.com"
    booking_public_key: SecretStr | None = None
    booking_private_key: SecretStr | None = None
    carrier_api_key: SecretStr | None = None
    carrier_account: str | None = None

    # --- jobs ---------------------------------------------------------------------------
    worker_concurrency: int = 4

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        if isinstance(value, str):
            text = value.strip()
            if text.startswith("["):
                return json.loads(text)
            return [part.strip() for part in text.split(",") if part.strip()]
        return value

    @field_validator("public_api_url", "frontend_url")
    @classmethod
    def _strip_slash(cls, value: str) -> str:
        return value.rstrip("/")

    @model_validator(mode="after")
    def _check(self) -> Settings:
        if len(self.jwt_secret.get_secret_value()) < 32:
            raise ValueError("JWT_SECRET must be at least 32 characters.")
        if self.is_production and self.jwt_secret.get_secret_value() == DEV_JWT_SECRET:
            raise ValueError("Set JWT_SECRET: the development placeholder is not allowed here.")
        ZoneInfo(self.business_tz)  # validates the zone name
        return self

    # --- derived ------------------------------------------------------------------------
    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.business_tz)

    @property
    def psycopg_conninfo(self) -> str:
        """``DATABASE_URL`` without the SQLAlchemy driver suffix (psycopg / procrastinate)."""
        url = make_url(self.database_url).set(drivername="postgresql")
        return url.render_as_string(hide_password=False)


@lru_cache
def get_settings() -> Settings:
    return Settings()
