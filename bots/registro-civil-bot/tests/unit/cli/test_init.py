"""`rcbot init`: мастер секретов, тема ntfy, определение chat_id владельца (SPEC 4.3, 2.6)."""

from __future__ import annotations

import asyncio
import builtins
import getpass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar

import aiogram
import aiogram.exceptions
import aiogram.methods
import pytest
from tests.conftest import MemoryKeyring

from rcbot.cli import setup
from rcbot.config.paths import Paths
from rcbot.config.secrets import DESCRIPTIONS, REQUIRED, SecretKey, SecretStore
from rcbot.config.settings import Settings

DOC = "SYN-DOC-0000001"
PASSWORD = "syn-parol-Zx9!qL"
TOKEN = "123456789:AAH-synthetic-token-abcdefghijklmnopqrstuvwxyz0123"
SECRETS = (DOC, PASSWORD, TOKEN)


class Wizard:
    """Подставные input/getpass: отвечает по описанию секрета, запоминает вопросы."""

    def __init__(
        self,
        answers: dict[SecretKey, str],
        *,
        stage: list[str] | None = None,
        tramite: list[str] | None = None,
    ) -> None:
        self.answers = answers
        self.prompts: dict[SecretKey, str] = {}
        self.hidden: dict[SecretKey, bool] = {}
        # Вопросы про номер этапа и номер заявки: пустой ответ — «пропустить»
        self.stage = stage or []
        self.tramite = tramite or []
        self.case_questions: list[str] = []

    def _answer(self, prompt: str, hidden: bool) -> str:
        for name, queue in (("Номер этапа", self.stage), ("Номер заявки", self.tramite)):
            if prompt.startswith(name):
                self.case_questions.append(name)
                return queue.pop(0) if queue else ""
        for key, description in DESCRIPTIONS.items():
            if description in prompt:
                self.prompts[key] = prompt
                self.hidden[key] = hidden
                return self.answers.get(key, "")
        raise AssertionError(f"неожиданный вопрос мастера: {prompt!r}")

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(builtins, "input", lambda prompt="": self._answer(prompt, False))
        monkeypatch.setattr(getpass, "getpass", lambda prompt="": self._answer(prompt, True))


def update(update_id: int, chat_id: int, chat_type: str, name: str = "Тест Владелец") -> Any:
    return SimpleNamespace(
        update_id=update_id,
        message=SimpleNamespace(
            chat=SimpleNamespace(id=chat_id, type=chat_type),
            from_user=SimpleNamespace(full_name=name),
        ),
    )


class FakeBot:
    """Подставной aiogram.Bot: пачки обновлений по порядку, дальше — пустые ответы."""

    batches: ClassVar[list[list[Any]]] = []
    errors: ClassVar[list[BaseException]] = []
    instances: ClassVar[list[FakeBot]] = []
    poll_delay = 0.0

    def __init__(self, token: str) -> None:
        self.token = token
        self.webhook_deleted: dict[str, Any] | None = None
        self.get_updates_calls: list[dict[str, Any]] = []
        self.session = SimpleNamespace(closed=False, close=self._close_session)
        FakeBot.instances.append(self)

    async def _close_session(self) -> None:
        self.session.closed = True

    async def delete_webhook(self, **kwargs: Any) -> None:
        self.webhook_deleted = kwargs
        if FakeBot.errors:
            raise FakeBot.errors[0]

    async def get_updates(self, **kwargs: Any) -> list[Any]:
        self.get_updates_calls.append(kwargs)
        await asyncio.sleep(FakeBot.poll_delay)
        if FakeBot.errors:
            raise FakeBot.errors[0]
        return FakeBot.batches.pop(0) if FakeBot.batches else []


@pytest.fixture
def fake_bot(monkeypatch: pytest.MonkeyPatch) -> type[FakeBot]:
    FakeBot.batches = []
    FakeBot.errors = []
    FakeBot.instances = []
    FakeBot.poll_delay = 0.0
    monkeypatch.setattr(aiogram, "Bot", FakeBot)
    monkeypatch.setattr(setup, "CHAT_ID_WAIT_SEC", 0.3)
    return FakeBot


