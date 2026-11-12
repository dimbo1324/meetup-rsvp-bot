"""`runner.run_app`: порядок запуска и остановки, отказоустойчивость, simulate (SPEC 2.3, 3.4)."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest
from tests.fakes import slots
from tests.integration.process.harness import CHAT_ID, TOKEN, Rig, build_rig

from rcbot import runner
from rcbot.application.alerting import RepeatPolicy
from rcbot.application.poll_loop import PollLoop
from rcbot.bot import texts
from rcbot.config.secrets import SecretKey, SecretMissing, SecretStore
from rcbot.domain.errors import SessionExpired
from rcbot.domain.models import AlertKind, Channel, SourceKind
from rcbot.infrastructure.notify.callmebot import CallMeBotNotifier
from rcbot.infrastructure.notify.ntfy import NtfyNotifier
from rcbot.infrastructure.notify.telegram import TelegramNotifier

STOPPED = texts.STOPPED
EXPECTED_STARTS = [
    "start:telegram",
    "start:warmup",
    "journal.quota",  # квота до запуска цикла: сначала место на диске, потом работа
    "start:housekeeping",
    "start:journal-quota",
    "start:reports",
    "start:watchdog",
    "start:poll",
    "start:liveness",
    "journal:bot.start",
]


@contextlib.asynccontextmanager
async def running(rig: Rig, **kwargs: Any) -> AsyncIterator[asyncio.Task[int]]:
    task = asyncio.create_task(runner.run_app(rig.core, **kwargs))
    rig.task = task
    try:
        yield task
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def is_subsequence(wanted: list[str], order: list[str]) -> bool:
    it = iter(order)
    return all(item in it for item in wanted)


def positions(order: list[str], *items: str) -> list[int]:
    return [order.index(item) for item in items]


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch) -> Rig:
    return build_rig(monkeypatch.setattr)


# --- запуск ---


async def test_задачи_запускаются_в_документированном_порядке_квота_до_опроса(rig: Rig) -> None:
    async with running(rig):
        await rig.started()
        await rig.until(lambda: rig.dom.calls != [], "первая проверка")

        order = rig.order
        starts = [item for item in order if item.startswith("start:") or item == "journal.quota"]
        assert starts[: len(EXPECTED_STARTS) - 1] == EXPECTED_STARTS[:-1]
        assert is_subsequence(EXPECTED_STARTS, order)
        quota, poll_start, first_fetch = positions(
            order, "journal.quota", "start:poll", "poll.first-fetch"
        )
        assert quota < poll_start < first_fetch
        assert rig.journal.quota_calls >= 1


async def test_о_запуске_пишется_событие_и_приходит_сообщение_только_в_telegram(rig: Rig) -> None:
    async with running(rig):
        await rig.started()

        [(code, message, level, _)] = [e for e in rig.journal.events if e[0] == "bot.start"]
        assert level == "INFO"
        assert message == "бот запущен: источник dom, интервал ≈ 60 с"
        started = texts.STARTED.format(source="DOM", interval=60)
        assert rig.sent_texts(Channel.TELEGRAM) == [started]
        # служебные сообщения идут только в Telegram: в ntfy и звонок — только даты и места
        assert rig.notifiers[Channel.NTFY].sent == []
        assert code == "bot.start"


async def test_telegram_получает_токен_и_только_владельца_из_секретов(rig: Rig) -> None:
    async with running(rig):
        await rig.started()
        assert [bot.token for bot in rig.bots] == [TOKEN]
        [(controller, tz, owner)] = rig.dispatcher_args
        assert owner == int(CHAT_ID)  # фильтр middleware строится по chat_id владельца
        assert str(tz) == "America/Montevideo"
        assert controller is not None


async def test_цикл_опроса_работает_и_при_истекшей_сессии_входит_заново(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig = build_rig(monkeypatch.setattr, script=[SessionExpired("нет входа"), frozenset()])
    async with running(rig):
        await rig.started()
        await rig.until(lambda: rig.auth.logins == 1 and len(rig.dom.calls) >= 2, "вход и опрос")
        # cookies нового входа переданы источнику: подписка источника на сессию собрана
        assert rig.dom.cookie_history == [rig.auth.cookies]
        assert rig.dom.calls[0].name == "registro-civil"
        assert rig.dom.calls[0].id > 0  # цель сохранена в базе, а не разовая


async def test_появление_мест_доходит_до_всех_каналов_через_шину(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig = build_rig(monkeypatch.setattr, script=[slots(23, 24)])
    async with running(rig):
        await rig.started()
        await rig.until(
            lambda: all(
                any(a.kind is not AlertKind.SERVICE for a, _ in n.sent)
                for n in rig.notifiers.values()
            ),
            "тревога во всех каналах",
        )
        for notifier in rig.notifiers.values():
            alert = next(a for a, _ in notifier.sent if a.kind is not AlertKind.SERVICE)
            assert alert.kind is AlertKind.APPEARED
            assert "23.02.2027" in alert.text
        assert "slots.appeared" in rig.journal.codes()


async def test_монитор_живости_пишет_состояние_с_данными_инцидента_и_входов(rig: Rig) -> None:
    async with running(rig):
        await rig.started()
        await rig.until(lambda: rig.journal.statuses != [], "status.json")
        status = rig.journal.statuses[0]
        assert status["incident_open"] is False
        assert status["incident_days"] == []
        assert status["alert_repeating"] is False
        assert status["hung"] is False
        assert "logins_last_hour" in status
        assert "alive_at" in status


async def test_источник_выбирается_dom_если_api_не_настроен_и_всё_закрывается(rig: Rig) -> None:
    async with running(rig):
        await rig.started()
        assert rig.dom.kind is SourceKind.DOM
    assert rig.dom.closed == 1


# --- остановка ---


async def test_остановка_закрывает_ресурсы_в_документированном_порядке(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RCBOT_WATCHDOG_URL", "https://hc.example/ping")
    rig = build_rig(monkeypatch.setattr)
    async with running(rig) as task:
        await rig.started()
        assert [w.url for w in rig.watchdogs] == ["https://hc.example/ping"]
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    expected = [
        f"msg:{STOPPED}",  # «⏹ остановлен» уходит, пока каналы ещё открыты
        "telegram.cancelled",  # фоновые задачи остановлены до закрытия ресурсов
        "alerts.close",  # рассылка
        "bus.drain",  # подписчики событий
        "source.close",  # источник/браузер
        "notifier.close:telegram",  # каналы
        "notifier.close:ntfy",
        "watchdog.close",  # сторож
        "bot.session.close",  # сессия Telegram — последней из внешних
        "storage.close",  # база
        "journal:bot.stop",
    ]
    stop_part = rig.order[rig.order.index(f"msg:{STOPPED}") :]
    assert [item for item in stop_part if item in expected] == expected
    assert rig.sent_texts()[-1] == STOPPED


async def test_отмена_не_проглатывается_run_app_завершается_отменой(rig: Rig) -> None:
    async with running(rig) as task:
        await rig.started()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 10)
    assert task.cancelled()


async def test_после_остановки_не_остаётся_фоновых_задач(rig: Rig) -> None:
    supervised = {"telegram", "warmup", "housekeeping", "journal-quota", "reports", "watchdog"}
    supervised |= {"poll", "liveness"}
    async with running(rig) as task:
        await rig.started()
        assert supervised & rig.tasks()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    await asyncio.sleep(0.05)
    assert supervised & rig.tasks() == set()
    assert rig.dom.closed == 1


async def test_сбой_одного_ресурса_при_закрытии_не_мешает_остальным(rig: Rig) -> None:
    async def broken() -> None:
        raise RuntimeError("канал не закрылся")

    rig.notifiers[Channel.TELEGRAM].close = broken  # type: ignore[method-assign]
    async with running(rig) as task:
        await rig.started()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert is_subsequence(
        ["notifier.close:ntfy", "bot.session.close", "storage.close", "journal:bot.stop"],
        rig.order,
    )


async def test_база_закрывается_даже_если_закрытие_ресурса_прервано_отменой(rig: Rig) -> None:
    async def interrupted() -> None:
        raise asyncio.CancelledError

    rig.notifiers[Channel.NTFY].close = interrupted  # type: ignore[method-assign]
    async with running(rig) as task:
        await rig.started()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert "storage.close" in rig.order
    assert "journal:bot.stop" in rig.order


async def test_зависшее_сообщение_об_остановке_не_задерживает_закрытие_больше_таймаута(
    monkeypatch: pytest.MonkeyPatch, rig: Rig
) -> None:
    monkeypatch.setattr(runner, "STOP_MESSAGE_TIMEOUT_SEC", 0.05)
    hang = asyncio.Event()

    original = rig.notifiers[Channel.TELEGRAM].send

    async def slow_send(alert: Any, attempt: int) -> Any:
        if alert.text == STOPPED:
            await hang.wait()
        return await original(alert, attempt)

    rig.notifiers[Channel.TELEGRAM].send = slow_send  # type: ignore[method-assign]
    async with running(rig) as task:
        await rig.started()
        task.cancel()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 10)
    assert "storage.close" in rig.order


# --- ошибки при запуске ---


async def test_без_секретов_telegram_run_app_требует_их_и_закрывает_базу(rig: Rig) -> None:
    keyring_store = SecretStore()
    keyring_store.delete(SecretKey.TELEGRAM_TOKEN)

    with pytest.raises(SecretMissing, match="rcbot init"):
        await runner.run_app(rig.core)

    assert "storage.close" in rig.order
    assert rig.bots == []
    assert not [item for item in rig.order if item.startswith("start:")]
    assert rig.dom.closed == 0 and rig.dom.calls == []


async def test_без_chat_id_владельца_бот_не_стартует(rig: Rig) -> None:
    SecretStore().delete(SecretKey.TELEGRAM_CHAT_ID)
    with pytest.raises(SecretMissing):
        await runner.run_app(rig.core)
    assert rig.bots == []
    assert "storage.close" in rig.order


async def test_сбой_при_инициализации_закрывает_базу_и_не_шлёт_сообщение_об_остановке(
    monkeypatch: pytest.MonkeyPatch, rig: Rig
) -> None:
    """Планировщик перезапускает упавший бот каждую минуту: без запуска «остановлен» не шлём."""

    def explode(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("сбой сборки")

    monkeypatch.setattr(runner, "build_dispatcher", explode)

    with pytest.raises(RuntimeError, match="сбой сборки"):
        await runner.run_app(rig.core)

    assert "storage.close" in rig.order
    assert rig.sent_texts() == []
    assert not [item for item in rig.order if item.startswith("start:")]
    assert rig.dom.calls == []


# --- падение задач: бот не должен молча жить без опроса или без Telegram ---


async def test_упавший_поллинг_telegram_перезапускается_с_нарастающей_паузой(rig: Rig) -> None:
    rig.polling.script = ["crash", "crash", "forever"]
    async with running(rig):
        await rig.started()
        await rig.until(lambda: rig.polling.starts == 1, "первый запуск поллинга")
        await rig.clock.advance(4.9)
        assert rig.polling.starts == 1  # пауза 5 с: раньше не перезапускается
        await rig.clock.advance(0.2)
        await rig.until(lambda: rig.polling.starts == 2, "второй запуск")
        await rig.clock.advance(9.8)
        assert rig.polling.starts == 2  # вторая пауза вдвое длиннее
        await rig.clock.advance(0.4)
        await rig.until(lambda: rig.polling.starts == 3, "третий запуск")
        await rig.clock.advance(600)
        assert rig.polling.starts == 3  # третий «висит» — больше перезапусков нет


async def test_поллинг_telegram_который_вернул_управление_сам_перезапускается(rig: Rig) -> None:
    """Вернувшийся без ошибки поллинг означал бы бота, который не отвечает владельцу."""
    rig.polling.script = ["return", "forever"]
    async with running(rig):
        await rig.started()
        await rig.until(lambda: rig.polling.starts == 1, "первый запуск поллинга")
        await rig.clock.advance(5.1)
        await rig.until(lambda: rig.polling.starts == 2, "перезапуск после тихого выхода")


async def test_упавший_цикл_опроса_перезапускается_и_опрос_продолжается(
    monkeypatch: pytest.MonkeyPatch, rig: Rig
) -> None:
    starts: list[int] = []

    class Flaky(PollLoop):
        async def run(self) -> None:
            starts.append(1)
            if len(starts) == 1:
                raise RuntimeError("цикл опроса упал")
            await super().run()

    monkeypatch.setattr(runner, "PollLoop", Flaky)
    async with running(rig):
        await rig.started()
        await rig.until(lambda: starts == [1], "первый запуск цикла")
        await asyncio.sleep(0.05)
        assert rig.dom.calls == []  # цикл мёртв, опроса нет — это и нужно починить
        await rig.clock.advance(5.1)
        await rig.until(lambda: len(rig.dom.calls) >= 1, "опрос после перезапуска")
        assert starts == [1, 1]


async def test_зависший_опрос_перезапускается_монитором_живости_вместе_с_источником(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def hang() -> frozenset[Any]:
        await asyncio.Event().wait()
        return frozenset()

    rig = build_rig(monkeypatch.setattr, script=[hang, slots(23)])
    async with running(rig):
        await rig.started()
        await rig.until(lambda: len(rig.dom.calls) == 1, "зависшая проверка")
        assert rig.dom.closed == 0

        await rig.clock.advance(300)  # монитор: проверка идёт дольше HUNG_CHECK_SEC
        await rig.until(lambda: len(rig.dom.calls) >= 2, "перезапущенная проверка")

        assert rig.dom.closed >= 1  # браузер закрыт: зависшая проверка прервана
        assert any(m.startswith("⚠️ Цикл опроса завис") for m in rig.sent_texts())
        assert "liveness.hung" in rig.journal.codes()


# --- simulate ---


async def test_simulate_успех_после_вижу_код_0_и_повторы_остановлены(
    rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    deliveries: list[Any] = []
    async with running(rig, simulate_wait_sec=30, on_delivery=deliveries.append) as task:
        await rig.until(lambda: rig.alerts is not None and rig.alerts.repeating(), "повторы")
        assert deliveries  # владелец видит результат по каждому каналу
        assert {d.channel for d in deliveries} == {Channel.TELEGRAM, Channel.NTFY}

        assert await rig.alerts.ack() is True  # type: ignore[union-attr]
        await rig.clock.advance(1)
        assert await asyncio.wait_for(task, 10) == 0

    out = capsys.readouterr().out
    assert "Тестовая тревога отправлена" in out
    assert "«Вижу» получено" in out
    assert rig.alerts is not None and not rig.alerts.repeating()
    assert is_subsequence(["alerts.close", "source.close", "storage.close"], rig.order)


async def test_simulate_без_вижу_по_таймауту_код_1_и_повторы_останавливаются(
    rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    async with running(rig, simulate_wait_sec=3) as task:
        await rig.until(lambda: rig.alerts is not None and rig.alerts.repeating(), "повторы")
        for _ in range(4):
            await rig.clock.advance(1)
        assert await asyncio.wait_for(task, 10) == 1

    assert "«Вижу» не нажата: повторы остановлены по таймауту." in capsys.readouterr().out
    assert rig.alerts is not None and not rig.alerts.repeating()
    assert "storage.close" in rig.order


async def test_simulate_повторы_идут_пока_ждёт_владельца(rig: Rig) -> None:
    async with running(rig, simulate_wait_sec=500):
        await rig.until(lambda: rig.alerts is not None and rig.alerts.repeating(), "повторы")
        tg = rig.notifiers[Channel.TELEGRAM]
        first = len(tg.sent)
        await rig.clock.advance(61)  # tg_repeat_sec = 60
        assert len(tg.sent) > first
        assert tg.attempts[-1] >= 2


async def test_simulate_не_запускает_опрос_отчёты_и_сообщение_о_запуске(rig: Rig) -> None:
    async with running(rig, simulate_wait_sec=1) as task:
        await rig.clock.advance(0)
        await rig.until(lambda: rig.alerts is not None and rig.alerts.repeating(), "повторы")
        await rig.clock.advance(2)
        await asyncio.wait_for(task, 10)

    starts = [item for item in rig.order if item.startswith("start:")]
    assert starts == ["start:telegram", "start:warmup"]
    assert "journal.quota" not in rig.order
    assert "journal:bot.start" not in rig.order
    assert rig.dom.calls == []
    assert not any(t.startswith("▶️") for t in rig.sent_texts())


# --- сборка каналов и политик ---


def make_core_with(monkeypatch: pytest.MonkeyPatch, **env: str) -> Any:
    from rcbot.bootstrap import build_core

    for name, value in env.items():
        monkeypatch.setenv(f"RCBOT_{name.upper()}", value)
    return build_core(console=False)


def test_каналы_без_секретов_только_telegram(monkeypatch: pytest.MonkeyPatch) -> None:
    core = make_core_with(monkeypatch)
    notifiers = runner._notifiers(core, SimpleNamespace(), 777)  # type: ignore[arg-type]
    assert list(notifiers) == [Channel.TELEGRAM]
    assert isinstance(notifiers[Channel.TELEGRAM], TelegramNotifier)
    assert notifiers[Channel.TELEGRAM]._chat_id == 777  # type: ignore[attr-defined]


def test_канал_ntfy_создаётся_только_с_темой(monkeypatch: pytest.MonkeyPatch) -> None:
    core = make_core_with(monkeypatch, ntfy_server="https://ntfy.example/")
    SecretStore().set(SecretKey.NTFY_TOPIC, "rcbot-synthetic-topic-0123456789abcdefgh")

    notifiers = runner._notifiers(core, SimpleNamespace(), 1)  # type: ignore[arg-type]

    assert set(notifiers) == {Channel.TELEGRAM, Channel.NTFY}
    ntfy = notifiers[Channel.NTFY]
    assert isinstance(ntfy, NtfyNotifier)
    assert ntfy._topic == "rcbot-synthetic-topic-0123456789abcdefgh"  # type: ignore[attr-defined]
    assert ntfy._server == "https://ntfy.example"  # type: ignore[attr-defined]
    assert "ntfy.example" in ntfy._http._allowed  # type: ignore[attr-defined]


def test_звонок_создаётся_только_с_именем_и_ходит_только_на_callmebot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    core = make_core_with(monkeypatch, call_lang="en-US-Standard-B")
    SecretStore().set(SecretKey.CALLMEBOT_USER, "owner")

    notifiers = runner._notifiers(core, SimpleNamespace(), 1)  # type: ignore[arg-type]

    assert set(notifiers) == {Channel.TELEGRAM, Channel.CALL}
    call = notifiers[Channel.CALL]
    assert isinstance(call, CallMeBotNotifier)
    assert call._user == "@owner"  # type: ignore[attr-defined]
    assert call._lang == "en-US-Standard-B"  # type: ignore[attr-defined]
    assert call._http._allowed == frozenset({"api.callmebot.com"})  # type: ignore[attr-defined]


def test_все_три_канала_при_всех_секретах(monkeypatch: pytest.MonkeyPatch) -> None:
    core = make_core_with(monkeypatch)
    store = SecretStore()
    store.set(SecretKey.NTFY_TOPIC, "rcbot-synthetic-topic-0123456789abcdefgh")
    store.set(SecretKey.CALLMEBOT_USER, "@owner")
    notifiers = runner._notifiers(core, SimpleNamespace(), 1)  # type: ignore[arg-type]
    assert set(notifiers) == set(Channel)


def test_политики_повторов_берутся_из_настроек(monkeypatch: pytest.MonkeyPatch) -> None:
    core = make_core_with(
        monkeypatch,
        tg_repeat_sec="45",
        tg_repeat_max="7",
        ntfy_repeat_sec="90",
        ntfy_repeat_max="3",
        call_repeat_sec="200",
        call_repeat_max="2",
    )
    assert runner._policies(core) == {
        Channel.TELEGRAM: RepeatPolicy(45, 7),
        Channel.NTFY: RepeatPolicy(90, 3),
        Channel.CALL: RepeatPolicy(200, 2),
    }


def test_политики_по_умолчанию_соответствуют_spec(monkeypatch: pytest.MonkeyPatch) -> None:
    policies = runner._policies(make_core_with(monkeypatch))
    assert policies[Channel.TELEGRAM] == RepeatPolicy(60, 30)
    assert policies[Channel.NTFY] == RepeatPolicy(120, 10)
    assert policies[Channel.CALL] == RepeatPolicy(180, 5)
