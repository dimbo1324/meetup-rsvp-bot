"""Меню на aiogram-dialog (SPEC 2.5): статус, сканирование, уведомления, обслуживание.

Интервал выбирается из готовых значений: ввести меньше 10 с из меню нельзя (критерий 9).
"""

from __future__ import annotations

from typing import Any
from zoneinfo import ZoneInfo

from aiogram.types import CallbackQuery, Message
from aiogram_dialog import Dialog, DialogManager, Window
from aiogram_dialog.widgets.kbd import Button, Cancel, Column, Row, Select, SwitchTo
from aiogram_dialog.widgets.text import Const, Format

from rcbot.application.control import Controller
from rcbot.bot import texts
from rcbot.bot.dialogs.states import MenuSG
from rcbot.domain.models import Channel

INTERVALS = ("20", "30", "45", "60", "120", "300")
MONTHS = ("3", "6", "9", "12")
PAUSES = (("30", "30 мин"), ("60", "1 ч"), ("180", "3 ч"), ("0", "до /resume"))


def _controller(manager: DialogManager) -> Controller:
    controller: Controller = manager.middleware_data["controller"]
    return controller


def _tz(manager: DialogManager) -> ZoneInfo:
    tz: ZoneInfo = manager.middleware_data["tz"]
    return tz


async def _status_getter(dialog_manager: DialogManager, **_: Any) -> dict[str, Any]:
    view = await _controller(dialog_manager).status()
    return {"text": texts.status_text(view, _tz(dialog_manager))}


async def _scan_getter(dialog_manager: DialogManager, **_: Any) -> dict[str, Any]:
    view = await _controller(dialog_manager).status()
    return {
        "interval": f"{view.base_interval_sec:.0f}",
        "effective": f"{view.interval_sec:.0f}",
        "months": view.months_ahead,
        "paused": "да" if view.paused_until else "нет",
        "intervals": INTERVALS,
        "month_options": MONTHS,
        "pauses": PAUSES,
    }


async def _notify_getter(dialog_manager: DialogManager, **_: Any) -> dict[str, Any]:
    view = await _controller(dialog_manager).status()
    channels = [
        (c.value, f"{'🔕' if c in view.channels_off else '🔔'} {texts.CHANNEL_NAMES[c]}")
        for c in Channel
    ]
    return {
        "channels": channels,
        "tests": [(c.value, f"Тест: {texts.CHANNEL_NAMES[c]}") for c in Channel],
    }


async def _maintenance_getter(dialog_manager: DialogManager, **_: Any) -> dict[str, Any]:
    controller = _controller(dialog_manager)
    view = await controller.status()
    errors = texts.errors_text(await controller.recent_errors(5), _tz(dialog_manager))
    return {"size": texts.human_size(view.folder_bytes), "errors": errors}


async def _on_interval(callback: CallbackQuery, _: Any, manager: DialogManager, item: str) -> None:
    value = await _controller(manager).set_interval(float(item))
    await callback.answer(f"Интервал {value:.0f} с")


async def _on_months(callback: CallbackQuery, _: Any, manager: DialogManager, item: str) -> None:
    await callback.answer(f"Месяцев вперёд: {await _controller(manager).set_months(int(item))}")


async def _on_pause(callback: CallbackQuery, _: Any, manager: DialogManager, item: str) -> None:
    await _controller(manager).pause(int(item) or None)
    await callback.answer("Пауза включена")


async def _on_resume(callback: CallbackQuery, _: Button, manager: DialogManager) -> None:
    await _controller(manager).resume()
    await callback.answer(texts.RESUMED)


async def _on_toggle(callback: CallbackQuery, _: Any, manager: DialogManager, item: str) -> None:
    enabled = await _controller(manager).toggle_channel(Channel(item))
    await callback.answer("Канал включён" if enabled else "Канал выключен")


async def _on_test(callback: CallbackQuery, _: Any, manager: DialogManager, item: str) -> None:
    results = await _controller(manager).test(Channel(item))
    await callback.answer(texts.deliveries_text(results)[:200], show_alert=True)


async def _on_relogin(callback: CallbackQuery, _: Button, manager: DialogManager) -> None:
    await callback.answer("Вхожу заново…")
    error = await _controller(manager).relogin()
    if isinstance(callback.message, Message):
        await callback.message.answer(
            texts.RELOGIN_FAILED.format(error=error) if error else texts.RELOGIN_OK
        )


async def _on_cleanup(callback: CallbackQuery, _: Button, manager: DialogManager) -> None:
    removed = await _controller(manager).cleanup()
    await callback.answer(texts.cleanup_text(removed)[:200], show_alert=True)


def build_menu_dialog() -> Dialog:
    """Новый экземпляр на каждый Dispatcher: роутер aiogram нельзя подключить дважды."""
    to_main = SwitchTo(Const("⬅️ Назад"), id="to_main", state=MenuSG.main)

    return Dialog(
        Window(
            Const("Меню rcbot"),
            Column(
                SwitchTo(Const("📊 Статус"), id="status", state=MenuSG.status),
                SwitchTo(Const("🔎 Сканирование"), id="scan", state=MenuSG.scan),
                SwitchTo(Const("🔔 Уведомления"), id="notify", state=MenuSG.notify),
                SwitchTo(Const("🛠 Обслуживание"), id="maint", state=MenuSG.maintenance),
                Cancel(Const("✖️ Закрыть")),
            ),
            state=MenuSG.main,
        ),
        Window(
            Format("{text}"),
            Row(SwitchTo(Const("🔄 Обновить"), id="refresh", state=MenuSG.status), to_main),
            state=MenuSG.status,
            getter=_status_getter,
        ),
        Window(
            Format(
                "Интервал: {interval} с (фактически ≈ {effective} с)\n"
                "Месяцев вперёд: {months}\nПауза: {paused}\n\nИнтервал, с:"
            ),
            Row(
                Select(
                    Format("{item}"),
                    id="iv",
                    item_id_getter=str,
                    items="intervals",
                    on_click=_on_interval,
                )
            ),
            Const("Месяцев вперёд:"),
            Row(
                Select(
                    Format("{item}"),
                    id="mo",
                    item_id_getter=str,
                    items="month_options",
                    on_click=_on_months,
                )
            ),
            Const("Пауза:"),
            Row(
                Select(
                    Format("{item[1]}"),
                    id="pz",
                    item_id_getter=lambda item: item[0],
                    items="pauses",
                    on_click=_on_pause,
                )
            ),
            Button(Const("▶️ Продолжить"), id="resume", on_click=_on_resume),
            to_main,
            state=MenuSG.scan,
            getter=_scan_getter,
        ),
        Window(
            Const("Каналы (нажмите, чтобы включить или выключить):"),
            Column(
                Select(
                    Format("{item[1]}"),
                    id="ch",
                    item_id_getter=lambda item: item[0],
                    items="channels",
                    on_click=_on_toggle,
                )
            ),
            Column(
                Select(
                    Format("{item[1]}"),
                    id="tst",
                    item_id_getter=lambda item: item[0],
                    items="tests",
                    on_click=_on_test,
                )
            ),
            to_main,
            state=MenuSG.notify,
            getter=_notify_getter,
        ),
        Window(
            Format("Размер папки: {size}\n\nПоследние ошибки:\n{errors}"),
            Button(Const("🔑 Войти заново"), id="relogin", on_click=_on_relogin),
            Button(Const("🧹 Очистить сейчас"), id="cleanup", on_click=_on_cleanup),
            to_main,
            state=MenuSG.maintenance,
            getter=_maintenance_getter,
        ),
    )
