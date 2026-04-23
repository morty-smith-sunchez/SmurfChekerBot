from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from config import SETTINGS


@dataclass(frozen=True)
class SteamProfile:
    steamid: str
    personaname: str | None
    profileurl: str | None
    avatarfull: str | None
    timecreated: int | None


class SteamClient:
    def __init__(self, *, api_key: str, timeout_s: float = 20.0) -> None:
        self._api_key = api_key
        limits = httpx.Limits(
            max_connections=SETTINGS.http_max_connections,
            max_keepalive_connections=SETTINGS.http_max_keepalive_connections,
        )
        self._client = httpx.AsyncClient(
            base_url="https://api.steampowered.com",
            timeout=httpx.Timeout(timeout_s),
            headers={"User-Agent": "dota_profile_bot/1.0"},
            limits=limits,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def get_player_summaries(self, steamid64: int) -> SteamProfile | None:
        params = {"key": self._api_key, "steamids": str(int(steamid64))}
        r = await self._client.get("/ISteamUser/GetPlayerSummaries/v0002/", params=params)
        r.raise_for_status()
        data = r.json()
        players = (((data or {}).get("response") or {}).get("players") or [])
        if not players:
            return None
        p: dict[str, Any] = players[0]
        return SteamProfile(
            steamid=str(p.get("steamid") or ""),
            personaname=p.get("personaname"),
            profileurl=p.get("profileurl"),
            avatarfull=p.get("avatarfull"),
            timecreated=p.get("timecreated"),
        )

    async def get_steam_level(self, steamid64: int) -> int | None:
        params = {"key": self._api_key, "steamid": str(int(steamid64))}
        r = await self._client.get("/IPlayerService/GetSteamLevel/v1/", params=params)
        r.raise_for_status()
        data = r.json()
        lvl = (((data or {}).get("response") or {}).get("player_level"))
        return int(lvl) if isinstance(lvl, int) else None

    async def resolve_vanity_url(self, vanity: str) -> int | None:
        """Resolve steamcommunity.com/id/<vanity> to steamid64 via ISteamUser/ResolveVanityURL."""
        v = (vanity or "").strip().strip("/")
        if not v or len(v) > 64:
            return None
        params = {"key": self._api_key, "vanityurl": v, "url_type": 1}
        r = await self._client.get("/ISteamUser/ResolveVanityURL/v0001/", params=params)
        r.raise_for_status()
        data = r.json()
        resp = (data or {}).get("response") or {}
        sid = resp.get("steamid")
        if sid is None:
            return None
        try:
            return int(sid)
        except (TypeError, ValueError):
            return None

