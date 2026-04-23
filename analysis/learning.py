from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DATA_DIR = Path(__file__).resolve().parent.parent / "data"
SMURF_FILE = DATA_DIR / "confirmed_smurfs.json"


@dataclass(frozen=True)
class AdaptiveCalibration:
    """Пороги адаптивного бонуса к смурф-скору из медиан подтверждённых кейсов."""

    n_samples: int
    med_games: int
    med_wr30: float
    med_wr90: float
    med_kda: float
    games_limit: int
    wr30_bar: float
    wr90_bar: float
    kda_bar: float
    bonus_cap: float


@dataclass(frozen=True)
class SmurfSample:
    account_id: int
    total_games: int
    wr30: float
    wr90: float
    kda30: float
    matches30: int
    rank_tier: int | None


def avg_kda(matches: list[dict[str, Any]]) -> float:
    vals: list[float] = []
    for m in matches:
        k = m.get("kills")
        d = m.get("deaths")
        a = m.get("assists")
        if isinstance(k, (int, float)) and isinstance(d, (int, float)) and isinstance(a, (int, float)):
            vals.append((float(k) + float(a)) / max(1.0, float(d)))
    return (sum(vals) / len(vals)) if vals else 0.0


def _sample_to_dict(s: SmurfSample) -> dict[str, Any]:
    return {
        "account_id": s.account_id,
        "total_games": s.total_games,
        "wr30": s.wr30,
        "wr90": s.wr90,
        "kda30": s.kda30,
        "matches30": s.matches30,
        "rank_tier": s.rank_tier,
    }


def _dict_to_sample(d: dict[str, Any]) -> SmurfSample | None:
    try:
        account_id = int(d.get("account_id"))
        total_games = int(d.get("total_games"))
        wr30 = float(d.get("wr30"))
        wr90 = float(d.get("wr90"))
        kda30 = float(d.get("kda30"))
        matches30 = int(d.get("matches30"))
        rt = d.get("rank_tier")
        rank_tier = int(rt) if isinstance(rt, int) else None
        if account_id <= 0:
            return None
        return SmurfSample(
            account_id=account_id,
            total_games=total_games,
            wr30=wr30,
            wr90=wr90,
            kda30=kda30,
            matches30=matches30,
            rank_tier=rank_tier,
        )
    except Exception:
        return None


def load_confirmed_smurfs() -> list[SmurfSample]:
    if not SMURF_FILE.exists():
        return []
    try:
        raw = json.loads(SMURF_FILE.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            return []
        out: list[SmurfSample] = []
        for item in raw:
            if isinstance(item, dict):
                s = _dict_to_sample(item)
                if s is not None:
                    out.append(s)
        return out
    except Exception:
        return []


def save_confirmed_smurfs(samples: list[SmurfSample]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    payload = [_sample_to_dict(s) for s in samples]
    SMURF_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def register_confirmed_smurf(sample: SmurfSample) -> int:
    samples = load_confirmed_smurfs()
    by_acc: dict[int, SmurfSample] = {s.account_id: s for s in samples}
    by_acc[sample.account_id] = sample
    merged = list(by_acc.values())
    save_confirmed_smurfs(merged)
    return len(merged)


def remove_confirmed_smurf(account_id: int) -> tuple[bool, int]:
    """Удалить кейс из калибровки. Возвращает (был ли удалён, новое количество)."""
    samples = load_confirmed_smurfs()
    new_list = [s for s in samples if s.account_id != account_id]
    if len(new_list) == len(samples):
        return False, len(samples)
    save_confirmed_smurfs(new_list)
    return True, len(new_list)


def calibration_from_samples(samples: list[SmurfSample]) -> AdaptiveCalibration | None:
    n = len(samples)
    if n < 2:
        return None
    med_games = sorted(s.total_games for s in samples)[n // 2]
    med_wr30 = sorted(s.wr30 for s in samples)[n // 2]
    med_wr90 = sorted(s.wr90 for s in samples)[n // 2]
    med_kda = sorted(s.kda30 for s in samples)[n // 2]
    games_limit = max(120, int(med_games * 1.8))
    wr30_bar = max(55.0, med_wr30 - 6.0)
    wr90_bar = max(56.0, med_wr90 - 7.0)
    kda_bar = max(3.6, med_kda - 1.2)
    bonus_cap = min(0.35, 0.08 * float(n))
    return AdaptiveCalibration(
        n_samples=n,
        med_games=int(med_games),
        med_wr30=float(med_wr30),
        med_wr90=float(med_wr90),
        med_kda=float(med_kda),
        games_limit=games_limit,
        wr30_bar=wr30_bar,
        wr90_bar=wr90_bar,
        kda_bar=kda_bar,
        bonus_cap=bonus_cap,
    )


def get_adaptive_calibration() -> AdaptiveCalibration | None:
    return calibration_from_samples(load_confirmed_smurfs())


def adaptive_smurf_bonus(
    *,
    total_games: int,
    wr30: float,
    wr90: float,
    kda30: float,
    matches30: int,
) -> tuple[float, str | None]:
    samples = load_confirmed_smurfs()
    cal = calibration_from_samples(samples)
    if cal is None:
        return 0.0, None

    if (
        matches30 >= 15
        and total_games <= cal.games_limit
        and wr30 >= cal.wr30_bar
        and wr90 >= cal.wr90_bar
        and kda30 >= cal.kda_bar
    ):
        bonus = cal.bonus_cap
        reason = (
            f"Адаптивный сигнал: профиль похож на подтвержденные смурфы "
            f"(база: {cal.n_samples} кейсов)"
        )
        return bonus, reason
    return 0.0, None
