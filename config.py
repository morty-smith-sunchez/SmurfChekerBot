from __future__ import annotations

from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    telegram_bot_token: str | None = None
    telegram_proxy: str | None = None
    telegram_api_id: str | None = None
    telegram_api_hash: str | None = None
    telegram_mtproxy_server: str | None = None
    telegram_mtproxy_port: str | None = None
    telegram_mtproxy_secret: str | None = None

    # Optional
    steam_api_key: str | None = None
    opendota_api_key: str | None = None
    stratz_api_key: str | None = None

    # Network tuning
    http_timeout_s: float = 20.0
    http_proxy: str | None = None
    https_proxy: str | None = None
    # httpx pool: helps many concurrent users (each /analyze opens many parallel GETs)
    http_max_connections: int = 48
    http_max_keepalive_connections: int = 24
    # Concurrent OpenDota-heavy probes per match (suspicious accounts); caps burst per callback
    match_player_probe_concurrency: int = 6

    # Optional donation block shown in bot menu
    donation_text: str | None = None
    donation_url: str | None = None
    donation_card: str | None = None

    def require_telegram_token(self) -> str:
        if not self.telegram_bot_token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN is missing. Create .env next to bot.py")
        return self.telegram_bot_token

    def require_telegram_api_credentials(self) -> tuple[int, str]:
        if not self.telegram_api_id or not self.telegram_api_hash:
            raise RuntimeError("TELEGRAM_API_ID/TELEGRAM_API_HASH are missing for MTProto mode")
        return int(self.telegram_api_id), self.telegram_api_hash


class ScoreWeights(BaseModel):
    smurf: float = 1.0
    bought: float = 1.0


SETTINGS = Settings()
