"""Как показывать человека: единое правило для поста, списков админа, CSV и уведомлений."""

from __future__ import annotations

from meetup_bot.db.models import User

FALLBACK_NAME = "Участник"
MANUAL_SUFFIX = " (вручную)"


def public_name(user: User | None, manual_name: str | None) -> str:
    """Имя в посте и уведомлениях: «Имя Фамилия», иначе @username, иначе «Участник»."""
    if user is None:
        return manual_name or FALLBACK_NAME
    name = " ".join(part for part in (user.first_name, user.last_name) if part)
    return name or (f"@{user.username}" if user.username else FALLBACK_NAME)


def display_name(user: User | None, manual_name: str | None) -> str:
    """Имя в списках админа и CSV: добавленный вручную человек помечается «(вручную)»."""
    name = public_name(user, manual_name)
    return name + MANUAL_SUFFIX if user is None else name


def admin_label(
    first_name: str | None, last_name: str | None, username: str | None, user_id: int
) -> str:
    """Подпись человека в меню админа: «Имя Фамилия (@username)».

    Без имени — «@username», а без того и другого — «Пользователь <ID>»: админу важно
    отличить людей друг от друга, даже если о человеке ничего не известно.
    """
    name = " ".join(part for part in (first_name, last_name) if part)
    if name and username:
        return f"{name} (@{username})"
    if name:
        return name
    return f"@{username}" if username else f"Пользователь {user_id}"
