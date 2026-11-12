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

DOC = "SYN-DOC-0000001"
PASSWORD = "syn-parol-Zx9!qL"
TOKEN = "123456789:AAH-synthetic-token-abcdefghijklmnopqrstuvwxyz0123"
SECRETS = (DOC, PASSWORD, TOKEN)


class Wizard:
    """Подставные input/getpass: отвечает по описанию секрета, запоминает вопросы."""

    def __init__(self, answers: dict[SecretKey, str]) -> None:
        self.answers = answers
        self.prompts: dict[SecretKey, str] = {}
        self.hidden: dict[SecretKey, bool] = {}

    def _answer(self, prompt: str, hidden: bool) -> str:
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
