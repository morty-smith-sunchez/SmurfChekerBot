from __future__ import annotations

import asyncio
import html
import logging
from datetime import datetime
from pathlib import Path
from time import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums.parse_mode import ParseMode
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BotCommand,
    BufferedInputFile,
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    User,
)

from analytics.store import (
    fetch_recent_messages,
    fetch_stats,
    init_db,
    record_message,
    sponsored_promo_eligible,
    sponsored_promo_mark_shown,
)
from analysis.learning import (
    SmurfSample,
    adaptive_smurf_bonus,
    avg_kda,
    get_adaptive_calibration,
    load_confirmed_smurfs,
    register_confirmed_smurf,
    remove_confirmed_smurf,
)
from analysis.metrics import PeriodStats, compute_period_stats
from analysis.scoring import score_suspicion
from config import SETTINGS
from dota.dotabuff_client import DotabuffClient
from dota.opendota_client import OpenDotaClient
from dota.steam_client import SteamClient
from dota.stratz_client import StratzClient
from rendering.analyze_card import format_rank_summary_en, render_analyze_card
from rendering.schemas import AnalyzeResult
from utils.parse_ids import ParsedPlayerId, parse_match_id, parse_player_id_resolved


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("dota_profile_bot")

_ASSETS_DIR = Path(__file__).resolve().parent / "assets"
WELCOME_BANNER_PATH = _ASSETS_DIR / "welcome_banner.png"

_OD_NET_EXC = (
    httpx.TimeoutException,
    httpx.ConnectError,
    httpx.NetworkError,
    httpx.HTTPStatusError,
)


BTN_ANALYZE = "Проверить профиль"
BTN_CONFIRM_SMURF = "Подтвердить смурфа (100%)"
BTN_DONATE = "Пожертвовать на развитие"
BTN_MATCH = "Инфо о матче"
BTN_CANCEL = "Отмена"
CB_LAST_MATCHES_PREFIX = "last_matches:"
CB_SUS_MATCH_PREFIX = "sus_match:"


class UserInputState(StatesGroup):
    waiting_analyze_target = State()
    waiting_confirm_smurf_target = State()
    waiting_match_target = State()


def main_menu_reply_markup() -> ReplyKeyboardMarkup:
    ch = (SETTINGS.promo_channel_url or "").strip()
    label = (SETTINGS.promo_channel_button_text or "Наш канал").strip() or "Наш канал"
    rows: list[list[KeyboardButton]] = [
        [KeyboardButton(text=BTN_ANALYZE)],
        [KeyboardButton(text=BTN_MATCH)],
        [KeyboardButton(text=BTN_CONFIRM_SMURF)],
        [KeyboardButton(text=BTN_DONATE)],
    ]
    if ch:
        rows.append([KeyboardButton(text=label, url=ch)])
    rows.append([KeyboardButton(text=BTN_CANCEL)])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def support_contact_lines() -> list[str]:
    """Строки HTML для блока «связь с автором», если задан SUPPORT_TELEGRAM_URL."""
    url = (SETTINGS.support_telegram_url or "").strip()
    if not url:
        return []
    safe = html.escape(url, quote=True)
    label = (SETTINGS.support_telegram_label or "Написать автору").strip() or "Написать автору"
    return ["", f"📩 <b>Поддержка</b>: <a href=\"{safe}\">{html.escape(label)}</a>."]


def _inline_with_channel_row(kb: InlineKeyboardMarkup) -> InlineKeyboardMarkup:
    ch = (SETTINGS.promo_channel_url or "").strip()
    if not ch:
        return kb
    label = (SETTINGS.promo_channel_button_text or "Наш канал").strip() or "Наш канал"
    return InlineKeyboardMarkup(
        inline_keyboard=[*kb.inline_keyboard, [InlineKeyboardButton(text=label, url=ch)]]
    )


