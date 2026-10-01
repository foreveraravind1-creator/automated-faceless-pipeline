"""
core/config.py
Centralized settings - loaded once at import time from .env or Cloud Run env vars.
"""
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Anthropic (Agent 1)
    anthropic_api_key: str

    # Google / Gemini (Agents 2-4)
    google_api_key: str
    # Path to a GCP service-account JSON file (for TTS + GCS).
    # On Cloud Run with Workload Identity, leave this as an empty string.
    google_application_credentials: str = ""

    # Google Cloud Storage (Agent 4)
    gcs_bucket_name: str = "faceless-reels-public-assets"
    gcs_region: str = "US"

    # Meta / Instagram (Agent 4)
    instagram_access_token: str
    instagram_account_id: str

    # Pexels Stock Video (Agent 2)
    pexels_api_key: str

    # Runtime
    output_dir: Path = Path("output")
    log_level: str = "INFO"

    # Cloud Run / server
    port: int = 8080


# Singleton - import this everywhere
settings = Settings()
