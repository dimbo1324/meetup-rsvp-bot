"""Номера владельца не в репозитории (решение R1, инварианты 6 и 9): проверка по ШАБЛОНАМ.

В тесте нет ни одного настоящего числа: он ищет форму («адрес этапа с цифрами», «заявка рядом
с шестизначным числом»), а не конкретные значения. Положительный контроль — на синтетических
«плохих» файлах, которые собираются в памяти (чтобы сам тест шаблонов не содержал).
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

BOT_DIR = Path(__file__).resolve().parents[2]

STAGE_URL = re.compile(r"/etapas/ejecutar/\d+")
ENV_TRAMITE = re.compile(r"RCBOT_TRAMITE_ID\s*[=:]\s*[\"']?\d+")
ENV_AGENDA = re.compile(r"RCBOT_AGENDA_URL\s*[=:][^\n]*ejecutar/\d+")
WORDS = r"(?:заявк\w*|tr[aá]mite\w*|этап\w*|etapa\w*)"
NEAR_NUMBER = re.compile(
    rf"{WORDS}[^\n\d]{{0,30}}\d{{6,}}|\d{{6,}}[^\n\d]{{0,30}}{WORDS}", re.IGNORECASE
)
# Форма токена Telegram и закрытого ключа: настоящему секрету в репозитории не место
# Явная подделка помечается словом «synthetic» в значении: допуск по маркеру, а не «всё подряд»
TOKEN_SHAPE = re.compile(r"\b\d{8,10}:(?![A-Za-z0-9_-]*(?i:synthetic))[A-Za-z0-9_-]{30,}")
PRIVATE_KEY = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")

CHECKS = {
    "адрес этапа с цифрами": STAGE_URL,
    "RCBOT_TRAMITE_ID с цифрами": ENV_TRAMITE,
    "RCBOT_AGENDA_URL с цифрами": ENV_AGENDA,
    "заявка/этап рядом с длинным числом": NEAR_NUMBER,
    "токен Telegram": TOKEN_SHAPE,
    "закрытый ключ": PRIVATE_KEY,
}
SKIP_NAMES = {"uv.lock"}
# Каталоги и файлы с локальными данными: в git не идут (R2, SPEC 2.7)
LOCAL_ONLY = re.compile(
    r"(^|/)(\.env|data|logs|\.browsers|\.cache|rcbot-journal)(/|$)|\.(sqlite3|sqlite3-wal|db|log)$"
)


def scan(texts: dict[str, str]) -> list[str]:
    found = []
    for name, text in texts.items():
        for label, pattern in CHECKS.items():
            if match := pattern.search(text):
                line = text.count("\n", 0, match.start()) + 1
                found.append(f"{name}:{line}: {label}")
    return found


def tracked_paths() -> list[str]:
    git = shutil.which("git")
    if git is None:
        pytest.skip("git не найден: проверка отслеживаемых файлов пропущена")
    result = subprocess.run(
        [git, "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=BOT_DIR,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.skip("каталог бота не в репозитории git")
    return [p for p in result.stdout.decode("utf-8").split("\0") if p]


def read_texts(names: list[str]) -> dict[str, str]:
    texts = {}
    for name in names:
        path = BOT_DIR / name
        if Path(name).name in SKIP_NAMES or not path.is_file():
            continue
        try:
            texts[name] = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue  # бинарный файл
    return texts


def test_в_отслеживаемых_файлах_нет_номеров_заявки_и_этапа_и_секретов() -> None:
    names = tracked_paths()
    assert len(names) > 20, "git вернул слишком мало файлов: проверка ничего не проверила бы"
    assert scan(read_texts(names)) == []


def test_локальные_данные_не_отслеживаются_git() -> None:
    assert [n for n in tracked_paths() if LOCAL_ONLY.search(n.replace("\\", "/"))] == []


def test_в_документах_и_примере_настроек_стоят_метки_а_не_номера() -> None:
    texts = read_texts(["SPEC.md", ".env.example", "docs/open-questions.md"])
    assert "<НОМЕР_ЭТАПА>" in texts["SPEC.md"]
    assert "<НОМЕР_ЗАЯВКИ>" in texts["SPEC.md"]
    example = texts[".env.example"]
    assert re.search(r"^RCBOT_TRAMITE_ID=<НОМЕР_ЗАЯВКИ>$", example, re.MULTILINE)
    assert re.search(r"^RCBOT_AGENDA_URL=\S*ejecutar/<НОМЕР_ЭТАПА>/1$", example, re.MULTILINE)


def test_каждый_адрес_этапа_в_отслеживаемых_файлах_с_меткой_или_условным_именем() -> None:
    """Допустимо только то, что не цифры: метка `<НОМЕР_ЭТАПА>`, `TEST-…`, подстановка `{…}`."""
    bad = []
    for name, text in read_texts(tracked_paths()).items():
        for match in re.finditer(r"/etapas/ejecutar/([^/\s\"')]+)", text):
            token = match.group(1)
            # "\\" и "(" — это шаблон регулярного выражения (в самом тесте), а не адрес
            if not token.startswith(("<", "TEST", "{", "%", "$", "\\", "(")):
                bad.append(f"{name}: {token[:6]}…")
    assert bad == []


# --- положительный контроль: шаблоны видят плохое ---

DIGITS = "4" * 7


@pytest.mark.parametrize(
    ("label", "text"),
    [
        ("адрес этапа с цифрами", "см. https://bpmgob.mec.gub.uy/etapas/ejecutar/" + DIGITS + "/1"),
        ("адрес этапа с цифрами", "/etapas/ejecutar/" + "5"),
        ("RCBOT_TRAMITE_ID с цифрами", "RCBOT_TRAMITE_ID=" + DIGITS),
        ("RCBOT_TRAMITE_ID с цифрами", 'RCBOT_TRAMITE_ID = "' + DIGITS + '"'),
        ("RCBOT_AGENDA_URL с цифрами", "RCBOT_AGENDA_URL=https://x/etapas/ejecutar/" + DIGITS),
        ("заявка/этап рядом с длинным числом", "Заявка " + DIGITS + " стоит на шаге 2"),
        ("заявка/этап рядом с длинным числом", "заявка № " + "9" * 6),
        ("заявка/этап рядом с длинным числом", "tramite: " + DIGITS),
        ("заявка/этап рядом с длинным числом", "Trámite " + DIGITS),
        ("заявка/этап рядом с длинным числом", DIGITS + " — номер этапа"),
        ("заявка/этап рядом с длинным числом", "этап " + DIGITS),
        ("токен Telegram", "TOKEN=" + "1" * 9 + ":" + "A" * 35),
        ("закрытый ключ", "-----BEGIN " + "RSA PRIVATE KEY-----"),
    ],
)
def test_шаблоны_ловят_плохие_файлы(label: str, text: str) -> None:
    found = scan({"bad.md": text})
    assert found, f"{label}: не поймано"
    assert found[0].startswith("bad.md:1:")


@pytest.mark.parametrize(
    "text",
    [
        "Заявка <НОМЕР_ЗАЯВКИ> → «Realizar» → шаг «Agenda»",
        "https://bpmgob.mec.gub.uy/etapas/ejecutar/<НОМЕР_ЭТАПА>/1",
        "AGENDA_URL = 'https://bpmgob.mec.gub.uy/etapas/ejecutar/TEST-ETAPA/1'",
        "Oficina Tramite Extranjeros, Av. Uruguay 933",
        "код услуги 8942: /tramites/iniciar/8942",
        "Заявка стоит на шаге 2, этапов 3",
        "RCBOT_TRAMITE_ID=<НОМЕР_ЗАЯВКИ>",
        "бот 123456:ABC",
        "TOKEN = " + "1" * 9 + ":AAH-synthetic-token-abcdefghijklmnopqrstuvwxyz0123",
    ],
)
def test_шаблоны_не_ругаются_на_метки_и_короткие_числа(text: str) -> None:
    assert scan({"ok.md": text}) == []


def test_сам_тест_не_содержит_настоящих_чисел() -> None:
    """Файл теста проходит те же шаблоны: числа в нём собираются из повторов, а не пишутся."""
    me = Path(__file__)
    assert scan({me.name: me.read_text(encoding="utf-8")}) == []