FULL = {
    SecretKey.DOC_NUMBER: DOC,
    SecretKey.PASSWORD: PASSWORD,
    SecretKey.TELEGRAM_TOKEN: TOKEN,
    SecretKey.CALLMEBOT_USER: "owner_name",
}


def run_init(capsys: pytest.CaptureFixture[str]) -> tuple[int, str]:
    code = setup.cmd_init(None)  # type: ignore[arg-type]
    return code, capsys.readouterr().out


def stored(store: SecretStore, key: SecretKey) -> str | None:
    value = store.get(key)
    return None if value is None else value.get_secret_value()


@pytest.mark.usefixtures("env_example")
def test_первый_запуск_кладёт_секреты_в_keyring_и_определяет_владельца(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    paths: Paths,
    fake_bot: type[FakeBot],
    memory_keyring: MemoryKeyring,
) -> None:
    wizard = Wizard(FULL)
    wizard.install(monkeypatch)
    fake_bot.batches = [[update(10, 777, "private")]]

    code, out = run_init(capsys)

    assert code == 0
    store = SecretStore()
    assert stored(store, SecretKey.DOC_NUMBER) == DOC
    assert stored(store, SecretKey.PASSWORD) == PASSWORD
    assert stored(store, SecretKey.TELEGRAM_TOKEN) == TOKEN
    assert stored(store, SecretKey.TELEGRAM_CHAT_ID) == "777"
    assert "Владелец определён: Тест Владелец" in out
    assert paths.env_file.exists()
    assert "Создан" in out
    # Все пароли и токены вводятся скрыто, имя для звонка — открыто
    assert wizard.hidden[SecretKey.DOC_NUMBER] is True
    assert wizard.hidden[SecretKey.PASSWORD] is True
    assert wizard.hidden[SecretKey.TELEGRAM_TOKEN] is True
    assert wizard.hidden[SecretKey.CALLMEBOT_USER] is False
    assert fake_bot.instances[0].token == TOKEN
    assert fake_bot.instances[0].session.closed is True


@pytest.mark.usefixtures("env_example")
def test_секреты_не_печатаются_и_не_ложатся_в_файлы(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    bot_home: Path,
    tmp_path: Path,
    fake_bot: type[FakeBot],
) -> None:
    Wizard(FULL).install(monkeypatch)
    fake_bot.batches = [[update(10, 777, "private")]]

    code, out = run_init(capsys)
    captured_err = capsys.readouterr().err

    assert code == 0
    topic = SecretStore().get(SecretKey.NTFY_TOPIC)
    assert topic is not None
    assert out.count(topic.get_secret_value()) == 1  # тему нужно подписать — по SPEC она печатается
    for secret in (*SECRETS, "777"):
        assert secret not in out
        assert secret not in captured_err
    # Каталог бота и журнал наблюдения: секретов на диске нет ни в именах, ни в содержимом
    leaked = []
    for root in (bot_home, tmp_path / "journal"):
        for file in root.rglob("*"):
            if not file.is_file():
                continue
            blob = file.read_bytes().decode("utf-8", errors="replace") + file.name
            leaked += [(file.name, s) for s in (*SECRETS, topic.get_secret_value()) if s in blob]
    assert leaked == []


@pytest.mark.usefixtures("env_example")
def test_тема_ntfy_создаётся_один_раз_и_достаточно_длинная(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fake_bot: type[FakeBot],
) -> None:
    Wizard(FULL).install(monkeypatch)
    fake_bot.batches = [[update(1, 5, "private")]]
    assert run_init(capsys)[0] == 0
    first = stored(SecretStore(), SecretKey.NTFY_TOPIC)
    assert first is not None
    assert len(first) >= 32
    assert first.isascii()

    Wizard({}).install(monkeypatch)
    code, out = run_init(capsys)

    assert code == 0
    assert stored(SecretStore(), SecretKey.NTFY_TOPIC) == first
    assert first not in out  # повторный запуск тему заново не показывает