def build_analyze_report_keyboard(account_id: int) -> InlineKeyboardMarkup:
    """Инлайн-кнопки под отчётом анализа: последние игры → опционально PROMO → канал из ANALYZE_CHANNEL (последним)."""
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                text="Подробно: 3 последние игры",
                callback_data=f"{CB_LAST_MATCHES_PREFIX}{account_id}",
            )
        ]
    ]
    promo = (SETTINGS.promo_channel_url or "").strip()
    if promo:
        plab = (SETTINGS.promo_channel_button_text or "Наш канал").strip() or "Наш канал"
        rows.append([InlineKeyboardButton(text=plab, url=promo)])
    ach = (SETTINGS.analyze_channel_url or "").strip()
    if ach:
        alab = (SETTINGS.analyze_channel_button_text or "Канал разраба").strip() or "Канал разраба"
        rows.append([InlineKeyboardButton(text=alab, url=ach)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def maybe_sponsored_after_analyze(message: Message) -> None:
    block = (SETTINGS.promo_sponsored_after_analyze_html or "").strip()
    if not block:
        return
    user = message.from_user
    if not user:
        return
    ok = await asyncio.to_thread(
        sponsored_promo_eligible,
        user.id,
        SETTINGS.promo_sponsored_cooldown_seconds,
    )
    if not ok:
        return
    await message.answer(block, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    await asyncio.to_thread(sponsored_promo_mark_shown, user.id)


def _admin_ids() -> set[int]:
    return SETTINGS.admin_id_set()


def _is_admin(user: User | None) -> bool:
    if not user:
        return False
    if user.id in SETTINGS.admin_id_set():
        return True
    un = (user.username or "").strip().lstrip("@").lower()
    return bool(un) and un in SETTINGS.admin_username_set()


class IncomingMessageLoggingMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[Message, dict[str, Any]], Awaitable[Any]],
        event: Message,
        data: dict[str, Any],
    ) -> Any:
        user = event.from_user
        text = event.text or event.caption or "<non-text>"
        text = text.replace("\n", "\\n")
        if len(text) > 500:
            text = text[:500] + "..."

        logger.info(
            "INCOMING user_id=%s username=%s chat_id=%s text=%r",
            user.id if user else None,
            user.username if user else None,
            event.chat.id if event.chat else None,
            text,
        )
        if user and event.chat:
            raw = (event.text or event.caption or "").strip() or "<non-text>"
            raw = raw[:4000]
            try:
                await asyncio.to_thread(
                    record_message,
                    user_id=user.id,
                    username=user.username,
                    chat_id=event.chat.id,
                    text=raw,
                )
            except Exception:
                logger.exception("analytics record_message failed")
            if SETTINGS.admin_message_mirror and _admin_ids():
                bot = data.get("bot")
                if isinstance(bot, Bot):
                    mirror = (
                        f"📩 <b>chat</b> <code>{event.chat.id}</code>\n"
                        f"<b>user</b> <code>{user.id}</code> @{html.escape(user.username or '—')}\n"
                        f"<pre>{html.escape(raw[:3500])}</pre>"
                    )
                    # Зеркало только на числовые ADMIN_USER_IDS (у @username в ЛС нет стабильного id в .env)
                    for aid in _admin_ids():
                        if aid == user.id:
                            continue
                        try:
                            await bot.send_message(aid, mirror, parse_mode=ParseMode.HTML)
                        except Exception:
                            logger.debug("admin mirror to %s failed", aid, exc_info=True)
        return await handler(event, data)


def _pct(x: float) -> str:
    return f"{x:.1f}%"


def _fmt_ts(ts: int | None) -> str | None:
    if not isinstance(ts, int) or ts <= 0:
        return None
    try:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
    except Exception:
        return None


def _hero_name(hero_map: dict[int, str], hero_id: int) -> str:
    return hero_map.get(hero_id) or f"Hero#{hero_id}"


def _format_top_heroes(ps: PeriodStats, hero_map: dict[int, str], *, k: int = 5) -> str:
    if not ps.top_heroes_by_winrate:
        return "—"
    parts: list[str] = []
    for hid, games, wr in ps.top_heroes_by_winrate[:k]:
        parts.append(f"{_hero_name(hero_map, hid)} ({games}, {_pct(wr)})")
    return ", ".join(parts)


def _is_win(match: dict) -> bool | None:
    radiant_win = match.get("radiant_win")
    player_slot = match.get("player_slot")
    if not isinstance(radiant_win, bool) or not isinstance(player_slot, int):
        return None
    is_radiant = player_slot < 128
    return radiant_win if is_radiant else not radiant_win


def _format_duration(duration_s: int | None) -> str:
    if not isinstance(duration_s, int) or duration_s < 0:
        return "—"
    minutes, seconds = divmod(duration_s, 60)
    return f"{minutes}:{seconds:02d}"


_GAME_MODES: dict[int, str] = {
    0: "Unknown",
    1: "All Pick",
    2: "Captains Mode",
    3: "Random Draft",
    4: "Single Draft",
    5: "All Random",
    11: "Mid Only",
    12: "Least Played",
    13: "Limited Heroes",
    16: "Captains Draft",
    22: "All Draft",
    23: "Turbo",
}


def _game_mode_label(mode: object) -> str:
    if isinstance(mode, int):
        return _GAME_MODES.get(mode, f"режим {mode}")
    return "—"


def _extract_prev60_from_matches(matches90: list[dict], *, now_ts: int) -> list[dict]:
    cutoff30 = now_ts - 30 * 86400
    cutoff90 = now_ts - 90 * 86400
    out: list[dict] = []
    for m in matches90:
        st = m.get("start_time")
        if isinstance(st, int) and cutoff90 <= st < cutoff30:
            out.append(m)
    return out


def _matches_between(matches: list[dict], *, start_ts: int, end_ts: int) -> list[dict]:
    out: list[dict] = []
    for m in matches:
        st = m.get("start_time")
        if isinstance(st, int) and start_ts <= st < end_ts:
            out.append(m)
    return out


def _inactivity_gap_days_before_recent_window(matches: list[dict], *, recent_days: int, now_ts: int) -> float | None:
    """
    Finds a break before the recent activity window:
    - earliest match inside recent window
    - latest match before that point
    """
    recent_start = now_ts - recent_days * 86400
    recent_matches = [m for m in matches if isinstance(m.get("start_time"), int) and m["start_time"] >= recent_start]
    if not recent_matches:
        return None
    earliest_recent = min(m["start_time"] for m in recent_matches)
    older_matches = [m for m in matches if isinstance(m.get("start_time"), int) and m["start_time"] < earliest_recent]
    if not older_matches:
        return None
    latest_old = max(m["start_time"] for m in older_matches)
    gap_days = (earliest_recent - latest_old) / 86400.0
    return gap_days if gap_days >= 0 else None


def _score_label(x01: float) -> str:
    if x01 >= 0.75:
        return "высокая"
    if x01 >= 0.45:
        return "средняя"
    if x01 >= 0.20:
        return "низкая"
    return "очень низкая"


def _fmt_source_matches_wr(matches: int | None, wr: float | None) -> str:
    parts: list[str] = []
    if matches is not None:
        parts.append(f"матчей: {matches}")
    if wr is not None:
        parts.append(f"WR: {_pct(wr)}")
    return ", ".join(parts) if parts else "нет данных"


def _wl_to_wr(wl: dict) -> tuple[int, int, float]:
    w = int(wl.get("win") or 0)
    l = int(wl.get("lose") or 0)
    g = w + l
    wr = (w / g * 100.0) if g else 0.0
    return w, g, wr


async def analyze_player(pid: ParsedPlayerId) -> AnalyzeResult:
    now_ts = int(time())
    outbound_proxy = SETTINGS.https_proxy or SETTINGS.http_proxy
    od = OpenDotaClient(api_key=SETTINGS.opendota_api_key, timeout_s=SETTINGS.http_timeout_s)
    steam = SteamClient(api_key=SETTINGS.steam_api_key, timeout_s=SETTINGS.http_timeout_s) if SETTINGS.steam_api_key else None
    stratz = StratzClient(api_key=SETTINGS.stratz_api_key, timeout_s=SETTINGS.http_timeout_s, proxy=outbound_proxy)
    dotabuff = DotabuffClient(timeout_s=SETTINGS.http_timeout_s, proxy=outbound_proxy)

    try:
        async def _safe_stratz():
            try:
                return await stratz.get_player_summary(pid.account_id)
            except Exception:
                return None

        async def _safe_dotabuff():
            try:
                return await dotabuff.get_player_summary(pid.account_id)
            except Exception:
                return None

        async def _hero_map_load() -> dict[int, str]:
            hm: dict[int, str] = {}
            try:
                hero_stats = await od.get_hero_stats()
                if isinstance(hero_stats, list):
                    for h in hero_stats:
                        hid = h.get("id")
                        name = h.get("localized_name")
                        if isinstance(hid, int) and isinstance(name, str) and name:
                            hm[hid] = name
            except Exception:
                pass
            return hm

        stratz_summary, dotabuff_summary, hero_map = await asyncio.gather(
            _safe_stratz(),
            _safe_dotabuff(),
            _hero_map_load(),
        )

        core = await asyncio.gather(
            od.get_player(pid.account_id),
            od.get_matches(pid.account_id, days=90, limit=500),
            od.get_matches(pid.account_id, days=365, limit=500),
            return_exceptions=True,
        )
        player_r, m90_r, m365_r = core
        opendota_unreachable = any(isinstance(x, _OD_NET_EXC) for x in core) or isinstance(player_r, BaseException)

        if opendota_unreachable:
            steam_profile = None
            steam_level = None
            if steam is not None and pid.steamid64 is not None:
                try:
                    sp_r, sl_r = await asyncio.gather(
                        steam.get_player_summaries(pid.steamid64),
                        steam.get_steam_level(pid.steamid64),
                        return_exceptions=True,
                    )
                    steam_profile = sp_r if not isinstance(sp_r, BaseException) else None
                    steam_level = sl_r if not isinstance(sl_r, BaseException) else None
                except Exception:
                    steam_profile = None
                    steam_level = None

            lines: list[str] = []
            lines.append("🛡 <b>Dota2 — анализ профиля</b>")
            lines.append(f"<b>account_id</b>: <code>{pid.account_id}</code>")
            if pid.steamid64:
                lines.append(f"<b>steamid64</b>: <code>{pid.steamid64}</code>")
            if steam_level is not None:
                lines.append(f"<b>Steam Level</b>: {steam_level}")
            if steam_profile and steam_profile.timecreated:
                created = _fmt_ts(steam_profile.timecreated)
                if created:
                    lines.append(f"<b>Steam created</b>: {created}")
            lines.append("")
            lines.append("📴 <b>OpenDota сейчас недоступен</b> (ошибка сети)")
            lines.append("📡 <b>Данные по источникам</b>")
            lines.append("- OpenDota: недоступен")
            lines.append(f"- STRATZ: {_fmt_source_matches_wr(stratz_summary.matches if stratz_summary else None, stratz_summary.winrate if stratz_summary else None)}")
            lines.append(f"- Dotabuff: {_fmt_source_matches_wr(dotabuff_summary.matches if dotabuff_summary else None, dotabuff_summary.winrate if dotabuff_summary else None)}")
            if steam_profile is not None or steam_level is not None:
                created = _fmt_ts(steam_profile.timecreated) if steam_profile and steam_profile.timecreated else None
                steam_parts: list[str] = []
                if steam_level is not None:
                    steam_parts.append(f"level: {steam_level}")
                if created:
                    steam_parts.append(f"created: {created}")
                lines.append(f"- Steam: {', '.join(steam_parts) if steam_parts else 'нет данных'}")
            lines.append("Попробуйте ещё раз через минуту или с VPN/другой сетью.")
            return AnalyzeResult(html="\n".join(lines), card_pngs=())

        player = player_r
        matches90 = m90_r if isinstance(m90_r, list) else []
        matches365 = m365_r if isinstance(m365_r, list) else []

        # Prefer OpenDota /wl for win/loss (more accurate than limited match list)
        wl_total: dict | None = None
        wl30: dict | None = None
        wl90: dict | None = None
        wl_bundle = await asyncio.gather(
            od.get_winloss(pid.account_id, days=None),
            od.get_winloss(pid.account_id, days=30),
            od.get_winloss(pid.account_id, days=90),
            return_exceptions=True,
        )
        if any(isinstance(x, _OD_NET_EXC) for x in wl_bundle) or not all(isinstance(x, dict) for x in wl_bundle):
            wl_total = None
            wl30 = None
            wl90 = None
        else:
            wl_total, wl30, wl90 = wl_bundle[0], wl_bundle[1], wl_bundle[2]

        p30 = compute_period_stats(matches90, days=30, now_ts=now_ts)
        p90 = compute_period_stats(matches90, days=90, now_ts=now_ts)

        w_total, g_total, wr_total = _wl_to_wr(wl_total) if wl_total is not None else (p90.wins, p90.matches, p90.winrate)
        w30, g30, wr30 = _wl_to_wr(wl30) if wl30 is not None else (p30.wins, p30.matches, p30.winrate)
        w90, g90, wr90 = _wl_to_wr(wl90) if wl90 is not None else (p90.wins, p90.matches, p90.winrate)
        if g_total <= 0 and stratz_summary is not None and stratz_summary.matches and stratz_summary.matches > 0:
            g_total = stratz_summary.matches
            wr_total = float(stratz_summary.winrate or 0.0)
            w_total = int(round(g_total * wr_total / 100.0))
        if g_total <= 0 and dotabuff_summary is not None and dotabuff_summary.matches and dotabuff_summary.matches > 0:
            g_total = dotabuff_summary.matches
            wr_total = float(dotabuff_summary.winrate or 0.0)
            w_total = int(round(g_total * wr_total / 100.0))

        # Baseline before recent 30d: interval [now-120d, now-30d]
        before_start = now_ts - 120 * 86400
        before_end = now_ts - 30 * 86400
        before_matches = _matches_between(matches365, start_ts=before_start, end_ts=before_end)
        p_before = compute_period_stats(before_matches, days=90, now_ts=now_ts) if before_matches else None

        inactivity_days = _inactivity_gap_days_before_recent_window(matches365, recent_days=30, now_ts=now_ts)

        steam_profile = None
        steam_level = None
        if steam is not None and pid.steamid64 is not None:
            sp_r, sl_r = await asyncio.gather(
                steam.get_player_summaries(pid.steamid64),
                steam.get_steam_level(pid.steamid64),
                return_exceptions=True,
            )
            steam_profile = sp_r if not isinstance(sp_r, BaseException) else None
            steam_level = sl_r if not isinstance(sl_r, BaseException) else None

        account_age_days = None
        if steam_profile is not None and isinstance(steam_profile.timecreated, int) and steam_profile.timecreated > 0:
            account_age_days = max(0.0, (now_ts - steam_profile.timecreated) / 86400.0)

        recent_start_30 = now_ts - 30 * 86400
        matches30 = [m for m in matches90 if isinstance(m.get("start_time"), int) and m["start_time"] >= recent_start_30]
        susp = score_suspicion(
            p30=p30,
            p90=p90,
            p_before=p_before,
            matches30=matches30,
            matches90=matches90,
            rank_tier=player.rank_tier,
            account_age_days=account_age_days,
            total_games=g_total,
            total_winrate=wr_total,
            inactivity_days=inactivity_days,
        )
        kda30 = avg_kda(matches30)
        adaptive_bonus, adaptive_reason = adaptive_smurf_bonus(
            total_games=g_total,
            wr30=wr30,
            wr90=wr90,
            kda30=kda30,
            matches30=p30.matches,
        )
        smurf_score = min(1.0, susp.smurf_score + adaptive_bonus)
        boost_score = susp.boost_score

        # --- render ---
        title = None
        if player.profile and isinstance(player.profile.get("personaname"), str):
            title = player.profile.get("personaname")
        if steam_profile and steam_profile.personaname:
            title = steam_profile.personaname

        lines: list[str] = []
        lines.append("🛡 <b>Dota2 — анализ профиля</b>")
        if title:
            lines.append(f"<b>Ник</b>: {html.escape(str(title))}")
        lines.append(f"<b>account_id</b>: <code>{pid.account_id}</code>")
        if pid.steamid64:
            lines.append(f"<b>steamid64</b>: <code>{pid.steamid64}</code>")

        if steam_level is not None:
            lines.append(f"<b>Steam Level</b>: {steam_level}")
        if steam_profile and steam_profile.timecreated:
            created = _fmt_ts(steam_profile.timecreated)
            if created:
                lines.append(f"<b>Steam created</b>: {created}")

        if player.rank_tier is not None or player.leaderboard_rank is not None:
            lines.append(
                f"<b>Rank</b>: {html.escape(format_rank_summary_en(player.rank_tier, player.leaderboard_rank))}"
            )

        lines.append("")
        lines.append("📡 <b>Данные по источникам</b>")
        lines.append(f"- OpenDota: матчей: {g_total}, WR: {_pct(wr_total)}")
        lines.append(
            f"- STRATZ: {_fmt_source_matches_wr(stratz_summary.matches if stratz_summary else None, stratz_summary.winrate if stratz_summary else None)}"
        )
        lines.append(
            f"- Dotabuff: {_fmt_source_matches_wr(dotabuff_summary.matches if dotabuff_summary else None, dotabuff_summary.winrate if dotabuff_summary else None)}"
        )
        created = _fmt_ts(steam_profile.timecreated) if steam_profile and steam_profile.timecreated else None
        steam_parts: list[str] = []
        if steam_level is not None:
            steam_parts.append(f"level: {steam_level}")
        if created:
            steam_parts.append(f"created: {created}")
        lines.append(f"- Steam: {', '.join(steam_parts) if steam_parts else 'нет данных'}")

        lines.append("")
        lines.append("📈 <b>Активность</b>")
        lines.append(f"- 30д: {p30.matches} матчей (~{p30.matches_per_day:.2f}/день)")
        lines.append(f"- 90д: {p90.matches} матчей (~{p90.matches_per_day:.2f}/день)")

        lines.append("")
        lines.append("📊 <b>Винрейт</b>")
        lines.append(f"- общий: {_pct(wr_total)} ({w_total}/{g_total})")
        lines.append(f"- 30д: {_pct(wr30)} ({w30}/{g30})")
        lines.append(f"- 90д: {_pct(wr90)} ({w90}/{g90})")
        if p_before is not None:
            lines.append(f"- пред.90д (120→30): {_pct(p_before.winrate)} ({p_before.wins}/{p_before.matches})")

        lines.append("")
        lines.append("🎭 <b>Герои (топ)</b>")
        lines.append("<b>Винрейт на героях за период</b>")
        lines.append(f"- 30д: {_format_top_heroes(p30, hero_map)}")
        lines.append(f"- 90д: {_format_top_heroes(p90, hero_map)}")

        if inactivity_days is not None:
            lines.append("")
            lines.append(f"<b>Пауза перед последней активностью</b>: ~{inactivity_days:.0f} дней")

        lines.append("")
        lines.append("⚠️ <b>Подозрительность</b>")
        lines.append(f"- Смурф: <b>{_score_label(smurf_score)}</b> ({smurf_score:.2f})")
        reasons_smurf = list(susp.reasons_smurf)
        if adaptive_reason:
            reasons_smurf.append(adaptive_reason)
        if reasons_smurf:
            for r in reasons_smurf[:5]:
                lines.append(f"  · {r}")
        lines.append(f"- Буст: <b>{_score_label(boost_score)}</b> ({boost_score:.2f})")
        if susp.reasons_boost:
            for r in susp.reasons_boost[:4]:
                lines.append(f"  · {r}")
        lines.append(f"- Куплен/передан: <b>{_score_label(susp.bought_score)}</b> ({susp.bought_score:.2f})")
        if susp.reasons_bought:
            for r in susp.reasons_bought[:4]:
                lines.append(f"  · {r}")

        lines.append("")
        lines.append("💡 <i>Важно: это эвристики по публичной статистике, не “вердикт”.</i>")
        lines.append("")
        lines.append("🧪 <b>Поучаствовать в разработке</b>")
        lines.append(
            "Если вы на 100% уверены, что это смурф, отправьте:\n"
            "<code>/confirm_smurf_100 &lt;id или ссылка&gt;</code>"
        )
        lines.append("Отправляйте только при полной уверенности: эти кейсы участвуют в калибровке алгоритма.")
        report_html = "\n".join(lines)

        avatar_bytes: bytes | None = None
        if steam_profile and steam_profile.avatarfull and isinstance(steam_profile.avatarfull, str):
            try:
                async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as h:
                    r = await h.get(steam_profile.avatarfull)
                    if r.status_code == 200 and r.content:
                        avatar_bytes = r.content
            except Exception:
                avatar_bytes = None

        card_pngs = tuple(
            render_analyze_card(
                html_lines=lines,
                nickname=str(title).strip() if title else None,
                account_id=pid.account_id,
                steamid64=pid.steamid64,
                steam_level=steam_level,
                steam_created=_fmt_ts(steam_profile.timecreated) if steam_profile and steam_profile.timecreated else None,
                rank_tier=player.rank_tier if isinstance(player.rank_tier, int) else None,
                leaderboard_rank=player.leaderboard_rank if isinstance(player.leaderboard_rank, int) else None,
                avatar_png=avatar_bytes,
            )
        )
        return AnalyzeResult(html=report_html, card_pngs=card_pngs)
    finally:
        await od.aclose()
        if steam is not None:
            await steam.aclose()
        await stratz.aclose()
        await dotabuff.aclose()


async def build_last_matches_report(account_id: int) -> str:
    od = OpenDotaClient(api_key=SETTINGS.opendota_api_key, timeout_s=SETTINGS.http_timeout_s)
    try:
        hero_map: dict[int, str] = {}
        try:
            hero_stats = await od.get_hero_stats()
            for h in hero_stats:
                hid = h.get("id")
                name = h.get("localized_name")
                if isinstance(hid, int) and isinstance(name, str) and name:
                    hero_map[hid] = name
        except Exception:
            hero_map = {}

        matches = await od.get_matches(account_id, days=30, limit=3)
    finally:
        await od.aclose()

    if not matches:
        return (
            "🕹 <b>Последние 3 игры</b>\n"
            "Не удалось получить матчи по этому профилю (возможно, профиль закрыт или нет свежих игр)."
        )

    lines: list[str] = [f"🕹 <b>Последние 3 игры</b> (<code>{account_id}</code>)"]
    for idx, m in enumerate(matches[:3], start=1):
        hero_id = m.get("hero_id") if isinstance(m.get("hero_id"), int) else -1
        hero = _hero_name(hero_map, hero_id) if hero_id != -1 else "—"
        kills = int(m.get("kills") or 0)
        deaths = int(m.get("deaths") or 0)
        assists = int(m.get("assists") or 0)
        duration = _format_duration(m.get("duration"))
        start = _fmt_ts(m.get("start_time")) or "—"
        wl = _is_win(m)
        result = "Победа" if wl is True else ("Поражение" if wl is False else "Неизвестно")
        lines.append(
            f"\n<b>{idx}) {hero}</b>\n"
            f"- Результат: {result}\n"
            f"- K/D/A: {kills}/{deaths}/{assists}\n"
            f"- Длительность: {duration}\n"
            f"- Дата: {start}"
        )

    return "\n".join(lines)


async def build_match_info_report(match_id: int) -> str:
    od = OpenDotaClient(api_key=SETTINGS.opendota_api_key, timeout_s=SETTINGS.http_timeout_s)
    try:
        hero_map: dict[int, str] = {}
        try:
            hero_stats = await od.get_hero_stats()
            for h in hero_stats:
                hid = h.get("id")
                name = h.get("localized_name")
                if isinstance(hid, int) and isinstance(name, str) and name:
                    hero_map[hid] = name
        except Exception:
            hero_map = {}

        match = await od.get_match(match_id)
    finally:
        await od.aclose()

    if not match:
        return "🎮 <b>Матч</b>: данные не получены (пустой ответ OpenDota)."

    players_raw = match.get("players")
    if not isinstance(players_raw, list) or not players_raw:
        return f"🎮 <b>Матч <code>{match_id}</code></b>\nНе удалось загрузить состав игроков (матч не найден или скрыт)."

    duration = match.get("duration")
    start_ts = match.get("start_time")
    radiant_win = match.get("radiant_win")
    r_score = match.get("radiant_score")
    d_score = match.get("dire_score")
    game_mode = match.get("game_mode")
    lobby_type = match.get("lobby_type")
    avg_mmr = match.get("average_mmr") or match.get("mmr_average")

    winner = "—"
    if radiant_win is True:
        winner = "Победа Radiant"
    elif radiant_win is False:
        winner = "Победа Dire"

    date_s = _fmt_ts(start_ts) if isinstance(start_ts, int) else None
    lines: list[str] = [
        f"🎮 <b>Матч</b> <code>{match_id}</code>",
        f"<b>Дата</b>: {date_s or '—'}",
        f"<b>Длительность</b>: {_format_duration(duration if isinstance(duration, int) else None)}",
        f"<b>Исход</b>: {winner}",
    ]
    if isinstance(r_score, int) and isinstance(d_score, int):
        lines.append(f"<b>Счёт</b>: Radiant {r_score} — {d_score} Dire")
    lines.append(f"<b>Режим</b>: {_game_mode_label(game_mode)}")
    if lobby_type is not None:
        lines.append(f"<b>Lobby type</b>: {lobby_type}")
    if isinstance(avg_mmr, int) and avg_mmr > 0:
        lines.append(f"<b>Средний MMR</b> (если есть): ~{avg_mmr}")
    lines.append("")
    lines.append(
        f"<a href=\"https://www.opendota.com/matches/{match_id}\">OpenDota</a> · "
        f"<a href=\"https://www.dotabuff.com/matches/{match_id}\">Dotabuff</a>"
    )
    lines.append("")

    players: list[dict] = [p for p in players_raw if isinstance(p, dict)]
    players.sort(key=lambda p: int(p.get("player_slot") or 0))

    def row(p: dict) -> str:
        slot = p.get("player_slot")
        is_radiant = isinstance(slot, int) and slot < 128
        side = "Radiant" if is_radiant else "Dire"
        hid = p.get("hero_id")
        hero = _hero_name(hero_map, int(hid)) if isinstance(hid, int) else "—"
        k = int(p.get("kills") or 0)
        da = int(p.get("deaths") or 0)
        a = int(p.get("assists") or 0)
        gpm = int(p.get("gold_per_min") or 0) if p.get("gold_per_min") is not None else None
        acc = p.get("account_id")
        acc_s = f"<code>{acc}</code>" if isinstance(acc, int) and acc > 0 else "аноним"
        raw_name = p.get("personaname")
        if isinstance(raw_name, str) and raw_name.strip():
            nick = html.escape(raw_name.strip()[:48])
        else:
            nick = "—"
        gpm_s = f"{gpm}" if isinstance(gpm, int) and gpm > 0 else "—"
        return f"· <b>{side}</b> {hero} | {nick} | {acc_s} | KDA {k}/{da}/{a} | GPM {gpm_s}"

    lines.append("⚔️ <b>Игроки</b>")
    for p in players:
        lines.append(row(p))

    return "\n".join(lines)


async def _analyze_account_smurf_score(
    od: OpenDotaClient, account_id: int, *, now_ts: int
) -> tuple[float, float, float, int]:
    player = await od.get_player(account_id)
    matches90 = await od.get_matches(account_id, days=90, limit=500)
    matches365 = await od.get_matches(account_id, days=365, limit=500)

    wl_total: dict | None = None
    wl30: dict | None = None
    wl90: dict | None = None
    try:
        wl_total = await od.get_winloss(account_id, days=None)
        wl30 = await od.get_winloss(account_id, days=30)
        wl90 = await od.get_winloss(account_id, days=90)
    except (httpx.TimeoutException, httpx.ConnectError, httpx.NetworkError, httpx.HTTPStatusError):
        wl_total = None
        wl30 = None
        wl90 = None

    p30 = compute_period_stats(matches90, days=30, now_ts=now_ts)
    p90 = compute_period_stats(matches90, days=90, now_ts=now_ts)

    _w_total, g_total, wr_total = _wl_to_wr(wl_total) if wl_total is not None else (p90.wins, p90.matches, p90.winrate)
    _w30, _g30, wr30 = _wl_to_wr(wl30) if wl30 is not None else (p30.wins, p30.matches, p30.winrate)
    _w90, _g90, wr90 = _wl_to_wr(wl90) if wl90 is not None else (p90.wins, p90.matches, p90.winrate)

    before_start = now_ts - 120 * 86400
    before_end = now_ts - 30 * 86400
    before_matches = _matches_between(matches365, start_ts=before_start, end_ts=before_end)
    p_before = compute_period_stats(before_matches, days=90, now_ts=now_ts) if before_matches else None
    inactivity_days = _inactivity_gap_days_before_recent_window(matches365, recent_days=30, now_ts=now_ts)
    recent_start_30 = now_ts - 30 * 86400
    matches30 = [m for m in matches90 if isinstance(m.get("start_time"), int) and m["start_time"] >= recent_start_30]

    susp = score_suspicion(
        p30=p30,
        p90=p90,
        p_before=p_before,
        matches30=matches30,
        matches90=matches90,
        rank_tier=player.rank_tier,
        account_age_days=None,
        total_games=g_total,
        total_winrate=wr_total,
        inactivity_days=inactivity_days,
    )
    kda30 = avg_kda(matches30)
    adaptive_bonus, _adaptive_reason = adaptive_smurf_bonus(
        total_games=g_total,
        wr30=wr30,
        wr90=wr90,
        kda30=kda30,
        matches30=p30.matches,
    )
    smurf_score = min(1.0, susp.smurf_score + adaptive_bonus)
    boost_score = susp.boost_score
    bought_score = susp.bought_score
    return smurf_score, boost_score, bought_score, p30.matches


def build_donation_message_html() -> str:
    lines = [
        "💝 <b>Поддержать развитие бота</b>",
        "Спасибо за поддержку проекта! Это помогает оплачивать сервер и улучшать функционал.",
    ]
    if SETTINGS.donation_text:
        lines.append("")
        lines.append(SETTINGS.donation_text)
    card = (SETTINGS.donation_card or "").strip()
    if card:
        lines.append("")
        lines.append(f"<b>Номер карты для перевода</b>: <code>{html.escape(card)}</code>")
    if SETTINGS.donation_url:
        lines.append("")
        lines.append(f"Ссылка для доната: {SETTINGS.donation_url}")
    if not SETTINGS.donation_text and not SETTINGS.donation_url and not card:
        lines.append("")
        lines.append("Реквизиты пока не настроены. Напишите администратору бота.")
    lines.extend(support_contact_lines())
    return "\n".join(lines)


async def reply_donation_details(message: Message) -> None:
    await message.answer(
        build_donation_message_html(),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
        reply_markup=main_menu_reply_markup(),
    )


async def cmd_privacy(message: Message) -> None:
    body = (
        "<b>Конфиденциальность</b>\n\n"
        "Бот запрашивает у публичных API (OpenDota и др.) статистику Dota 2 по тому id или ссылке, "
        "которые вы присылаете. Пароль Steam бот не запрашивает и в аккаунт не заходит.\n\n"
        "Сообщения в чате с ботом могут записываться в локальную базу на стороне владельца бота "
        "(аналитика и модерация). Это не публикуется в открытый доступ.\n\n"
        "Оценки «смурф / буст / купленный аккаунт» — эвристики для ориентира, не официальный вердикт Valve."
    )
    extra = support_contact_lines()
    if extra:
        body += "\n" + "\n".join(extra).lstrip()
    await message.answer(
        body,
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu_reply_markup(),
    )


async def cmd_support(message: Message) -> None:
    url = (SETTINGS.support_telegram_url or "").strip()
    if not url:
        await message.answer(
            "Контакт автора в Telegram пока не настроен. Попробуйте позже или напишите через канал проекта.",
            parse_mode=ParseMode.HTML,
            reply_markup=main_menu_reply_markup(),
        )
        return
    safe = html.escape(url, quote=True)
    label = (SETTINGS.support_telegram_label or "Написать автору").strip() or "Написать автору"
    await message.answer(
        f"Если бот глючит, есть идея или вопрос — пишите: <a href=\"{safe}\">{html.escape(label)}</a>.",
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
        reply_markup=main_menu_reply_markup(),
    )


async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    if WELCOME_BANNER_PATH.is_file():
        await message.answer_photo(
            photo=FSInputFile(WELCOME_BANNER_PATH),
            caption="🛡 <b>SmurfChekBot</b> — смурфы, аккбаеры, матчи и донат кнопками ниже.",
            parse_mode=ParseMode.HTML,
        )
    parts = [
        "Пришлите команду:\n"
        "<code>/analyze &lt;steamid64 | account_id | Dotabuff/OpenDota | Steam-профиль&gt;</code>\n"
        "<code>/match &lt;match_id | ссылка на матч&gt;</code> — сводка по матчу\n"
        "<code>/donate</code> — реквизиты для поддержки бота\n"
        "<code>/support</code> — связь с автором\n\n"
        "Если вы 100% уверены, что профиль смурф:\n"
        "<code>/confirm_smurf_100 &lt;id или ссылка&gt;</code>\n\n"
        "Пример:\n"
        "<code>/analyze 76561198xxxxxxxxx</code>\n"
        "<code>/analyze https://steamcommunity.com/profiles/76561198…</code>\n"
        "<code>/match https://www.opendota.com/matches/7890123456</code>\n\n"
        "Или просто используйте кнопки ниже 👇",
    ]
    promo_url = (SETTINGS.promo_channel_url or "").strip()
    custom_promo = (SETTINGS.promo_start_line_html or "").strip()
    if custom_promo:
        parts.append("")
        parts.append(custom_promo)
    elif promo_url:
        safe_u = html.escape(promo_url, quote=True)
        parts.append("")
        parts.append(f"Новости и обновления — в <a href=\"{safe_u}\">telegram-канале</a>.")
    parts.append("")
    parts.append("<code>/privacy</code> — что бот делает с данными.")
    parts.extend(support_contact_lines())
    card = (SETTINGS.donation_card or "").strip()
    if card or SETTINGS.donation_text or SETTINGS.donation_url:
        parts.append("")
        parts.append("<b>Поддержка</b>")
        parts.append("Кнопка «Пожертвовать на развитие» или команда <code>/donate</code>.")
        if card:
            parts.append(f"<b>Номер карты</b>: <code>{html.escape(card)}</code>")
    await message.answer(
        "\n".join(parts),
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu_reply_markup(),
    )


async def cmd_donate(message: Message) -> None:
    await reply_donation_details(message)


async def cmd_analyze(message: Message, command: CommandObject) -> None:
    if not command.args:
        await message.answer("Нужно указать id или ссылку. Например: <code>/analyze 123456789</code>", parse_mode=ParseMode.HTML)
        return

    steam_r = SteamClient(api_key=SETTINGS.steam_api_key, timeout_s=SETTINGS.http_timeout_s) if SETTINGS.steam_api_key else None
    try:
        pid = await parse_player_id_resolved(command.args, steam_r)
    except ValueError as e:
        hint = (str(e) or "").strip()
        await message.answer(
            "Не смог распознать id. Пришлите steamid64, account_id или ссылку на профиль Dotabuff, OpenDota или Steam.\n"
            + (f"<i>{html.escape(hint)}</i>" if hint else ""),
            parse_mode=ParseMode.HTML,
        )
        return
    finally:
        if steam_r is not None:
            await steam_r.aclose()

    msg = await message.answer("Собираю данные и считаю статистику…", parse_mode=ParseMode.HTML)
    try:
        res = await analyze_player(pid)
    except Exception as e:
        logger.exception("Analyze failed for account_id=%s", pid.account_id)
        short = (str(e) or "").strip()
        if len(short) > 180:
            short = short[:180] + "…"
        details = f"{type(e).__name__}: {short}" if short else type(e).__name__
        await msg.edit_text(
            "Ошибка при анализе:\n"
            f"<code>{details}</code>\n\n"
            "Если повторяется — пришлите мне этот текст ошибки.",
            parse_mode=ParseMode.HTML,
        )
        return

    report = res.html
    keyboard = build_analyze_report_keyboard(pid.account_id)
    if res.card_pngs:
        await msg.delete()
        n = len(res.card_pngs)
        for i, png in enumerate(res.card_pngs):
            last = i == n - 1
            await message.answer_photo(
                photo=BufferedInputFile(png, filename=f"smurfcheck_report_{i + 1}.png"),
                caption="<b>SmurfChekBot</b> — отчёт на изображении." if last else None,
                parse_mode=ParseMode.HTML,
                reply_markup=None,
            )
        # Кнопки на отдельном сообщении: так видно и при 2+ страницах, и без привязки к последнему фото.
        await message.answer(
            "<b>Действия</b> — кнопки ниже.",
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
        )
    else:
        await msg.edit_text(
            report,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
            reply_markup=keyboard,
        )
    await maybe_sponsored_after_analyze(message)


async def cmd_match(message: Message, command: CommandObject) -> None:
    if not command.args:
        await message.answer(
            "Укажи <b>match_id</b> или ссылку на матч (OpenDota / Dotabuff).\n"
            "Пример: <code>/match 7890123456</code>\n"
            "или <code>/match https://www.dotabuff.com/matches/7890123456</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    try:
        match_id = parse_match_id(command.args)
    except ValueError as e:
        hint = (str(e) or "").strip()
        extra = f"\n{hint}" if hint else ""
        await message.answer(
            "Не смог распознать матч. Пришли числовой match_id или ссылку с <code>/matches/...</code>."
            f"{extra}",
            parse_mode=ParseMode.HTML,
        )
        return

    msg = await message.answer("Загружаю данные матча…", parse_mode=ParseMode.HTML)
    try:
        report = await build_match_info_report(match_id)
    except Exception as e:
        logger.exception("Match lookup failed for match_id=%s", match_id)
        short = (str(e) or "").strip()
        if len(short) > 180:
            short = short[:180] + "…"
        details = f"{type(e).__name__}: {short}" if short else type(e).__name__
        await msg.edit_text(
            "Ошибка при запросе матча:\n"
            f"<code>{details}</code>\n\n"
            "Проверь match_id или попробуйте позже.",
            parse_mode=ParseMode.HTML,
        )
        return

    sus_keyboard = _inline_with_channel_row(
        InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Подозрительные аккаунты в этом матче",
                        callback_data=f"{CB_SUS_MATCH_PREFIX}{match_id}",
                    )
                ]
            ]
        )
    )
    await msg.edit_text(
        report,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
        reply_markup=sus_keyboard,
    )


