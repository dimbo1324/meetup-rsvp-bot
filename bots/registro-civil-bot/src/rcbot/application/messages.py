"""Тексты тревог и служебных сообщений (SPEC 2.4). Чистые функции, без сети.

В Telegram (личный чат владельца) — даты, номер заявки и ссылка. В ntfy и звонок CallMeBot —
только даты, время и общая фраза: эти сервисы сторонние (SPEC 2.4, «Приватность каналов»).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

from rcbot.domain.models import Alert, AlertAction, AlertKind, AvailabilitySnapshot, Channel, Slot

WEEKDAYS = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")
CALL_TEXT_RU = "Внимание. В Регистро Сивиль появились свободные места. Срочно записывайся."
CALL_TEXT_EN = "Attention. Free appointments appeared at Registro Civil. Book now."
CALL_MAX_CHARS = 256
TELEGRAM_ONLY = frozenset({Channel.TELEGRAM})


def _day_lines(slots: frozenset[Slot]) -> list[str]:
    by_day: dict[date, list[str]] = defaultdict(list)
    for slot in sorted(slots):
        if slot.at is not None:
            by_day[slot.day].append(slot.at.strftime("%H:%M"))
        else:
            by_day.setdefault(slot.day, [])
    lines = []
    for day, times in sorted(by_day.items()):
        head = f"{day:%d.%m.%Y} ({WEEKDAYS[day.weekday()]})"
        lines.append(f"• {head}: {', '.join(times)}" if times else f"• {head}")
    return lines


def _short_days(slots: frozenset[Slot], limit: int = 6) -> str:
    days = sorted({slot.day for slot in slots})
    shown = ", ".join(f"{d:%d.%m}" for d in days[:limit])
    return shown + (f" и ещё {len(days) - limit}" if len(days) > limit else "")


@dataclass(frozen=True)
class MessageComposer:
    tz: ZoneInfo
    agenda_url: str
    tramite_id: str = ""

    def local(self, moment: datetime) -> datetime:
        return moment.astimezone(self.tz)

    def slots_alert(
        self, snapshot: AvailabilitySnapshot, now: datetime, added: frozenset[Slot] | None = None
    ) -> Alert:
        changed = added is not None
        title = (
            "🟢 Registro Civil: появились новые места!"
            if changed
            else "🟢 Registro Civil: появились места!"
        )
        lines = [title, *_day_lines(snapshot.slots)]
        if changed and added:
            lines.append("Новые: " + _short_days(added))
        application = f"Заявка {self.tramite_id}" if self.tramite_id else "Заявка"
        # Задержка от начала проверки до отправки: время ответа сайта плюс обработка
        delay = snapshot.latency_ms / 1000 + max(0.0, (now - snapshot.observed_at).total_seconds())
        delay_text = f"{delay:.1f}".replace(".", ",")
        found_at = self.local(snapshot.observed_at)
        lines += [
            f"{application} → «Realizar» → шаг «Agenda»",
            self.agenda_url,
            f"Найдено в {found_at:%H:%M:%S}, уведомление за {delay_text} с.",
        ]
        days = _short_days(snapshot.slots)
        return Alert(
            kind=AlertKind.CHANGED if changed else AlertKind.APPEARED,
            text="\n".join(lines),
            call_text=CALL_TEXT_RU[:CALL_MAX_CHARS],
            call_fallback_text=CALL_TEXT_EN[:CALL_MAX_CHARS],
            push_title="Registro Civil: появились места",
            push_text=f"Свободные даты: {days}. Срочно записывайтесь.",
            action=AlertAction.ACK,
        )

    def unconfirmed_alert(self, snapshot: AvailabilitySnapshot) -> Alert:
        lines = [
            "🟡 SAE показывает места (неподтверждённые, источник вторичный):",
            *_day_lines(snapshot.slots),
        ]
        return Alert(AlertKind.UNCONFIRMED, "\n".join(lines), channels=TELEGRAM_ONLY)

    def gone(self, at: datetime) -> Alert:
        text = f"⚪ Места пропали ({self.local(at):%H:%M:%S}): три проверки подряд без мест."
        return Alert(AlertKind.SERVICE, text, channels=TELEGRAM_ONLY)

    def manual_step(self, reason: str) -> Alert:
        text = (
            "🟠 Сайт требует пройти шаг 1 «Datos necesarios» с контрольным вопросом.\n"
            "Бот на него не отвечает. Откройте заявку в своём браузере, пройдите шаг 1 и нажмите\n"
            "«Готово» — проверка продолжится.\n"
            f"{self.agenda_url}\nПричина: {reason}"
        )
        return Alert(
            AlertKind.SERVICE, text, action=AlertAction.MANUAL_DONE, channels=TELEGRAM_ONLY
        )

    def service(self, text: str) -> Alert:
        return Alert(AlertKind.SERVICE, text, channels=TELEGRAM_ONLY)

    def test(self, now: datetime) -> Alert:
        return Alert(
            kind=AlertKind.TEST,
            text=f"🧪 Проверка канала Telegram ({self.local(now):%H:%M:%S}). Это тест, мест нет.",
            call_text="Это проверка бота Регистро Сивиль. Мест нет, ничего делать не нужно.",
            push_title="rcbot: проверка",
            push_text="Это тест уведомлений. Мест нет.",
            action=AlertAction.ACK,
        )
