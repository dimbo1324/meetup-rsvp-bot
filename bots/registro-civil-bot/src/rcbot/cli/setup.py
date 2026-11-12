"""Команды подготовки: `rcbot init` (секреты, тема ntfy, chat_id) и `rcbot doctor`."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import importlib.metadata
import os
import secrets
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path

from aiogram.exceptions import TelegramAPIError

from rcbot.bootstrap import Core, build_core
from rcbot.config.secrets import DESCRIPTIONS, REQUIRED, SecretKey
from rcbot.config.settings import Settings
from rcbot.infrastructure.http.client import SafeHttp
from rcbot.infrastructure.journal.files import FileJournal

CHAT_ID_WAIT_SEC = 180


def _ask(key: SecretKey, *, hidden: bool, existing: bool) -> str:
    hint = " (Enter — оставить как есть)" if existing else ""
    prompt = f"{DESCRIPTIONS[key]}{hint}: "
    return (getpass.getpass(prompt) if hidden else input(prompt)).strip()


def cmd_init(_: argparse.Namespace) -> int:
    core = build_core(command="init")
    if not core.paths.env_file.exists():
        example = core.paths.home / ".env.example"
        shutil.copyfile(example, core.paths.env_file)
        print(f"Создан {core.paths.env_file} — проверьте RCBOT_AGENDA_URL и RCBOT_TRAMITE_ID.")
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
            if key is SecretKey.CALLMEBOT_USER and not value.startswith("@"):
                value = f"@{value}"
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
        print(f"Напишите своему боту /start в Telegram — жду до {CHAT_ID_WAIT_SEC} секунд…")
        try:
            chat = asyncio.run(_wait_owner_chat(token.get_secret_value()))
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
        core.secrets.set(SecretKey.TELEGRAM_CHAT_ID, str(chat_id))
        print(f"Владелец определён: {name}. Бот будет отвечать только ему.")
    if missing := [k for k in REQUIRED if not core.secrets.present()[k]]:
        print(f"\nНе хватает обязательного ({len(missing)}): запустите `rcbot init` ещё раз.")
        return 1
    print("\nГотово. Дальше: `rcbot doctor`.")
    return 0


async def _wait_owner_chat(token: str) -> tuple[int, str] | None:
    from aiogram import Bot

    bot = Bot(token)
    try:
        await bot.delete_webhook(drop_pending_updates=False)
        offset: int | None = None
        deadline = asyncio.get_running_loop().time() + CHAT_ID_WAIT_SEC
        while asyncio.get_running_loop().time() < deadline:
            updates = await bot.get_updates(offset=offset, timeout=20, allowed_updates=["message"])
            for update in updates:
                offset = update.update_id + 1
                message = update.message
                if message and message.chat.type == "private" and message.from_user:
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
    _line(True, f"Журнал наблюдения: {root} ({size / 2**20:.1f} МБ)")
    _line(
        True,
        f"Квота журнала: больше {s.journal_max_gb:g} ГБ — очистка старых дней до "
        f"{s.journal_trim_to_gb:g} ГБ",
    )
    if any(root.is_relative_to(cloud) for cloud in _onedrive_roots()):
        _line(None, "Журнал лежит в OneDrive: он будет синхронизироваться в облако")


def cmd_doctor(args: argparse.Namespace) -> int:
    core = build_core(console=False, command="doctor")
    ok = True
    py = sys.version_info
    _line(py[:2] == (3, 14), f"Python {py.major}.{py.minor}.{py.micro} (нужен 3.14)")
    _line(sys.platform == "win32" or None, f"Платформа {sys.platform} (бот рассчитан на Windows)")
    for package in ("aiohttp", "aiogram", "aiogram-dialog", "sqlalchemy", "playwright", "keyring"):
        _line(True, f"{package} {importlib.metadata.version(package)}")

    ready, where = _playwright_ready()
    ok &= ready
    _line(ready, f"Chromium для Playwright: {where}")
    if not ready:
        print("    Установка: uv run python dev.py setup --bot registro-civil-bot")

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

    probes = [] if args.offline else asyncio.run(_probe_hosts(core.settings))
    for host, reachable, detail, drift in probes:
        ok &= reachable
        _line(reachable, f"{host}: {detail}")
        if drift is not None and abs(drift) > 30:
            _line(
                False,
                f"часы ПК расходятся с {host} на {drift:+.0f} с — включите синхронизацию времени",
            )

    if args.full:
        audit = subprocess.run([sys.executable, "-m", "pip_audit", "--skip-editable"], check=False)
        ok &= audit.returncode == 0
        _line(audit.returncode == 0, "pip-audit: известных уязвимостей нет")

    print("\nВсё готово." if ok else "\nЕсть проблемы — см. строки «НЕТ».")
    return 0 if ok else 1
