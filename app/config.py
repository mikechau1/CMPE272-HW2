"""12-factor configuration: everything comes from the environment.

The five variable names required by the assignment (``GITHUB_TOKEN``,
``GITHUB_OWNER``, ``GITHUB_REPO``, ``WEBHOOK_SECRET``, ``PORT``) are read
verbatim; everything else has a working default so the service starts with
only those five set.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- required by the assignment ---------------------------------------
    # Defaulted to "" rather than required so the process can still boot and
    # serve /healthz with an incomplete environment; requests that actually
    # need a missing value fail loudly with a 401/503 naming the variable.
    github_token: str = ""
    github_owner: str = ""
    github_repo: str = ""
    webhook_secret: str = ""
    port: int = 8000

    # --- optional ----------------------------------------------------------
    github_api_url: str = "https://api.github.com"
    github_timeout_s: float = 10.0
    github_max_retries: int = Field(default=3, ge=0, le=10)
    enable_etag_cache: bool = True

    log_level: str = "INFO"
    log_format: str = "json"  # "json" | "text"

    event_store_path: str = "data/events.db"
    event_retention: int = Field(default=500, ge=1)
    max_webhook_body_bytes: int = Field(default=5 * 1024 * 1024, ge=1024)

    @field_validator("github_token", "github_owner", "github_repo", "webhook_secret", mode="before")
    @classmethod
    def _strip(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @field_validator("github_api_url")
    @classmethod
    def _no_trailing_slash(cls, value: str) -> str:
        return value.rstrip("/")

    @field_validator("log_format")
    @classmethod
    def _known_format(cls, value: str) -> str:
        value = value.lower()
        if value not in {"json", "text"}:
            raise ValueError("LOG_FORMAT must be 'json' or 'text'")
        return value

    # --- derived -----------------------------------------------------------
    @property
    def repo_slug(self) -> str:
        return f"{self.github_owner}/{self.github_repo}"

    @property
    def has_token(self) -> bool:
        return bool(self.github_token)

    @property
    def has_repo(self) -> bool:
        return bool(self.github_owner and self.github_repo)

    @property
    def has_webhook_secret(self) -> bool:
        return bool(self.webhook_secret)

    def missing_vars(self) -> list[str]:
        """Names of required env vars that are still unset."""
        missing = []
        if not self.github_token:
            missing.append("GITHUB_TOKEN")
        if not self.github_owner:
            missing.append("GITHUB_OWNER")
        if not self.github_repo:
            missing.append("GITHUB_REPO")
        if not self.webhook_secret:
            missing.append("WEBHOOK_SECRET")
        return missing


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
