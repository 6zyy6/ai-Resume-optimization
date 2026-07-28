import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    app_env: str
    database_url: str
    trusted_proxy_ips: tuple[str, ...] = ()


def get_settings() -> Settings:
    return Settings(
        app_env=os.getenv("APP_ENV", "development"),
        database_url=os.getenv("DATABASE_URL", "postgresql+asyncpg://localhost/ai_resume"),
        trusted_proxy_ips=tuple(
            address.strip()
            for address in os.getenv("TRUSTED_PROXY_IPS", "").split(",")
            if address.strip()
        ),
    )
