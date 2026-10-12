"""Application settings, read from environment variables (and an optional ``.env`` file).

Every setting maps 1:1 to an upper-case env var, e.g. ``database_url`` <- ``DATABASE_URL``.
Defaults are the "main" row of the workspace port/DB table so a fresh checkout runs
against the shared dev containers without any configuration.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict
from sqlalchemy.engine import make_url

DEV_JWT_SECRET = "dev-only-jwt-secret-change-me-0123456789abcdef"  # noqa: S105 - local placeholder


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- runtime -----------------------------------------------------------------------
    environment: Literal["development", "test", "production"] = "development"
    host: str = "127.0.0.1"
    port: int = 8100
    log_level: str = "INFO"
    log_format: Literal["json", "console"] = "json"

    # --- database ----------------------------------------------------------------------
    database_url: str = "postgresql+psycopg://dev:dev@localhost:5432/hangar_main"
    database_echo: bool = False
    database_pool_size: int = 10

    # --- auth --------------------------------------------------------------------------
    jwt_secret: SecretStr = SecretStr(DEV_JWT_SECRET)
    jwt_algorithm: str = "HS256"
    jwt_ttl_minutes: int = 480

    # --- mail --------------------------------------------------------------------------
    smtp_host: str = "localhost"
    smtp_port: int = 1025
    smtp_username: str | None = None
    smtp_password: SecretStr | None = None
    smtp_starttls: bool = False
    mail_from: str = "Hangar <hangar@example.com>"

    # --- urls / http -------------------------------------------------------------------
    app_url: str = "http://localhost:8100"
    frontend_url: str = "http://localhost:5173"
    cors_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["http://localhost:5173"]
    )

    # --- demo --------------------------------------------------------------------------
    demo_mode: bool = True

    # --- files -------------------------------------------------------------------------
    storage_dir: Path = Path("storage")
    storage_max_upload_mb: int = 20
    file_url_ttl_seconds: int = 900

    # --- jobs --------------------------------------------------------------------------
    worker_concurrency: int = 4

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        """Accept a JSON list or a comma-separated string."""
        if isinstance(value, str):
            text = value.strip()
            if text.startswith("["):
                return json.loads(text)
            return [part.strip() for part in text.split(",") if part.strip()]
        return value

    @field_validator("app_url", "frontend_url")
    @classmethod
    def _strip_trailing_slash(cls, value: str) -> str:
        return value.rstrip("/")

    @model_validator(mode="after")
    def _check_secrets(self) -> Settings:
        secret = self.jwt_secret.get_secret_value()
        if len(secret) < 32:
            raise ValueError("JWT_SECRET must be at least 32 characters.")
        if self.is_production and secret == DEV_JWT_SECRET:
            raise ValueError("Set JWT_SECRET: the built-in development secret is not allowed here.")
        return self

    # --- derived -----------------------------------------------------------------------
    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def psycopg_conninfo(self) -> str:
        """``DATABASE_URL`` without the SQLAlchemy driver suffix (for psycopg / procrastinate)."""
        url = make_url(self.database_url).set(drivername="postgresql")
        return url.render_as_string(hide_password=False)

    @property
    def storage_max_upload_bytes(self) -> int:
        return self.storage_max_upload_mb * 1024 * 1024


@lru_cache
def get_settings() -> Settings:
    return Settings()
