"""Рабочие команды: login, check, discover, run, simulate (SPEC 4.5, 8.2–8.3)."""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Callable, Coroutine
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest
from playwright.async_api import Error as PlaywrightError
from tests.fakes import FakeAuthenticator, FakeClock, FakeSource, slots, slots_at
from tests.unit.cli.conftest import journal_checks, journal_events

from rcbot import cli
from rcbot.application.ports import SessionCookie
from rcbot.application.session_manager import SessionManager
from rcbot.bootstrap import Core
from rcbot.cli import operate
from rcbot.config.paths import Paths
from rcbot.config.secrets import SecretKey, SecretStore
from rcbot.domain.errors import (
    ManualStepRequired,
    ReloginDeferred,
    SessionExpired,
    SiteUnavailable,
)
from rcbot.domain.models import Channel, DeliveryResult, SourceKind
from rcbot.infrastructure.gubuy.discovery import DiscoveryResult
from rcbot.infrastructure.gubuy.pages import PageKind
from rcbot.infrastructure.logging.redaction import MASK, REDACTOR
from rcbot.infrastructure.win.single_instance import SingleInstance

COOKIE_VALUE = "syn-cookie-value-Qw3rTy"
COOKIES = [SessionCookie("simple_bpm_session", COOKIE_VALUE, "bpmgob.mec.gub.uy")]


# --- login ---


class FakeLoginAuth:
    def __init__(self, error: BaseException | None = None) -> None:
        self.error = error
        self.logins = 0

    async def login(self) -> list[SessionCookie]:
        self.logins += 1
        if self.error is not None:
            raise self.error
        return list(COOKIES)


def patch_login(
    monkeypatch: pytest.MonkeyPatch, auth: FakeLoginAuth, probe: PageKind | None = None
) -> list[tuple[str, list[SessionCookie]]]:
    probes: list[tuple[str, list[SessionCookie]]] = []

    async def fake_probe(url: str, cookies: list[SessionCookie], options: object) -> PageKind:
        probes.append((url, list(cookies)))
        assert probe is not None
        return probe

    monkeypatch.setattr(operate, "build_authenticator", lambda core: auth)
    monkeypatch.setattr(operate, "probe_agenda", fake_probe)
    return probes


