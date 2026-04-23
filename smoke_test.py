from __future__ import annotations

import asyncio

from config import SETTINGS
from dota.dotabuff_client import DotabuffClient
from dota.opendota_client import OpenDotaClient
from dota.steam_client import SteamClient
from dota.stratz_client import StratzClient


async def main() -> None:
    outbound_proxy = SETTINGS.https_proxy or SETTINGS.http_proxy
    od = OpenDotaClient(api_key=SETTINGS.opendota_api_key, timeout_s=SETTINGS.http_timeout_s)
    try:
        print("OpenDota: requesting /heroStats ...")
        heroes = await od.get_hero_stats()
        print("OpenDota heroStats:", len(heroes))
    except Exception as e:
        print("OpenDota error:", type(e).__name__, str(e)[:200])
    finally:
        await od.aclose()

    if SETTINGS.steam_api_key:
        steam = SteamClient(api_key=SETTINGS.steam_api_key, timeout_s=SETTINGS.http_timeout_s)
        try:
            # Valve test steamid (Gabe). We only validate that the request works.
            print("Steam: requesting GetPlayerSummaries ...")
            profile = await steam.get_player_summaries(76561197960287930)
            print("Steam summaries:", "ok" if profile else "empty")
        except Exception as e:
            print("Steam error:", type(e).__name__, str(e)[:200])
        finally:
            await steam.aclose()

    stratz = StratzClient(
        api_key=SETTINGS.stratz_api_key,
        timeout_s=SETTINGS.http_timeout_s,
        proxy=outbound_proxy,
    )
    try:
        print("STRATZ: requesting player summary ...")
        summary = await stratz.get_player_summary(321580662)
        if summary and (summary.matches is not None or summary.winrate is not None):
            print("STRATZ summary:", summary.matches, summary.winrate)
        else:
            print("STRATZ summary: empty (check STRATZ_API_KEY / profile visibility)")
    except Exception as e:
        print("STRATZ error:", type(e).__name__, str(e)[:200])
    finally:
        await stratz.aclose()

    dotabuff = DotabuffClient(timeout_s=SETTINGS.http_timeout_s, proxy=outbound_proxy)
    try:
        print("Dotabuff: requesting player page ...")
        summary = await dotabuff.get_player_summary(86745912)
        if summary:
            print("Dotabuff summary:", summary.matches, summary.winrate)
        else:
            print("Dotabuff summary: empty")
    except Exception as e:
        print("Dotabuff error:", type(e).__name__, str(e)[:200])
    finally:
        await dotabuff.aclose()


if __name__ == "__main__":
    asyncio.run(main())

