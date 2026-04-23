from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AnalyzeResult:
    """HTML report for Telegram and optional Full HD PNG page(s) on `assets/report_background.png` (text-free)."""

    html: str
    card_pngs: tuple[bytes, ...] = ()
