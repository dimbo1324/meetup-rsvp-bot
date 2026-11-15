"""Дымовая проверка стека из SPEC.md (2.1) до начала фаз: зависимости ставятся и работают вместе.

Живой сайт и внешние сервисы не трогаем (SPEC.md, 6.4).
"""

from __future__ import annotations

import importlib
import sys
from importlib.metadata import version
from pathlib import Path

import pytest

STACK = (
    "aiohttp",
    "aiogram",
    "aiogram_dialog",
    "sqlalchemy",
    "aiosqlite",
    "alembic",
    "pydantic",
    "pydantic_settings",
    "sentry_sdk",
    "keyring",
    "hypothesis",
    "trustme",
)


@pytest.mark.parametrize("module", STACK)
def test_зависимость_импортируется(module: str) -> None:
    importlib.import_module(module)


def test_python_314() -> None:
    assert sys.version_info[:2] == (3, 14)


def test_версии_не_ниже_тз() -> None:
    def parts(raw: str) -> tuple[int, ...]:
        return tuple(int(p) for p in raw.split(".")[:3] if p.isdigit())

    minimum = {"aiohttp": "3.14.5", "aiogram": "3.31.0", "sqlalchemy": "2.1.6"}
    for package, floor in minimum.items():
        assert parts(version(package)) >= parts(floor), package


def test_часовой_пояс_монтевидео_доступен() -> None:
    from zoneinfo import ZoneInfo

    assert ZoneInfo("America/Montevideo").key == "America/Montevideo"


async def test_aiosqlite_в_режиме_wal(tmp_path: Path) -> None:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'probe.sqlite3'}")
    try:
        async with engine.connect() as conn:
            mode = (await conn.execute(text("PRAGMA journal_mode=WAL"))).scalar_one()
        assert mode == "wal"
    finally:
        await engine.dispose()


async def test_фальшивый_сервер_aiohttp_отвечает_клиенту() -> None:
    # Основа интеграционных тестов SPEC.md 7: локальный сервер вместо живого сайта
    import aiohttp
    from aiohttp import web
    from aiohttp.test_utils import TestServer

    async def handler(_: web.Request) -> web.Response:
        return web.json_response({"cupos": []})

    app = web.Application()
    app.router.add_get("/agenda", handler)
    async with (
        TestServer(app) as server,
        aiohttp.ClientSession() as session,
        session.get(server.make_url("/agenda")) as response,
    ):
        assert response.status == 200
        assert await response.json() == {"cupos": []}
