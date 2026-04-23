from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AnalyzeResult:
    """HTML report for Telegram and optional two Full HD PNG pages on `assets/report_background.png`."""

    html: str
    card_pngs: tuple[bytes, ...] = ()