@pytest.mark.usefixtures("env_example")
def test_две_установки_дают_разные_темы(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fake_bot: type[FakeBot],
    memory_keyring: MemoryKeyring,
) -> None:
    topics = []
    for _ in range(2):
        memory_keyring.store.clear()
        Wizard(FULL).install(monkeypatch)
        fake_bot.batches = [[update(1, 5, "private")]]
        run_init(capsys)
        topics.append(stored(SecretStore(), SecretKey.NTFY_TOPIC))
    assert topics[0] != topics[1]


@pytest.mark.usefixtures("env_example")
def test_enter_оставляет_существующее_и_не_спрашивает_chat_id_заново(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fake_bot: type[FakeBot],
) -> None:
    Wizard(FULL).install(monkeypatch)
    fake_bot.batches = [[update(1, 5, "private")]]
    run_init(capsys)
    fake_bot.instances.clear()

    again = Wizard({})  # на все вопросы — пустой Enter
    again.install(monkeypatch)
    code, out = run_init(capsys)

    assert code == 0
    store = SecretStore()
    assert stored(store, SecretKey.DOC_NUMBER) == DOC
    assert stored(store, SecretKey.PASSWORD) == PASSWORD
    assert stored(store, SecretKey.TELEGRAM_TOKEN) == TOKEN
    assert stored(store, SecretKey.CALLMEBOT_USER) == "@owner_name"
    assert stored(store, SecretKey.TELEGRAM_CHAT_ID) == "5"
    assert all("Enter — оставить как есть" in p for p in again.prompts.values())
    assert fake_bot.instances == []  # chat_id уже известен: к Telegram не ходим
    assert "Пропущено" not in out


@pytest.mark.usefixtures("env_example")
def test_подсказка_про_enter_только_у_существующих_секретов(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], fake_bot: type[FakeBot]
) -> None:
    SecretStore().set(SecretKey.PASSWORD, PASSWORD)
    wizard = Wizard({})
    wizard.install(monkeypatch)
    run_init(capsys)
    assert "Enter — оставить как есть" in wizard.prompts[SecretKey.PASSWORD]
    assert "Enter" not in wizard.prompts[SecretKey.DOC_NUMBER]


@pytest.mark.usefixtures("env_example")
def test_новое_значение_заменяет_старое(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], fake_bot: type[FakeBot]
) -> None:
    SecretStore().set(SecretKey.PASSWORD, "старый-пароль-1")
    Wizard({SecretKey.PASSWORD: "  новый-пароль-2  "}).install(monkeypatch)
    run_init(capsys)
    assert stored(SecretStore(), SecretKey.PASSWORD) == "новый-пароль-2"


@pytest.mark.parametrize(
    ("typed", "saved"),
    [("owner_name", "@owner_name"), ("@owner_name", "@owner_name"), ("  name  ", "@name")],
)
@pytest.mark.usefixtures("env_example")
def test_к_имени_callmebot_добавляется_собака(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fake_bot: type[FakeBot],
    typed: str,
    saved: str,
) -> None:
    Wizard({SecretKey.CALLMEBOT_USER: typed}).install(monkeypatch)
    run_init(capsys)
    assert stored(SecretStore(), SecretKey.CALLMEBOT_USER) == saved


@pytest.mark.usefixtures("env_example")
def test_пропущенные_обязательные_секреты_называются_и_init_не_считается_законченным(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], fake_bot: type[FakeBot]
) -> None:
    Wizard({SecretKey.CALLMEBOT_USER: "owner"}).install(monkeypatch)

    code, out = run_init(capsys)

    for key in (SecretKey.DOC_NUMBER, SecretKey.PASSWORD, SecretKey.TELEGRAM_TOKEN):
        assert f"Пропущено обязательное: {DESCRIPTIONS[key]}" in out
    # Токена нет — владельца определить нечем: Telegram не трогаем
    assert fake_bot.instances == []
    assert code == 1
    assert "Готово" not in out
    assert stored(SecretStore(), SecretKey.TELEGRAM_CHAT_ID) is None


