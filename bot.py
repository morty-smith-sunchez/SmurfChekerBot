from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from time import time
from collections.abc import Awaitable, Callable

import httpx
from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums.parse_mode import ParseMode
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, Message, ReplyKeyboardMarkup

from analysis.learning import SmurfSample, adaptive_smurf_bonus, avg_kda, register_confirmed_smurf
from analysis.metrics import PeriodStats, compute_period_stats
from analysis.scoring import score_suspicion
from config import SETTINGS
from dota.dotabuff_client import DotabuffClient
from dota.opendota_client import OpenDotaClient
from dota.steam_client import SteamClient
from dota.stratz_client import StratzClient
from utils.parse_ids import ParsedPlayerId, parse_player_id


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("dota_profile_bot")


BTN_ANALYZE = "Проверить профиль"
BTN_CONFIRM_SMURF = "Подтвердить смурфа (100%)"
BTN_DONATE = "Пожертвовать на развитие"
BTN_CANCEL = "Отмена"
CB_LAST_MATCHES_PREFIX = "last_matches:"
CB_SUS_MATCH_PREFIX = "sus_match:"


class UserInputState(StatesGroup):
    waiting_analyze_target = State()
    waiting_confirm_smurf_target = State()


MAIN_MENU = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=BTN_ANALYZE)],
        [KeyboardButton(text=BTN_CONFIRM_SMURF)],
        [KeyboardButton(text=BTN_DONATE)],
        [KeyboardButton(text=BTN_CANCEL)],
    ],
    resize_keyboard=True,
)


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


async def analyze_player(pid: ParsedPlayerId) -> str:
    now_ts = int(time())
    outbound_proxy = SETTINGS.https_proxy or SETTINGS.http_proxy
    od = OpenDotaClient(api_key=SETTINGS.opendota_api_key, timeout_s=SETTINGS.http_timeout_s)
    steam = SteamClient(api_key=SETTINGS.steam_api_key, timeout_s=SETTINGS.http_timeout_s) if SETTINGS.steam_api_key else None
    stratz = StratzClient(api_key=SETTINGS.stratz_api_key, timeout_s=SETTINGS.http_timeout_s, proxy=outbound_proxy)
    dotabuff = DotabuffClient(timeout_s=SETTINGS.http_timeout_s, proxy=outbound_proxy)

    try:
        stratz_summary = None
        dotabuff_summary = None
        try:
            stratz_summary = await stratz.get_player_summary(pid.account_id)
        except Exception:
            stratz_summary = None
        try:
            dotabuff_summary = await dotabuff.get_player_summary(pid.account_id)
        except Exception:
            dotabuff_summary = None

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

        try:
            player = await od.get_player(pid.account_id)
            matches90 = await od.get_matches(pid.account_id, days=90, limit=500)
            matches365 = await od.get_matches(pid.account_id, days=365, limit=500)
        except (httpx.TimeoutException, httpx.ConnectError, httpx.NetworkError, httpx.HTTPStatusError):
            steam_profile = None
            steam_level = None
            if steam is not None and pid.steamid64 is not None:
                try:
                    steam_profile = await steam.get_player_summaries(pid.steamid64)
                    steam_level = await steam.get_steam_level(pid.steamid64)
                except Exception:
                    steam_profile = None
                    steam_level = None

            lines: list[str] = []
            lines.append("<b>Dota2 анализ профиля</b>")
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
            lines.append("<b>OpenDota сейчас недоступен (ошибка сети)</b>")
            lines.append("<b>Данные по источникам</b>")
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
            return "\n".join(lines)

        # Prefer OpenDota /wl for win/loss (more accurate than limited match list)
        wl_total: dict | None = None
        wl30: dict | None = None
        wl90: dict | None = None
        try:
            wl_total = await od.get_winloss(pid.account_id, days=None)
            wl30 = await od.get_winloss(pid.account_id, days=30)
            wl90 = await od.get_winloss(pid.account_id, days=90)
        except (httpx.TimeoutException, httpx.ConnectError, httpx.NetworkError, httpx.HTTPStatusError):
            # fallback to computed values from recent matches if /wl is unavailable
            wl_total = None
            wl30 = None
            wl90 = None

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
            steam_profile = await steam.get_player_summaries(pid.steamid64)
            steam_level = await steam.get_steam_level(pid.steamid64)

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
        lines.append("<b>Dota2 анализ профиля</b>")
        if title:
            lines.append(f"<b>Ник</b>: {title}")
        lines.append(f"<b>account_id</b>: <code>{pid.account_id}</code>")
        if pid.steamid64:
            lines.append(f"<b>steamid64</b>: <code>{pid.steamid64}</code>")

        if steam_level is not None:
            lines.append(f"<b>Steam Level</b>: {steam_level}")
        if steam_profile and steam_profile.timecreated:
            created = _fmt_ts(steam_profile.timecreated)
            if created:
                lines.append(f"<b>Steam created</b>: {created}")

        if player.rank_tier is not None:
            lines.append(f"<b>Rank tier</b>: {player.rank_tier}")
        if player.leaderboard_rank is not None:
            lines.append(f"<b>Leaderboard</b>: {player.leaderboard_rank}")

        lines.append("")
        lines.append("<b>Данные по источникам</b>")
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
        lines.append("<b>Активность</b>")
        lines.append(f"- 30д: {p30.matches} матчей (~{p30.matches_per_day:.2f}/день)")
        lines.append(f"- 90д: {p90.matches} матчей (~{p90.matches_per_day:.2f}/день)")

        lines.append("")
        lines.append("<b>Винрейт</b>")
        lines.append(f"- общий: {_pct(wr_total)} ({w_total}/{g_total})")
        lines.append(f"- 30д: {_pct(wr30)} ({w30}/{g30})")
        lines.append(f"- 90д: {_pct(wr90)} ({w90}/{g90})")
        if p_before is not None:
            lines.append(f"- пред.90д (120→30): {_pct(p_before.winrate)} ({p_before.wins}/{p_before.matches})")

        lines.append("")
        lines.append("<b>Герои (топ)</b>")
        lines.append("<b>Винрейт на героях за период</b>")
        lines.append(f"- 30д: {_format_top_heroes(p30, hero_map)}")
        lines.append(f"- 90д: {_format_top_heroes(p90, hero_map)}")

        if inactivity_days is not None:
            lines.append("")
            lines.append(f"<b>Пауза перед последней активностью</b>: ~{inactivity_days:.0f} дней")

        lines.append("")
        lines.append("<b>Подозрительность</b>")
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
        lines.append("<i>Важно: это эвристики по публичной статистике, не “вердикт”.</i>")
        lines.append("")
        lines.append("<b>Поучаствовать в разработке</b>")
        lines.append(
            "Если вы на 100% уверены, что это смурф, отправьте:\n"
            "<code>/confirm_smurf_100 &lt;id или ссылка&gt;</code>"
        )
        lines.append("Отправляйте только при полной уверенности: эти кейсы участвуют в калибровке алгоритма.")
        return "\n".join(lines)
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
            "<b>Последние 3 игры</b>\n"
            "Не удалось получить матчи по этому профилю (возможно, профиль закрыт или нет свежих игр)."
        )

    lines: list[str] = [f"<b>Последние 3 игры</b> (<code>{account_id}</code>)"]
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


