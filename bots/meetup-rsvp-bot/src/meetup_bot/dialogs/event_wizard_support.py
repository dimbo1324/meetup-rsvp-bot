"""Чистые помощники мастера создания встречи: предлагаемые значения и проверка длины поста."""

from __future__ import annotations

from dataclasses import dataclass, replace

from meetup_bot.dialogs._buttons import BACK
from meetup_bot.renderer.post import (
    CAPTION_LIMIT,
    TEXT_LIMIT,
    PostData,
    render_post,
    visible_length,
)
from meetup_bot.services.event_template import EventTemplate
from meetup_bot.services.settings import SettingsService
from meetup_bot.texts.render import TIME_FORMAT

TEMPLATE_SOURCE = " (как на прошлой встрече)"

# Описание из одного символа: на нём рендер меряет длину служебного блока поста
_LENGTH_PROBE = "x"
_TOO_LONG_WITH_PHOTO = (
    "С фото пост не помещается в подпись (лимит {limit}): лишних символов: {extra}. "
    f"Сократите текст ({BACK}) или пропустите фото, иначе описание будет обрезано."
)
_TOO_LONG_WITHOUT_PHOTO = (
    "Без фото пост не помещается в сообщение (лимит {limit}): лишних символов: {extra}. "
    f"Сократите текст ({BACK}), иначе описание будет обрезано."
)


@dataclass(frozen=True)
class Suggestions:
    """Что мастер предлагает «по умолчанию» на каждом шаге."""

    time: str  # «ЧЧ:ММ» в часовом поясе встреч
    place: str
    place_url: str
    description_html: str
    photo_file_id: str | None
    capacity: int
    from_template: bool

    @property
    def source(self) -> str:
        """Пометка для админа, откуда взято значение; для общих настроек — пусто."""
        return TEMPLATE_SOURCE if self.from_template else ""


def suggestions(settings: SettingsService, template: EventTemplate | None) -> Suggestions:
    """Поля прошлой встречи целиком (как в прошлый раз), а без неё — общие настройки."""
    if template is not None:
        return Suggestions(
            time=template.start_time.strftime(TIME_FORMAT),
            place=template.place,
            place_url=template.place_url,
            description_html=template.description_html,
            photo_file_id=template.photo_file_id,
            capacity=template.capacity,
            from_template=True,
        )
    defaults = settings.defaults_now()
    return Suggestions(
        time=settings.schedule_now().event_time.strftime(TIME_FORMAT),
        place=defaults.place,
        place_url=defaults.place_url,
        description_html=defaults.description_html,
        photo_file_id=defaults.photo_file_id,
        capacity=defaults.capacity,
        from_template=False,
    )


def overflow(post: PostData) -> int:
    """На сколько видимых символов пост длиннее лимита (0 — помещается без обрезки)."""
    limit = CAPTION_LIMIT if post.has_photo else TEXT_LIMIT
    # Служебный блок (дата, место, счётчик) вместе с разделителями меряет сам рендер: так
    # оценка не расходится с тем местом, где он начнёт обрезать описание
    probe = render_post(replace(post, description_html=_LENGTH_PROBE)).text
    frame = visible_length(probe) - visible_length(_LENGTH_PROBE)
    return max(0, frame + visible_length(post.description_html) - limit)


def length_warnings(post: PostData) -> list[str]:
    """Предупреждения для поста с фото и без; пусто, если описание помещается в оба."""
    if not post.description_html:
        return []
    warnings = []
    for has_photo, template, limit in (
        (True, _TOO_LONG_WITH_PHOTO, CAPTION_LIMIT),
        (False, _TOO_LONG_WITHOUT_PHOTO, TEXT_LIMIT),
    ):
        extra = overflow(replace(post, has_photo=has_photo))
        if extra:
            warnings.append(template.format(limit=limit, extra=extra))
    return warnings
