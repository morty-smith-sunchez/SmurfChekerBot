from __future__ import annotations

from base64 import b64decode
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

import api
from api import app
from rendering.schemas import AnalyzeResult


@pytest.fixture(autouse=True)
def fake_steam_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """Не ходим в реальный Steam API: parse_player_id_resolved может создать SteamClient."""

    class FakeSteamClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def aclose(self) -> None:
            return None

        async def resolve_vanity_url(self, vanity: str) -> int | None:
            return None

    monkeypatch.setattr(api, "SteamClient", FakeSteamClient)


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


# --- /health и / ---


def test_health_ok(client: TestClient) -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["version"] == "1.0.0"


def test_root_ok(client: TestClient) -> None:
    resp = client.get("/")
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "Dota Profile Analyzer API"
    assert body["docs"] == "/docs"
    assert body["health"] == "/health"


# --- /api/v1/analyze ---


async def _fake_analyze_player(pid: object) -> AnalyzeResult:
    account_id = getattr(pid, "account_id")
    return AnalyzeResult(
        html=f"<b>Отчёт</b> для account_id={account_id}",
        card_pngs=(b"\x89PNG\r\n fake",),
    )


def test_analyze_post_ok(client: TestClient) -> None:
    with patch.object(api, "analyze_player", new=_fake_analyze_player):
        resp = client.post("/api/v1/analyze", json={"player": "123456789"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["account_id"] == 123456789
    assert body["steamid64"] == 76561198083722517
    assert "account_id=123456789" in body["report_html"]
    assert len(body["report_png_base64"]) == 1
    assert b64decode(body["report_png_base64"][0]) == b"\x89PNG\r\n fake"
    assert body["warnings"] == []


def test_analyze_by_path_ok(client: TestClient) -> None:
    with patch.object(api, "analyze_player", new=_fake_analyze_player):
        resp = client.post("/api/v1/analyze/76561198083722517")
    assert resp.status_code == 200
    body = resp.json()
    assert body["account_id"] == 123456789
    assert body["steamid64"] == 76561198083722517


def test_analyze_invalid_player_400(client: TestClient) -> None:
    with patch.object(api, "analyze_player", new=_fake_analyze_player):
        resp = client.post("/api/v1/analyze", json={"player": "no digits here"})
    assert resp.status_code == 400
    assert "no digits" in resp.json()["detail"]


def test_analyze_missing_field_422(client: TestClient) -> None:
    resp = client.post("/api/v1/analyze", json={})
    assert resp.status_code == 422


def test_analyze_upstream_error_502(client: TestClient) -> None:
    async def _boom(pid: object) -> AnalyzeResult:
        raise RuntimeError("opendota timeout")

    with patch.object(api, "analyze_player", new=_boom):
        resp = client.post("/api/v1/analyze", json={"player": "123456789"})
    assert resp.status_code == 502
    assert "opendota timeout" in resp.json()["detail"]


# --- /api/v1/match ---


async def _fake_build_match_report(match_id: int) -> str:
    return f"<b>Матч {match_id}</b> — сводка"


def test_match_by_path_ok(client: TestClient) -> None:
    with patch.object(api, "build_match_info_report", new=_fake_build_match_report):
        resp = client.get("/api/v1/match/7890123456")
    assert resp.status_code == 200
    body = resp.json()
    assert body["match_id"] == 7890123456
    assert "Матч 7890123456" in body["report_html"]


def test_match_by_query_ok(client: TestClient) -> None:
    with patch.object(api, "build_match_info_report", new=_fake_build_match_report):
        resp = client.get("/api/v1/match", params={"match": "https://www.dotabuff.com/matches/7890123456"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["match_id"] == 7890123456


def test_match_invalid_query_400(client: TestClient) -> None:
    resp = client.get("/api/v1/match", params={"match": "abc"})
    assert resp.status_code == 400


def test_match_invalid_path_422(client: TestClient) -> None:
    resp = client.get("/api/v1/match/not-a-number")
    assert resp.status_code == 422


def test_match_upstream_error_502(client: TestClient) -> None:
    async def _boom(match_id: int) -> str:
        raise RuntimeError("match not found")

    with patch.object(api, "build_match_info_report", new=_boom):
        resp = client.get("/api/v1/match/7890123456")
    assert resp.status_code == 502
    assert "match not found" in resp.json()["detail"]


def test_analyze_player_awaited_successfully(client: TestClient) -> None:
    """Проверяем, что эндпоинт ждёт асинхронный результат (регрессия с утерянным await)."""
    mock = AsyncMock(return_value=AnalyzeResult(html="<b>ok</b>", card_pngs=()))
    with patch.object(api, "analyze_player", new=mock):
        resp = client.post("/api/v1/analyze", json={"player": "123456789"})
    assert resp.status_code == 200
    assert resp.json()["report_html"] == "<b>ok</b>"
    assert mock.await_count == 1
