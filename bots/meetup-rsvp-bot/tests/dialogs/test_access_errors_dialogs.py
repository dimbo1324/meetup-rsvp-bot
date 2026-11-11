"""Отказ в доступе и сбои сервисов в диалогах: человек всегда получает ответ."""

import pytest

from meetup_bot.services.errors import NotFound
from meetup_bot.telegram.common import GENERIC_ERROR_TEXT
from tests.helpers import ADMIN_ID, OWNER_ID, App
from tests.tg_fake import Harness, command_update, text_update


def _inline(tg: Harness) -> int:
    return max(m for m, mk in tg.session.last_markup.items() if "inline_keyboard" in mk)


def _screen(tg: Harness) -> str:
    for name, data in reversed(tg.session.calls):
        if name in {"SendMessage", "EditMessageText"} and "text" in data:
            return str(data["text"])
    return ""


def _alerts(tg: Harness) -> list[str]:
    return [str(d["text"]) for d in tg.session.of("AnswerCallbackQuery") if d.get("text")]


async def _press(tg: Harness, label: str, user: int = OWNER_ID) -> None:
    await tg.click(user, _inline(tg), label)


async def _open(tg: Harness, *path: str, user: int = OWNER_ID) -> None:
    await tg.feed(command_update(user, "/menu"))
    for label in path:
        await _press(tg, label, user)


# ---- настройки: отказ виден ----------------------------------------------------------------


async def test_потерявший_права_видит_отказ_при_нажатии_в_настройках(app: App, tg: Harness) -> None:
    await _open(tg, "Настройки", "Правила", "Имена в посте", user=ADMIN_ID)
    await app.services.admins.remove_admin(OWNER_ID, ADMIN_ID)
    await _press(tg, "Включить", ADMIN_ID)
    assert _alerts(tg)[-1] == "Нет доступа."
    assert app.services.settings.get_now("show_names_in_post") is False


async def test_потерявший_права_видит_отказ_при_вводе_значения(app: App, tg: Harness) -> None:
    await _open(tg, "Настройки", "Встречи по умолчанию", "Лимит мест", user=ADMIN_ID)
    await app.services.admins.remove_admin(OWNER_ID, ADMIN_ID)
    await tg.feed(text_update(ADMIN_ID, "77"))
    assert any(d["text"] == "Нет доступа." for d in tg.session.of("SendMessage"))
    assert app.services.settings.get_now("capacity") == 10


async def test_потерявший_права_видит_отказ_при_открытии_раздела_настроек(
    app: App, tg: Harness
) -> None:
    await _open(tg, "Настройки", user=ADMIN_ID)
    await app.services.admins.remove_admin(OWNER_ID, ADMIN_ID)
    await _press(tg, "Часовой пояс", ADMIN_ID)
    assert _alerts(tg)[-1] == "Нет доступа."


# ---- сбои сервисов ---------------------------------------------------------------------------


async def test_ошибка_сервиса_в_подтверждении_удаления_админа_показана_в_окне(
    app: App, tg: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _open(tg, "Админы", "Админ (@admin2)")

    async def failing(actor_id: int) -> list[object]:
        raise NotFound

    monkeypatch.setattr(app.services.admins, "list_admins", failing)
    await _press(tg, "Удалить из админов")
    assert _screen(tg) == "Не найдено."


async def test_непредвиденная_ошибка_не_глотается_а_доходит_до_общего_обработчика(
    app: App, tg: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _open(tg)

    async def broken(actor_id: int) -> list[object]:
        raise RuntimeError("сбой базы")

    monkeypatch.setattr(app.services.bans, "list_banned", broken)
    await _press(tg, "Чёрный список")
    assert _alerts(tg)[-1] == GENERIC_ERROR_TEXT
