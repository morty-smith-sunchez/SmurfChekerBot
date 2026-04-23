from __future__ import annotations

import re
from dataclasses import dataclass

from dota.steam_client import SteamClient


STEAMID64_BASE = 76561197960265728


@dataclass(frozen=True)
class ParsedPlayerId:
    account_id: int
    steamid64: int | None


_DIGITS_RE = re.compile(r"(\d{6,20})")
_MATCH_IN_URL_RE = re.compile(r"/matches/(\d{6,14})", re.IGNORECASE)
# steamcommunity.com/profiles/7656119…
_STEAM_PROFILES_URL_RE = re.compile(
    r"(?:https?://)?(?:www\.)?steamcommunity\.com/profiles/(\d{17})\b",
    re.IGNORECASE,
)
# store.steampowered.com/profile/7656119…
_STEAM_STORE_PROFILE_RE = re.compile(
    r"(?:https?://)?(?:www\.)?store\.steampowered\.com/profile/(\d{17})\b",
    re.IGNORECASE,
)
# steamcommunity.com/id/<vanity> (custom URL)
_STEAM_VANITY_URL_RE = re.compile(
    r"(?:https?://)?(?:www\.)?steamcommunity\.com/id/([^/?#\s]+)",
    re.IGNORECASE,
)


def steamid64_to_account_id(steamid64: int) -> int:
    return int(steamid64) - STEAMID64_BASE


def account_id_to_steamid64(account_id: int) -> int:
    return int(account_id) + STEAMID64_BASE


def _parsed_from_steamid64(sid: int) -> ParsedPlayerId:
    if sid < STEAMID64_BASE:
        raise ValueError("некорректный steamid64 в ссылке")
    return ParsedPlayerId(account_id=steamid64_to_account_id(sid), steamid64=sid)


def parse_player_id(text: str) -> ParsedPlayerId:
    """
    Accepts:
    - account_id (OpenDota/Dota id)
    - steamid64 (17 digits)
    - Dotabuff / OpenDota links (digits in URL)
    - steamcommunity.com/profiles/<steamid64>
    - store.steampowered.com/profile/<steamid64>
    """
    s = (text or "").strip()
    if not s:
        raise ValueError("empty input")

    m = _STEAM_PROFILES_URL_RE.search(s) or _STEAM_STORE_PROFILE_RE.search(s)
    if m:
        return _parsed_from_steamid64(int(m.group(1)))

    if _STEAM_VANITY_URL_RE.search(s) and not _STEAM_PROFILES_URL_RE.search(s):
        raise ValueError(
            "Короткая ссылка Steam (custom URL) сейчас не поддерживается в этом режиме. "
            "Пришлите числовой steamid64 или ссылку на профиль с id в пути /profiles/…"
        )

    m = _DIGITS_RE.search(s)
    if not m:
        raise ValueError("no digits found")

    n = int(m.group(1))
    if n >= STEAMID64_BASE:
        return ParsedPlayerId(account_id=steamid64_to_account_id(n), steamid64=n)
    acc = n
    if acc < 0:
        raise ValueError("invalid id")
    return ParsedPlayerId(account_id=acc, steamid64=account_id_to_steamid64(acc))


async def parse_player_id_resolved(text: str, steam: SteamClient | None) -> ParsedPlayerId:
    """
    Like parse_player_id, plus steamcommunity.com/id/<vanity> when ``steam`` client is configured.
    """
    s = (text or "").strip()
    if not s:
        raise ValueError("Пустой ввод.")

    m = _STEAM_PROFILES_URL_RE.search(s) or _STEAM_STORE_PROFILE_RE.search(s)
    if m:
        return _parsed_from_steamid64(int(m.group(1)))

    mv = _STEAM_VANITY_URL_RE.search(s)
    if mv and not _STEAM_PROFILES_URL_RE.search(s):
        if steam is None:
            raise ValueError(
                "Короткая ссылка Steam (custom URL) недоступна. Пришлите steamid64 или ссылку на профиль с числовым id."
            )
        vanity = mv.group(1)
        sid = await steam.resolve_vanity_url(vanity)
        if sid is None:
            raise ValueError("Не удалось найти профиль по этой ссылке (vanity URL).")
        return _parsed_from_steamid64(sid)

    try:
        return parse_player_id(s)
    except ValueError as e:
        msg = str(e) or "не удалось разобрать id"
        raise ValueError(msg) from e


def parse_match_id(text: str) -> int:
    """
    Accepts:
    - numeric match_id
    - OpenDota / Dotabuff / STRATZ URLs containing .../matches/<id>
    """
    s = (text or "").strip()
    if not s:
        raise ValueError("empty input")

    m = _MATCH_IN_URL_RE.search(s)
    if m:
        return int(m.group(1))

    m = _DIGITS_RE.search(s)
    if not m:
        raise ValueError("no digits found")

    n = int(m.group(1))
    if n < 100_000:
        raise ValueError("match id looks too small")
    if n >= STEAMID64_BASE:
        raise ValueError("looks like steamid64, not match id")
    return n