async def on_last_matches_callback(callback: CallbackQuery) -> None:
    data = callback.data or ""
    if not data.startswith(CB_LAST_MATCHES_PREFIX):
        await callback.answer()
        return
    account_id_raw = data[len(CB_LAST_MATCHES_PREFIX) :]
    try:
        account_id = int(account_id_raw)
    except Exception:
        await callback.answer("Некорректный ID профиля", show_alert=True)
        return

    await callback.answer("Загружаю последние матчи…")
    try:
        report = await build_last_matches_report(account_id)
    except Exception:
        report = "Не удалось получить последние матчи из OpenDota. Попробуйте чуть позже."
    if callback.message is not None:
        await callback.message.answer(report, parse_mode=ParseMode.HTML, disable_web_page_preview=True)

    try:
        od = OpenDotaClient(api_key=SETTINGS.opendota_api_key, timeout_s=SETTINGS.http_timeout_s)
        matches = await od.get_matches(account_id, days=30, limit=3)
    except Exception:
        matches = []
    finally:
        if "od" in locals():
            await od.aclose()

    if callback.message is not None and matches:
        buttons: list[list[InlineKeyboardButton]] = []
        for m in matches[:3]:
            match_id = m.get("match_id")
            if not isinstance(match_id, int):
                continue
            buttons.append(
                [
                    InlineKeyboardButton(
                        text=f"Подозрительные аккаунты матча {match_id}",
                        callback_data=f"{CB_SUS_MATCH_PREFIX}{match_id}",
                    )
                ]
            )
        if buttons:
            await callback.message.answer(
                "Выбери матч для проверки участников:",
                parse_mode=ParseMode.HTML,
                reply_markup=_inline_with_channel_row(InlineKeyboardMarkup(inline_keyboard=buttons)),
            )


