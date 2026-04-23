from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from config import SETTINGS


@dataclass(frozen=True)
class StratzSummary:
    matches: int | None
    wins: int | None
    winrate: float | None


class StratzClient:
    def __init__(self, *, api_key: str | None, timeout_s: float = 20.0, proxy: str | None = None) -> None:
        self._api_key = api_key
        headers = {"User-Agent": "dota_profile_bot/1.0"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        limits = httpx.Limits(
            max_connections=SETTINGS.http_max_connections,
            max_keepalive_connections=SETTINGS.http_max_keepalive_connections,
        )
        self._client = httpx.AsyncClient(
            base_url="https://api.stratz.com",
            timeout=httpx.Timeout(timeout_s),
            headers=headers,
            proxy=proxy,
            limits=limits,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def get_player_summary(self, account_id: int) -> StratzSummary | None:
        # STRATZ is most stable via GraphQL endpoint.
        if not self._api_key:
            return None

        query = """
        query PlayerSummary($id: Long!) {
          player(steamAccountId: $id) {
            matchCount
            winCount
          }
        }
        """
        payload = {"query": query, "variables": {"id": int(account_id)}}
        r = await self._client.post("/graphql", json=payload)
        if r.status_code in (401, 403, 404):
            return None
        r.raise_for_status()

        raw: dict[str, Any] = r.json() if isinstance(r.json(), dict) else {}
        if raw.get("errors"):
            return None
        data = ((raw.get("data") or {}).get("player") or {})
        if not isinstance(data, dict):
            return None

        matches = data.get("matchCount")
        wins = data.get("winCount")
        m = int(matches) if isinstance(matches, int) else None
        w = int(wins) if isinstance(wins, int) else None
        wr = (w / m * 100.0) if (m and w is not None and m > 0) else None
        return StratzSummary(matches=m, wins=w, winrate=wr)
