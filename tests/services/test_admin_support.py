"""Общие мелочи админских сервисов: шаблоны, форматирование, нарезка, безопасная рассылка."""

import logging
from datetime import UTC, datetime
from typing import Any

import pytest

from meetup_bot.services.admin_support import (
    MESSAGE_CHUNK,
    chunk_lines,
    default_render_text,
    notify_promotions,
    resolve,
    resolve_any,
    send_dms,
)
from meetup_bot.services.safe import safe_schedule, safe_send_dm, safe_send_service
from meetup_bot.services.timing import ScheduleSettings
from meetup_bot.services.waitlist import Promotion
from meetup_bot.texts.render import format_moment, text_variables, tidy
from tests.helpers import RecordingNotifier, RecordingRefresher

# Суббота 1 ноября 17:00 в Монтевидео
START = datetime(2026, 11, 1, 20, 0, tzinfo=UTC)


# ---- тексты по умолчанию (дата передаётся моментом, форматирует рендер) ----------------


def test_перенос_подставляет_новое_время_и_место() -> None:
    assert default_render_text("event_moved", {"starts_at": START, "place": "Кафе"}) == (
        "Встреча перенесена: теперь воскресенье, 1 ноября, 17:00, Кафе."
    )


def test_перенос_без_места_не_оставляет_запятую() -> None:
    assert default_render_text("event_moved", {"starts_at": START}) == (
        "Встреча перенесена: теперь воскресенье, 1 ноября, 17:00."
    )


def test_отмена_без_причины_не_добавляет_хвост() -> None:
    assert default_render_text("event_cancelled", {"starts_at": START, "reason": ""}) == (
        "Встреча 1 ноября отменена."
    )


def test_отмена_без_ключа_причины_тоже_работает() -> None:
    assert default_render_text("event_cancelled", {"starts_at": START}) == (
        "Встреча 1 ноября отменена."
    )


def test_отмена_с_причиной() -> None:
    text = default_render_text("event_cancelled", {"starts_at": START, "reason": "дождь"})
    assert text == "Встреча 1 ноября отменена. Причина: дождь"


def test_отмена_экранирует_html_в_причине() -> None:
    text = default_render_text("event_cancelled", {"starts_at": START, "reason": "<b>&"})
    assert text.endswith("Причина: &lt;b&gt;&amp;")


def test_продвижение_из_очереди() -> None:
    assert default_render_text("promoted", {"starts_at": START}) == (
        "Освободилось место — вы записаны на воскресенье, 1 ноября, 17:00!"
    )


def test_неизвестный_шаблон_это_KeyError() -> None:
    with pytest.raises(KeyError):
        default_render_text("нет_такого", {})


def test_подстановки_из_сырых_параметров() -> None:
    values = text_variables(
        {"starts_at": START, "deadline_at": START, "place": "a&b", "position": 3}, "UTC"
    )
    assert values == {
        "weekday": "воскресенье",
        "date": "1 ноября",
        "time": "20:00",
        "deadline": "1 ноября, 20:00",
        "place": "a&b",
        "position": "3",
    }


@pytest.mark.parametrize(
    ("raw", "clean"),
    [
        ("Встреча 17:00, .", "Встреча 17:00."),
        ("a, , b", "a, b"),
        ("слово  ,  ещё", "слово, ещё"),
        ("хвост   ", "хвост"),
        ("строка 1 \nстрока 2", "строка 1\nстрока 2"),
    ],
)
def test_tidy_убирает_следы_пустых_подстановок(raw: str, clean: str) -> None:
    assert tidy(raw) == clean


async def test_продвинутым_пишем_только_если_разрешили_личку() -> None:
    notifier = RecordingNotifier()
    promotions = [
        Promotion(1, 10, False, START, "Кафе", dm_available=True),
        Promotion(2, 11, False, START, "Кафе", dm_available=False),
        Promotion(3, None, False, START, "Кафе", dm_available=True),
    ]
    await notify_promotions(notifier, default_render_text, promotions)
    assert [uid for uid, _ in notifier.dms] == [10]


# ---- format_moment ---------------------------------------------------------------------


def test_format_moment_переводит_в_локальную_зону() -> None:
    moment = datetime(2026, 10, 31, 20, 0, tzinfo=UTC)
    assert format_moment(moment, "America/Montevideo") == "суббота, 31 октября, 17:00"


def test_format_moment_граница_суток_уходит_на_предыдущий_день() -> None:
    moment = datetime(2026, 11, 1, 1, 30, tzinfo=UTC)
    assert format_moment(moment, "America/Montevideo") == "суббота, 31 октября, 22:30"


def test_format_moment_в_UTC_без_сдвига() -> None:
    assert (
        format_moment(datetime(2026, 1, 5, 9, 5, tzinfo=UTC), "UTC")
        == "понедельник, 5 января, 09:05"
    )


# ---- resolve / resolve_any ------------------------------------------------------------


def test_resolve_значение_возвращается_как_есть() -> None:
    settings = ScheduleSettings(timezone="UTC")
    assert resolve(settings) is settings


def test_resolve_вызывает_провайдер_при_каждом_обращении() -> None:
    calls: list[int] = []

    def provider() -> int:
        calls.append(1)
        return len(calls)

    assert (resolve(provider), resolve(provider)) == (1, 2)


