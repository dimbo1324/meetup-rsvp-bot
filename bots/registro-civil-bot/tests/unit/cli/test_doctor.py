"""`rcbot doctor`: строки OK/НЕТ/--, коды выхода, хосты, расхождение часов (SPEC 4.3, 8.1)."""

from __future__ import annotations

import argparse
import asyncio
import subprocess
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from tests.fakes import LocalTlsServer
from tests.unit.cli.conftest import journal_events, write_env

from rcbot.cli import setup
from rcbot.config.paths import Paths
from rcbot.config.secrets import REQUIRED, SecretKey, SecretStore
from rcbot.config.settings import Settings
from rcbot.infrastructure.gubuy import browser
from rcbot.infrastructure.http.client import SafeHttp

ETAPA_URL = "https://bpmgob.mec.gub.uy/etapas/ejecutar/TEST-ETAPA/1"


def args(*, offline: bool = True, full: bool = False) -> argparse.Namespace:
    return argparse.Namespace(offline=offline, full=full)


def no_installed_browsers(monkeypatch: pytest.MonkeyPatch) -> None:
    """На машине разработчика может стоять настоящий Chrome: тест от него не зависит."""
    monkeypatch.setattr(browser, "installed_path", lambda channel, env=None: None)


@pytest.fixture
def ready(monkeypatch: pytest.MonkeyPatch, paths: Paths) -> None:
    """Исправное окружение: Chromium на месте, адрес заявки и все секреты заданы."""
    monkeypatch.setattr(setup, "_playwright_ready", lambda: (True, "chromium.exe"))
    no_installed_browsers(monkeypatch)
    write_env(paths, agenda_url=ETAPA_URL, tramite_id="TEST-TRAMITE", api_url=ETAPA_URL)
    store = SecretStore()
    for key in REQUIRED:
        store.set(key, f"synthetic-{key.value}")


def lines_of(out: str) -> list[str]:
    return out.splitlines()


def mark_of(out: str, fragment: str) -> str:
    """Метка строки с фрагментом: «OK », «НЕТ» или «-- »."""
    matching = [line for line in lines_of(out) if fragment in line]
    assert len(matching) == 1, f"строка с «{fragment}» должна быть одна: {matching}"
    return matching[0][:3]


@pytest.mark.usefixtures("ready")
def test_исправное_окружение_даёт_код_0(capsys: pytest.CaptureFixture[str]) -> None:
    code = setup.cmd_doctor(args())
    out = capsys.readouterr().out
    assert code == 0
    assert "Всё готово." in out
    assert "НЕТ" not in out
    assert mark_of(out, "Chromium для Playwright") == "OK "
    assert mark_of(out, "Хранилище секретов: MemoryKeyring") == "OK "
    assert mark_of(out, "RCBOT_AGENDA_URL задан") == "OK "
    assert mark_of(out, "RCBOT_TRAMITE_ID задан") == "OK "
    assert mark_of(out, "RCBOT_API_URL") == "OK "
    assert mark_of(out, "Каталог бота вне OneDrive") == "OK "


@pytest.mark.usefixtures("ready")
def test_показывает_версии_пакетов_и_python(capsys: pytest.CaptureFixture[str]) -> None:
    setup.cmd_doctor(args())
    out = capsys.readouterr().out
    for package in ("aiohttp", "aiogram", "aiogram-dialog", "sqlalchemy", "playwright", "keyring"):
        assert mark_of(out, f"{package} ") == "OK "
    assert "Python 3.14" in out


