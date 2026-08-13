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

    # Anthropic Claude — used for AI engineering-notes generation on Tab 5,
    # and as the fallback credential for the PMS-Agent chat when no admin
    # provider is active (see app/services/ai_provider.py). Optional: when
    # unset the app falls back to the placeholder UI.
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-4-6"
    anthropic_max_tokens: int = 2000

    # Encrypts saved AI-provider API keys at rest in the ai_provider_configs
    # table (managed from /admin/ai-settings). Generate one with:
    #   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    # Required only to save/read providers from that admin page — the
    # existing .env ANTHROPIC_API_KEY fallback above works without it.
    ai_credentials_encryption_key: str = ""

    # PMS-Agent chat session persistence. When set, the SQL store uses
    # this Postgres URL. When empty the session endpoints return 503 and
    # the chat operates without history (frontend shows "history sync
    # off"). Format: postgresql://user:password@host[:port]/dbname
    database_url: str = ""

    base_dir: Path = BASE_DIR
    data_dir: Path = BASE_DIR / "data"
    static_dir: Path = BASE_DIR / "static"
    templates_dir: Path = BASE_DIR / "templates"

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


settings = Settings()
