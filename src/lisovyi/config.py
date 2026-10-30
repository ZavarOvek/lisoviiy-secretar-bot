from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """Налаштування зі змінних оточення (у Docker їх дає `env_file: .env`)."""

    model_config = SettingsConfigDict(extra="ignore")

    telegram_bot_token: str = ""
    groq_api_key: str = ""
    groq_model: str = "openai/gpt-oss-120b"
    # Reasoning-токени теж рахуються в ліміт токенів на хвилину.
    groq_reasoning_effort: Literal["low", "medium", "high"] = "low"
    # Друга модель на 429; порожньо — чекати й повторювати основною. Запит до неї той самий:
    # reasoning_effort і strict json_schema, тож годиться лише модель, що їх підтримує
    # (openai/gpt-oss-20b). Інша на кожен 429 дасть 400 і «щось пішло не так».
    groq_fallback_model: str = ""
    # Разом із reasoning.
    groq_max_completion_tokens: int = Field(default=1024, gt=0)
    # Скільки бот пам'ятає попереднє питання звіра для уточнень місця.
    dialogue_ttl_seconds: int = Field(default=900, gt=0)

    postgres_user: str = ""
    postgres_password: str = ""
    postgres_db: str = ""
    postgres_host: str = "db"
    postgres_port: int = 5432
    # Якщо задано, має пріоритет над POSTGRES_*: так тести працюють з окремою базою.
    database_url: str = ""

    archive_dir: Path = PROJECT_ROOT / "data" / "archive"
    transcripts_dir: Path = PROJECT_ROOT / "data" / "transcripts"

    # Сума символів документів у пакеті контексту для LLM.
    context_char_budget: int = Field(default=6000, gt=0)

    def db_url(self) -> str:
        if self.database_url:
            return self.database_url
        required = {
            "POSTGRES_USER": self.postgres_user,
            "POSTGRES_PASSWORD": self.postgres_password,
            "POSTGRES_DB": self.postgres_db,
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise RuntimeError(f"DATABASE_URL is not set, and neither is {', '.join(missing)}")
        url = URL.create(
            "postgresql+asyncpg",
            username=self.postgres_user,
            password=self.postgres_password,
            host=self.postgres_host,
            port=self.postgres_port,
            database=self.postgres_db,
        )
        return url.render_as_string(hide_password=False)