async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "Пришлите команду:\n"
        "<code>/analyze &lt;steamid64 | account_id | ссылка&gt;</code>\n\n"
        "Если вы 100% уверены, что профиль смурф:\n"
        "<code>/confirm_smurf_100 &lt;id или ссылка&gt;</code>\n\n"
        "Пример:\n"
        "<code>/analyze 76561198xxxxxxxxx</code>\n\n"
        "Или просто используйте кнопки ниже 👇",
        parse_mode=ParseMode.HTML,
        reply_markup=MAIN_MENU,
    )


async def cmd_analyze(message: Message, command: CommandObject) -> None:
    if not command.args:
        await message.answer("Нужно указать id или ссылку. Например: <code>/analyze 123456789</code>", parse_mode=ParseMode.HTML)
        return

    try:
        pid = parse_player_id(command.args)
    except Exception:
        await message.answer("Не смог распознать id. Пришлите steamid64 / account_id / ссылку на dotabuff.", parse_mode=ParseMode.HTML)
        return

    msg = await message.answer("Собираю данные и считаю статистику…", parse_mode=ParseMode.HTML)
    try:
        report = await analyze_player(pid)
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

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Подробно: 3 последние игры",
                    callback_data=f"{CB_LAST_MATCHES_PREFIX}{pid.account_id}",
                )
            ]
        ]
    )
    await msg.edit_text(
        report,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
        reply_markup=keyboard,
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
                reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
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
        suspicious: list[tuple[float, int, str, str, int, float, float, float]] = []
        for p in players:
            if not isinstance(p, dict):
                continue
            account_id = p.get("account_id")
            if not isinstance(account_id, int) or account_id <= 0:
                continue
            persona = p.get("personaname")
            name = persona if isinstance(persona, str) and persona.strip() else f"Player {account_id}"
            hero_id = p.get("hero_id")
            hero_name = f"Hero#{hero_id}" if isinstance(hero_id, int) else "Unknown hero"
            try:
                smurf_score, boost_score, bought_score, matches30 = await _analyze_account_smurf_score(
                    od, account_id, now_ts=now_ts
                )
            except Exception:
                continue
            primary = max(smurf_score, boost_score, bought_score)
            if primary >= 0.45:
                suspicious.append(
                    (primary, account_id, name, hero_name, matches30, smurf_score, boost_score, bought_score)
                )
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
            f"<b>Матч {match_id}</b>\nПодозрительных аккаунтов не найдено по текущим эвристикам.",
            parse_mode=ParseMode.HTML,
        )
        return

    suspicious.sort(key=lambda x: x[0], reverse=True)
    lines: list[str] = [f"<b>Подозрительные аккаунты в матче {match_id}</b>"]
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
            "Пример: <code>/confirm_smurf_100 https://www.dotabuff.com/players/123456789</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    try:
        pid = parse_player_id(command.args)
    except Exception:
        await message.answer("Не смог распознать id. Пришлите steamid64 / account_id / ссылку.", parse_mode=ParseMode.HTML)
        return

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
        "Пришли steamid64 / account_id / ссылку на профиль Dotabuff для анализа.",
        parse_mode=ParseMode.HTML,
        reply_markup=MAIN_MENU,
    )


