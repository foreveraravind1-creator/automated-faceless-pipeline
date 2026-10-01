"""
core/config.py
Centralized settings - loaded once at import time from .env or the environment.

The script stage requires only GEMINI_API_KEY. Meta, GCS, Google TTS, and
Pexels settings stay optional so importing this module does not demand them.
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

    # Gemini (script stage). Empty until the process is configured;
    # the script stage raises a clear error if the key is missing.
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.8-flash"

    # Optional until later passes. Agent 2/4 still read these when called.
    google_api_key: str = ""
    # Path to a GCP service-account JSON file (for TTS + GCS).
    # On Cloud Run with Workload Identity, leave this as an empty string.
    google_application_credentials: str = ""

    # Google Cloud Storage (later publish pass)
    gcs_bucket_name: str = "faceless-reels-public-assets"
    gcs_region: str = "US"

    # Meta / Instagram (later publish pass)
    instagram_access_token: str = ""
    instagram_account_id: str = ""

    # Pexels Stock Video (later media pass)
    pexels_api_key: str = ""

    # Runtime
    output_dir: Path = Path("output")
    log_level: str = "INFO"

    # Cloud Run / server
    port: int = 8080


# Singleton - import this everywhere
settings = Settings()
