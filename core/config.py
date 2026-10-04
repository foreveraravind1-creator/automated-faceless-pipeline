"""
core/config.py
Centralized settings - loaded once at import time from .env or the environment.

The script stage requires only GEMINI_API_KEY. Meta and Pexels settings stay
optional so importing this module does not demand them. Publish reads the
Meta settings only after --publish --confirm-publish.
"""
import re
from pathlib import Path

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Gemini (script stage). Empty until the process is configured;
    # the script stage raises a clear error if the key is missing.
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.8-flash"

    # Unused by the script, render, and publish stages. Kept so older env
    # files still load.
    google_api_key: str = ""
    google_application_credentials: str = ""

    # Meta / Instagram. Used only by --publish --confirm-publish.
    # graph.facebook.com is the Facebook Login for Business host. Resumable
    # Reel upload is documented for that login type, not Instagram Login.
    instagram_access_token: str = ""
    instagram_account_id: str = ""
    graph_api_version: str = "v26.0"
    # Status poll. Defaults match "about once a minute" with a bounded wait.
    publish_poll_interval_seconds: float = 60
    publish_max_wait_seconds: float = 600

    # Pexels stock video. Required only for --dry-run.
    pexels_api_key: str = ""

    # Local Kokoro-82M voice. Niche voice / kokoro_lang override these.
    kokoro_voice: str = "af_heart"
    kokoro_lang: str = "a"

    # Runtime
    output_dir: Path = Path("output")
    log_level: str = "INFO"

    # Cloud Run / server
    port: int = 8080

    @field_validator("graph_api_version")
    @classmethod
    def _graph_version(cls, value: str) -> str:
        cleaned = value.strip()
        if re.fullmatch(r"v\d+\.\d+", cleaned) is None:
            raise ValueError("GRAPH_API_VERSION must look like v26.0")
        return cleaned

    @model_validator(mode="after")
    def _poll_bounds(self) -> "Settings":
        if self.publish_poll_interval_seconds <= 0:
            raise ValueError("PUBLISH_POLL_INTERVAL_SECONDS must be positive")
        if self.publish_max_wait_seconds < self.publish_poll_interval_seconds:
            raise ValueError(
                "PUBLISH_MAX_WAIT_SECONDS must be at least PUBLISH_POLL_INTERVAL_SECONDS"
            )
        return self


# Singleton - import this everywhere
settings = Settings()