async def on_suspicious_match_callback(callback: CallbackQuery) -> None:
    data = callback.data or ""
    if not data.startswith(CB_SUS_MATCH_PREFIX):
        await callback.answer()
        return
    match_id_raw = data[len(CB_SUS_MATCH_PREFIX) :]
    try:
        match_id = int(match_id_raw)
    except Exception:
        await callback.answer("Некорректный ID матча", show_alert=True)
        return

    await callback.answer("Проверяю игроков матча…")
    od = OpenDotaClient(api_key=SETTINGS.opendota_api_key, timeout_s=SETTINGS.http_timeout_s)
    try:
        match = await od.get_match(match_id)
        players = match.get("players")
        if not isinstance(players, list) or not players:
            raise RuntimeError("empty players")

        now_ts = int(time())
        sem = asyncio.Semaphore(max(1, SETTINGS.match_player_probe_concurrency))

        async def _probe_player_row(p: dict) -> tuple[float, int, str, str, int, float, float, float] | None:
            if not isinstance(p, dict):
                return None
            account_id = p.get("account_id")
            if not isinstance(account_id, int) or account_id <= 0:
                return None
            persona = p.get("personaname")
            name = persona if isinstance(persona, str) and persona.strip() else f"Player {account_id}"
            hero_id = p.get("hero_id")
            hero_name = f"Hero#{hero_id}" if isinstance(hero_id, int) else "Unknown hero"
            async with sem:
                try:
                    smurf_score, boost_score, bought_score, matches30 = await _analyze_account_smurf_score(
                        od, account_id, now_ts=now_ts
                    )
                except Exception:
                    return None
            primary = max(smurf_score, boost_score, bought_score)
            if primary >= 0.45:
                return (primary, account_id, name, hero_name, matches30, smurf_score, boost_score, bought_score)
            return None

        probe_results = await asyncio.gather(
            *[_probe_player_row(p) for p in players],
            return_exceptions=True,
        )
        suspicious = [
            r
            for r in probe_results
            if r is not None and not isinstance(r, BaseException)
        ]
    except Exception:
        if callback.message is not None:
            await callback.message.answer(
                "Не удалось получить список подозрительных аккаунтов для этого матча. Попробуйте позже.",
                parse_mode=ParseMode.HTML,
            )
        await od.aclose()
        return
    finally:
        await od.aclose()

    if callback.message is None:
        return

    if not suspicious:
        await callback.message.answer(
            f"🎮 <b>Матч {match_id}</b>\n🔍 Подозрительных аккаунтов не найдено по текущим эвристикам.",
            parse_mode=ParseMode.HTML,
        )
        return

    suspicious.sort(key=lambda x: x[0], reverse=True)
    lines: list[str] = [f"🔎 <b>Подозрительные аккаунты в матче {match_id}</b>"]
    for _primary, account_id, name, hero_name, matches30, smurf_score, boost_score, bought_score in suspicious[:10]:
        lines.append(
            f"\n- <b>{name}</b> (<code>{account_id}</code>)\n"
            f"  герой: {hero_name}\n"
            f"  смурф: <b>{_score_label(smurf_score)}</b> ({smurf_score:.2f}), "
            f"буст: <b>{_score_label(boost_score)}</b> ({boost_score:.2f}), "
            f"куплен: <b>{_score_label(bought_score)}</b> ({bought_score:.2f})\n"
            f"  матчей за 30д: {matches30}"
        )
    await callback.message.answer("\n".join(lines), parse_mode=ParseMode.HTML, disable_web_page_preview=True)