@pytest.mark.usefixtures("env_example")
def test_необязательный_callmebot_можно_пропустить(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], fake_bot: type[FakeBot]
) -> None:
    Wizard({k: v for k, v in FULL.items() if k is not SecretKey.CALLMEBOT_USER}).install(
        monkeypatch
    )
    fake_bot.batches = [[update(1, 5, "private")]]
    code, out = run_init(capsys)
    assert code == 0
    assert "Пропущено" not in out
    assert stored(SecretStore(), SecretKey.CALLMEBOT_USER) is None
    assert all(stored(SecretStore(), key) for key in REQUIRED)


@pytest.mark.usefixtures("env_example")
def test_env_создаётся_из_примера_и_существующий_не_перезаписывается(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    paths: Paths,
    bot_home: Path,
    fake_bot: type[FakeBot],
) -> None:
    Wizard({}).install(monkeypatch)
    run_init(capsys)
    assert paths.env_file.read_text(encoding="utf-8") == (bot_home / ".env.example").read_text(
        encoding="utf-8"
    )

    paths.env_file.write_text("RCBOT_MONTHS_AHEAD=3\n", encoding="utf-8")
    run_init(capsys)
    assert paths.env_file.read_text(encoding="utf-8") == "RCBOT_MONTHS_AHEAD=3\n"


@pytest.mark.usefixtures("env_example")
def test_групповые_сообщения_игнорируются_и_апдейты_подтверждаются(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fake_bot: type[FakeBot],
) -> None:
    Wizard(FULL).install(monkeypatch)
    fake_bot.batches = [
        [update(10, -1001, "group", "Чужая группа"), update(11, -1002, "supergroup")],
        [update(12, 555, "private", "Владелец")],
    ]

    code, out = run_init(capsys)

    assert code == 0
    assert stored(SecretStore(), SecretKey.TELEGRAM_CHAT_ID) == "555"
    assert "Чужая группа" not in out
    calls = fake_bot.instances[0].get_updates_calls
    # Первый запрос — без offset; после группового апдейта 11 ждём с 12
    assert calls[0]["offset"] is None
    assert calls[1]["offset"] == 12
    # Найдя владельца, подтверждаем его апдейт (offset=13), чтобы /start не пришёл боту повторно
    assert calls[-1] == {"offset": 13, "timeout": 0}
    assert all(c.get("allowed_updates", ["message"]) == ["message"] for c in calls[:-1])


@pytest.mark.usefixtures("env_example")
def test_сообщение_без_отправителя_пропускается(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], fake_bot: type[FakeBot]
) -> None:
    Wizard(FULL).install(monkeypatch)
    anonymous = update(10, 99, "private")
    anonymous.message.from_user = None
    no_message = SimpleNamespace(update_id=11, message=None)
    fake_bot.batches = [[anonymous, no_message], [update(12, 555, "private")]]
    assert run_init(capsys)[0] == 0
    assert stored(SecretStore(), SecretKey.TELEGRAM_CHAT_ID) == "555"


@pytest.mark.usefixtures("env_example")
def test_вебхук_снимается_без_потери_ожидающих_обновлений(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], fake_bot: type[FakeBot]
) -> None:
    Wizard(FULL).install(monkeypatch)
    fake_bot.batches = [[update(1, 5, "private")]]
    run_init(capsys)
    assert fake_bot.instances[0].webhook_deleted == {"drop_pending_updates": False}


@pytest.mark.usefixtures("env_example")
def test_таймаут_ожидания_владельца_даёт_код_1_и_chat_id_не_сохраняется(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], fake_bot: type[FakeBot]
) -> None:
    Wizard(FULL).install(monkeypatch)
    fake_bot.poll_delay = 0.02
    fake_bot.batches = [[update(1, -100, "group")]]

    code, out = run_init(capsys)

    assert code == 1
    assert "Сообщение не пришло" in out
    assert "Готово" not in out
    assert stored(SecretStore(), SecretKey.TELEGRAM_CHAT_ID) is None
    assert fake_bot.instances[0].session.closed is True
    # Остальные секреты уже сохранены: повторный запуск не заставит вводить всё заново
    assert stored(SecretStore(), SecretKey.TELEGRAM_TOKEN) == TOKEN


