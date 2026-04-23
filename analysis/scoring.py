from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .metrics import PeriodStats, jaccard_top_heroes


@dataclass(frozen=True)
class SuspicionScore:
    smurf_score: float
    bought_score: float
    reasons_smurf: list[str]
    reasons_bought: list[str]


def clamp01(x: float) -> float:
    if x < 0:
        return 0.0
    if x > 1:
        return 1.0
    return x


def _match_win(m: dict[str, Any]) -> bool | None:
    rw = m.get("radiant_win")
    ps = m.get("player_slot")
    if isinstance(rw, bool) and isinstance(ps, int):
        is_radiant = ps < 128
        return rw if is_radiant else (not rw)
    w = m.get("win")
    return True if w is True else False if w is False else None


def _safe_float(v: Any) -> float | None:
    if isinstance(v, (int, float)):
        return float(v)
    return None


def _avg_perf(matches: list[dict[str, Any]]) -> tuple[float, float, float]:
    kda_vals: list[float] = []
    gpm_vals: list[float] = []
    xpm_vals: list[float] = []
    for m in matches:
        k = _safe_float(m.get("kills"))
        d = _safe_float(m.get("deaths"))
        a = _safe_float(m.get("assists"))
        gpm = _safe_float(m.get("gold_per_min"))
        xpm = _safe_float(m.get("xp_per_min"))
        if k is not None and d is not None and a is not None:
            kda_vals.append((k + a) / max(1.0, d))
        if gpm is not None:
            gpm_vals.append(gpm)
        if xpm is not None:
            xpm_vals.append(xpm)
    kda = sum(kda_vals) / len(kda_vals) if kda_vals else 0.0
    gpm = sum(gpm_vals) / len(gpm_vals) if gpm_vals else 0.0
    xpm = sum(xpm_vals) / len(xpm_vals) if xpm_vals else 0.0
    return kda, gpm, xpm


def _perf_field_coverage(matches: list[dict[str, Any]]) -> tuple[int, int, int]:
    kda_rows = 0
    gpm_rows = 0
    xpm_rows = 0
    for m in matches:
        k = _safe_float(m.get("kills"))
        d = _safe_float(m.get("deaths"))
        a = _safe_float(m.get("assists"))
        if k is not None and d is not None and a is not None:
            kda_rows += 1
        if _safe_float(m.get("gold_per_min")) is not None:
            gpm_rows += 1
        if _safe_float(m.get("xp_per_min")) is not None:
            xpm_rows += 1
    return kda_rows, gpm_rows, xpm_rows


def _party_and_solo_wr(matches: list[dict[str, Any]]) -> tuple[int, float, int, float]:
    party_games = 0
    party_wins = 0
    solo_games = 0
    solo_wins = 0
    for m in matches:
        win = _match_win(m)
        if win is None:
            continue
        ps = m.get("party_size")
        if isinstance(ps, int) and ps >= 2:
            party_games += 1
            if win:
                party_wins += 1
        elif isinstance(ps, int) and ps == 1:
            solo_games += 1
            if win:
                solo_wins += 1
    party_wr = (party_wins / party_games * 100.0) if party_games else 0.0
    solo_wr = (solo_wins / solo_games * 100.0) if solo_games else 0.0
    return party_games, party_wr, solo_games, solo_wr


def _best_wr_window(matches: list[dict[str, Any]], *, window: int = 20) -> tuple[float, int]:
    seq: list[int] = []
    for m in sorted(matches, key=lambda x: int(x.get("start_time") or 0)):
        w = _match_win(m)
        if w is None:
            continue
        seq.append(1 if w else 0)
    if len(seq) < window:
        return 0.0, 0
    best = 0.0
    cnt = 0
    cur = sum(seq[:window])
    wr = cur / window * 100.0
    if wr > best:
        best = wr
    if 75.0 <= wr <= 95.0:
        cnt += 1
    for i in range(window, len(seq)):
        cur += seq[i] - seq[i - window]
        wr = cur / window * 100.0
        if wr > best:
            best = wr
        if 75.0 <= wr <= 95.0:
            cnt += 1
    return best, cnt