async def on_confirm_button(message: Message, state: FSMContext) -> None:
    await state.set_state(UserInputState.waiting_confirm_smurf_target)
    await message.answer(
        "Пришли steamid64 / account_id / ссылку на профиль, который ты подтверждаешь как смурф.",
        parse_mode=ParseMode.HTML,
        reply_markup=MAIN_MENU,
    )


async def on_donate_button(message: Message) -> None:
    lines = [
        "<b>Поддержать развитие бота</b>",
        "Спасибо за поддержку проекта! Это помогает оплачивать сервер и улучшать функционал.",
    ]
    if SETTINGS.donation_text:
        lines.append("")
        lines.append(SETTINGS.donation_text)
    if SETTINGS.donation_url:
        lines.append("")
        lines.append(f"Ссылка для доната: {SETTINGS.donation_url}")
    if not SETTINGS.donation_text and not SETTINGS.donation_url:
        lines.append("")
        lines.append("Реквизиты пока не настроены. Напишите администратору бота.")

    await message.answer(
        "\n".join(lines),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
        reply_markup=MAIN_MENU,
    )


async def on_cancel_button(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "Действие отменено. Выбери следующий шаг кнопками ниже.",
        parse_mode=ParseMode.HTML,
        reply_markup=MAIN_MENU,
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


async def main() -> None:
    session = AiohttpSession(proxy=SETTINGS.telegram_proxy) if SETTINGS.telegram_proxy else AiohttpSession()
    bot = Bot(
        token=SETTINGS.require_telegram_token(),
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()
    dp.message.outer_middleware(IncomingMessageLoggingMiddleware())

    dp.message.register(cmd_start, Command("start"))
    dp.message.register(on_cancel_button, F.text == BTN_CANCEL)
    dp.message.register(on_analyze_button, F.text == BTN_ANALYZE)
    dp.message.register(on_confirm_button, F.text == BTN_CONFIRM_SMURF)
    dp.message.register(on_donate_button, F.text == BTN_DONATE)
    dp.message.register(on_analyze_target_input, UserInputState.waiting_analyze_target, F.text)
    dp.message.register(on_confirm_target_input, UserInputState.waiting_confirm_smurf_target, F.text)
    dp.message.register(cmd_analyze, Command("analyze"))
    dp.message.register(cmd_analyze, F.text.startswith("/analyze"))
    dp.message.register(cmd_confirm_smurf_100, Command("confirm_smurf_100"))
    dp.message.register(cmd_confirm_smurf_100, F.text.startswith("/confirm_smurf_100"))
    dp.callback_query.register(on_last_matches_callback, F.data.startswith(CB_LAST_MATCHES_PREFIX))
    dp.callback_query.register(on_suspicious_match_callback, F.data.startswith(CB_SUS_MATCH_PREFIX))

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())