@pytest.mark.parametrize(
    "error",
    [
        aiogram.exceptions.TelegramUnauthorizedError(aiogram.methods.GetUpdates(), "Unauthorized"),
        aiogram.exceptions.TelegramNetworkError(aiogram.methods.GetUpdates(), "нет сети"),
    ],
    ids=["токен-отклонён", "сеть-недоступна"],
)
@pytest.mark.usefixtures("env_example")
def test_сбой_telegram_при_определении_владельца_объясняется_а_не_падает_трассировкой(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fake_bot: type[FakeBot],
    error: BaseException,
) -> None:
    Wizard(FULL).install(monkeypatch)
    fake_bot.errors = [error]

    code, out = run_init(capsys)

    assert code == 1
    assert type(error).__name__ in out
    assert "rcbot init" in out
    assert TOKEN not in out
    assert fake_bot.instances[0].session.closed is True


def test_нужные_ключи_покрыты_описаниями() -> None:
    """Мастер спрашивает по описанию: каждый ключ обязан его иметь (иначе KeyError на вводе)."""
    assert set(DESCRIPTIONS) == set(SecretKey)


# --- номер этапа и номер заявки: проверка ввода и запись в локальный .env ---

STAGE = "4242424"
TRAMITE = "3030303"
STAGE_URL = f"https://bpmgob.mec.gub.uy/etapas/ejecutar/{STAGE}/1"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (STAGE, STAGE_URL),
        (f"  {STAGE}  ", STAGE_URL),
        (STAGE_URL, STAGE_URL),
        (f"https://bpmgob.mec.gub.uy/etapas/ejecutar/{STAGE}/3/", STAGE_URL),  # шаг приводится к 1
    ],
)
def test_parse_stage_принимает_номер_и_адрес(text: str, expected: str) -> None:
    assert setup.parse_stage(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "12",  # слишком короткий
        "1234567890123456",  # слишком длинный
        "12ab34",
        f"http://bpmgob.mec.gub.uy/etapas/ejecutar/{STAGE}/1",  # не https
        f"https://evil.example/etapas/ejecutar/{STAGE}/1",  # чужой хост
        f"https://bpmgob.mec.gub.uy.evil.example/etapas/ejecutar/{STAGE}/1",
        f"https://bpmgob.mec.gub.uy/etapas/ejecutar/{STAGE}/1?x=1",  # лишнее в адресе
        f"https://bpmgob.mec.gub.uy/etapas/ejecutar/abc/1",  # noqa: F541 — не число
    ],
)
def test_parse_stage_отклоняет_лишнее(text: str) -> None:
    assert setup.parse_stage(text) is None


def test_parse_tramite_только_цифры() -> None:
    assert setup.parse_tramite(f" {TRAMITE} ") == TRAMITE
    for bad in ("", "12", "12ab", "<НОМЕР_ЗАЯВКИ>", "1" * 16):
        assert setup.parse_tramite(bad) is None


