import os
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Any model LangChain's init_chat_model understands, as "provider:model".
    model: str = "anthropic:claude-sonnet-5-5"
    anthropic_api_key: str = ""

    # WhatsApp Cloud API (Meta for Developers > your app > WhatsApp > API setup)
    whatsapp_token: str = ""
    whatsapp_phone_number_id: str = ""
    whatsapp_verify_token: str = "change-me"
    whatsapp_app_secret: str = ""
    graph_api_version: str = "v23.0"

    # Optional: WhatsApp number that gets a message when a chat is handed to a human.
    human_notify_number: str = ""

    admin_token: str = "change-me"
    database_path: Path = ROOT / "data" / "bot.db"
    business_file: Path = ROOT / "data" / "business.json"
    history_limit: int = 20


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    # LangChain's provider packages read keys from the environment, so pass on the one from .env.
    if settings.anthropic_api_key:
        os.environ.setdefault("ANTHROPIC_API_KEY", settings.anthropic_api_key)
    return settings