def test_login_без_check_только_входит_и_не_показывает_значения_cookies(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    probes = patch_login(monkeypatch, FakeLoginAuth())
    assert cli.main(["login"]) == 0
    out = capsys.readouterr().out
    assert "1 cookies сессии" in out
    assert COOKIE_VALUE not in out
    assert probes == []


def test_login_ошибка_входа_даёт_код_1_и_сообщение_в_stderr(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    patch_login(monkeypatch, FakeLoginAuth(SiteUnavailable("вход не завершился")), PageKind.AGENDA)
    assert cli.main(["login", "--check"]) == 1
    captured = capsys.readouterr()
    assert "Вход не удался: вход не завершился" in captured.err
    assert "Вход выполнен" not in captured.out


@pytest.mark.parametrize(
    ("kind", "code"),
    [
        (PageKind.AGENDA, 0),
        (PageKind.STEP1, 1),
        (PageKind.LOGIN, 1),
        (PageKind.BLOCKED, 1),
        (PageKind.UNKNOWN, 1),
    ],
)
def test_login_check_сообщение_и_код_для_каждого_вида_страницы(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    paths: Paths,
    kind: PageKind,
    code: int,
) -> None:
    probes = patch_login(monkeypatch, FakeLoginAuth(), kind)

    assert cli.main(["login", "--check"]) == code

    out = capsys.readouterr().out
    assert operate.PROBE_MESSAGES[kind][1] in out
    assert COOKIE_VALUE not in out
    assert len(probes) == 1
    assert probes[0][0].startswith("https://bpmgob.mec.gub.uy/")
    assert probes[0][1] == COOKIES


def test_сообщения_для_всех_видов_страниц_заданы_и_успех_только_у_agenda() -> None:
    assert set(operate.PROBE_MESSAGES) == set(PageKind)
    assert [k for k, (ok, _) in operate.PROBE_MESSAGES.items() if ok] == [PageKind.AGENDA]
    # Шаг 1 бот не решает (D3): сообщение называет кнопку «Готово»
    assert "«Готово»" in operate.PROBE_MESSAGES[PageKind.STEP1][1]


def test_login_без_секретов_даёт_код_2_до_запуска_браузера(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Настоящий PlaywrightAuthenticator: секреты читаются до браузера, значит Chromium не нужен."""
    assert cli.main(["login"]) == 2
    assert "rcbot init" in capsys.readouterr().err


# --- check ---


class FakeApi:
    """Источник API: настроен он или нет, определяется адресом из .env."""

    def __init__(self, configured: bool, source: FakeSource | None = None) -> None:
        self.configured = configured
        self.closed = 0
        self._source = source

    def __getattr__(self, name: str) -> Any:
        if self._source is None:
            raise AttributeError(name)
        return getattr(self._source, name)

    async def close(self) -> None:
        self.closed += 1


class CheckEnv:
    def __init__(
        self, monkeypatch: pytest.MonkeyPatch, *, dom: FakeSource, api: FakeApi | None = None
    ) -> None:
        self.clock = FakeClock()
        self.auth = FakeAuthenticator()
        self.dom = dom
        self.api = api or FakeApi(configured=False)
        self.session: SessionManager | None = None

        def build_session(core: Core) -> SessionManager:
            self.session = SessionManager(self.auth, self.clock, 6)
            return self.session

        monkeypatch.setattr(operate, "build_session", build_session)
        monkeypatch.setattr(operate, "build_api_source", lambda core: self.api)
        monkeypatch.setattr(operate, "build_dom_source", lambda core: dom)


def test_check_печатает_состояние_по_месяцам_и_пишет_запись_в_журнал(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    found = slots(23, 24, month=2) | slots(3, month=3) | slots_at((5, 9, 30), month=4)
    env = CheckEnv(monkeypatch, dom=FakeSource([found], latency_ms=42.0))

    assert cli.main(["check", "--once"]) == 0

    out = capsys.readouterr().out.splitlines()
    assert out[0] == "Источник: dom, проверка заняла 42 мс."
    assert out[1:] == [
        "02.2027: 2027-02-23, 2027-02-24",
        "03.2027: 2027-03-03",
        "04.2027: 2027-04-05 09:30",
    ]
    assert env.auth.logins == 1
    [record] = journal_checks(tmp_path / "journal")
    assert record["ok"] is True
    assert record["source"] == "dom"
    assert record["slots_count"] == 4
    assert record["state"] == "check-once"
    # Одноразовая проверка освобождает браузер/сессию и закрывает ненужный API-источник
    assert env.dom.closed == 1
    assert env.api.closed == 1


def test_check_без_мест_так_и_говорит(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    CheckEnv(monkeypatch, dom=FakeSource([frozenset()]))
    assert cli.main(["check"]) == 0
    assert "Свободных мест нет." in capsys.readouterr().out


def test_check_по_умолчанию_не_требует_once(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    CheckEnv(monkeypatch, dom=FakeSource([slots(1)]))
    assert cli.main(["check"]) == 0


def test_check_при_истёкшей_сессии_входит_заново_ровно_один_раз(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    dom = FakeSource([SessionExpired("страница входа"), slots(23)])
    env = CheckEnv(monkeypatch, dom=dom)

    assert cli.main(["check", "--once"]) == 0

    assert env.auth.logins == 2
    assert len(dom.calls) == 2
    assert "Источник: dom" in capsys.readouterr().out
    assert len(dom.cookie_history) == 2  # источник получил cookies обоих входов


def test_check_повторное_истечение_сессии_не_зацикливается(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    dom = FakeSource([SessionExpired("раз"), SessionExpired("два")], repeat_last=False)
    env = CheckEnv(monkeypatch, dom=dom)

    assert cli.main(["check", "--once"]) == 1

    assert env.auth.logins == 2
    assert len(dom.calls) == 2
    assert "Проверка не удалась: SessionExpired: два" in capsys.readouterr().err
    [record] = journal_checks(tmp_path / "journal")
    assert record["ok"] is False
    assert record["error_kind"] == "SessionExpired"
    assert dom.closed == 1


def test_check_шаг_1_просит_пройти_его_вручную(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    dom = FakeSource([ManualStepRequired("контрольный вопрос")])
    CheckEnv(monkeypatch, dom=dom)

    assert cli.main(["check", "--once"]) == 1

    err = capsys.readouterr().err
    assert "Сайт требует шаг 1" in err
    assert "Проверка не удалась" not in err
    [record] = journal_checks(tmp_path / "journal")
    assert record["error_kind"] == "ManualStepRequired"
    assert dom.closed == 1


def test_check_ошибка_бота_даёт_код_1_и_запись_об_ошибке(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    CheckEnv(monkeypatch, dom=FakeSource([SiteUnavailable("таймаут")]))
    assert cli.main(["check", "--once"]) == 1
    assert "Проверка не удалась: SiteUnavailable: таймаут" in capsys.readouterr().err
    [record] = journal_checks(tmp_path / "journal")
    assert record["ok"] is False
    assert record["slots_count"] == 0
    assert record["error"] == "таймаут"


def test_check_ошибка_входа_закрывает_источник_и_даёт_код_1(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    dom = FakeSource([slots(1)])
    env = CheckEnv(monkeypatch, dom=dom)
    env.auth = FakeAuthenticator(failures=[SiteUnavailable("вход")])

    assert cli.main(["check", "--once"]) == 1

    assert dom.calls == []
    assert dom.closed == 1


def test_check_слишком_частый_вход_сообщается_а_не_падает(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    env = CheckEnv(monkeypatch, dom=FakeSource([slots(1)]))
    env.auth = FakeAuthenticator(failures=[ReloginDeferred("после тревоги")])
    assert cli.main(["check"]) == 1
    assert "ReloginDeferred" in capsys.readouterr().err


def test_check_с_настроенным_api_идёт_через_api_и_запись_помечена_api(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """Запись об ошибке не должна приписывать проверку DomSource, если работал ApiSource."""
    api_source = FakeSource([SiteUnavailable("503")], kind=SourceKind.API)
    dom = FakeSource([slots(1)])
    env = CheckEnv(monkeypatch, dom=dom, api=FakeApi(True, api_source))

    assert cli.main(["check", "--once"]) == 1

    assert dom.calls == []
    assert env.api.closed == 1  # настроенный API закрывается в конце, а не сразу
    [record] = journal_checks(tmp_path / "journal")
    assert record["source"] == "api"


def test_check_с_настроенным_api_успех_берёт_источник_из_снимка(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    api_source = FakeSource([slots(1)], kind=SourceKind.API)
    CheckEnv(monkeypatch, dom=FakeSource([]), api=FakeApi(True, api_source))
    assert cli.main(["check"]) == 0
    assert "Источник: api" in capsys.readouterr().out
    [record] = journal_checks(tmp_path / "journal")
    assert record["source"] == "api"


def test_check_пишет_событие_команды_в_журнал(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    CheckEnv(monkeypatch, dom=FakeSource([slots(1)]))
    cli.main(["check"])
    assert "cli.check" in [e["code"] for e in journal_events(tmp_path / "journal")]


# --- discover ---


class FakeRecorder:
    instances: ClassVar[list[FakeRecorder]] = []
    outcome: DiscoveryResult | BaseException

    def __init__(self, *args: Any) -> None:
        self.args = args
        FakeRecorder.instances.append(self)

    async def run(self, at: object) -> DiscoveryResult:
        if isinstance(FakeRecorder.outcome, BaseException):
            raise FakeRecorder.outcome
        return FakeRecorder.outcome


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch, paths: Paths) -> type[FakeRecorder]:
    FakeRecorder.instances = []
    FakeRecorder.outcome = DiscoveryResult(
        paths.discovery / "discovery-test.json",
        [
            {"method": "GET", "status": 200, "resource": "xhr", "url": "https://x/api/ok"},
            {"method": "POST", "status": 201, "resource": "fetch", "url": "https://x/socket.io/"},
        ],
    )
    monkeypatch.setattr(operate, "DiscoveryRecorder", FakeRecorder)
    return FakeRecorder


def test_discover_печатает_итог_и_сводку(
    recorder: type[FakeRecorder], capsys: pytest.CaptureFixture[str], paths: Paths
) -> None:
    assert cli.main(["discover"]) == 0
    out = capsys.readouterr().out
    assert "Записано запросов: 2" in out
    assert str(paths.discovery / "discovery-test.json") in out
    assert "GET  200 xhr    https://x/api/ok" in out
    assert "Замечен socket.io" in out
    assert "очищен от cookies и личных данных; хранится 7 дней" in out


def test_discover_открывает_видимое_окно_и_пишет_в_каталог_разведки(
    recorder: type[FakeRecorder], paths: Paths, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RCBOT_BROWSER", "msedge")  # разведка идёт в выбранном браузере (R8)
    cli.main(["discover"])
    login_url, _agenda_url, location, months, out_dir, options = recorder.instances[0].args
    assert login_url.startswith("https://bpmgob.mec.gub.uy/autenticacion/login")
    assert location == "Oficina Tramite Extranjeros"
    assert months == 6
    assert out_dir == paths.discovery
    assert options.headless is False  # владелец сам входит в аккаунт в окне
    assert options.channel == "msedge"


def test_discover_регистрирует_номер_документа_в_фильтре_очистки(
    recorder: type[FakeRecorder],
) -> None:
    SecretStore().set(SecretKey.DOC_NUMBER, "SYN-DOC-998877")
    assert REDACTOR.redact("номер SYN-DOC-998877") == "номер SYN-DOC-998877"
    cli.main(["discover"])
    assert REDACTOR.redact("номер SYN-DOC-998877") == f"номер {MASK}"


@pytest.mark.parametrize(
    "error",
    [SiteUnavailable("нет сети"), PlaywrightError("браузер закрыт владельцем")],
    ids=["ошибка-бота", "ошибка-playwright"],
)
def test_discover_сбой_даёт_код_1_и_понятное_сообщение(
    recorder: type[FakeRecorder], capsys: pytest.CaptureFixture[str], error: BaseException
) -> None:
    recorder.outcome = error
    assert cli.main(["discover"]) == 1
    captured = capsys.readouterr()
    assert f"Разведка не завершилась: {type(error).__name__}" in captured.err
    assert "Записано запросов" not in captured.out


# --- run и simulate ---


class SpyAwake:
    """Подмена KeepAwake: помнит, был ли включён во время работы."""

    events: ClassVar[list[str]] = []

    def __enter__(self) -> SpyAwake:
        SpyAwake.events.append("вкл")
        return self

    def __exit__(self, *exc: object) -> None:
        SpyAwake.events.append("выкл")


class RunSpy:
    def __init__(self, monkeypatch: pytest.MonkeyPatch, paths: Paths) -> None:
        self.calls: list[dict[str, Any]] = []
        self.result = 0
        self.error: BaseException | None = None
        self.lock_held_during_run: bool | None = None
        self.loop_factories: list[object] = []
        SpyAwake.events = []
        spy = self

        async def fake_run_app(core: Core, **kwargs: Any) -> int:
            SpyAwake.events.append("работа")
            probe = SingleInstance(paths.lock_file)
            try:
                probe.acquire()
                spy.lock_held_during_run = False
                probe.release()
            except Exception:
                spy.lock_held_during_run = True
            spy.calls.append({"core": core, **kwargs})
            if spy.error is not None:
                raise spy.error
            return spy.result

        real_run = asyncio.run

        def recording_run(main: Coroutine[Any, Any, int], **kwargs: Any) -> int:
            spy.loop_factories.append(kwargs.get("loop_factory"))
            return real_run(main, loop_factory=None)

        monkeypatch.setattr(operate, "run_app", fake_run_app)
        monkeypatch.setattr(operate, "KeepAwake", SpyAwake)
        monkeypatch.setattr(asyncio, "run", recording_run)


@pytest.fixture
def run_spy(monkeypatch: pytest.MonkeyPatch, paths: Paths) -> RunSpy:
    return RunSpy(monkeypatch, paths)


def test_run_держит_замок_и_защиту_от_сна_только_на_время_работы(
    run_spy: RunSpy, paths: Paths
) -> None:
    run_spy.result = 0
    assert cli.main(["run"]) == 0
    assert SpyAwake.events == ["вкл", "работа", "выкл"]
    assert run_spy.lock_held_during_run is True
    assert run_spy.calls[0].keys() == {"core"}  # run без режима simulate
    # После выхода замок свободен
    again = SingleInstance(paths.lock_file)
    again.acquire()
    again.release()


def test_run_при_занятом_замке_даёт_код_3_и_ничего_не_запускает(
    run_spy: RunSpy, paths: Paths, capsys: pytest.CaptureFixture[str]
) -> None:
    holder = SingleInstance(paths.lock_file)
    holder.acquire()
    try:
        assert cli.main(["run"]) == 3
    finally:
        holder.release()
    assert "Остановите работающий экземпляр" in capsys.readouterr().err
    assert run_spy.calls == []
    assert SpyAwake.events == []


def test_замок_и_защита_от_сна_снимаются_при_ошибке_работы(run_spy: RunSpy, paths: Paths) -> None:
    run_spy.error = RuntimeError("сбой")
    assert cli.main(["run"]) == 1
    assert SpyAwake.events == ["вкл", "работа", "выкл"]
    again = SingleInstance(paths.lock_file)
    again.acquire()
    again.release()


def test_замок_и_защита_от_сна_снимаются_при_ctrl_c(run_spy: RunSpy, paths: Paths) -> None:
    run_spy.error = KeyboardInterrupt()
    assert cli.main(["run"]) == 130
    assert SpyAwake.events == ["вкл", "работа", "выкл"]
    again = SingleInstance(paths.lock_file)
    again.acquire()
    again.release()


def test_run_без_консоли_пишет_только_в_файл(
    run_spy: RunSpy, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[bool] = []
    real = operate.build_core

    def spy_build(*args: Any, **kwargs: Any) -> Core:
        seen.append(kwargs["console"])
        return real(*args, **kwargs)

    monkeypatch.setattr(operate, "build_core", spy_build)
    monkeypatch.setattr(sys, "stderr", None)
    cli.main(["run"])
    assert seen == [False]


def test_simulate_передаёт_ожидание_и_печать_доставок_и_код_работы(
    run_spy: RunSpy, capsys: pytest.CaptureFixture[str]
) -> None:
    run_spy.result = 1
    assert cli.main(["simulate", "--wait", "30"]) == 1
    call = run_spy.calls[0]
    assert call["simulate_wait_sec"] == 30
    ok = DeliveryResult(Channel.NTFY, True, 120.4, 1, "", None)
    bad = DeliveryResult(Channel.CALL, False, 8.0, 2, "нет прав", None)
    call["on_delivery"](ok)
    call["on_delivery"](bad)
    out = capsys.readouterr().out.splitlines()
    assert out == ["✅ ntfy, попытка 1: 120 мс", "❌ call, попытка 2: 8 мс — нет прав"]


def test_simulate_при_занятом_замке_даёт_код_3(run_spy: RunSpy, paths: Paths) -> None:
    holder = SingleInstance(paths.lock_file)
    holder.acquire()
    try:
        assert cli.main(["simulate"]) == 3
    finally:
        holder.release()
    assert run_spy.calls == []


def test_simulate_по_умолчанию_ждёт_десять_минут(run_spy: RunSpy) -> None:
    cli.main(["simulate"])
    assert run_spy.calls[0]["simulate_wait_sec"] == 600


# --- цикл событий ---


def test_на_windows_цикл_событий_стандартный(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    assert operate._loop_factory() is None


def test_на_macos_uvloop_не_используется(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    assert operate._loop_factory() is None


def test_на_linux_берётся_uvloop(monkeypatch: pytest.MonkeyPatch) -> None:
    sentinel: Callable[[], asyncio.AbstractEventLoop] = lambda: asyncio.new_event_loop()  # noqa: E731
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(
        operate.importlib, "import_module", lambda name: SimpleNamespace(new_event_loop=sentinel)
    )
    assert operate._loop_factory() is sentinel


def test_на_linux_без_uvloop_запасной_стандартный_цикл(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(name: str) -> None:
        raise ImportError(name)

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(operate.importlib, "import_module", missing)
    assert operate._loop_factory() is None


def test_run_передаёт_фабрику_цикла_в_asyncio_run(
    run_spy: RunSpy, monkeypatch: pytest.MonkeyPatch
) -> None:
    sentinel = object()
    monkeypatch.setattr(operate, "_loop_factory", lambda: sentinel)
    cli.main(["run"])
    cli.main(["simulate"])
    assert run_spy.loop_factories == [sentinel, sentinel]


def test_namespace_команд_не_ломается_без_атрибутов() -> None:
    """cmd_login читает `args.check`: парсер обязан его задавать всегда."""
    parsed = cli._parser().parse_args(["login"])
    assert isinstance(parsed, argparse.Namespace)
    assert parsed.check is False
