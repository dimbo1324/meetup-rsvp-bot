import re
from datetime import UTC, datetime

from meetup_bot.db.models import EventStatus
from meetup_bot.renderer.post import (
    CAPTION_LIMIT,
    Participant,
    PostData,
    render_post,
    truncate_html,
    visible_length,
)

STARTS = datetime(2026, 10, 24, 20, 0, tzinfo=UTC)


def make(**overrides: object) -> PostData:
    base: dict[str, object] = {
        "event_id": 7,
        "status": EventStatus.OPEN.value,
        "starts_at": STARTS,
        "timezone": "America/Montevideo",
        "capacity": 10,
        "seats_taken": 2,
        "participants": [Participant("Света"), Participant("Александр", guests=1)],
        "description_html": "<b>Встреча</b> сообщества",
        "place": "Кафе",
        "place_url": "https://maps.example/cafe?a=1&b=2",
    }
    base.update(overrides)
    return PostData(**base)  # type: ignore[arg-type]


def test_базовый_пост() -> None:
    post = render_post(make())
    assert "<b>Встреча</b> сообщества" in post.text
    assert "📅 суббота, 24 октября, 17:00" in post.text  # время местное
    assert '📍 <a href="https://maps.example/cafe?a=1&amp;b=2">Кафе</a>' in post.text
    assert "Записались: 2 из 10" in post.text
    assert "1. Света\n2. Александр (+1)" in post.text
    assert [b.callback_data for b in post.keyboard[0]] == ["r:7:j", "r:7:c"]


def test_имя_экранируется() -> None:  # кейс 18
    post = render_post(make(participants=[Participant("<b>Хакер</b> & Ко")], seats_taken=1))
    assert "1. &lt;b&gt;Хакер&lt;/b&gt; &amp; Ко" in post.text
    assert "<b>Хакер</b>" not in post.text


def test_длинная_подпись_с_фото_сокращает_имена() -> None:  # кейс 19
    names = [Participant(f"{'Ж' * 60}{i}") for i in range(10)]
    post = render_post(
        make(
            has_photo=True,
            participants=names,
            seats_taken=10,
            description_html="Описание. " * 55,
        )
    )
    assert visible_length(post.text) <= CAPTION_LIMIT
    assert re.search(r"… и ещё \d+", post.text)
    assert "Мест нет" in post.text


def test_без_фото_лимит_4096_не_режет_короткий_пост() -> None:
    post = render_post(make(has_photo=False))
    assert "… и ещё" not in post.text


def test_очень_длинное_описание_обрезается_с_закрытием_тегов() -> None:
    post = render_post(
        make(has_photo=True, description_html="<b><i>" + "слово " * 400 + "</i></b>")
    )
    assert visible_length(post.text) <= CAPTION_LIMIT
    assert "…</i></b>" in post.text
    assert "Записались: 2 из 10" in post.text


def test_обрезка_html_не_ломает_сущности() -> None:
    cut = truncate_html("a &amp; b &lt; c " * 10, 20)
    assert visible_length(cut) <= 20
    assert "&amp" not in cut.replace("&amp;", "")


def test_счётчик_без_имён() -> None:
    post = render_post(make(show_names=False))
    assert "1. Света" not in post.text
    assert "Записались: 2 из 10" in post.text


def test_статусы_и_клавиатура() -> None:
    assert "Мест нет" in render_post(make(seats_taken=10)).text
    assert render_post(make(seats_taken=10)).keyboard  # отмена остаётся доступной
    for status, line in (
        (EventStatus.CLOSED, "Запись закрыта"),
        (EventStatus.FINISHED, "Встреча прошла"),
        (EventStatus.CANCELLED, "Встреча отменена"),
    ):
        post = render_post(make(status=status.value))
        assert line in post.text
        assert post.keyboard == ()


def test_причина_отмены_экранируется() -> None:
    post = render_post(make(status=EventStatus.CANCELLED.value, cancel_reason="<дождь>"))
    assert "Встреча отменена: &lt;дождь&gt;" in post.text


def test_кнопка_плюс_один_и_лимит_callback_data() -> None:
    post = render_post(make(plus_one_enabled=True, event_id=2**31))
    flat = [b for row in post.keyboard for b in row]
    assert [b.callback_data.split(":")[2] for b in flat] == ["j", "c", "j1"]
    assert all(len(b.callback_data.encode()) <= 64 for b in flat)


def test_хеш_стабилен_и_меняется_с_содержимым() -> None:
    assert render_post(make()).content_hash == render_post(make()).content_hash
    assert render_post(make()).content_hash != render_post(make(seats_taken=3)).content_hash
