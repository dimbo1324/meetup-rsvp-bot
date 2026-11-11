"""Тексты Telegram-интерфейса (SPEC 2.5). Обычный текст без разметки: экранировать нечего."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from rcbot.application.control import IncidentView, StatusView
from rcbot.domain.models import AppEvent, Channel, DeliveryResult, PollState

STATE_NAMES = {
    PollState.RUNNING: "работает",
    PollState.REAUTH: "входит на сайт заново",
    PollState.BACKOFF: "замедлился из-за ошибок сайта",
    PollState.NEEDS_MANUAL: "ждёт, пока вы пройдёте шаг 1",
    PollState.PAUSED: "на паузе",
}
CHANNEL_NAMES = {Channel.TELEGRAM: "Telegram", Channel.NTFY: "ntfy", Channel.CALL: "звонок"}

HELP = """Команды:
/status — состояние
/pause [минуты] — пауза (без числа — до /resume)
/resume — продолжить
/interval <сек> — интервал опроса (не меньше 10)
/test — проверить все каналы уведомлений
/last — последние 5 инцидентов
/relogin — войти на сайт заново
/cleanup — очистить старые данные сейчас
/menu — меню
Подтвердить тревогу: кнопка «✅ Вижу» или слово «ок»."""

NOT_ACKED = "Нечего подтверждать: тревоги сейчас нет."
ACKED = "Принято: повторы остановлены. Инцидент открыт, пока места есть."
MANUAL_THANKS = "Спасибо, проверяю календарь."
STARTED = "▶️ rcbot запущен. Источник: {source}, интервал ≈ {interval:.0f} с. /status — состояние."
STOPPED = "⏹ rcbot остановлен."
INTERVAL_SET = "Интервал опроса: {value:.0f} с (в режиме браузера фактически не чаще раза в 60 с)."
INTERVAL_BAD = "Нужно число секунд, не меньше 10: /interval 20"
PAUSED = "Пауза до {until}. /resume — продолжить."
PAUSED_FOREVER = "Пауза до команды /resume."
RESUMED = "Опрос продолжен."
RELOGIN_OK = "Вход выполнен."
RELOGIN_FAILED = "Вход не удался: {error}"


def human_size(size: int | None) -> str:
    if size is None:
        return "—"
    value = float(size)
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if value < 1024 or unit == "ГБ":
            return f"{value:.0f} {unit}" if unit == "Б" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} ГБ"


def _ago(now: datetime, moment: datetime | None) -> str:
    if moment is None:
        return "ещё не было"
    seconds = int((now - moment).total_seconds())
    return f"{seconds} с назад" if seconds < 120 else f"{seconds // 60} мин назад"


def _uptime(delta: timedelta) -> str:
    hours, rest = divmod(int(delta.total_seconds()), 3600)
    return f"{hours // 24} д {hours % 24} ч {rest // 60} мин"


def status_text(view: StatusView, tz: ZoneInfo) -> str:
    s = view.status
    lines = [f"Состояние: {STATE_NAMES[s.state]}"]
    if s.state_reason and s.state is not PollState.RUNNING:
        lines.append(f"Причина: {s.state_reason}")
    if view.paused_until is not None:
        lines.append(f"Пауза до {view.paused_until.astimezone(tz):%d.%m %H:%M}")
    latencies = sorted(s.recent_latencies)
    p50 = latencies[len(latencies) // 2] if latencies else None
    lines += [
        f"Источник: {view.source.value.upper()}",
        f"Интервал: ≈ {view.interval_sec:.0f} с (базовый {view.base_interval_sec:.0f} с)",
        f"Месяцев вперёд: {view.months_ahead}",
        f"Последняя проверка: {_ago(view.now, s.last_check_at)}",
        f"Время ответа: последнее {s.last_latency_ms or 0:.0f} мс, медиана {p50 or 0:.0f} мс",
        f"Проверок: {s.checks_total}, ошибок: {s.errors_total}, "
        f"входов за час: {view.logins_last_hour}",
        f"Работает: {_uptime(view.now - s.started_at)}",
        f"Размер папки: {human_size(view.folder_bytes)}",
    ]
    if view.channels_off:
        lines.append("Выключены каналы: " + ", ".join(CHANNEL_NAMES[c] for c in view.channels_off))
    if view.incident is not None:
        acked = "подтверждена" if view.incident.acked else "не подтверждена"
        lines.append(
            f"Открыт инцидент №{view.incident.id}: {len(view.incident.slots)} мест, тревога {acked}"
        )
    if view.quiet_remaining_sec > 0:
        lines.append(f"Повторный вход отложен ещё на {view.quiet_remaining_sec / 60:.0f} мин (D5)")
    if s.last_error:
        lines.append(f"Последняя ошибка: {s.last_error} ({_ago(view.now, s.last_error_at)})")
    if view.folder_bytes is not None and view.folder_bytes > 300 * 1024 * 1024:
        lines.append("⚠️ Папка бота больше 300 МБ: /cleanup")
    return "\n".join(lines)


def deliveries_text(results: list[DeliveryResult]) -> str:
    if not results:
        return "Нет включённых каналов."
    return "\n".join(
        f"{'✅' if r.ok else '❌'} {CHANNEL_NAMES[r.channel]}: {r.latency_ms:.0f} мс"
        + (f" — {r.error}" if r.error else "")
        for r in results
    )


def last_text(items: list[IncidentView], tz: ZoneInfo) -> str:
    if not items:
        return "Инцидентов ещё не было."
    blocks = []
    for item in items:
        i = item.incident
        closed = f"закрыт {i.closed_at.astimezone(tz):%d.%m %H:%M}" if i.closed_at else "открыт"
        days = ", ".join(sorted({f"{s.day:%d.%m}" for s in i.slots})) or "—"
        ok = sum(1 for d in item.deliveries if d.ok)
        blocks.append(
            f"№{i.id}: {i.opened_at.astimezone(tz):%d.%m %H:%M:%S}, {closed}\n"
            f"  даты: {days}\n  доставок: {ok} из {len(item.deliveries)}"
            + (", подтверждён" if i.acked else "")
        )
    return "\n\n".join(blocks)


def errors_text(events: list[AppEvent], tz: ZoneInfo) -> str:
    if not events:
        return "Ошибок нет."
    return "\n".join(f"{e.ts.astimezone(tz):%d.%m %H:%M} {e.level}: {e.message}" for e in events)


def cleanup_text(removed: dict[str, int]) -> str:
    if not removed:
        return "Очищать нечего."
    return "Удалено: " + ", ".join(f"{k} — {v}" for k, v in removed.items())