async def test_resolve_any_синхронный_провайдер() -> None:
    assert await resolve_any(lambda: 5) == 5


async def test_resolve_any_асинхронный_провайдер() -> None:
    async def provider() -> int:
        return 7

    assert await resolve_any(provider) == 7


# ---- chunk_lines ----------------------------------------------------------------------


def test_chunk_lines_пустой_ввод_даёт_пустой_список() -> None:
    assert chunk_lines([]) == []


def test_chunk_lines_короткий_ввод_одним_сообщением() -> None:
    assert chunk_lines(["a", "b", "c"]) == ["a\nb\nc"]


def test_chunk_lines_режет_по_лимиту_и_не_рвёт_строки() -> None:
    lines = [f"строка {n:02d}" for n in range(30)]
    chunks = chunk_lines(lines, limit=50)
    assert len(chunks) > 1
    assert all(len(chunk) <= 50 for chunk in chunks)
    assert "\n".join(chunks).split("\n") == lines


def test_chunk_lines_порядок_строк_сохраняется() -> None:
    lines = [str(n) for n in range(1000)]
    chunks = chunk_lines(lines, limit=100)
    assert [line for chunk in chunks for line in chunk.split("\n")] == lines


def test_chunk_lines_слишком_длинная_строка_идёт_отдельным_сообщением() -> None:
    chunks = chunk_lines(["a", "б" * 100, "c"], limit=50)
    assert chunks == ["a", "б" * 100, "c"]


def test_chunk_lines_лимит_по_умолчанию_с_запасом_до_4096() -> None:
    assert MESSAGE_CHUNK < 4096
    chunks = chunk_lines(["x" * 100] * 200)
    assert all(len(chunk) <= MESSAGE_CHUNK for chunk in chunks)


def test_chunk_lines_принимает_итератор() -> None:
    assert chunk_lines(iter(["a", "b"])) == ["a\nb"]


def test_chunk_lines_граница_лимита() -> None:
    # «ab» + «\n» + «cd» = 5 символов, учёт идёт с завершающим переводом строки (+1 на строку)
    assert chunk_lines(["ab", "cd"], limit=6) == ["ab\ncd"]
    assert chunk_lines(["ab", "cd"], limit=5) == ["ab", "cd"]


# ---- send_dms и безопасные обёртки ----------------------------------------------------


async def test_send_dms_пишет_каждому() -> None:
    notifier = RecordingNotifier()
    await send_dms(notifier, [1, 2, 3], "привет")
    assert notifier.dms == [(1, "привет"), (2, "привет"), (3, "привет")]


async def test_send_dms_пустой_список_ничего_не_шлёт() -> None:
    notifier = RecordingNotifier()
    await send_dms(notifier, [], "привет")
    assert notifier.dms == []


async def test_send_dms_заблокировавший_бота_не_мешает_остальным() -> None:
    notifier = RecordingNotifier()
    notifier.blocked.add(2)
    await send_dms(notifier, [1, 2, 3], "привет")
    assert [uid for uid, _ in notifier.dms] == [1, 3]


class RaisingNotifier(RecordingNotifier):
    async def send_dm(self, user_id: int, text: str) -> bool:
        if user_id == 2:
            raise RuntimeError("сеть")
        return await super().send_dm(user_id, text)

    async def send_service(self, text: str) -> None:
        raise RuntimeError("сеть")


async def test_send_dms_исключение_отправки_не_ломает_рассылку() -> None:
    notifier = RaisingNotifier()
    await send_dms(notifier, [1, 2, 3], "привет")
    assert [uid for uid, _ in notifier.dms] == [1, 3]


async def test_safe_send_dm_возвращает_результат_порта() -> None:
    notifier = RecordingNotifier()
    notifier.blocked.add(9)
    assert await safe_send_dm(notifier, 1, "x") is True
    assert await safe_send_dm(notifier, 9, "x") is False


async def test_safe_send_dm_исключение_даёт_False_и_лог_без_текста(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.ERROR):
        assert await safe_send_dm(RaisingNotifier(), 2, "секретный текст") is False
    assert "user_id=2" in caplog.text
    assert "секретный текст" not in caplog.text


async def test_safe_send_service_глотает_исключение(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.ERROR):
        await safe_send_service(RaisingNotifier(), "секрет")
    assert "служебный чат" in caplog.text
    assert "секрет" not in caplog.text


async def test_safe_send_service_доставляет_текст() -> None:
    notifier = RecordingNotifier()
    await safe_send_service(notifier, "привет")
    assert notifier.service == ["привет"]


def test_safe_schedule_передаёт_id_встречи() -> None:
    refresher = RecordingRefresher()
    safe_schedule(refresher, 5)
    assert refresher.scheduled == [5]


def test_safe_schedule_глотает_исключение(caplog: pytest.LogCaptureFixture) -> None:
    class Broken:
        def schedule(self, event_id: int) -> None:
            raise RuntimeError("очередь")

    broken: Any = Broken()
    with caplog.at_level(logging.ERROR):
        safe_schedule(broken, 5)
    assert "event_id=5" in caplog.text
