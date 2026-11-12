"""Секреты лежат только в keyring: после работы в каталогах бота и временных ни одного значения.

Проверяется каждый байт каждого файла — включая SQLite, её WAL, лог, журнал на рабочем столе
и каталог запуска — в виде как есть, UTF-16, JSON-экранированном и URL-кодированном.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from rcbot.bootstrap import build_core, open_storage
from rcbot.config.paths import Paths
from rcbot.config.secrets import SERVICE, SecretKey, SecretStore
from rcbot.domain.models import AppEvent
from rcbot.infrastructure.logging.redaction import REDACTOR
from tests.fakes import T0
from tests.privacy.helpers import Canaries, find_in_tree, forms, sqlite_text

C = Canaries()
log = logging.getLogger("rcbot.test.disk")


def test_инструмент_поиска_находит_значение_во_всех_видах(tmp_path: Path) -> None:
    """Положительный контроль: сканер, который ничего не находит, ничего и не доказывает."""
    value = 'пароль"с\\кавычкой&='
    for index, form in enumerate(sorted(forms(value))):
        (tmp_path / f"f{index}.bin").write_bytes(b"xx" + form + b"yy")
    assert len(find_in_tree(tmp_path, [value])) == len(forms(value))
    assert find_in_tree(tmp_path, ["совсем другое значение"]) == []


def test_set_пишет_только_в_keyring_а_не_на_диск(tmp_path: Path, memory_keyring: object) -> None:
    store = SecretStore(on_read=REDACTOR.register)
    for key, value in C.secrets().items():
        store.set(key, value)
    assert find_in_tree(tmp_path, C.registered()) == []
    stored = memory_keyring.store  # type: ignore[attr-defined]
    assert {name for service, name in stored if service == SERVICE} == {k.value for k in SecretKey}


@pytest.mark.parametrize("journal_enabled", [True, False])
async def test_после_полного_сценария_каталоги_бота_и_tmp_не_содержат_секретов(
    tmp_path: Path,
    paths: Paths,
    monkeypatch: pytest.MonkeyPatch,
    journal_enabled: bool,
) -> None:
    monkeypatch.setenv("RCBOT_AGENDA_URL", f"https://bpmgob.mec.gub.uy/etapas/ejecutar/{C.etapa}/1")
    monkeypatch.setenv("RCBOT_TRAMITE_ID", C.tramite)
    monkeypatch.setenv("RCBOT_JOURNAL_ENABLED", str(journal_enabled).lower())
    store = SecretStore()
    for key, value in C.secrets().items():
        store.set(key, value)

    core = build_core(console=False, level="DEBUG", command="run")
    # то, что делает запуск: чтение всех секретов регистрирует их в фильтре
    core.secrets.credentials()
    for key in C.secrets():
        core.secrets.require(key)

    for value in C.registered():
        log.info("значение %s попало в сообщение", value)
        log.warning("и в аргумент: %s", value)
        core.journal.event("test.secret", f"в событии {value}", "WARNING", value=value)
    try:
        raise RuntimeError(f"падение с {C.password} и {C.telegram_token}")
    except RuntimeError:
        log.exception("сбой входа")
    core.journal.status({"last_error": f"вход не удался: {C.password}", "doc": C.doc_number})

    storage = await open_storage(core)
    await storage.events.write(AppEvent(T0, "WARNING", "state.backoff", "замедление"))
    await storage.kv.set("poll_interval_sec", "20")
    await storage.close()
    core.close()
    logging.shutdown()

    assert paths.log_file.exists()
    assert paths.database.exists()
    assert find_in_tree(tmp_path, C.registered()) == []
    # то же, но по строкам БД: значение могло лечь в страницу SQLite разорванным WAL-журналом
    assert not [v for v in C.registered() if v in sqlite_text(paths.database)]
