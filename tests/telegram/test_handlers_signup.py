"""Кнопки под постом: запись, запись с гостем, отмена; тексты всплывающих окон (раздел 8.2)."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import AnswerCallbackQuery
from sqlalchemy import select

from meetup_bot.db.models import EventPost
from meetup_bot.repositories import event_posts
from meetup_bot.repositories.users import UserInfo
from meetup_bot.services.errors import DomainError
from meetup_bot.telegram.common import ALERT_LIMIT, GENERIC_ERROR_TEXT
from tests.helpers import OWNER_ID, App
from tests.tg_fake import Harness, callback_update, command_update

USER = 1001
OTHER = 1002
INLINE = "inline-1"
USER_INFO = UserInfo(id=USER, first_name="Люди1001", username="u1001")
DEFAULTS = {
    "text.already": "Вы уже записаны.",
    "text.full": "Извините, мест не осталось. Попробуйте в следующий раз!",
    "text.closed": "Запись на эту встречу закрыта.",
    "text.cancelled": "Запись отменена. Спасибо, что предупредили!",
    "text.not_registered": "Вы не были записаны на эту встречу.",
    "text.only_one_seat": "Осталось одно место — запишитесь без гостя.",
}
HINT = "Нажмите «Старт» у бота, чтобы получать напоминания."


async def make_event(app: App, capacity: int = 10, **kwargs: Any) -> int:
    created = await app.services.events.create_event_as_admin(OWNER_ID, capacity=capacity, **kwargs)
    return created.event_id


async def press(
    tg: Harness, event_id: int, action: str = "j", user: int = USER, inline: str | None = INLINE
) -> dict[str, Any]:
    """Нажимает кнопку и возвращает данные ответа на callback."""
    before = len(tg.session.of("AnswerCallbackQuery"))
    await tg.feed(callback_update(user, f"r:{event_id}:{action}", inline_message_id=inline))
    answers = tg.session.of("AnswerCallbackQuery")
    assert len(answers) == before + 1, "на callback должен быть ровно один ответ"
    return answers[-1]


async def posts_of(app: App, event_id: int) -> list[EventPost]:
    async with app.sessions() as session:
        rows = await session.scalars(select(EventPost).where(EventPost.event_id == event_id))
        return list(rows.all())


async def test_успешная_запись_показывает_дату_и_дедлайн_в_окне(app: App, tg: Harness) -> None:
    event_id = await make_event(app)
    answer = await press(tg, event_id)
    assert answer["show_alert"] is True
    # Новый текст по умолчанию (изменён вместе с итоговыми требованиями): встреча и время окончания
    assert answer["text"].startswith("Вы записаны на встречу: пятница, 23 октября, 16:30–18:30.")
    assert "отмените запись до 22 октября, 20:00" in answer["text"]


async def test_ответ_всегда_во_всплывающем_окне_и_не_длиннее_лимита(app: App, tg: Harness) -> None:
    event_id = await make_event(app)
    answer = await press(tg, event_id)
    assert answer["show_alert"] is True
    assert len(answer["text"]) <= ALERT_LIMIT


async def test_подсказка_про_старт_если_личка_недоступна(app: App, tg: Harness) -> None:
    event_id = await make_event(app)
    answer = await press(tg, event_id)
    assert answer["text"].endswith(HINT)
    assert answer["url"] == "https://t.me/mybot?start=remind"


async def test_подсказки_нет_если_личка_доступна(app: App, tg: Harness) -> None:
    await tg.feed(command_update(USER, "/start"))
    event_id = await make_event(app)
    answer = await press(tg, event_id)
    assert HINT not in answer["text"]
    assert "url" not in answer


async def test_подсказка_опускается_если_не_влезает_в_лимит(app: App, tg: Harness) -> None:
    await app.services.settings.set("text.registered", "О" * 190, OWNER_ID)
    event_id = await make_event(app)
    answer = await press(tg, event_id)
    assert answer["text"] == "О" * 190
    assert "url" not in answer


async def test_подсказка_не_добавляется_к_повторной_записи(app: App, tg: Harness) -> None:
    event_id = await make_event(app)
    await press(tg, event_id)
    answer = await press(tg, event_id)
    assert answer["text"] == DEFAULTS["text.already"]
    assert "url" not in answer


async def test_запись_после_дедлайна_отмены_предупреждает(app: App, tg: Harness) -> None:
    event_id = await make_event(app)
    app.clock.advance(timedelta(hours=12))
    answer = await press(tg, event_id)
    assert "отменить запись уже нельзя" in answer["text"]


async def test_нет_мест_без_листа_ожидания(app: App, tg: Harness) -> None:
    await app.services.settings.set("waitlist_enabled", False, OWNER_ID)
    event_id = await make_event(app, capacity=1)
    await press(tg, event_id, user=OTHER)
    answer = await press(tg, event_id)
    assert answer["text"] == DEFAULTS["text.full"]


async def test_лист_ожидания_показывает_номер_в_очереди(app: App, tg: Harness) -> None:
    await app.services.settings.set("waitlist_enabled", True, OWNER_ID)
    event_id = await make_event(app, capacity=1)
    await press(tg, event_id, user=OTHER)
    answer = await press(tg, event_id)
    assert "в листе ожидания под номером 1" in answer["text"]


async def test_повторное_нажатие_в_листе_ожидания_называет_номер(app: App, tg: Harness) -> None:
    await app.services.settings.set("waitlist_enabled", True, OWNER_ID)
    event_id = await make_event(app, capacity=1)
    await press(tg, event_id, user=OTHER)
    await press(tg, event_id)
    answer = await press(tg, event_id)
    assert answer["text"] == "Вы уже в листе ожидания, номер 1."


async def test_запись_закрыта(app: App, tg: Harness) -> None:
    event_id = await make_event(app)
    await app.services.events.set_registration_open(OWNER_ID, event_id, False)
    answer = await press(tg, event_id)
    assert answer["text"] == DEFAULTS["text.closed"]


async def test_забаненному_отказ_с_текстом_бана(app: App, tg: Harness) -> None:
    event_id = await make_event(app)
    await app.services.bans.ban(OWNER_ID, USER)
    answer = await press(tg, event_id)
    assert answer["text"].startswith("К сожалению, вы не можете записываться на встречи.")


async def test_запись_с_гостем_при_включённой_настройке(app: App, tg: Harness) -> None:
    await app.services.settings.set("plus_one_enabled", True, OWNER_ID)
    event_id = await make_event(app, capacity=2)
    answer = await press(tg, event_id, action="j1")
    assert answer["text"].startswith("Вы записаны")
    await tg.updater.aclose()
    assert "Записались: 2 из 2" in tg.session.of("EditMessageText")[-1]["text"]


async def test_гость_при_одном_свободном_месте(app: App, tg: Harness) -> None:
    await app.services.settings.set("waitlist_enabled", False, OWNER_ID)
    await app.services.settings.set("plus_one_enabled", True, OWNER_ID)
    event_id = await make_event(app, capacity=2)
    await press(tg, event_id, user=OTHER)
    answer = await press(tg, event_id, action="j1")
    assert answer["text"] == DEFAULTS["text.only_one_seat"]


async def test_гость_при_выключенной_настройке_получает_понятный_отказ(
    app: App, tg: Harness
) -> None:
    event_id = await make_event(app)
    answer = await press(tg, event_id, action="j1")
    assert answer["text"] == "Запись с гостем сейчас недоступна."
    assert answer["show_alert"] is True


async def test_отмена_записи(app: App, tg: Harness) -> None:
    event_id = await make_event(app)
    await press(tg, event_id)
    answer = await press(tg, event_id, action="c")
    assert answer["text"] == DEFAULTS["text.cancelled"]


async def test_отмена_без_записи(app: App, tg: Harness) -> None:
    event_id = await make_event(app)
    answer = await press(tg, event_id, action="c")
    assert answer["text"] == DEFAULTS["text.not_registered"]


async def test_отмена_после_дедлайна_отклоняется(app: App, tg: Harness) -> None:
    event_id = await make_event(app)
    await press(tg, event_id)
    app.clock.advance(timedelta(hours=12))
    answer = await press(tg, event_id, action="c")
    assert answer["text"].startswith("Отменить запись уже нельзя.")


async def test_отмена_подставляет_дату_в_настроенный_текст(app: App, tg: Harness) -> None:
    await app.services.settings.set("text.cancelled", "Снято: {weekday}, {date}, {time}", OWNER_ID)
    event_id = await make_event(app)
    await press(tg, event_id)
    answer = await press(tg, event_id, action="c")
    assert answer["text"] == "Снято: пятница, 23 октября, 16:30"


async def test_текст_на_границе_лимита_окна_не_режется(app: App, tg: Harness) -> None:
    # Настройки не дают сохранить шаблон длиннее 200 символов с худшими подстановками
    await app.services.settings.set("text.already", "{place}", OWNER_ID)
    event_id = await make_event(app, place="М" * 200)
    await press(tg, event_id)
    answer = await press(tg, event_id)
    assert answer["text"] == "М" * ALERT_LIMIT


async def test_спецсимволы_в_месте_не_экранируются_в_окне(app: App, tg: Harness) -> None:
    await app.services.settings.set("text.already", "{place}", OWNER_ID)
    event_id = await make_event(app, place="Кафе <Пушкин> & Ко")
    await press(tg, event_id)
    answer = await press(tg, event_id)
    assert answer["text"] == "Кафе <Пушкин> & Ко"


async def test_несуществующая_встреча(app: App, tg: Harness) -> None:
    answer = await press(tg, 9999)
    assert answer["text"] == "Встреча не найдена"
    assert answer["show_alert"] is True


async def test_несуществующая_встреча_не_создаёт_пост_и_не_правит(app: App, tg: Harness) -> None:
    await press(tg, 9999)
    await tg.updater.aclose()
    assert tg.session.of("EditMessageText") == []
    async with app.sessions() as session:
        assert (await session.scalars(select(EventPost))).all() == []


async def test_отмена_несуществующей_встречи(app: App, tg: Harness) -> None:
    answer = await press(tg, 9999, action="c")
    assert answer["text"] == "Встреча не найдена"


async def test_неизвестное_действие_даёт_общий_ответ_без_поста(app: App, tg: Harness) -> None:
    event_id = await make_event(app)
    answer = await press(tg, event_id, action="zzz")
    assert answer["text"] == GENERIC_ERROR_TEXT
    assert await posts_of(app, event_id) == []


async def test_кнопка_старого_формата_устарела(app: App, tg: Harness) -> None:
    await tg.feed(callback_update(USER, "r:abc", inline_message_id=INLINE))
    (answer,) = tg.session.of("AnswerCallbackQuery")
    assert answer["text"] == "Эта кнопка устарела. Откройте свежий пост встречи."
    assert answer["show_alert"] is True


async def test_чужой_callback_не_обрабатывается_обработчиком_записи(app: App, tg: Harness) -> None:
    await tg.feed(callback_update(USER, "x:1:j", inline_message_id=INLINE))
    assert tg.session.of("AnswerCallbackQuery") == []


async def test_первое_нажатие_регистрирует_пост(app: App, tg: Harness) -> None:
    event_id = await make_event(app)
    await press(tg, event_id)
    (post,) = await posts_of(app, event_id)
    assert post.inline_message_id == INLINE
    assert post.posted_by is None
    assert post.created_at == app.clock.now()


async def test_отмена_не_регистрирует_пост(app: App, tg: Harness) -> None:
    event_id = await make_event(app)
    await press(tg, event_id, "c")
    assert await posts_of(app, event_id) == []


async def test_запись_с_гостем_регистрирует_пост(app: App, tg: Harness) -> None:
    event_id = await make_event(app)
    await app.services.settings.set("plus_one_enabled", True, OWNER_ID)
    await press(tg, event_id, "j1")
    assert len(await posts_of(app, event_id)) == 1


async def test_повторные_нажатия_не_дублируют_пост(app: App, tg: Harness) -> None:
    event_id = await make_event(app)
    for _ in range(3):
        await press(tg, event_id)
    assert len(await posts_of(app, event_id)) == 1


async def test_нажатие_из_разных_постов_регистрирует_каждый(app: App, tg: Harness) -> None:
    event_id = await make_event(app)
    await press(tg, event_id, inline="a")
    await press(tg, event_id, inline="b")
    assert {p.inline_message_id for p in await posts_of(app, event_id)} == {"a", "b"}


async def test_нажатие_без_inline_id_пост_не_регистрирует(app: App, tg: Harness) -> None:
    event_id = await make_event(app)
    answer = await press(tg, event_id, inline=None)
    assert answer["text"].startswith("Вы записаны")
    assert await posts_of(app, event_id) == []


async def test_повторная_запись_оживляет_мёртвый_пост(app: App, tg: Harness) -> None:
    event_id = await make_event(app)
    async with app.sessions() as session, session.begin():
        post, _ = await event_posts.register(
            session,
            event_id=event_id,
            inline_message_id=INLINE,
            posted_by=None,
            now=app.clock.now(),
        )
        post.last_render_hash = "старый"
        await event_posts.mark_dead(session, post.id)
    await press(tg, event_id)
    await tg.updater.aclose()
    (post,) = await posts_of(app, event_id)
    assert not post.is_dead
    assert post.last_render_hash not in (None, "старый")
    assert tg.session.of("EditMessageText")


async def test_запись_пересобирает_пост(app: App, tg: Harness) -> None:
    event_id = await make_event(app)
    await press(tg, event_id)
    await tg.updater.aclose()
    (edit,) = tg.session.of("EditMessageText")
    assert edit["inline_message_id"] == INLINE
    assert "Записались: 1 из 10" in edit["text"]


async def test_отмена_пересобирает_пост(app: App, tg: Harness) -> None:
    event_id = await make_event(app)
    await press(tg, event_id)
    await tg.updater.aclose()
    await press(tg, event_id, action="c")
    await tg.updater.aclose()
    assert "Записались: 0 из 10" in tg.session.of("EditMessageText")[-1]["text"]


async def test_известное_нажатие_без_изменений_всё_равно_чинит_устаревший_пост(
    app: App, tg: Harness
) -> None:
    # Пользователь уже записан, а пост на экране показывает старые данные (хеш не совпадает)
    event_id = await make_event(app)
    await app.services.registration.register(event_id, USER_INFO)
    await press(tg, event_id)
    await tg.updater.aclose()
    (edit,) = tg.session.of("EditMessageText")
    assert "Записались: 1 из 10" in edit["text"]


async def test_отказ_сервиса_записи_даёт_общий_ответ(
    app: App, tg: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    event_id = await make_event(app)

    async def boom(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("сбой")

    monkeypatch.setattr(app.services.registration, "register", boom)
    answer = await press(tg, event_id)
    assert answer["text"] == GENERIC_ERROR_TEXT


async def test_доменная_ошибка_даёт_общий_ответ(
    app: App, tg: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    event_id = await make_event(app)

    async def refuse(*_a: Any, **_k: Any) -> None:
        raise DomainError("отказ")

    monkeypatch.setattr(app.services.registration, "cancel", refuse)
    answer = await press(tg, event_id, action="c")
    assert answer["text"] == GENERIC_ERROR_TEXT


async def test_устаревший_query_не_роняет_обработчик(app: App, tg: Harness) -> None:
    event_id = await make_event(app)

    def too_old(name: str, _data: dict[str, Any]) -> Exception | None:
        if name == "AnswerCallbackQuery":
            return TelegramBadRequest(
                AnswerCallbackQuery(callback_query_id="1"), "query is too old"
            )
        return None

    tg.session.fail = too_old
    await tg.feed(callback_update(USER, f"r:{event_id}:j", inline_message_id=INLINE))
    # Запись при этом состоялась
    (item,) = await app.services.events.list_events(OWNER_ID)
    assert item.seats_taken == 1
