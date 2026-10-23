"""Группы состояний всех диалогов меню админа (единый контракт между модулями).

Каждый модуль диалогов реализует окна своих групп. Нужны дополнительные состояния —
заводите СВОЮ группу в своём модуле, а не правьте этот файл.

Контракты запуска (`start_data` и результат):
- `EventsSG.card`: {"event_id": int}.
- `ParticipantsSG.list`: {"event_id": int}.
- `CreateEventSG.date`: без данных; по завершении `done(result={"event_id": int})`.
- `PickerSG.pick`: {"title": str, "exclude_ids": list[int]} (необязательно); по завершении
  `done(result={"user_id": int, "first_name": str, "last_name": str | None,
  "username": str | None})`, при отмене — `done()` без результата.
- `BansSG.item`: {"user_id": int}.
"""

from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class MainSG(StatesGroup):
    menu = State()
    howto = State()


class EventsSG(StatesGroup):
    list = State()
    card = State()
    preview = State()
    cancel_reason = State()
    cancel_confirm = State()
    edit_menu = State()
    edit_description = State()
    edit_photo = State()
    edit_place = State()
    edit_place_url = State()
    edit_starts = State()
    edit_capacity = State()
    edit_moments = State()


class CreateEventSG(StatesGroup):
    date = State()
    time = State()
    place = State()
    description = State()
    photo = State()
    capacity = State()
    preview = State()


class ParticipantsSG(StatesGroup):
    list = State()
    item = State()
    add_choose = State()
    add_manual_name = State()
    confirm_over_capacity = State()
    confirm_remove = State()
    ban_comment = State()


class BansSG(StatesGroup):
    list = State()
    item = State()
    history = State()
    ban_comment = State()
    confirm_unban = State()


class AdminsSG(StatesGroup):
    list = State()
    item = State()
    confirm_remove = State()


class PickerSG(StatesGroup):
    pick = State()
    known = State()


class SettingsSG(StatesGroup):
    menu = State()
    group = State()
    edit = State()
    texts = State()
    text_edit = State()
    timezone = State()
