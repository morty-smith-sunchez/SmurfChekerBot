from __future__ import annotations

import asyncio
import io
import logging

from telethon import TelegramClient, events
from telethon.network.connection.tcpmtproxy import ConnectionTcpMTProxyRandomizedIntermediate

from bot import analyze_player, build_donation_message_html, build_match_info_report
from config import SETTINGS
from rendering.schemas import AnalyzeResult
from utils.parse_ids import parse_match_id, parse_player_id

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("dota_profile_bot.mtproto")


def _help_text() -> str:
    lines = [
        "Пришлите команду:",
        "/analyze <steamid64 | account_id | ссылка>",
        "/match <match_id | ссылка на матч OpenDota/Dotabuff>",
        "/donate — реквизиты для поддержки бота",
        "",
        "Пример:",
        "/analyze 76561198xxxxxxxxx",
        "/match https://www.opendota.com/matches/7890123456",
    ]
    card = (SETTINGS.donation_card or "").strip()
    if card:
        lines.extend(["", f"Номер карты: {card}"])
    return "\n".join(lines)


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
            res = await analyze_player(pid)
        except Exception as e:
            logger.exception("Analyze failed for account_id=%s", pid.account_id)
            msg = f"Ошибка при анализе:\n{type(e).__name__}: {str(e)[:220]}"
            await status.edit(msg)
            return

        report = res.html if isinstance(res, AnalyzeResult) else str(res)
        png = res.card_png if isinstance(res, AnalyzeResult) else None
        if png:
            await status.delete()
            await event.client.send_file(
                event.chat_id,
                file=io.BytesIO(png),
                caption="SmurfChekBot — отчёт на изображении.",
            )
        else:
            await status.edit(report, parse_mode="html")

    @client.on(events.NewMessage(pattern=r"^/match(?:\s+.+)?$"))
    async def on_match(event: events.NewMessage.Event) -> None:
        arg = _extract_arg(event.raw_text, "/match")
        if not arg:
            await event.respond(
                "Укажи match_id или ссылку на матч.\n"
                "Пример: /match 7890123456\n"
                "или /match https://www.dotabuff.com/matches/7890123456"
            )
            return

        try:
            match_id = parse_match_id(arg)
        except Exception as e:
            hint = (str(e) or "").strip()
            extra = f"\n{hint}" if hint else ""
            await event.respond(
                "Не смог распознать матч. Пришли числовой match_id или ссылку с /matches/...." + extra
            )
            return

        status = await event.respond("Загружаю данные матча…")
        try:
            report = await build_match_info_report(match_id)
        except Exception as e:
            logger.exception("Match lookup failed for match_id=%s", match_id)
            await status.edit(f"Ошибка при запросе матча:\n{type(e).__name__}: {str(e)[:220]}")
            return

        await status.edit(report, parse_mode="html")

    @client.on(events.NewMessage(pattern=r"^/donate$"))
    async def on_donate(event: events.NewMessage.Event) -> None:
        await event.respond(build_donation_message_html(), parse_mode="html")

    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())

