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

    # Comma-separated Telegram user ids (digits) — /admin_* и зеркало сообщений
    admin_user_ids: str | None = None
    # Логины без @ (те же /admin_*). Чисто числовые значения здесь тоже считаются user id (если перепутали с ADMIN_USER_IDS)
    admin_usernames: str | None = None
    # If true, each user text message is also copied to admins (can be noisy)
    admin_message_mirror: bool = False

    def require_telegram_token(self) -> str:
        if not self.telegram_bot_token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN is missing. Create .env next to bot.py")
        return self.telegram_bot_token

    def require_telegram_api_credentials(self) -> tuple[int, str]:
        if not self.telegram_api_id or not self.telegram_api_hash:
            raise RuntimeError("TELEGRAM_API_ID/TELEGRAM_API_HASH are missing for MTProto mode")
        return int(self.telegram_api_id), self.telegram_api_hash

    def admin_id_set(self) -> set[int]:
        """Numeric ids from ADMIN_USER_IDS and any digit-only entries from ADMIN_USERNAMES."""
        out: set[int] = set()

        def _ingest_ids(s: str | None) -> None:
            raw = (s or "").strip()
            if not raw:
                return
            for p in raw.replace(" ", "").split(","):
                if p.isdigit():
                    out.add(int(p))

        _ingest_ids(self.admin_user_ids)
        _ingest_ids(self.admin_usernames)
        return out

    def admin_username_set(self) -> set[str]:
        """@usernames only (segments that are not purely digits)."""
        raw = (self.admin_usernames or "").strip()
        if not raw:
            return set()
        out: set[str] = set()
        for p in raw.split(","):
            n = p.strip().lstrip("@")
            if n.isdigit():
                continue
            n = n.lower()
            if n:
                out.add(n)
        return out


class ScoreWeights(BaseModel):
    smurf: float = 1.0
    bought: float = 1.0


SETTINGS = Settings()
