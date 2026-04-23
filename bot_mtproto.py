from __future__ import annotations

import asyncio
import logging

from telethon import TelegramClient, events
from telethon.network.connection.tcpmtproxy import ConnectionTcpMTProxyRandomizedIntermediate

from bot import analyze_player
from config import SETTINGS
from utils.parse_ids import parse_player_id

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("dota_profile_bot.mtproto")


def _help_text() -> str:
    return (
        "Пришлите команду:\n"
        "/analyze <steamid64 | account_id | ссылка>\n\n"
        "Пример:\n"
        "/analyze 76561198xxxxxxxxx"
    )


def _extract_arg(text: str, command: str) -> str:
    s = (text or "").strip()
    if not s:
        return ""
    if s == command:
        return ""
    if s.startswith(command + " "):
        return s[len(command) + 1 :].strip()
    return ""


async def main() -> None:
    api_id, api_hash = SETTINGS.require_telegram_api_credentials()
    bot_token = SETTINGS.require_telegram_token()

    proxy = None
    conn = None
    if SETTINGS.telegram_mtproxy_server and SETTINGS.telegram_mtproxy_port and SETTINGS.telegram_mtproxy_secret:
        proxy = (
            SETTINGS.telegram_mtproxy_server,
            int(SETTINGS.telegram_mtproxy_port),
            SETTINGS.telegram_mtproxy_secret,
        )
        conn = ConnectionTcpMTProxyRandomizedIntermediate
        logger.info("MTProxy enabled: %s:%s", SETTINGS.telegram_mtproxy_server, SETTINGS.telegram_mtproxy_port)

    client = TelegramClient(
        "dota_profile_mtproto",
        api_id,
        api_hash,
        connection=conn,
        proxy=proxy,
    )
    await client.start(bot_token=bot_token)
    me = await client.get_me()
    logger.info("MTProto bot started: @%s", getattr(me, "username", "unknown"))

    @client.on(events.NewMessage(pattern=r"^/start$"))
    async def on_start(event: events.NewMessage.Event) -> None:
        await event.respond(_help_text())

    @client.on(events.NewMessage(pattern=r"^/analyze(?:\s+.+)?$"))
    async def on_analyze(event: events.NewMessage.Event) -> None:
        arg = _extract_arg(event.raw_text, "/analyze")
        if not arg:
            await event.respond("Нужно указать id или ссылку. Например: /analyze 123456789")
            return

        try:
            pid = parse_player_id(arg)
        except Exception:
            await event.respond("Не смог распознать id. Пришлите steamid64 / account_id / ссылку на dotabuff.")
            return

        status = await event.respond("Собираю данные и считаю статистику…")
        try:
            report = await analyze_player(pid)
        except Exception as e:
            logger.exception("Analyze failed for account_id=%s", pid.account_id)
            msg = f"Ошибка при анализе:\n{type(e).__name__}: {str(e)[:220]}"
            await status.edit(msg)
            return

        # Telethon supports html parse mode
        await status.edit(report, parse_mode="html")

    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())

