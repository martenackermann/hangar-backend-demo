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
#: Markers of the documented placeholder secrets (``.env.example``, ``docker-compose.yml``).
#: ``ENVIRONMENT=production`` with ``DEMO_MODE=false`` refuses any secret containing one.
PLACEHOLDER_MARKERS = ("change-me", "dev-only", "demo-only")
MIN_SECRET_LENGTH = 32
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
        hide_input_in_errors=True,  # startup errors never echo secrets
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
    # Pinned to the HMAC family: never "none", never an asymmetric algorithm with a shared key.
    jwt_algorithm: Literal["HS256", "HS384", "HS512"] = "HS256"
    jwt_ttl_minutes: int = Field(default=720, ge=5, le=7 * 24 * 60)
    # In-process login throttling (per API process): failed password logins per
    # (client IP, email) and per client IP, and persona logins per client IP, in a sliding window.
    login_rate_limit_window_seconds: int = Field(default=300, ge=1)
    login_rate_limit_per_account: int = Field(default=10, ge=1)
    login_rate_limit_per_ip: int = Field(default=50, ge=1)
    persona_login_rate_limit_per_ip: int = Field(default=300, ge=1)  # ~1/s: e2e suites fit

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
    #: XLSX files are zip archives: cap the *decompressed* size before parsing (zip bombs).
    xlsx_max_uncompressed_bytes: int = 128 * 1024 * 1024
    xlsx_max_entries: int = 1_000

    # --- request limits -----------------------------------------------------------------
    #: Body limit for JSON (non-multipart) requests; uploads get ``import_max_bytes`` + 1 MiB.
    max_request_bytes: int = 2 * 1024 * 1024

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

    @field_validator("cors_origins")
    @classmethod
    def _no_wildcard_origin(cls, value: list[str]) -> list[str]:
        # Credentials are allowed (CORSMiddleware allow_credentials=True): a wildcard would
        # let Starlette reflect any origin. List the frontend origins explicitly.
        if any(origin.strip() == "*" for origin in value):
            raise ValueError("HANGAR_CORS_ORIGINS must list explicit origins, not '*'.")
        return [origin.rstrip("/") for origin in value]

    @field_validator("public_api_url", "frontend_url")
    @classmethod
    def _strip_slash(cls, value: str) -> str:
        return value.rstrip("/")

    @model_validator(mode="after")
    def _check(self) -> Settings:
        jwt_secret = self.jwt_secret.get_secret_value()
        signing_key = self.file_url_signing_key.get_secret_value()
        if len(jwt_secret) < MIN_SECRET_LENGTH:
            raise ValueError(f"JWT_SECRET must be at least {MIN_SECRET_LENGTH} characters.")
        if self.is_production:
            if jwt_secret == DEV_JWT_SECRET:
                raise ValueError("Set JWT_SECRET: the development placeholder is not allowed here.")
            if signing_key == DEV_SIGNING_KEY:
                raise ValueError(
                    "Set FILE_URL_SIGNING_KEY: the development placeholder is not allowed here."
                )
            if len(signing_key) < MIN_SECRET_LENGTH:
                raise ValueError(
                    f"FILE_URL_SIGNING_KEY must be at least {MIN_SECRET_LENGTH} characters."
                )
            if not self.demo_mode:
                for name, value in (
                    ("JWT_SECRET", jwt_secret),
                    ("FILE_URL_SIGNING_KEY", signing_key),
                ):
                    if any(marker in value.lower() for marker in PLACEHOLDER_MARKERS):
                        raise ValueError(
                            f"{name} is a documented placeholder; production without "
                            "DEMO_MODE needs a long random value."
                        )
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
