from __future__ import annotations

from dataclasses import dataclass
import re

import httpx

from config import SETTINGS


@dataclass(frozen=True)
class DotabuffSummary:
    matches: int | None
    winrate: float | None


class DotabuffClient:
    def __init__(self, *, timeout_s: float = 20.0, proxy: str | None = None) -> None:
        limits = httpx.Limits(
            max_connections=SETTINGS.http_max_connections,
            max_keepalive_connections=SETTINGS.http_max_keepalive_connections,
        )
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_s),
            limits=limits,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                )
            },
            follow_redirects=True,
            proxy=proxy,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def get_player_summary(self, account_id: int) -> DotabuffSummary | None:
        url = f"https://www.dotabuff.com/players/{int(account_id)}"
        r = await self._client.get(url)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        text = r.text or ""

        matches = None
        winrate = None

        m1 = re.search(r"Lifetime[^<]{0,120}(\d[\d, ]*)\s+Matches", text, flags=re.IGNORECASE | re.DOTALL)
        if m1:
            raw = m1.group(1).replace(",", "").replace(" ", "")
            if raw.isdigit():
                matches = int(raw)

        m2 = re.search(r"Win Rate[^<]{0,120}(\d{1,3}(?:\.\d+)?)%", text, flags=re.IGNORECASE | re.DOTALL)
        if m2:
            try:
                winrate = float(m2.group(1))
            except ValueError:
                winrate = None

        return DotabuffSummary(matches=matches, winrate=winrate)
