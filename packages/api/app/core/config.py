import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    app_env: str
    database_url: str
    trusted_proxy_ips: tuple[str, ...] = ()
    cors_allowed_origins: tuple[str, ...] = ()
    storage_backend: str = "memory"
    storage_local_root: str = ".data/objects"
    storage_signing_secret: str = ""
    cos_region: str = ""
    cos_bucket: str = ""
    cos_secret_id: str = ""
    cos_secret_key: str = ""
    ai_internal_url: str = "http://127.0.0.1:3101"
    ai_service_token: str = ""


def get_settings() -> Settings:
    app_env = os.getenv("APP_ENV", "development")
    return Settings(
        app_env=app_env,
        database_url=os.getenv("DATABASE_URL", "postgresql+asyncpg://localhost/ai_resume"),
        trusted_proxy_ips=tuple(
            address.strip()
            for address in os.getenv("TRUSTED_PROXY_IPS", "").split(",")
            if address.strip()
        ),
        cors_allowed_origins=tuple(
            origin.strip()
            for origin in os.getenv("CORS_ALLOWED_ORIGINS", "").split(",")
            if origin.strip()
        ),
        storage_backend=os.getenv(
            "STORAGE_BACKEND", "memory" if app_env == "test" else "local"
        ),
        storage_local_root=os.getenv("STORAGE_LOCAL_ROOT", ".data/objects"),
        storage_signing_secret=os.getenv("STORAGE_SIGNING_SECRET", ""),
        cos_region=os.getenv("COS_REGION", ""),
        cos_bucket=os.getenv("COS_BUCKET", ""),
        cos_secret_id=os.getenv("COS_SECRET_ID", ""),
        cos_secret_key=os.getenv("COS_SECRET_KEY", ""),
        ai_internal_url=os.getenv("AI_INTERNAL_URL", "http://127.0.0.1:3101"),
        ai_service_token=os.getenv("AI_SERVICE_TOKEN", ""),
    )
