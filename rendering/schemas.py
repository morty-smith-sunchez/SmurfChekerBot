from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AnalyzeResult:
    """HTML report for Telegram and optional PNG card rendered on the welcome banner."""

    html: str
    card_png: bytes | None = None