async def cmd_confirm_smurf_100(message: Message, command: CommandObject) -> None:
    if not command.args:
        await message.answer(
            "Нужно указать id или ссылку.\n"
            "Пример: <code>/confirm_smurf_100 https://www.dotabuff.com/players/123456789</code>\n"
            "или <code>/confirm_smurf_100 https://steamcommunity.com/profiles/76561198…</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    steam_r = SteamClient(api_key=SETTINGS.steam_api_key, timeout_s=SETTINGS.http_timeout_s) if SETTINGS.steam_api_key else None
    try:
        pid = await parse_player_id_resolved(command.args, steam_r)
    except ValueError as e:
        hint = (str(e) or "").strip()
        await message.answer(
            "Не смог распознать id. Пришлите steamid64, account_id или ссылку на профиль Dotabuff, OpenDota или Steam.\n"
            + (f"<i>{html.escape(hint)}</i>" if hint else ""),
            parse_mode=ParseMode.HTML,
        )
        return
    finally:
        if steam_r is not None:
            await steam_r.aclose()

    od = OpenDotaClient(api_key=SETTINGS.opendota_api_key, timeout_s=SETTINGS.http_timeout_s)
    try:
        now_ts = int(time())
        player = await od.get_player(pid.account_id)
        matches90 = await od.get_matches(pid.account_id, days=90, limit=500)
        wl_total = await od.get_winloss(pid.account_id, days=None)
        wl30 = await od.get_winloss(pid.account_id, days=30)
        wl90 = await od.get_winloss(pid.account_id, days=90)
    except Exception:
        await message.answer(
            "Не получилось подтвердить кейс из-за сетевой ошибки OpenDota. Попробуйте еще раз через минуту.",
            parse_mode=ParseMode.HTML,
        )
        await od.aclose()
        return
    finally:
        await od.aclose()

    _w_total, g_total, _wr_total = _wl_to_wr(wl_total)
    _w30, _g30, wr30 = _wl_to_wr(wl30)
    _w90, _g90, wr90 = _wl_to_wr(wl90)
    recent_start_30 = now_ts - 30 * 86400
    matches30 = [m for m in matches90 if isinstance(m.get("start_time"), int) and m["start_time"] >= recent_start_30]
    sample = SmurfSample(
        account_id=pid.account_id,
        total_games=g_total,
        wr30=wr30,
        wr90=wr90,
        kda30=avg_kda(matches30),
        matches30=len(matches30),
        rank_tier=player.rank_tier,
    )
    total = register_confirmed_smurf(sample)
    await message.answer(
        "Кейс сохранен как <b>100% смурф</b> и добавлен в калибровку.\n"
        f"В базе подтвержденных кейсов: <b>{total}</b>.",
        parse_mode=ParseMode.HTML,
    )


async def on_analyze_button(message: Message, state: FSMContext) -> None:
    await state.set_state(UserInputState.waiting_analyze_target)
    await message.answer(
        "Пришли steamid64, account_id или ссылку на профиль Dotabuff, OpenDota или Steam.",
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu_reply_markup(),
    )


async def on_confirm_button(message: Message, state: FSMContext) -> None:
    await state.set_state(UserInputState.waiting_confirm_smurf_target)
    await message.answer(
        "Пришли steamid64, account_id, ссылку на Dotabuff/OpenDota или Steam-профиль, который подтверждаешь как смурф.",
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu_reply_markup(),
    )


async def on_match_button(message: Message, state: FSMContext) -> None:
    await state.set_state(UserInputState.waiting_match_target)
    await message.answer(
        "Пришли <b>match_id</b> или ссылку на матч (страница матча на OpenDota / Dotabuff).",
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu_reply_markup(),
    )


async def on_donate_button(message: Message) -> None:
    await reply_donation_details(message)


async def on_cancel_button(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "Действие отменено. Выбери следующий шаг кнопками ниже.",
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu_reply_markup(),
    )


async def on_analyze_target_input(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if not text:
        await message.answer("Пришли id или ссылку текстом.")
        return
    await state.clear()
    await cmd_analyze(message, CommandObject(prefix="/", command="analyze", args=text))


async def on_confirm_target_input(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if not text:
        await message.answer("Пришли id или ссылку текстом.")
        return
    await state.clear()
    await cmd_confirm_smurf_100(message, CommandObject(prefix="/", command="confirm_smurf_100", args=text))


async def on_match_target_input(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if not text:
        await message.answer("Пришли match_id или ссылку на матч текстом.")
        return
    await state.clear()
    await cmd_match(message, CommandObject(prefix="/", command="match", args=text))


async def cmd_admin_stats(message: Message) -> None:
    if not message.from_user or not _is_admin(message.from_user):
        await message.answer(
            "Нет доступа к админ-командам. В <code>.env</code> укажите свой числовой Telegram id в "
            "<code>ADMIN_USER_IDS</code> или логин в <code>ADMIN_USERNAMES</code> (без @), затем перезапустите бота.\n"
            "Если id был в <code>ADMIN_USERNAMES</code> — обновите бота: числа теперь подхватываются и оттуда.",
            parse_mode=ParseMode.HTML,
        )
        return
    try:
        stats = await asyncio.to_thread(fetch_stats)
    except Exception:
        logger.exception("fetch_stats failed")
        await message.answer("Не удалось прочитать базу аналитики.", parse_mode=ParseMode.HTML)
        return
    await message.answer(
        "<b>Аналитика</b> (локальная SQLite рядом с ботом)\n"
        f"Уникальных пользователей: <b>{stats['total_users']}</b>\n"
        f"Писали за последние 7 дней: <b>{stats['active_users_7d']}</b>\n"
        f"Всего учтённых сообщений (сумма): <b>{stats['total_messages']}</b>\n"
        f"Записей в логе за 24 часа: <b>{stats['logged_messages_24h']}</b>\n\n"
        "<code>/admin_recent</code> — последние тексты пользователей.\n"
        "<code>/admin_calib</code> — калибровка эвристики смурфа.",
        parse_mode=ParseMode.HTML,
    )


async def cmd_admin_calib(message: Message) -> None:
    if not message.from_user or not _is_admin(message.from_user):
        await message.answer(
            "Нет доступа к админ-командам. Проверьте <code>ADMIN_USER_IDS</code> / <code>ADMIN_USERNAMES</code> в "
            "<code>.env</code> и перезапуск бота.",
            parse_mode=ParseMode.HTML,
        )
        return
    raw = (message.text or "").strip()
    parts = raw.split(maxsplit=2)
    head = parts[0].split("@", 1)[0].lower() if parts else ""
    if head != "/admin_calib":
        return
    sub = (parts[1].split("@", 1)[0].lower() if len(parts) > 1 else "help")
    arg_tail = parts[2].strip() if len(parts) > 2 else ""

    if sub in ("help", "?", ""):
        await message.answer(
            "<b>/admin_calib</b> — калибровка адаптивного бонуса к «смурф»-скору "
            "(данные из <code>data/confirmed_smurfs.json</code>, пополняется через "
            "<code>/confirm_smurf_100</code>).\n\n"
            "<code>/admin_calib summary</code> — медианы и пороги, по которым начисляется бонус\n"
            "<code>/admin_calib list</code> — список сохранённых кейсов\n"
            "<code>/admin_calib remove &lt;account_id&gt;</code> — удалить кейс из базы",
            parse_mode=ParseMode.HTML,
        )
        return

    if sub == "summary":
        try:
            cal = await asyncio.to_thread(get_adaptive_calibration)
            n = len(await asyncio.to_thread(load_confirmed_smurfs))
        except Exception:
            logger.exception("admin_calib summary")
            await message.answer("Ошибка чтения базы калибровки.", parse_mode=ParseMode.HTML)
            return
        if cal is None:
            await message.answer(
                f"Подтверждённых смурфов в базе: <b>{n}</b>. Для адаптивного бонуса в анализе нужно <b>≥2</b> кейса.",
                parse_mode=ParseMode.HTML,
            )
            return
        await message.answer(
            "<b>Калибровка (адаптивный бонус)</b>\n"
            f"Кейсов: <b>{cal.n_samples}</b>\n\n"
            "<b>Медианы</b> по подтверждённым:\n"
            f"• матчей всего: <code>{cal.med_games}</code>\n"
            f"• WR 30д: <code>{cal.med_wr30:.1f}%</code>\n"
            f"• WR 90д: <code>{cal.med_wr90:.1f}%</code>\n"
            f"• KDA 30д: <code>{cal.med_kda:.2f}</code>\n\n"
            "<b>Пороги</b> (если выполнены все + ≥15 матчей за 30д, даётся бонус до cap):\n"
            f"• games ≤ <code>{cal.games_limit}</code> (max(120, med_games×1.8))\n"
            f"• wr30 ≥ <code>{cal.wr30_bar:.1f}%</code> (max(55, med−6))\n"
            f"• wr90 ≥ <code>{cal.wr90_bar:.1f}%</code> (max(56, med−7))\n"
            f"• kda30 ≥ <code>{cal.kda_bar:.2f}</code> (max(3.6, med−1.2))\n"
            f"• бонус cap: <code>{cal.bonus_cap:.2f}</code> (min(0.35, 0.08×N))",
            parse_mode=ParseMode.HTML,
        )
        return

    if sub == "list":
        try:
            samples = await asyncio.to_thread(load_confirmed_smurfs)
        except Exception:
            logger.exception("admin_calib list")
            await message.answer("Ошибка чтения списка кейсов.", parse_mode=ParseMode.HTML)
            return
        if not samples:
            await message.answer("База подтверждённых смурфов пуста.", parse_mode=ParseMode.HTML)
            return
        lines: list[str] = ["<b>Кейсы калибровки</b>"]
        for s in sorted(samples, key=lambda x: x.account_id):
            rt = s.rank_tier if s.rank_tier is not None else "—"
            lines.append(
                f"<code>{s.account_id}</code> — games {s.total_games}, wr30 {s.wr30:.1f}%, wr90 {s.wr90:.1f}%, "
                f"kda30 {s.kda30:.2f}, m30 {s.matches30}, tier {rt}"
            )
        body = "\n".join(lines)
        while body:
            chunk = body[:3800]
            body = body[3800:]
            suffix = "\n…" if body else ""
            await message.answer(chunk + suffix, parse_mode=ParseMode.HTML)
        return

    if sub == "remove":
        id_str = arg_tail.split()[0] if arg_tail else ""
        if not id_str.isdigit():
            await message.answer(
                "Укажите: <code>/admin_calib remove &lt;account_id&gt;</code>",
                parse_mode=ParseMode.HTML,
            )
            return
        aid = int(id_str)
        try:
            removed, newn = await asyncio.to_thread(remove_confirmed_smurf, aid)
        except Exception:
            logger.exception("admin_calib remove")
            await message.answer("Ошибка записи базы.", parse_mode=ParseMode.HTML)
            return
        if not removed:
            await message.answer(f"Кейса <code>{aid}</code> в базе не было.", parse_mode=ParseMode.HTML)
            return
        await message.answer(
            f"Удалён кейс <code>{aid}</code>. Осталось кейсов: <b>{newn}</b>.",
            parse_mode=ParseMode.HTML,
        )
        return

    await message.answer("Неизвестная подкоманда. <code>/admin_calib help</code>", parse_mode=ParseMode.HTML)


async def cmd_admin_recent(message: Message) -> None:
    if not message.from_user or not _is_admin(message.from_user):
        await message.answer(
            "Нет доступа к админ-командам. Проверьте <code>ADMIN_USER_IDS</code> / <code>ADMIN_USERNAMES</code> в "
            "<code>.env</code> и перезапуск бота.",
            parse_mode=ParseMode.HTML,
        )
        return
    try:
        rows = await asyncio.to_thread(fetch_recent_messages, 30)
    except Exception:
        logger.exception("fetch_recent_messages failed")
        await message.answer("Не удалось прочитать лог сообщений.", parse_mode=ParseMode.HTML)
        return
    if not rows:
        await message.answer("Пока нет сохранённых сообщений.", parse_mode=ParseMode.HTML)
        return
    parts: list[str] = ["<b>Последние сообщения</b>"]
    for ts, uid, un, cid, txt in rows:
        try:
            dt = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
        except Exception:
            dt = str(ts)
        udisp = html.escape(un) if un else "—"
        parts.append(
            f"\n<code>{html.escape(dt)}</code> uid <code>{uid}</code> @{udisp} chat <code>{cid}</code>"
        )
        parts.append(f"<pre>{html.escape(txt[:900])}</pre>")
    body = "\n".join(parts)
    if len(body) > 3800:
        body = body[:3800] + "\n…"
    await message.answer(body, parse_mode=ParseMode.HTML)


async def main() -> None:
    session = AiohttpSession(proxy=SETTINGS.telegram_proxy) if SETTINGS.telegram_proxy else AiohttpSession()
    bot = Bot(
        token=SETTINGS.require_telegram_token(),
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()
    dp.message.outer_middleware(IncomingMessageLoggingMiddleware())

    await asyncio.to_thread(init_db)

    dp.message.register(cmd_start, Command("start"))
    dp.message.register(cmd_donate, Command("donate"))
    dp.message.register(cmd_privacy, Command("privacy"))
    dp.message.register(cmd_support, Command("support"))
    # До FSM: команды и админка — до состояний «ожидаю текст», иначе /privacy и др. уходят в FSM.
    dp.message.register(cmd_admin_stats, F.text.startswith("/admin_stats"))
    dp.message.register(cmd_admin_recent, F.text.startswith("/admin_recent"))
    dp.message.register(cmd_admin_calib, F.text.startswith("/admin_calib"))
    dp.message.register(on_cancel_button, F.text == BTN_CANCEL)
    dp.message.register(on_analyze_button, F.text == BTN_ANALYZE)
    dp.message.register(on_match_button, F.text == BTN_MATCH)
    dp.message.register(on_confirm_button, F.text == BTN_CONFIRM_SMURF)
    dp.message.register(on_donate_button, F.text == BTN_DONATE)
    skip_commands_in_fsm = ~F.text.startswith("/")
    dp.message.register(
        on_analyze_target_input,
        UserInputState.waiting_analyze_target,
        F.text,
        skip_commands_in_fsm,
    )
    dp.message.register(
        on_confirm_target_input,
        UserInputState.waiting_confirm_smurf_target,
        F.text,
        skip_commands_in_fsm,
    )
    dp.message.register(
        on_match_target_input,
        UserInputState.waiting_match_target,
        F.text,
        skip_commands_in_fsm,
    )
    dp.message.register(cmd_analyze, Command("analyze"))
    dp.message.register(cmd_analyze, F.text.startswith("/analyze"))
    dp.message.register(cmd_match, Command("match"))
    dp.message.register(cmd_match, F.text.startswith("/match"))
    dp.message.register(cmd_confirm_smurf_100, Command("confirm_smurf_100"))
    dp.message.register(cmd_confirm_smurf_100, F.text.startswith("/confirm_smurf_100"))
    dp.callback_query.register(on_last_matches_callback, F.data.startswith(CB_LAST_MATCHES_PREFIX))
    dp.callback_query.register(on_suspicious_match_callback, F.data.startswith(CB_SUS_MATCH_PREFIX))

    try:
        await bot.set_my_commands(
            [
                BotCommand(command="start", description="Справка и меню"),
                BotCommand(command="analyze", description="Проверить профиль Dota"),
                BotCommand(command="match", description="Сводка по матчу"),
                BotCommand(command="donate", description="Поддержать проект"),
                BotCommand(command="privacy", description="Данные и конфиденциальность"),
                BotCommand(command="support", description="Связь с автором"),
            ]
        )
    except Exception:
        logger.exception("set_my_commands failed")

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())

