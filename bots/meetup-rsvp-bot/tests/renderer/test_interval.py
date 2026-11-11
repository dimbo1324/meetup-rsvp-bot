"""Интервал встречи «16:30–18:30»: единый форматтер для поста, inline-выдачи и текстов."""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from meetup_bot.db.models import EventStatus
from meetup_bot.renderer.post import (
    CAPTION_LIMIT,
    TEXT_LIMIT,
    Participant,
    PostData,
    render_post,
    visible_length,
)
from meetup_bot.texts.render import (
    TIME_RANGE_DASH,
    end_time_label,
    format_interval,
    text_variables,
)

MONTEVIDEO = ZoneInfo("America/Montevideo")
# Пятница, 30 октября 2026, 16:30 в Монтевидео (UTC-3)
STARTS = datetime(2026, 10, 30, 19, 30, tzinfo=UTC)
ENDS = STARTS + timedelta(hours=2)
# 23:00 пятницы — конец в 01:30 уже субботы
LATE_STARTS = datetime(2026, 10, 31, 2, 0, tzinfo=UTC)
LATE_ENDS = LATE_STARTS + timedelta(hours=2, minutes=30)


def make(**overrides: object) -> PostData:
    base: dict[str, object] = {
        "event_id": 7,
        "status": EventStatus.OPEN.value,
        "starts_at": STARTS,
        "ends_at": ENDS,
        "timezone": "America/Montevideo",
        "capacity": 10,
        "seats_taken": 2,
        "participants": [Participant("Света")],
        "place": "Кафе",
    }
    base.update(overrides)
    return PostData(**base)  # type: ignore[arg-type]


def test_тире_интервала_короткое() -> None:
    assert TIME_RANGE_DASH == "–"


def test_интервал_в_один_день() -> None:
    assert format_interval(STARTS, ENDS, MONTEVIDEO) == "пятница, 30 октября, 16:30–18:30"


def test_интервал_принимает_имя_пояса() -> None:
    assert format_interval(STARTS, ENDS, "America/Montevideo") == "пятница, 30 октября, 16:30–18:30"


def test_без_конца_только_начало() -> None:
    assert format_interval(STARTS, None, MONTEVIDEO) == "пятница, 30 октября, 16:30"


def test_конец_равный_началу_не_даёт_интервала() -> None:
    assert format_interval(STARTS, STARTS, MONTEVIDEO) == "пятница, 30 октября, 16:30"


def test_конец_раньше_начала_не_даёт_интервала() -> None:
    assert format_interval(STARTS, STARTS - timedelta(hours=1), MONTEVIDEO) == (
        "пятница, 30 октября, 16:30"
    )


def test_встреча_через_полночь_называет_дату_конца() -> None:
    assert format_interval(LATE_STARTS, LATE_ENDS, MONTEVIDEO) == (
        "пятница, 30 октября, 23:00–01:30 (31 октября)"
    )


def test_конец_считается_в_поясе_клуба() -> None:
    # 23:30 UTC пятницы — это ещё 20:30 пятницы в Монтевидео: дата конца не нужна
    assert end_time_label(STARTS, datetime(2026, 10, 30, 23, 30, tzinfo=UTC), MONTEVIDEO) == (
        "20:30"
    )


def test_конец_в_полночь_относится_к_следующему_дню() -> None:
    midnight = datetime(2026, 10, 31, 3, 0, tzinfo=UTC)
    assert end_time_label(STARTS, midnight, MONTEVIDEO) == "00:00 (31 октября)"


def test_подстановка_end_time() -> None:
    values = text_variables({"starts_at": STARTS, "ends_at": ENDS}, MONTEVIDEO)
    assert (values["weekday"], values["date"], values["time"]) == (
        "пятница",
        "30 октября",
        "16:30",
    )
    assert values["end_time"] == "18:30"


def test_подстановка_end_time_через_полночь() -> None:
    values = text_variables({"starts_at": LATE_STARTS, "ends_at": LATE_ENDS}, MONTEVIDEO)
    assert values["time"] == "23:00"
    assert values["end_time"] == "01:30 (31 октября)"


def test_без_конца_подстановки_end_time_нет() -> None:
    assert "end_time" not in text_variables({"starts_at": STARTS}, MONTEVIDEO)
    assert "end_time" not in text_variables({"starts_at": STARTS, "ends_at": None}, MONTEVIDEO)


def test_конец_без_начала_подстановки_не_даёт() -> None:
    # Без начала нельзя понять, на другой ли день приходится конец
    assert "end_time" not in text_variables({"ends_at": ENDS}, MONTEVIDEO)


def test_пост_показывает_интервал() -> None:
    assert "📅 пятница, 30 октября, 16:30–18:30\n" in render_post(make()).text


def test_пост_без_конца_как_раньше() -> None:
    text = render_post(make(ends_at=None)).text
    assert "📅 пятница, 30 октября, 16:30\n" in text
    assert TIME_RANGE_DASH not in text


def test_пост_через_полночь() -> None:
    text = render_post(make(starts_at=LATE_STARTS, ends_at=LATE_ENDS)).text
    assert "📅 пятница, 30 октября, 23:00–01:30 (31 октября)" in text


def test_хеш_меняется_со_сменой_конца() -> None:
    later = render_post(make(ends_at=ENDS + timedelta(minutes=30)))
    assert render_post(make()).content_hash != later.content_hash
    assert render_post(make()).content_hash != render_post(make(ends_at=None)).content_hash


def _fill_to(limit: int, has_photo: bool, extra: int) -> PostData:
    """Описание длиной ровно в остаток лимита плюс `extra` символов."""
    skeleton = render_post(make(has_photo=has_photo, description_html="")).text
    free = limit - visible_length(skeleton) - 2  # «\n\n» перед описанием
    return make(has_photo=has_photo, description_html="я" * (free + extra))


def test_подпись_с_интервалом_ровно_на_лимите_не_режется() -> None:
    post = render_post(_fill_to(CAPTION_LIMIT, True, 0))
    assert visible_length(post.text) == CAPTION_LIMIT
    assert "…" not in post.text
    assert "16:30–18:30" in post.text


def test_подпись_с_интервалом_на_символ_длиннее_обрезается() -> None:
    post = render_post(_fill_to(CAPTION_LIMIT, True, 1))
    assert visible_length(post.text) <= CAPTION_LIMIT
    assert "…" in post.text
    assert "16:30–18:30" in post.text


def test_текст_с_интервалом_ровно_на_лимите_не_режется() -> None:
    post = render_post(_fill_to(TEXT_LIMIT, False, 0))
    assert visible_length(post.text) == TEXT_LIMIT
    assert "…" not in post.text


def test_текст_с_интервалом_длиннее_лимита_обрезается() -> None:
    post = render_post(_fill_to(TEXT_LIMIT, False, 1))
    assert visible_length(post.text) <= TEXT_LIMIT
    assert "16:30–18:30" in post.text
