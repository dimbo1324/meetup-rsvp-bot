"""Команды подготовки: `rcbot init` (секреты, тема ntfy, chat_id) и `rcbot doctor`."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import importlib.metadata
import os
import re
import secrets
import shutil
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path

from aiogram.exceptions import TelegramAPIError

from rcbot.bootstrap import Core, build_core
from rcbot.config.secrets import DESCRIPTIONS, REQUIRED, SecretKey
from rcbot.config.settings import AGENDA_URL_TEMPLATE, Settings
from rcbot.infrastructure.gubuy.browser import CHROME, EDGE
from rcbot.infrastructure.gubuy.browser import NAMES as BROWSER_NAMES
from rcbot.infrastructure.gubuy.browser import survey as browser_survey
from rcbot.infrastructure.http.client import SafeHttp
from rcbot.infrastructure.journal.files import FileJournal

CHAT_ID_WAIT_SEC = 180
GET_UPDATES_TIMEOUT_SEC = 20
OWNER_CODE_BYTES = 3
CLOCK_DRIFT_MAX_SEC = 30
BYTES_PER_MB = 2**20
YES_ANSWERS = ("да", "д", "y", "yes")


def _ask(key: SecretKey, *, hidden: bool, existing: bool) -> str:
    hint = " (Enter — оставить как есть)" if existing else ""
    prompt = f"{DESCRIPTIONS[key]}{hint}: "
    return (getpass.getpass(prompt) if hidden else input(prompt)).strip()


# Короче четырёх знаков фильтр логов значение не маскирует: такой номер утёк бы в логи (R1)
NUMBER_MIN_DIGITS = 4
NUMBER_MAX_DIGITS = 15
_DIGITS = rf"\d{{{NUMBER_MIN_DIGITS},{NUMBER_MAX_DIGITS}}}"
_STAGE_URL = re.compile(rf"^https://bpmgob\.mec\.gub\.uy/etapas/ejecutar/({_DIGITS})/\d{{1,3}}/?$")
_NUMBER = re.compile(rf"^{_DIGITS}$")
ASK_ATTEMPTS = 3


def parse_stage(text: str) -> str | None:
    """Адрес шага «Agenda» из номера этапа или из целого адреса; не подошло — `None`."""
    text = text.strip()
    if _NUMBER.match(text):
        return AGENDA_URL_TEMPLATE.format(stage=text)
    if match := _STAGE_URL.match(text):
        return AGENDA_URL_TEMPLATE.format(stage=match.group(1))
    return None


def parse_tramite(text: str) -> str | None:
    text = text.strip()
    return text if _NUMBER.match(text) else None


def set_env_value(env_file: Path, key: str, value: str) -> None:
    """Меняет строку `KEY=...` в `.env` (или дописывает), остальное содержимое не трогает."""
    lines = env_file.read_text(encoding="utf-8").splitlines() if env_file.exists() else []
    for index, line in enumerate(lines):
        if line.startswith(f"{key}="):
            lines[index] = f"{key}={value}"
            break
    else:
        lines.append(f"{key}={value}")
    env_file.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def _ask_number(prompt: str, parse: Callable[[str], str | None]) -> str | None:
    for _ in range(ASK_ATTEMPTS):
        answer = input(prompt).strip()
        if not answer:
            return None
        if (value := parse(answer)) is not None:
            return value
        print(
            f"Не подошло: нужны только цифры (от {NUMBER_MIN_DIGITS} до {NUMBER_MAX_DIGITS}) — "
            "проверьте и введите ещё раз."
        )
    return None


def _ask_case(core: Core) -> None:
    """Номер этапа и номер заявки: нужны один раз, лежат в локальном `.env`, не в репозитории."""
    s = core.settings
    if not s.agenda_configured:
        stage = _ask_number(
            "Номер этапа — число из адресной строки шага «Agenda» (можно весь адрес;\n"
            "  Enter — пропустить и вписать в .env позже): ",
            parse_stage,
        )
        if stage:
            set_env_value(core.paths.env_file, "RCBOT_AGENDA_URL", stage)
            print("  Записано в .env: RCBOT_AGENDA_URL.")
    if not s.tramite_id or "<" in s.tramite_id:
        number = _ask_number(
            "Номер заявки из «Bandeja de entrada» (Enter — пропустить): ", parse_tramite
        )
        if number:
            set_env_value(core.paths.env_file, "RCBOT_TRAMITE_ID", number)
            print("  Записано в .env: RCBOT_TRAMITE_ID.")


def cmd_init(_: argparse.Namespace) -> int:
    with build_core(command="init") as core:
        return _init(core)


def _init(core: Core) -> int:
    if not core.paths.env_file.exists():
        example = core.paths.home / ".env.example"
        shutil.copyfile(example, core.paths.env_file)
        print(f"Создан {core.paths.env_file}.")
    _ask_case(core)
    present = core.secrets.present()
    print("Секреты кладутся в Windows Credential Manager (служба rcbot), не на диск.\n")

    for key, hidden in (
        (SecretKey.DOC_NUMBER, True),
        (SecretKey.PASSWORD, True),
        (SecretKey.TELEGRAM_TOKEN, True),
        (SecretKey.CALLMEBOT_USER, False),
    ):
        value = _ask(key, hidden=hidden, existing=present[key])
        if value:
            core.secrets.set(key, value)
        elif not present[key] and key in REQUIRED:
            print(f"Пропущено обязательное: {DESCRIPTIONS[key]}. Запустите init ещё раз.")

    if not present[SecretKey.NTFY_TOPIC]:
        topic = f"rcbot-{secrets.token_urlsafe(30)}"
        core.secrets.set(SecretKey.NTFY_TOPIC, topic)
        print(
            "\nТема ntfy создана. Подпишитесь на неё в приложении ntfy на телефоне,\n"
            "включите для неё максимальный приоритет и звук в режиме «Не беспокоить».\n"
            "Тему не пересылайте:\n"
            f"  {topic}\n"
        )

    token = core.secrets.get(SecretKey.TELEGRAM_TOKEN)
    if token is not None and not present[SecretKey.TELEGRAM_CHAT_ID]:
        code = _owner_code()
        print(
            f"Напишите своему боту в личные сообщения Telegram: /start {code}\n"
            f"(или просто {code}) — жду до {CHAT_ID_WAIT_SEC} секунд…"
        )
        try:
            chat = asyncio.run(_wait_owner_chat(token.get_secret_value(), code, core.clock.now()))
        except TelegramAPIError as error:
            print(
                f"Telegram отклонил запрос ({type(error).__name__}): проверьте токен и сеть, "
                "затем запустите `rcbot init` ещё раз."
            )
            return 1
        if chat is None:
            print("Сообщение не пришло. Запустите `rcbot init` ещё раз.")
            return 1
        chat_id, name = chat
        if input(f"Это вы: {name}? [да/нет] ").strip().lower() not in YES_ANSWERS:
            print("Владелец не записан. Запустите `rcbot init` ещё раз.")
            return 1
        core.secrets.set(SecretKey.TELEGRAM_CHAT_ID, str(chat_id))
        print(f"Владелец определён: {name}. Бот будет отвечать только ему.")
    if missing := [k for k in REQUIRED if not core.secrets.present()[k]]:
        print(f"\nНе хватает обязательного ({len(missing)}): запустите `rcbot init` ещё раз.")
        return 1
    print("\nГотово. Дальше: `rcbot doctor`.")
    return 0


def _owner_code() -> str:
    return secrets.token_hex(OWNER_CODE_BYTES)


def _carries_code(text: str | None, code: str) -> bool:
    """`/start <код>`, `/start@имя_бота <код>` или просто `<код>`."""
    words = (text or "").split()
    if words and words[0].split("@")[0] == "/start":
        words = words[1:]
    return len(words) == 1 and secrets.compare_digest(words[0].lower().encode(), code.encode())


async def _wait_owner_chat(token: str, code: str, since: datetime) -> tuple[int, str] | None:
    """Владелец — тот, кто прислал одноразовый код из консоли: первый встречный им не станет."""
    from aiogram import Bot

    # Telegram ставит дату с точностью до секунды: сообщение той же секунды — уже после начала
    since = since.replace(microsecond=0)
    bot = Bot(token)
    try:
        # Старые сообщения, пришедшие до запуска мастера, к владельцу отношения не имеют
        await bot.delete_webhook(drop_pending_updates=True)
        offset: int | None = None
        deadline = asyncio.get_running_loop().time() + CHAT_ID_WAIT_SEC
        while asyncio.get_running_loop().time() < deadline:
            updates = await bot.get_updates(
                offset=offset, timeout=GET_UPDATES_TIMEOUT_SEC, allowed_updates=["message"]
            )
            for update in updates:
                offset = update.update_id + 1
                message = update.message
                if (
                    message
                    and message.chat.type == "private"
                    and message.from_user
                    and message.date >= since
                    and _carries_code(message.text, code)
                ):
                    await bot.get_updates(offset=offset, timeout=0)
                    return message.chat.id, message.from_user.full_name
        return None
    finally:
        await bot.session.close()


def _line(ok: bool | None, text: str) -> None:
    mark = {True: "OK ", False: "НЕТ", None: "-- "}[ok]
    print(f"{mark} {text}")


def _onedrive_roots() -> list[Path]:
    names = ("OneDrive", "OneDriveConsumer", "OneDriveCommercial")
    return [Path(os.environ[n]).resolve() for n in names if os.environ.get(n)]


def _playwright_ready() -> tuple[bool, str]:
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            path = Path(pw.chromium.executable_path)
            return path.exists(), str(path)
    except Exception as error:  # Playwright сообщает о сломанной установке разными исключениями
        return False, f"Playwright недоступен: {type(error).__name__}"


async def _probe_hosts(settings: Settings) -> list[tuple[str, bool, str, float | None]]:
    http = SafeHttp(settings.allowed_hosts(), timeout_sec=settings.request_timeout_sec)
    results = []
    try:
        for host in sorted(settings.allowed_hosts()):
            try:
                async with http.request("HEAD", f"https://{host}/") as response:
                    drift = None
                    if date_header := response.headers.get("Date"):
                        drift = (
                            datetime.now(UTC) - parsedate_to_datetime(date_header)
                        ).total_seconds()
                    results.append((host, True, f"HTTP {response.status}", drift))
            except Exception as error:  # проверка доступности: любая сетевая ошибка — «нет»
                results.append((host, False, type(error).__name__, None))
    finally:
        await http.close()
    return results


def _doctor_journal(core: Core) -> None:
    s = core.settings
    if not isinstance(core.journal, FileJournal):
        _line(None if not s.journal_enabled else False, "Журнал наблюдения на рабочем столе")
        return
    root = core.journal.root
    size = core.journal.size()
    _line(True, f"Журнал наблюдения: {root} ({size / BYTES_PER_MB:.1f} МБ)")
    _line(
        True,
        f"Квота журнала: больше {s.journal_max_gb:g} ГБ — очистка старых дней до "
        f"{s.journal_trim_to_gb:g} ГБ",
    )
    if any(root.is_relative_to(cloud) for cloud in _onedrive_roots()):
        _line(None, "Журнал лежит в OneDrive: он будет синхронизироваться в облако")


def _doctor_browsers(choice: str) -> bool:
    """Какие браузеры нашлись и какой будет использован; нет ни одного — `False`."""
    bundled_ready, where = _playwright_ready()
    found = browser_survey(choice, Path(where) if bundled_ready else None)
    for channel in (CHROME, EDGE):
        path = found.found[channel]
        _line(
            True if path else None,
            f"{BROWSER_NAMES[channel]}: {path or 'не найден'}",
        )
    _line(
        True if bundled_ready else (None if found.chosen else False),
        f"Chromium для Playwright: {where}",
    )
    if found.chosen is None:
        _line(False, f"Браузер для работы (RCBOT_BROWSER={choice}): ничего не найдено")
        print("    Установите Google Chrome (рекомендуется) или Microsoft Edge,")
        print("    либо выполните: uv run python dev.py setup --bot registro-civil-bot")
        return False
    _line(True, f"Браузер для работы: {BROWSER_NAMES[found.chosen]} (RCBOT_BROWSER={choice})")
    return True


def cmd_doctor(args: argparse.Namespace) -> int:
    with build_core(console=False, command="doctor") as core:
        return _doctor(core, full=args.full, offline=args.offline)


def _doctor(core: Core, *, full: bool, offline: bool) -> int:
    ok = True
    py = sys.version_info
    _line(py[:2] == (3, 14), f"Python {py.major}.{py.minor}.{py.micro} (нужен 3.14)")
    _line(sys.platform == "win32" or None, f"Платформа {sys.platform} (бот рассчитан на Windows)")
    for package in ("aiohttp", "aiogram", "aiogram-dialog", "sqlalchemy", "playwright", "keyring"):
        _line(True, f"{package} {importlib.metadata.version(package)}")

    ok &= _doctor_browsers(core.settings.browser)

    _line(True, f"Хранилище секретов: {core.secrets.backend_name()}")
    for key, present in core.secrets.present().items():
        required = key in REQUIRED
        ok &= present or not required
        _line(present if present or required else None, f"секрет: {DESCRIPTIONS[key]}")

    _line(core.settings.agenda_configured, "RCBOT_AGENDA_URL задан (номер этапа вместо метки)")
    ok &= core.settings.agenda_configured
    _line(bool(core.settings.tramite_id) or None, "RCBOT_TRAMITE_ID задан (номер заявки в тексте)")
    _line(bool(core.settings.api_url) or None, "RCBOT_API_URL (после разведки; пока — DomSource)")

    in_cloud = [root for root in _onedrive_roots() if core.paths.home.is_relative_to(root)]
    _line(
        not in_cloud,
        "Каталог бота вне OneDrive" if not in_cloud else f"Каталог в OneDrive: {in_cloud[0]}",
    )
    if in_cloud:
        print("    Исключите каталог из синхронизации: в нём база и логи (секретов там нет).")

    _doctor_journal(core)

    probes = [] if offline else asyncio.run(_probe_hosts(core.settings))
    for host, reachable, detail, drift in probes:
        ok &= reachable
        _line(reachable, f"{host}: {detail}")
        if drift is not None and abs(drift) > CLOCK_DRIFT_MAX_SEC:
            _line(
                False,
                f"часы ПК расходятся с {host} на {drift:+.0f} с — включите синхронизацию времени",
            )

    if full:
        audit = subprocess.run([sys.executable, "-m", "pip_audit", "--skip-editable"], check=False)
        ok &= audit.returncode == 0
        _line(audit.returncode == 0, "pip-audit: известных уязвимостей нет")

    print("\nВсё готово." if ok else "\nЕсть проблемы — см. строки «НЕТ».")
    return 0 if ok else 1