def test_без_секретов_обязательные_нет_необязательные_пропуск_и_код_1(
    monkeypatch: pytest.MonkeyPatch, paths: Paths, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(setup, "_playwright_ready", lambda: (True, "chromium.exe"))
    write_env(paths, agenda_url=ETAPA_URL)

    code = setup.cmd_doctor(args())
    out = capsys.readouterr().out

    assert code == 1
    assert "Есть проблемы" in out
    assert "Всё готово" not in out
    for key in REQUIRED:
        assert mark_of(out, f"секрет: {setup.DESCRIPTIONS[key]}") == "НЕТ"
    # тема ntfy и звонок необязательны: «--», а не «НЕТ»
    assert mark_of(out, f"секрет: {setup.DESCRIPTIONS[SecretKey.NTFY_TOPIC]}") == "-- "
    assert mark_of(out, f"секрет: {setup.DESCRIPTIONS[SecretKey.CALLMEBOT_USER]}") == "-- "


@pytest.mark.parametrize("missing", list(REQUIRED))
def test_нехватка_любого_одного_обязательного_секрета_даёт_код_1(
    monkeypatch: pytest.MonkeyPatch,
    paths: Paths,
    capsys: pytest.CaptureFixture[str],
    ready: None,
    missing: SecretKey,
) -> None:
    assert SecretStore().delete(missing)
    assert setup.cmd_doctor(args()) == 1
    assert mark_of(capsys.readouterr().out, f"секрет: {setup.DESCRIPTIONS[missing]}") == "НЕТ"


@pytest.mark.usefixtures("ready")
def test_необязательные_секреты_не_влияют_на_код(capsys: pytest.CaptureFixture[str]) -> None:
    store = SecretStore()
    store.set(SecretKey.NTFY_TOPIC, "rcbot-synthetic-topic-0123456789abcdef")
    store.set(SecretKey.CALLMEBOT_USER, "@owner")
    code = setup.cmd_doctor(args())
    out = capsys.readouterr().out
    assert code == 0
    assert mark_of(out, f"секрет: {setup.DESCRIPTIONS[SecretKey.NTFY_TOPIC]}") == "OK "
    assert "synthetic-topic" not in out  # значение секрета не печатается
    assert "@owner" not in out


def test_нет_chromium_даёт_код_1_и_подсказку_по_установке(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    ready: None,
) -> None:
    monkeypatch.setattr(setup, "_playwright_ready", lambda: (False, "C:/нет/chrome.exe"))
    code = setup.cmd_doctor(args())
    out = capsys.readouterr().out
    assert code == 1
    assert mark_of(out, "Chromium для Playwright") == "НЕТ"
    assert "dev.py setup --bot registro-civil-bot" in out


def test_достаточно_chrome_встроенный_chromium_не_нужен(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], ready: None
) -> None:
    monkeypatch.setattr(setup, "_playwright_ready", lambda: (False, "C:/нет/chromium.exe"))
    monkeypatch.setattr(
        browser,
        "installed_path",
        lambda channel, env=None: Path("C:/chrome.exe") if channel == "chrome" else None,
    )
    code = setup.cmd_doctor(args())
    out = capsys.readouterr().out
    assert code == 0
    assert mark_of(out, "Google Chrome:") == "OK "
    assert mark_of(out, "Microsoft Edge:") == "-- "
    assert mark_of(out, "Chromium для Playwright") == "-- "
    assert mark_of(out, "Браузер для работы:") == "OK "
    assert "Браузер для работы: Google Chrome (RCBOT_BROWSER=auto)" in out


def test_edge_берётся_когда_chrome_нет(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], ready: None
) -> None:
    monkeypatch.setattr(
        browser,
        "installed_path",
        lambda channel, env=None: Path("C:/edge.exe") if channel == "msedge" else None,
    )
    assert setup.cmd_doctor(args()) == 0
    assert "Браузер для работы: Microsoft Edge" in capsys.readouterr().out


def test_явный_chrome_без_chrome_это_код_1_даже_если_есть_edge(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    ready: None,
    paths: Paths,
) -> None:
    write_env(
        paths, agenda_url=ETAPA_URL, tramite_id="TEST-TRAMITE", api_url=ETAPA_URL, browser="chrome"
    )
    monkeypatch.setattr(
        browser,
        "installed_path",
        lambda channel, env=None: Path("C:/edge.exe") if channel == "msedge" else None,
    )
    code = setup.cmd_doctor(args())
    out = capsys.readouterr().out
    assert code == 1
    assert mark_of(out, "Браузер для работы") == "НЕТ"
    assert "RCBOT_BROWSER=chrome" in out


