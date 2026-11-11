"""Идентификаторы окон aiogram-dialog не должны совпадать (причина «Stack is broken»)."""

from aiogram_dialog.api.entities import stack

from meetup_bot.dialogs import intent_ids


def test_генератор_библиотеки_заменён_при_сборке_роутера(tg: object) -> None:
    assert stack.new_int_id is intent_ids.secure_int_id


def test_тысячи_окон_за_одну_секунду_без_совпадений() -> None:
    intent_ids.install()
    ids = {stack.new_id() for _ in range(20_000)}
    assert len(ids) == 20_000


def test_идентификатор_короткий_для_лимита_callback_data() -> None:
    intent_ids.install()
    assert max(len(stack.new_id()) for _ in range(1000)) <= 7
