"""Выбор браузера (R8): Chrome → Edge → Chromium Playwright, явный выбор без запасных вариантов."""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from playwright.async_api import Error as PlaywrightError
from tests.unit.cli.conftest import write_env

from rcbot import bootstrap
from rcbot.bootstrap import build_core
from rcbot.config.paths import Paths
from rcbot.config.settings import Settings
from rcbot.infrastructure.gubuy import browser
from rcbot.infrastructure.gubuy.browser import (
    AUTO_ORDER,
    BrowserOptions,
    channel_ladder,
    installed_path,
    launch,
    survey,
)

SECRET_PATH = r"C:\Users\someone-private\AppData\Local\Google\Chrome\Application\chrome.exe"


class FakePlaywright:
    """`pw.chromium.launch`: ошибка для каналов из `missing`, остальным — «браузер»."""

    def __init__(self, missing: set[str], *, boom: bool = False) -> None:
        self.missing = missing
        self.boom = boom
        self.calls: list[dict[str, Any]] = []
        self.chromium = SimpleNamespace(launch=self._launch)

    async def _launch(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        channel = kwargs.get("channel", "chromium")
        if channel in self.missing or self.boom:
            raise PlaywrightError(
                f"Chromium distribution '{channel}' is not found at {SECRET_PATH}"
            )
        return f"браузер:{channel}"


def make_install(root: Path, *parts: str) -> None:
    path = root.joinpath(*parts)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("exe")


def test_лестница_auto_и_явный_выбор() -> None:
    assert channel_ladder("auto") == ("chrome", "msedge", "chromium") == AUTO_ORDER
    assert channel_ladder("chrome") == ("chrome",)
    assert channel_ladder("msedge") == ("msedge",)
    assert channel_ladder("chromium") == ("chromium",)


def test_по_умолчанию_параметры_браузера_это_встроенный_chromium() -> None:
    # Тесты и код, не знающий про выбор, не должны случайно запускать установленный Chrome
    assert BrowserOptions().channel == "chromium"


def test_installed_path_находит_chrome_и_edge_в_типичных_папках(tmp_path: Path) -> None:
    make_install(tmp_path / "pf", "Google", "Chrome", "Application", "chrome.exe")
    make_install(tmp_path / "pf86", "Microsoft", "Edge", "Application", "msedge.exe")
    env = {"PROGRAMFILES": str(tmp_path / "pf"), "PROGRAMFILES(X86)": str(tmp_path / "pf86")}
    assert installed_path("chrome", env) == tmp_path / "pf/Google/Chrome/Application/chrome.exe"
    assert installed_path("msedge", env) == tmp_path / "pf86/Microsoft/Edge/Application/msedge.exe"


def test_installed_path_видит_установку_для_пользователя_в_localappdata(tmp_path: Path) -> None:
    make_install(tmp_path, "Google", "Chrome", "Application", "chrome.exe")
    assert installed_path("chrome", {"LOCALAPPDATA": str(tmp_path)}) is not None


def test_installed_path_не_находит_то_чего_нет(tmp_path: Path) -> None:
    assert installed_path("chrome", {"PROGRAMFILES": str(tmp_path)}) is None
    assert installed_path("chrome", {}) is None
    assert (
        installed_path("chromium", {"PROGRAMFILES": str(tmp_path)}) is None
    )  # встроенный — не тут
    assert installed_path("firefox", {"PROGRAMFILES": str(tmp_path)}) is None


def test_survey_auto_предпочитает_chrome_потом_edge_потом_встроенный(tmp_path: Path) -> None:
    bundled = tmp_path / "chromium.exe"
    make_install(tmp_path, "Google", "Chrome", "Application", "chrome.exe")
    make_install(tmp_path, "Microsoft", "Edge", "Application", "msedge.exe")
    env = {"PROGRAMFILES": str(tmp_path)}
    assert survey("auto", bundled, env).chosen == "chrome"
    (tmp_path / "Google/Chrome/Application/chrome.exe").unlink()
    assert survey("auto", bundled, env).chosen == "msedge"
    (tmp_path / "Microsoft/Edge/Application/msedge.exe").unlink()
    assert survey("auto", bundled, env).chosen == "chromium"
    assert survey("auto", None, env).chosen is None


def test_survey_явный_выбор_не_откатывается_на_другой_браузер(tmp_path: Path) -> None:
    make_install(tmp_path, "Microsoft", "Edge", "Application", "msedge.exe")
    env = {"PROGRAMFILES": str(tmp_path)}
    assert survey("chrome", tmp_path / "chromium.exe", env).chosen is None
    assert survey("msedge", None, env).chosen == "msedge"
    assert survey("chromium", None, env).chosen is None


async def test_launch_берёт_chrome_если_он_есть() -> None:
    pw = FakePlaywright(missing=set())
    options = BrowserOptions(headless=False, channel="auto")
    assert await launch(pw, options) == "браузер:chrome"  # type: ignore[arg-type]
    assert pw.calls == [{"headless": False, "channel": "chrome"}]


async def test_launch_без_chrome_берёт_edge() -> None:
    pw = FakePlaywright(missing={"chrome"})
    assert await launch(pw, BrowserOptions(channel="auto")) == "браузер:msedge"  # type: ignore[arg-type]
    assert [c.get("channel") for c in pw.calls] == ["chrome", "msedge"]


async def test_launch_без_chrome_и_edge_берёт_встроенный_chromium_без_канала() -> None:
    pw = FakePlaywright(missing={"chrome", "msedge"})
    assert await launch(pw, BrowserOptions(channel="auto")) == "браузер:chromium"  # type: ignore[arg-type]
    assert pw.calls[-1] == {"headless": True}  # у встроенного Chromium канала нет


async def test_launch_явный_chrome_не_откатывается_и_пробрасывает_ошибку() -> None:
    pw = FakePlaywright(missing={"chrome"})
    with pytest.raises(PlaywrightError):
        await launch(pw, BrowserOptions(channel="chrome"))  # type: ignore[arg-type]
    assert [c.get("channel") for c in pw.calls] == ["chrome"]


async def test_launch_явный_edge_и_явный_chromium() -> None:
    pw = FakePlaywright(missing=set())
    assert await launch(pw, BrowserOptions(channel="msedge")) == "браузер:msedge"  # type: ignore[arg-type]
    assert await launch(pw, BrowserOptions(channel="chromium")) == "браузер:chromium"  # type: ignore[arg-type]


async def test_launch_если_нет_ни_одного_последняя_ошибка_и_подсказка_в_журнале(
    caplog: pytest.LogCaptureFixture,
) -> None:
    pw = FakePlaywright(missing=set(), boom=True)
    with caplog.at_level(logging.INFO, logger=browser.logger.name), pytest.raises(PlaywrightError):
        await launch(pw, BrowserOptions(channel="auto"))  # type: ignore[arg-type]
    assert [c.get("channel") for c in pw.calls] == ["chrome", "msedge", None]
    assert "установите Google Chrome" in caplog.text


async def test_launch_не_пишет_в_журнал_путь_из_текста_ошибки(
    caplog: pytest.LogCaptureFixture,
) -> None:
    pw = FakePlaywright(missing={"chrome"})
    with caplog.at_level(logging.DEBUG, logger=browser.logger.name):
        await launch(pw, BrowserOptions(channel="auto"))  # type: ignore[arg-type]
    assert "someone-private" not in caplog.text
    assert "Microsoft Edge" in caplog.text  # какой браузер взят — виден


def test_настройка_browser_по_умолчанию_auto_и_ограничена_списком(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert Settings().browser == "auto"
    monkeypatch.setenv("RCBOT_BROWSER", "msedge")
    assert Settings().browser == "msedge"
    monkeypatch.setenv("RCBOT_BROWSER", "firefox")
    with pytest.raises(ValueError):
        Settings()


def test_параметры_браузера_берут_выбор_из_настройки(paths: Paths) -> None:
    write_env(paths, browser="chrome")
    core = build_core(console=False)
    assert bootstrap.browser_options(core).channel == "chrome"
    assert bootstrap.browser_options(core, channel="msedge").channel == "msedge"
