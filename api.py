from __future__ import annotations

import base64
import logging
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from bot import analyze_player, build_match_info_report
from config import SETTINGS
from dota.steam_client import SteamClient
from utils.parse_ids import ParsedPlayerId, parse_match_id, parse_player_id_resolved


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
# httpx INFO пишет полные URL с api_key в query — не выводим секреты в логи
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("dota_profile_bot.api")

app = FastAPI(
    title="Dota Profile Analyzer API",
    description="REST-интерфейс поверх анализатора профилей Dota 2 (SmurfChekBot).",
    version="1.0.0",
)


class AnalyzeRequest(BaseModel):
    player: str = Field(..., description="steamid64, account_id или ссылка на профиль")


class AnalyzeResponse(BaseModel):
    account_id: int
    steamid64: int | None
    report_html: str
    report_png_base64: list[str]
    warnings: list[str] = []


class MatchResponse(BaseModel):
    match_id: int
    report_html: str


class HealthResponse(BaseModel):
    status: str
    version: str


def _parse_error(msg: str) -> HTTPException:
    return HTTPException(status_code=400, detail=msg or "Не удалось разобрать id")


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(status="ok", version="1.0.0")


@app.post("/api/v1/analyze", response_model=AnalyzeResponse)
async def analyze(req: AnalyzeRequest) -> AnalyzeResponse:
    """Анализ профиля игрока: активность, винрейт, герои, подозрительность."""
    pid: ParsedPlayerId
    steam_r = SteamClient(api_key=SETTINGS.steam_api_key, timeout_s=SETTINGS.http_timeout_s) if SETTINGS.steam_api_key else None
    try:
        pid = await parse_player_id_resolved(req.player.strip(), steam_r)
    except ValueError as e:
        raise _parse_error(str(e)) from e
    finally:
        if steam_r is not None:
            await steam_r.aclose()

    try:
        result = await analyze_player(pid)
    except Exception as e:
        logger.exception("analyze failed for account_id=%s", pid.account_id)
        raise HTTPException(status_code=502, detail=f"Ошибка при анализе: {type(e).__name__}: {str(e)[:180]}") from e

    pngs = [base64.b64encode(p).decode("ascii") for p in result.card_pngs]
    warnings: list[str] = []
    if not pngs:
        warnings.append("PNG-карточка не сгенерирована (нет фоновых ассетов или ошибка рендера)")

    return AnalyzeResponse(
        account_id=pid.account_id,
        steamid64=pid.steamid64,
        report_html=result.html,
        report_png_base64=pngs,
        warnings=warnings,
    )


@app.post("/api/v1/analyze/{player}", response_model=AnalyzeResponse)
async def analyze_by_path(player: str) -> AnalyzeResponse:
    """То же, что /api/v1/analyze, но id/ссылка в пути."""
    return await analyze(AnalyzeRequest(player=player))


@app.get("/api/v1/match/{match_id}", response_model=MatchResponse)
async def match_info(match_id: int) -> MatchResponse:
    """Сводка по матчу."""
    try:
        report = await build_match_info_report(match_id)
    except Exception as e:
        logger.exception("match lookup failed for match_id=%s", match_id)
        raise HTTPException(status_code=502, detail=f"Ошибка при запросе матча: {type(e).__name__}: {str(e)[:180]}") from e
    return MatchResponse(match_id=match_id, report_html=report)


@app.get("/api/v1/match", response_model=MatchResponse)
async def match_info_query(match: str = Query(..., description="match_id или ссылка на матч")) -> MatchResponse:
    try:
        match_id = parse_match_id(match)
    except ValueError as e:
        raise _parse_error(str(e)) from e
    return await match_info(match_id)


@app.get("/")
async def root() -> dict[str, Any]:
    return {"name": "Dota Profile Analyzer API", "docs": "/docs", "health": "/health"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("api:app", host="127.0.0.1", port=8000, reload=True)