def test_playwright_ready_сообщает_о_сломанной_установке_без_падения(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import playwright.sync_api

    def broken() -> None:
        raise RuntimeError("сломан")

    monkeypatch.setattr(playwright.sync_api, "sync_playwright", broken)
    ready_, where = setup._playwright_ready()
    assert ready_ is False
    assert "RuntimeError" in where


def test_playwright_ready_проверяет_наличие_файла_браузера(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import playwright.sync_api

    exe = tmp_path / "chrome.exe"

    class FakePlaywright:
        chromium = SimpleNamespace(executable_path=str(exe))

        def __enter__(self) -> FakePlaywright:
            return self

        def __exit__(self, *exc: object) -> None:
            return None

    monkeypatch.setattr(playwright.sync_api, "sync_playwright", FakePlaywright)
    assert setup._playwright_ready() == (False, str(exe))
    exe.write_bytes(b"x")
    assert setup._playwright_ready() == (True, str(exe))


def test_метка_вместо_номера_этапа_даёт_код_1(
    monkeypatch: pytest.MonkeyPatch, paths: Paths, capsys: pytest.CaptureFixture[str], ready: None
) -> None:
    paths.env_file.unlink()  # адрес по умолчанию содержит метку <НОМЕР_ЭТАПА>
    code = setup.cmd_doctor(args())
    out = capsys.readouterr().out
    assert code == 1
    assert mark_of(out, "RCBOT_AGENDA_URL задан") == "НЕТ"
    # без номера заявки и адреса API — только «--»: это не ошибки
    assert mark_of(out, "RCBOT_TRAMITE_ID задан") == "-- "
    assert mark_of(out, "RCBOT_API_URL") == "-- "


def test_каталог_в_onedrive_предупреждает_но_не_роняет_doctor(
    monkeypatch: pytest.MonkeyPatch, bot_home: Path, capsys: pytest.CaptureFixture[str], ready: None
) -> None:
    monkeypatch.setenv("OneDrive", str(bot_home.parent))
    code = setup.cmd_doctor(args())
    out = capsys.readouterr().out
    assert code == 0  # SPEC 2.6: предупреждение и совет, секретов на диске всё равно нет
    assert mark_of(out, "Каталог в OneDrive") == "НЕТ"
    assert "Исключите каталог из синхронизации" in out


@pytest.mark.parametrize("variable", ["OneDrive", "OneDriveConsumer", "OneDriveCommercial"])
def test_все_варианты_переменных_onedrive_распознаются(
    monkeypatch: pytest.MonkeyPatch, bot_home: Path, variable: str
) -> None:
    monkeypatch.setenv(variable, str(bot_home))
    assert setup._onedrive_roots() == [bot_home.resolve()]


def test_onedrive_не_задан_корней_нет(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
        monkeypatch.delenv(name, raising=False)
    assert setup._onedrive_roots() == []


def test_соседняя_папка_с_похожим_именем_не_считается_onedrive(
    monkeypatch: pytest.MonkeyPatch, bot_home: Path, capsys: pytest.CaptureFixture[str], ready: None
) -> None:
    monkeypatch.setenv("OneDrive", str(bot_home) + "-other")
    setup.cmd_doctor(args())
    assert mark_of(capsys.readouterr().out, "Каталог бота вне OneDrive") == "OK "


@pytest.mark.usefixtures("ready")
def test_журнал_виден_с_размером_квотой_и_пишется_событие_команды(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    setup.cmd_doctor(args())
    out = capsys.readouterr().out
    assert mark_of(out, f"Журнал наблюдения: {tmp_path / 'journal'}") == "OK "
    assert "Квота журнала: больше 10 ГБ — очистка старых дней до 5 ГБ" in out
    codes = [e["code"] for e in journal_events(tmp_path / "journal")]
    assert "cli.doctor" in codes


def test_журнал_выключен_строка_без_ошибки(
    paths: Paths, capsys: pytest.CaptureFixture[str], ready: None
) -> None:
    write_env(paths, agenda_url=ETAPA_URL, journal_enabled="false")
    code = setup.cmd_doctor(args())
    out = capsys.readouterr().out
    assert code == 0
    assert mark_of(out, "Журнал наблюдения на рабочем столе") == "-- "


def test_журнал_недоступен_показан_как_нет(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    ready: None,
) -> None:
    blocker = tmp_path / "занято-файлом"
    blocker.write_text("не папка", encoding="utf-8")
    monkeypatch.setenv("RCBOT_JOURNAL_DIR", str(blocker / "журнал"))
    code = setup.cmd_doctor(args())
    out = capsys.readouterr().out
    assert mark_of(out, "Журнал наблюдения на рабочем столе") == "НЕТ"
    assert code == 0  # бот без журнала работает


def test_журнал_в_onedrive_предупреждение(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str], ready: None
) -> None:
    monkeypatch.setenv("OneDrive", str(tmp_path))
    setup.cmd_doctor(args())
    assert mark_of(capsys.readouterr().out, "Журнал лежит в OneDrive") == "-- "


# --- проверка хостов: настоящие сокеты и TLS к локальному серверу ---


def _route_hosts_to(
    monkeypatch: pytest.MonkeyPatch, hosts: set[str], *, trust: LocalTlsServer | None
) -> None:
    monkeypatch.setattr(Settings, "allowed_hosts", lambda self: frozenset(hosts))
    monkeypatch.setattr(
        setup,
        "SafeHttp",
        lambda _hosts, timeout_sec: SafeHttp(
            {"127.0.0.1"},
            timeout_sec,
            ssl_context=trust.client_ssl if trust is not None else None,
        ),
    )


async def _doctor_in_thread(capsys: pytest.CaptureFixture[str], **kwargs: bool) -> tuple[int, str]:
    # cmd_doctor сам запускает цикл событий: рядом с циклом сервера — только в потоке
    code = await asyncio.to_thread(setup.cmd_doctor, args(offline=False, **kwargs))
    return code, capsys.readouterr().out


@pytest.mark.usefixtures("ready")
async def test_доступный_хост_ок_и_запрос_идёт_методом_head(
    monkeypatch: pytest.MonkeyPatch, tls_server: LocalTlsServer, capsys: pytest.CaptureFixture[str]
) -> None:
    tls_server.respond("HEAD", "/", status=200)
    host = f"127.0.0.1:{tls_server.port}"
    _route_hosts_to(monkeypatch, {host}, trust=tls_server)

    code, out = await _doctor_in_thread(capsys)

    assert code == 0
    assert mark_of(out, f"{host}: HTTP 200") == "OK "
    assert [r.method for r in tls_server.requests] == ["HEAD"]
    assert "часы ПК" not in out


@pytest.mark.usefixtures("ready")
async def test_http_ошибка_сайта_всё_равно_значит_что_хост_доступен(
    monkeypatch: pytest.MonkeyPatch, tls_server: LocalTlsServer, capsys: pytest.CaptureFixture[str]
) -> None:
    tls_server.respond("HEAD", "/", status=503)
    host = f"127.0.0.1:{tls_server.port}"
    _route_hosts_to(monkeypatch, {host}, trust=tls_server)
    code, out = await _doctor_in_thread(capsys)
    assert mark_of(out, f"{host}: HTTP 503") == "OK "
    assert code == 0


@pytest.mark.usefixtures("ready")
async def test_недоверенный_сертификат_отклоняется_и_даёт_код_1(
    monkeypatch: pytest.MonkeyPatch, tls_server: LocalTlsServer, capsys: pytest.CaptureFixture[str]
) -> None:
    """Проверка TLS не отключается: без доверия к тестовому центру хост «недоступен»."""
    tls_server.respond("HEAD", "/", status=200)
    host = f"127.0.0.1:{tls_server.port}"
    _route_hosts_to(monkeypatch, {host}, trust=None)

    code, out = await _doctor_in_thread(capsys)

    assert code == 1
    assert mark_of(out, host) == "НЕТ"
    assert "Certificate" in out


@pytest.mark.usefixtures("ready")
async def test_хост_вне_списка_разрешённых_не_опрашивается(
    monkeypatch: pytest.MonkeyPatch, tls_server: LocalTlsServer, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(Settings, "allowed_hosts", lambda self: frozenset({"evil.example"}))
    monkeypatch.setattr(
        setup,
        "SafeHttp",
        lambda _hosts, timeout_sec: SafeHttp({"127.0.0.1"}, timeout_sec),
    )
    code, out = await _doctor_in_thread(capsys)
    assert code == 1
    assert mark_of(out, "evil.example") == "НЕТ"
    assert "HostNotAllowed" in out
    assert tls_server.requests == []


@pytest.mark.usefixtures("ready")
async def test_недоступный_хост_даёт_нет_и_код_1_остальные_проверяются(
    monkeypatch: pytest.MonkeyPatch, tls_server: LocalTlsServer, capsys: pytest.CaptureFixture[str]
) -> None:
    tls_server.respond("HEAD", "/", status=200)
    good = f"127.0.0.1:{tls_server.port}"
    dead = "127.0.0.1:9"  # порт discard: на нём никто не слушает
    _route_hosts_to(monkeypatch, {good, dead}, trust=tls_server)

    code, out = await _doctor_in_thread(capsys)

    assert code == 1
    assert mark_of(out, f"{good}: HTTP 200") == "OK "
    assert mark_of(out, f"{dead}:") == "НЕТ"


def _dated(seconds_ago: float) -> str:
    return format_datetime(datetime.now(UTC) - timedelta(seconds=seconds_ago), usegmt=True)


@pytest.mark.parametrize(
    ("seconds_ago", "warned"),
    [(5, False), (20, False), (-20, False), (45, True), (-45, True), (600, True)],
)
@pytest.mark.usefixtures("ready")
async def test_расхождение_часов_свыше_30_секунд_предупреждает(
    monkeypatch: pytest.MonkeyPatch,
    tls_server: LocalTlsServer,
    capsys: pytest.CaptureFixture[str],
    seconds_ago: float,
    warned: bool,
) -> None:
    tls_server.respond("HEAD", "/", status=200, headers={"Date": _dated(seconds_ago)})
    host = f"127.0.0.1:{tls_server.port}"
    _route_hosts_to(monkeypatch, {host}, trust=tls_server)

    _, out = await _doctor_in_thread(capsys)

    drift_lines = [line for line in lines_of(out) if "часы ПК расходятся" in line]
    assert bool(drift_lines) is warned
    if warned:
        assert drift_lines[0].startswith("НЕТ")
        assert host in drift_lines[0]
        assert ("+" if seconds_ago > 0 else "-") in drift_lines[0]


@pytest.mark.usefixtures("ready")
async def test_без_заголовка_date_или_с_мусором_в_нём_расхождение_не_считается(
    monkeypatch: pytest.MonkeyPatch, tls_server: LocalTlsServer, capsys: pytest.CaptureFixture[str]
) -> None:
    tls_server.respond("HEAD", "/", status=200, headers={"Date": "вчера вечером"})
    host = f"127.0.0.1:{tls_server.port}"
    _route_hosts_to(monkeypatch, {host}, trust=tls_server)
    code, out = await _doctor_in_thread(capsys)
    # Непонятный Date не должен ронять doctor трассировкой и не должен выдавать ложное «НЕТ» на часы
    assert "часы ПК" not in out
    assert code in (0, 1)


# --- --full ---


@pytest.mark.parametrize(("returncode", "mark", "code"), [(0, "OK ", 0), (1, "НЕТ", 1)])
def test_full_запускает_pip_audit_и_учитывает_результат(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    ready: None,
    returncode: int,
    mark: str,
    code: int,
) -> None:
    calls: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: object) -> SimpleNamespace:
        calls.append(command)
        return SimpleNamespace(returncode=returncode)

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert setup.cmd_doctor(args(full=True)) == code
    assert mark_of(capsys.readouterr().out, "pip-audit") == mark
    assert len(calls) == 1
    assert calls[0][1:] == ["-m", "pip_audit", "--skip-editable"]


@pytest.mark.usefixtures("ready")
def test_без_full_pip_audit_не_запускается(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def forbidden(*_: object, **__: object) -> None:
        raise AssertionError("pip-audit без --full")

    monkeypatch.setattr(subprocess, "run", forbidden)
    assert setup.cmd_doctor(args()) == 0


def test_offline_не_создаёт_сетевых_клиентов(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], ready: None
) -> None:
    def forbidden(*_: object, **__: object) -> None:
        raise AssertionError("сеть при --offline")

    monkeypatch.setattr(setup, "SafeHttp", forbidden)
    assert setup.cmd_doctor(args(offline=True)) == 0
