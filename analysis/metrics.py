from __future__ import annotations

from dataclasses import dataclass
from time import time
from typing import Any


@dataclass(frozen=True)
class PeriodStats:
    days: int
    matches: int
    wins: int
    winrate: float
    matches_per_day: float
    top_heroes: list[tuple[int, int, float]]  # by games: (hero_id, games, winrate_pct)
    top_heroes_by_winrate: list[tuple[int, int, float]]  # by winrate: (hero_id, games, winrate_pct)
    lane_roles: dict[int, int]  # lane_role -> games


def _safe_div(a: float, b: float) -> float:
    return a / b if b else 0.0


def _match_is_win(m: dict[str, Any]) -> bool | None:
    """
    OpenDota match rows usually contain:
    - radiant_win: bool
    - player_slot: int (0..127 radiant, 128..255 dire)
    """
    rw = m.get("radiant_win")
    ps = m.get("player_slot")
    if not isinstance(rw, bool) or not isinstance(ps, int):
        # Some endpoints may already provide win flag
        w = m.get("win")
        return True if w is True else False if w is False else None
    is_radiant = ps < 128
    return rw if is_radiant else (not rw)


def compute_period_stats(matches: list[dict[str, Any]], *, days: int, now_ts: int | None = None) -> PeriodStats:
    now = int(now_ts if now_ts is not None else time())
    cutoff = now - int(days * 86400)

    filtered: list[dict[str, Any]] = []
    for m in matches:
        st = m.get("start_time")
        if isinstance(st, int) and st >= cutoff:
            filtered.append(m)

    wins = 0
    hero_counts: dict[int, int] = {}
    hero_wins: dict[int, int] = {}
    lane_roles: dict[int, int] = {}
    for m in filtered:
        w = _match_is_win(m)
        if w is True:
            wins += 1
        hid = m.get("hero_id")
        if isinstance(hid, int):
            hero_counts[hid] = hero_counts.get(hid, 0) + 1
            if w is True:
                hero_wins[hid] = hero_wins.get(hid, 0) + 1
        lr = m.get("lane_role")
        if isinstance(lr, int):
            lane_roles[lr] = lane_roles.get(lr, 0) + 1

    total = len(filtered)
    hero_stats: list[tuple[int, int, float]] = []
    for hid, games in hero_counts.items():
        wr_h = _safe_div(float(hero_wins.get(hid, 0)), float(games)) * 100.0
        hero_stats.append((hid, games, wr_h))

    top_heroes = sorted(hero_stats, key=lambda x: x[1], reverse=True)[:10]
    # Show heroes with best winrate, but require some sample size.
    hero_stats_min_games = [x for x in hero_stats if x[1] >= 3]
    ranking_pool = hero_stats_min_games if hero_stats_min_games else hero_stats
    top_heroes_by_winrate = sorted(ranking_pool, key=lambda x: (x[2], x[1]), reverse=True)[:10]
    wr = _safe_div(wins, total) * 100.0
    mpd = _safe_div(total, float(days))
    return PeriodStats(
        days=days,
        matches=total,
        wins=wins,
        winrate=wr,
        matches_per_day=mpd,
        top_heroes=top_heroes,
        top_heroes_by_winrate=top_heroes_by_winrate,
        lane_roles=lane_roles,
    )


def jaccard_top_heroes(a: PeriodStats, b: PeriodStats, *, k: int = 5) -> float:
    sa = {hid for hid, _games, _wr in a.top_heroes[:k]}
    sb = {hid for hid, _games, _wr in b.top_heroes[:k]}
    if not sa and not sb:
        return 1.0
    return len(sa & sb) / max(1, len(sa | sb))

