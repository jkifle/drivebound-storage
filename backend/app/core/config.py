from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str = "postgresql+asyncpg://photos:change-me@postgres:5432/photos"
    redis_url: str = "redis://redis:6379/0"
    originals_path: Path = Path("/data/originals")
    derivatives_path: Path = Path("/data/derivatives")
    staging_path: Path = Path("/data/staging")
    upload_chunk_size: int = 1024 * 1024
    max_upload_size: int = 50 * 1024 * 1024 * 1024
    cors_origins: str = "http://localhost:3000,http://127.0.0.1:3000"
    external_library_roots: str = "/data/imports"
    replica_path: Path = Path("/data/replicas")
    auto_protect_uploads: bool = True
    resumable_chunk_size: int = 8 * 1024 * 1024
    upload_session_hours: int = 24
    jwt_secret: str = "change-this-before-enabling-remote-access"
    jwt_expire_minutes: int = 60
    app_url: str = "http://localhost:3000"
    api_url: str = "http://localhost:8000"
    auth_cookie_name: str = "drivebound_session"
    auth_cookie_secure: bool = False
    refresh_cookie_name: str = "drivebound_refresh"
    access_token_minutes: int = 15
    refresh_session_days: int = 30
    email_delivery_mode: str = "console"
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_username: str | None = None
    smtp_password: str | None = None
    smtp_from: str = "Drivebound <noreply@drivebound.local>"
    smtp_use_tls: bool = True
    mfa_encryption_secret: str = "change-this-mfa-secret-before-production"
    auth_rate_limit_attempts: int = 8
    auth_rate_limit_window_seconds: int = 900
    google_client_id: str | None = None
    google_client_secret: str | None = None
    google_redirect_uri: str | None = None
    remote_access_enabled: bool = False
    trusted_hosts: str = "localhost,127.0.0.1"
    semantic_enabled: bool = True
    semantic_model: str = "BAAI/bge-small-en-v1.5"
    ocr_enabled: bool = True
    intelligence_cache_path: Path = Path("/data/models")
    monitor_interval_seconds: int = 900
    node_pairing_minutes: int = 10

    @property
    def trusted_host_list(self) -> list[str]:
        return [host.strip() for host in self.trusted_hosts.split(",") if host.strip()]

    @property
    def google_auth_enabled(self) -> bool:
        return bool(self.google_client_id and self.google_client_secret)

    @property
    def google_callback_url(self) -> str:
        return self.google_redirect_uri or f"{self.api_url.rstrip('/')}/api/v1/auth/google/callback"

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def external_root_list(self) -> list[Path]:
        return [Path(root.strip()).resolve() for root in self.external_library_roots.split(",") if root.strip()]

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
