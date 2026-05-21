from functools import lru_cache
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str = "postgresql+asyncpg://babeltower:dev@localhost:5432/babeltower"
    redis_url: str = "redis://localhost:6379/0"
    voyage_api_key: str = "your-key"
    github_oauth_client_id: str = ""
    github_oauth_client_secret: str = ""
    server_base_url: str = "http://localhost:8000"
    sentry_dsn: Optional[str] = None
    log_level: str = "INFO"
    env: str = "development"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()

