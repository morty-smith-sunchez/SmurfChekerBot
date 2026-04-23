from __future__ import annotations

import re
from dataclasses import dataclass


STEAMID64_BASE = 76561197960265728


@dataclass(frozen=True)
class ParsedPlayerId:
    account_id: int
    steamid64: int | None


_DIGITS_RE = re.compile(r"(\d{6,20})")


def steamid64_to_account_id(steamid64: int) -> int:
    return int(steamid64) - STEAMID64_BASE


def account_id_to_steamid64(account_id: int) -> int:
    return int(account_id) + STEAMID64_BASE


def parse_player_id(text: str) -> ParsedPlayerId:
    """
    Accepts:
    - account_id (typical OpenDota/Dota player id, ~9 digits)
    - steamid64 (17 digits)
    - dotabuff/opendota links containing the id
    """
    s = (text or "").strip()
    if not s:
        raise ValueError("empty input")

    m = _DIGITS_RE.search(s)
    if not m:
        raise ValueError("no digits found")

    n = int(m.group(1))
    if n >= STEAMID64_BASE:  # steamid64
        return ParsedPlayerId(account_id=steamid64_to_account_id(n), steamid64=n)
    # assume account_id
    acc = n
    if acc < 0:
        raise ValueError("invalid id")
    return ParsedPlayerId(account_id=acc, steamid64=account_id_to_steamid64(acc))