def test_set_env_value_меняет_строку_и_не_трогает_остальное(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("# заметка\nRCBOT_A=1\nRCBOT_AGENDA_URL=старое\nRCBOT_B=2", encoding="utf-8")
    setup.set_env_value(env, "RCBOT_AGENDA_URL", "новое")
    assert (
        env.read_text(encoding="utf-8")
        == "# заметка\nRCBOT_A=1\nRCBOT_AGENDA_URL=новое\nRCBOT_B=2\n"
    )


def test_set_env_value_дописывает_отсутствующий_ключ_и_создаёт_файл(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    setup.set_env_value(env, "RCBOT_X", "1")
    setup.set_env_value(env, "RCBOT_Y", "2")
    assert env.read_text(encoding="utf-8") == "RCBOT_X=1\nRCBOT_Y=2\n"


@pytest.mark.usefixtures("env_example")
def test_init_записывает_этап_и_заявку_в_env_и_настройки_их_читают(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    paths: Paths,
    fake_bot: type[FakeBot],
    memory_keyring: MemoryKeyring,
) -> None:
    Wizard(FULL, stage=[STAGE], tramite=[TRAMITE]).install(monkeypatch)
    fake_bot.batches = [[update(10, 777, "private")]]

    code, out = run_init(capsys)

    assert code == 0
    lines = paths.env_file.read_text(encoding="utf-8").splitlines()
    assert f"RCBOT_AGENDA_URL={STAGE_URL}" in lines
    assert f"RCBOT_TRAMITE_ID={TRAMITE}" in lines
    assert "<НОМЕР_ЭТАПА>" not in paths.env_file.read_text(encoding="utf-8")
    assert STAGE not in out and TRAMITE not in out  # номера в консоль не повторяются
    settings = Settings(_env_file=paths.env_file)  # type: ignore[call-arg]
    assert settings.agenda_url == STAGE_URL and settings.tramite_id == TRAMITE
    assert settings.agenda_configured


@pytest.mark.usefixtures("env_example")
def test_init_пустой_ответ_пропускает_и_оставляет_метки(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    paths: Paths,
    fake_bot: type[FakeBot],
    memory_keyring: MemoryKeyring,
) -> None:
    wizard = Wizard(FULL)
    wizard.install(monkeypatch)
    fake_bot.batches = [[update(10, 777, "private")]]

    code, _ = run_init(capsys)

    assert code == 0
    assert wizard.case_questions == ["Номер этапа", "Номер заявки"]
    text = paths.env_file.read_text(encoding="utf-8")
    assert "<НОМЕР_ЭТАПА>" in text and "<НОМЕР_ЗАЯВКИ>" in text


@pytest.mark.usefixtures("env_example")
def test_init_неверный_ввод_трижды_ничего_не_пишет(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    paths: Paths,
    fake_bot: type[FakeBot],
    memory_keyring: MemoryKeyring,
) -> None:
    wizard = Wizard(FULL, stage=["abc", "12", "https://evil.example/x"], tramite=["x"] * 3)
    wizard.install(monkeypatch)
    fake_bot.batches = [[update(10, 777, "private")]]

    code, out = run_init(capsys)

    assert code == 0
    assert wizard.case_questions.count("Номер этапа") == setup.ASK_ATTEMPTS
    assert out.count("Не подошло") == 2 * setup.ASK_ATTEMPTS
    assert "<НОМЕР_ЭТАПА>" in paths.env_file.read_text(encoding="utf-8")


@pytest.mark.usefixtures("env_example")
def test_init_одна_ошибка_и_исправление_принимаются(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    paths: Paths,
    fake_bot: type[FakeBot],
    memory_keyring: MemoryKeyring,
) -> None:
    Wizard(FULL, stage=["не число", STAGE]).install(monkeypatch)
    fake_bot.batches = [[update(10, 777, "private")]]
    assert run_init(capsys)[0] == 0
    assert f"RCBOT_AGENDA_URL={STAGE_URL}" in paths.env_file.read_text(encoding="utf-8")


def test_init_не_спрашивает_то_что_уже_задано(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    paths: Paths,
    fake_bot: type[FakeBot],
    memory_keyring: MemoryKeyring,
) -> None:
    paths.env_file.write_text(
        f"RCBOT_AGENDA_URL={STAGE_URL}\nRCBOT_TRAMITE_ID={TRAMITE}\n", encoding="utf-8"
    )
    wizard = Wizard(FULL)
    wizard.install(monkeypatch)
    fake_bot.batches = [[update(10, 777, "private")]]

    assert run_init(capsys)[0] == 0
    assert wizard.case_questions == []
    assert paths.env_file.read_text(encoding="utf-8").count(STAGE) == 1
