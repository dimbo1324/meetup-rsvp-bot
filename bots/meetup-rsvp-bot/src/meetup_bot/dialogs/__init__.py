"""Меню админа на aiogram-dialog.

Каждый модуль диалогов может определять `DIALOGS: list[Dialog]` и/или `router: Router`
(обычные обработчики команд). Здесь они собираются в один корневой роутер.
"""

from __future__ import annotations

import importlib

from aiogram import Router

from meetup_bot.dialogs import intent_ids

# Порядок важен: сначала главное меню (команды /menu, /cancel), затем остальные
MODULES = (
    "meetup_bot.dialogs.main_menu",
    "meetup_bot.dialogs.events",
    "meetup_bot.dialogs.event_wizard",
    "meetup_bot.dialogs.participants",
    "meetup_bot.dialogs.picker",
    "meetup_bot.dialogs.bans",
    "meetup_bot.dialogs.admins",
    "meetup_bot.dialogs.settings",
)


def build_dialog_router() -> Router:
    intent_ids.install()
    root = Router(name="dialogs")
    for name in MODULES:
        module = importlib.import_module(name)
        router = getattr(module, "router", None)
        if router is not None:
            root.include_router(router)
        for dialog in getattr(module, "DIALOGS", []):
            root.include_router(dialog)
    return root
