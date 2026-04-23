from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import asyncio
import httpx


@dataclass(frozen=True)
class OpenDotaPlayerSummary:
    profile: dict[str, Any] | None
    rank_tier: int | None
    leaderboard_rank: int | None


class OpenDotaClient:
    def __init__(self, *, api_key: str | None, timeout_s: float = 45.0) -> None:
        self._api_key = api_key
        self._client = httpx.AsyncClient(
            base_url="https://api.opendota.com",
            timeout=httpx.Timeout(timeout_s),
            headers={"User-Agent": "dota_profile_bot/1.0"},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    def _params(self, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        p: dict[str, Any] = {}
        if self._api_key:
            p["api_key"] = self._api_key
        if extra:
            p.update(extra)
        return p

    async def _get_json(self, path: str, *, params: dict[str, Any] | None = None) -> Any:
        """
        OpenDota is sometimes slow and can rate-limit (429).
        We retry timeouts/429/5xx with backoff to avoid hard failures.
        """
        backoff_s = 0.8
        last_exc: Exception | None = None
        for _ in range(3):
            try:
                r = await self._client.get(path, params=params)
                if r.status_code == 429 or 500 <= r.status_code <= 599:
                    raise httpx.HTTPStatusError("retryable status", request=r.request, response=r)
                r.raise_for_status()
                return r.json()
            except (httpx.ReadTimeout, httpx.ConnectTimeout, httpx.ConnectError, httpx.RemoteProtocolError, httpx.HTTPStatusError) as e:
                last_exc = e
                # simple exponential backoff
                await asyncio.sleep(backoff_s)
                backoff_s = min(backoff_s * 1.7, 4.0)
                continue
        if last_exc is not None:
            raise last_exc
        raise RuntimeError("OpenDota request failed")

    async def get_player(self, account_id: int) -> OpenDotaPlayerSummary:
        data = await self._get_json(f"/api/players/{account_id}", params=self._params())
        return OpenDotaPlayerSummary(
            profile=data.get("profile"),
            rank_tier=data.get("rank_tier"),
            leaderboard_rank=data.get("leaderboard_rank"),
        )

    async def get_winloss(self, account_id: int, *, days: int | None = None) -> dict[str, Any]:
        params = {}
        if days is not None:
            params["date"] = int(days)
        data = await self._get_json(f"/api/players/{account_id}/wl", params=self._params(params))
        return data if isinstance(data, dict) else {}

    async def get_matches(self, account_id: int, *, days: int, limit: int = 200) -> list[dict[str, Any]]:
        # OpenDota: date=number of days
        params = {"date": int(days), "limit": int(limit)}
        data = await self._get_json(f"/api/players/{account_id}/matches", params=self._params(params))
        return data if isinstance(data, list) else []

    async def get_heroes(self, account_id: int, *, limit: int = 20) -> list[dict[str, Any]]:
        params = {"limit": int(limit)}
        data = await self._get_json(f"/api/players/{account_id}/heroes", params=self._params(params))
        return data if isinstance(data, list) else []

    async def get_hero_stats(self) -> list[dict[str, Any]]:
        data = await self._get_json("/api/heroStats", params=self._params())
        return data if isinstance(data, list) else []

