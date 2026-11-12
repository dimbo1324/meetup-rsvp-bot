"""Общие помощники тестов приватности: придуманные «канарейки» и просмотр каждого байта диска.

Все значения выдуманы и уникальны: найти такую строку в любом файле, логе или сообщении канала
— значит утечка. Настоящих номеров, паролей и токенов здесь нет.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, quote_plus

from rcbot.config.secrets import SecretKey

# Номера заявки и этапа — выдуманные; формат (цифры) совпадает с настоящим
# Числа собраны из частей: файл с канарейками сам проходит шаблоны test_repo_privacy.py
NUM_APPLICATION = "731" + "0045"
NUM_STAGE = "912" + "0786"


@dataclass(frozen=True)
class Canaries:
    doc_number: str = "KQ7418239650"
    password: str = "Hx9!bR4qLm2#Canary"
    telegram_token: str = "246813579" + ":" + "CanaryTok_ABCdefGHIjklMNOpqrSTUvwx-12"
    chat_id: str = "5550142398"
    ntfy_topic: str = "rcbot-canaryTopicQ8w3Zk1Lp7Vn4Xc2Bm6Yt9Rd5Hs0Jf"
    call_user: str = "@canary_user_zq81"
    cookie_value: str = "kZ4pQ8wX2vNcanaryCOOKIE7mL1sB6tY"
    tramite: str = NUM_APPLICATION
    etapa: str = NUM_STAGE

    def secrets(self) -> dict[SecretKey, str]:
        return {
            SecretKey.DOC_NUMBER: self.doc_number,
            SecretKey.PASSWORD: self.password,
            SecretKey.TELEGRAM_TOKEN: self.telegram_token,
            SecretKey.TELEGRAM_CHAT_ID: self.chat_id,
            SecretKey.NTFY_TOPIC: self.ntfy_topic,
            SecretKey.CALLMEBOT_USER: self.call_user,
        }

    def registered(self) -> list[str]:
        """Секреты из keyring, номера заявки и этапа: запуск регистрирует их в фильтре."""
        return [*self.secrets().values(), self.tramite, self.etapa]

    def all_values(self) -> list[str]:
        return [*self.registered(), self.cookie_value]


def forms(value: str) -> set[bytes]:
    """Значение в тех видах, в каких оно может лежать в файле: как есть, UTF-16, JSON, URL."""
    texts = {
        value,
        json.dumps(value)[1:-1],
        json.dumps(value, ensure_ascii=False)[1:-1],
        quote(value, safe=""),
        quote_plus(value),
    }
    result = {t.encode("utf-8") for t in texts}
    result.add(value.encode("utf-16-le"))
    return result


def find_in_tree(root: Path, values: list[str]) -> list[str]:
    """Каждый байт каждого файла под `root`: где нашлось какое значение. Пусто — чисто."""
    found = []
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        data = path.read_bytes()
        for value in values:
            if any(form in data for form in forms(value)):
                found.append(f"{path.relative_to(root)}: {value[:12]}…")
    return found


def sqlite_text(path: Path) -> str:
    """Все строковые значения всех таблиц БД: поиск по смыслу, а не только по байтам файла."""
    chunks: list[str] = []
    with sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True) as conn:
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        for table in tables:
            for row in conn.execute(f'SELECT * FROM "{table}"'):
                chunks.extend(str(cell) for cell in row)
    return "\n".join(chunks)
