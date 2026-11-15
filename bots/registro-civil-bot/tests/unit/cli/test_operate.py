"""Рабочие команды CLI: check, run, simulate (источник — страница SAE, решения R12, R13)."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Callable, Coroutine
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest
from tests.fakes import FakeSource, slots, slots_at
from tests.unit.cli.conftest import journal_checks, journal_events

from rcbot import cli
from rcbot.bootstrap import Core
from rcbot.cli import operate
from rcbot.config.paths import Paths
from rcbot.domain.errors import SiteUnavailable
from rcbot.domain.models import AvailabilitySnapshot, Channel, DeliveryResult
from rcbot.infrastructure.gubuy.sae_source import SaeSource
from rcbot.infrastructure.win.single_instance import SingleInstance

# --- check ---


def use_source(monkeypatch: pytest.MonkeyPatch, source: FakeSource) -> FakeSource:
    """`check` собирает источник через `build_source`: тест подставляет свой по сценарию."""
    monkeypatch.setattr(operate, "build_source", lambda core: source)
    return source


def test_check_печатает_состояние_по_месяцам_и_пишет_запись_в_журнал(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    found = slots(23, 24, month=2) | slots(3, month=3) | slots_at((5, 9, 30), month=4)
    source = use_source(monkeypatch, FakeSource([found], latency_ms=42.0))

    assert cli.main(["check", "--once"]) == 0

    out = capsys.readouterr().out.splitlines()
    assert out[0] == "Источник: sae, проверка заняла 42 мс."
    assert out[1:] == [
        "02.2027: 2027-02-23, 2027-02-24",
        "03.2027: 2027-03-03",
        "04.2027: 2027-04-05 09:30",
    ]
    [record] = journal_checks(tmp_path / "journal")
    assert record["ok"] is True
    assert record["source"] == "sae"
    assert record["slots_count"] == 4
    assert record["state"] == "check-once"
    assert source.closed == 1  # одноразовая проверка закрывает соединение


def test_check_без_мест_так_и_говорит(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    source = use_source(monkeypatch, FakeSource([frozenset()]))
    assert cli.main(["check"]) == 0
    assert "Свободных мест нет." in capsys.readouterr().out
    assert len(source.calls) == 1 and source.closed == 1


def test_check_по_умолчанию_не_требует_once(monkeypatch: pytest.MonkeyPatch) -> None:
    use_source(monkeypatch, FakeSource([slots(1)]))
    assert cli.main(["check"]) == 0


def test_check_места_без_дат_так_и_говорит(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class OpenSae(FakeSource):
        async def fetch(self, target: Any) -> AvailabilitySnapshot:
            shot = await super().fetch(target)
            return replace(shot, dates_unknown=True, fingerprint="")

    use_source(monkeypatch, OpenSae([frozenset()]))
    assert cli.main(["check"]) == 0
    assert "Места появились" in capsys.readouterr().out


def test_check_ошибка_бота_даёт_код_1_и_запись_об_ошибке(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    source = use_source(monkeypatch, FakeSource([SiteUnavailable("таймаут")]))
    assert cli.main(["check", "--once"]) == 1
    assert "Проверка не удалась: SiteUnavailable: таймаут" in capsys.readouterr().err
    [record] = journal_checks(tmp_path / "journal")
    assert record["ok"] is False
    assert record["source"] == "sae"
    assert record["slots_count"] == 0
    assert record["error"] == "таймаут"
    assert source.closed == 1


def test_check_с_настоящим_источником_не_требует_секретов_и_входа(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Настоящий `build_source`: секретов нет, а проверка доходит до страницы SAE, не до входа."""
    seen: list[str] = []

    async def fake_fetch(self: Any, target: Any) -> AvailabilitySnapshot:
        seen.append(self._url)
        raise SiteUnavailable("сеть в тестах выключена")

    monkeypatch.setattr(SaeSource, "fetch", fake_fetch)
    assert cli.main(["check"]) == 1
    assert seen and seen[0].startswith("https://sae.mec.gub.uy/")
    assert "rcbot init" not in capsys.readouterr().err


def test_check_пишет_событие_команды_в_журнал(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    use_source(monkeypatch, FakeSource([slots(1)]))
    cli.main(["check"])
    assert "cli.check" in [e["code"] for e in journal_events(tmp_path / "journal")]


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


def test_check_отказывает_кодом_3_пока_бот_работает(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], paths: Paths
) -> None:
    """Вторая проверка рядом с работающим ботом удвоила бы запросы к сайту (COL-6)."""
    source = use_source(monkeypatch, FakeSource([slots(1)]))
    holder = SingleInstance(paths.lock_file)
    holder.acquire()
    try:
        assert cli.main(["check"]) == cli.EXIT_RUNNING
    finally:
        holder.release()
    assert "Остановите работающий экземпляр" in capsys.readouterr().err
    assert source.calls == []


def test_check_держит_замок_и_отпускает_его(monkeypatch: pytest.MonkeyPatch, paths: Paths) -> None:
    held: list[bool] = []

    class ProbingSource(FakeSource):
        async def fetch(self, target: Any) -> AvailabilitySnapshot:
            lock = SingleInstance(paths.lock_file)
            try:
                lock.acquire()
            except Exception:
                held.append(True)
            else:
                held.append(False)
                lock.release()
            return await super().fetch(target)

    use_source(monkeypatch, ProbingSource([slots(1)]))
    cli.main(["check"])
    assert held == [True]
    again = SingleInstance(paths.lock_file)
    again.acquire()
    again.release()


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
