from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AnalyzeResult:
    """HTML report for Telegram and optional Full HD PNG page(s) rendered on the welcome banner."""

    html: str
    card_pngs: tuple[bytes, ...] = ()
