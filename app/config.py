"""Settings loaded from .env (or environment) with sensible defaults."""
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


BASE_DIR = Path(__file__).resolve().parent


class Settings(BaseSettings):
    app_name: str = "PMS Generator (new)"
    app_version: str = "0.1.0"
    app_host: str = "0.0.0.0"
    app_port: int = 8004
    log_level: str = "INFO"

    # Anthropic Claude — used for AI engineering-notes generation on Tab 5.
    # Optional: when unset the app falls back to the placeholder UI.
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-4-6"
    anthropic_max_tokens: int = 2000

    base_dir: Path = BASE_DIR
    data_dir: Path = BASE_DIR / "data"
    static_dir: Path = BASE_DIR / "static"
    templates_dir: Path = BASE_DIR / "templates"

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


settings = Settings()