def _perf_thresholds(rank_tier: int | None) -> tuple[float, float, float]:
    # Rough high-percentile bars by medal group; tuned as heuristics.
    if isinstance(rank_tier, int):
        medal = rank_tier // 10
        if medal >= 8:  # Immortal
            return 4.0, 620.0, 700.0
        if medal >= 7:  # Divine
            return 3.8, 580.0, 670.0
        if medal >= 6:  # Ancient
            return 3.5, 540.0, 630.0
    return 3.3, 520.0, 600.0


def score_suspicion(
    *,
    p30: PeriodStats,
    p90: PeriodStats,
    p_before: PeriodStats | None,
    matches30: list[dict[str, Any]],
    matches90: list[dict[str, Any]],
    rank_tier: int | None,
    account_age_days: float | None,
    total_games: int,
    total_winrate: float,
    inactivity_days: float | None,
) -> SuspicionScore:

    smurf = 0.0
    bought = 0.0
    rs: list[str] = []
    rb: list[str] = []

    hero_similarity = None
    if p_before is not None:
        hero_similarity = jaccard_top_heroes(p_before, p30, k=5)

    avg_kda30, avg_gpm30, avg_xpm30 = _avg_perf(matches30)
    kda_rows30, gpm_rows30, xpm_rows30 = _perf_field_coverage(matches30)
    party_g, party_wr, solo_g, solo_wr = _party_and_solo_wr(matches90)
    best_window_wr, spike_windows = _best_wr_window(matches90, window=20)

    # 1) "Смурфы": относительно свежий аккаунт + высокий ранг и сильная статистика.
    if (
        total_games > 0
        and total_games <= 1200
        and p30.matches >= 20
        and p30.winrate >= 60.0
        and avg_kda30 >= 3.3
        and avg_gpm30 >= 520.0
        and avg_xpm30 >= 600.0
    ):
        smurf += 0.35
        rs.append(
            f"Высокий перформанс при небольшом объёме игр ({total_games}): "
            f"WR30 {p30.winrate:.1f}%, KDA {avg_kda30:.2f}, GPM {avg_gpm30:.0f}, XPM {avg_xpm30:.0f}"
        )

    # 1b) Калибровка для "свежих" аккаунтов, где OpenDota не всегда отдает GPM/XPM.
    if (
        total_games > 0
        and total_games <= 80
        and p30.matches >= 20
        and p30.winrate >= 68.0
        and kda_rows30 >= 15
        and avg_kda30 >= 4.8
    ):
        smurf += 0.75
        rs.append(
            f"Свежий аккаунт с аномальным аплифтом: {total_games} игр, "
            f"WR30 {p30.winrate:.1f}%, KDA {avg_kda30:.2f}"
        )

    # 2) Возврат после паузы + новые герои + много побед = смурф.
    if (
        inactivity_days is not None
        and inactivity_days >= 21
        and p30.matches >= 15
        and p30.winrate >= 58.0
        and hero_similarity is not None
        and hero_similarity <= 0.25
    ):
        smurf += 0.5
        rs.append(
            f"После паузы ~{inactivity_days:.0f}д игрок вернулся с другим пулом героев "
            f"и высоким винрейтом за 30д ({p30.winrate:.1f}%)"
        )

    # 3) Возврат после паузы + новые герои + много поражений = куплен/передан.
    if (
        inactivity_days is not None
        and inactivity_days >= 21
        and p30.matches >= 15
        and p30.winrate <= 45.0
        and hero_similarity is not None
        and hero_similarity <= 0.25
    ):
        bought += 0.7
        rb.append(
            f"После паузы ~{inactivity_days:.0f}д игрок вернулся с другим пулом героев "
            f"и низким винрейтом за 30д ({p30.winrate:.1f}%)"
        )

    # 4) Пати-буст: аномальная разница WR в пати и соло.
    if party_g >= 20 and solo_g >= 20 and party_wr >= 70.0 and (party_wr - solo_wr) >= 18.0:
        bought += 0.45
        rb.append(
            f"Аномальный пати-паттерн: пати WR {party_wr:.1f}% ({party_g} игр) "
            f"vs соло WR {solo_wr:.1f}% ({solo_g} игр)"
        )

    # 5) WR-спайки: окна 20+ матчей с 75-95% на фоне стабильного базового WR.
    if p90.matches >= 40 and 47.0 <= p90.winrate <= 55.0 and spike_windows >= 1:
        smurf += 0.20
        bought += 0.20
        rs.append(f"Есть WR-спайк: до {best_window_wr:.1f}% на окне 20 матчей при базовом WR90 {p90.winrate:.1f}%")
        rb.append(f"Есть WR-спайк: до {best_window_wr:.1f}% на окне 20 матчей при базовом WR90 {p90.winrate:.1f}%")

    # 5b) Устойчиво высокий WR на большой выборке + высокий KDA.
    if (
        p90.matches >= 80
        and p90.winrate >= 63.0
        and total_games <= 900
        and kda_rows30 >= 20
        and avg_kda30 >= 5.8
    ):
        smurf += 0.60
        rs.append(
            f"Устойчиво высокий WR на дистанции: WR90 {p90.winrate:.1f}% "
            f"на {p90.matches} матчах при KDA {avg_kda30:.2f}"
        )

    # 6) Перформанс: KDA/GPM/XPM заметно выше нормы по рангу.
    kda_t, gpm_t, xpm_t = _perf_thresholds(rank_tier)
    if (
        p30.matches >= 20
        and kda_rows30 >= 15
        and avg_kda30 >= kda_t
        and (
            (gpm_rows30 >= 10 and xpm_rows30 >= 10 and avg_gpm30 >= gpm_t and avg_xpm30 >= xpm_t)
            or (gpm_rows30 < 10 and xpm_rows30 < 10 and avg_kda30 >= (kda_t + 0.6))
        )
    ):
        smurf += 0.25
        if gpm_rows30 >= 10 and xpm_rows30 >= 10:
            rs.append(
                f"Перформанс выше бенчмарка ранга: KDA {avg_kda30:.2f} (>{kda_t:.1f}), "
                f"GPM {avg_gpm30:.0f} (>{gpm_t:.0f}), XPM {avg_xpm30:.0f} (>{xpm_t:.0f})"
            )
        else:
            rs.append(
                f"Перформанс выше бенчмарка ранга (по доступным данным): "
                f"KDA {avg_kda30:.2f} (>{kda_t + 0.6:.1f})"
            )

    # 7) Новые/«воскрешенные» герои с высоким WR.
    if p_before is not None and p30.matches >= 15:
        prev_hero_ids = {hid for hid, _g, _wr in p_before.top_heroes[:10]}
        hot_new_heroes = [
            (hid, games, wr)
            for hid, games, wr in p30.top_heroes_by_winrate
            if games >= 4 and 70.0 <= wr <= 85.0 and hid not in prev_hero_ids
        ]
        if hero_similarity is not None and hero_similarity <= 0.35 and len(hot_new_heroes) >= 2:
            smurf += 0.25
            rs.append(
                f"Новый пул героев с резким аплифтом: {len(hot_new_heroes)} героя(ев) "
                f"в диапазоне WR 70-85%"
            )

    # 8) Возраст аккаунта vs ранг (ускоренный прогресс).
    if account_age_days is not None and rank_tier is not None:
        medal = rank_tier // 10
        if (medal >= 7 and account_age_days <= 100) or (medal >= 8 and total_games <= 250):
            smurf += 0.35
            rs.append(
                f"Несоответствие возраста аккаунта и ранга: rank_tier={rank_tier}, "
                f"возраст ~{account_age_days:.0f} дней, матчей {total_games}"
            )

    # Доп. сигнал покупки: сильная просадка относительно предыдущего периода.
    if p_before is not None and p_before.matches >= 15 and p30.matches >= 15:
        drop = p_before.winrate - p30.winrate
        if drop >= 12.0:
            bought += 0.25
            rb.append(f"Резкое падение винрейта: было {p_before.winrate:.1f}% → стало {p30.winrate:.1f}%")

    return SuspicionScore(
        smurf_score=clamp01(smurf),
        bought_score=clamp01(bought),
        reasons_smurf=rs,
        reasons_bought=rb,
    )

