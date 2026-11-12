"""PlaywrightAuthenticator на подставных страницах ID Uruguay (SPEC 1.3, 2.3, 2.6).

Вход идёт по шагам из SPEC 1.3 в настоящем Chromium; сайт — локальный сервер (site.py).
Секреты синтетические; тест проверяет, что их нет ни в логах, ни в исключениях.
"""

from __future__ import annotations

import asyncio
import json
import logging
import traceback
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field

import pytest
from pydantic import SecretStr
from tests.integration.gubuy.site import (
    BPM_HOST,
    HOME_PAGE,
    ID_HOST,
    LOGIN_BPM,
    LOGIN_ID,
    LOGIN_ID_RETURN,
    FakeSite,
    Seen,
    id_uruguay_page,
    routed_launch,
    warm_up_chromium,
)

from rcbot.config.secrets import Credentials, SecretMissing
from rcbot.domain.errors import LoginFailed, RateLimited, SiteUnavailable
from rcbot.infrastructure.gubuy import auth, browser
from rcbot.infrastructure.gubuy.auth import PlaywrightAuthenticator
from rcbot.infrastructure.gubuy.browser import BrowserOptions

pytestmark = pytest.mark.browser

DOC_NUMBER = "TEST-DOC-0001"
PASSWORD = "TEST-PASSWORD-0002"
SECRETS = (DOC_NUMBER, PASSWORD)
BLOCK_HTML = "<html>The requested URL was rejected. Please consult with your administrator.</html>"


@dataclass
class Login:
    site: FakeSite
    posted: list[dict[str, object]] = field(default_factory=list)

    def authenticator(
        self,
        *,
        credentials: Callable[[], Credentials] | None = None,
        options: BrowserOptions | None = None,
    ) -> PlaywrightAuthenticator:
        creds = Credentials(SecretStr(DOC_NUMBER), SecretStr(PASSWORD))
        return PlaywrightAuthenticator(
            login_url=LOGIN_BPM,
            doc_country="Rusia",
            doc_type="Pasaporte",
            credentials=credentials or (lambda: creds),
            options=options or BrowserOptions(headless=True),
        )


@pytest.fixture
async def login(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Login]:
    async with FakeSite() as site:
        state = Login(site)

        async def submit(seen: Seen) -> tuple[int, str, dict[str, str]]:
            await asyncio.sleep(0.3)  # вход на живом сайте не мгновенный
            state.posted.append(json.loads(seen.body))
            return 200, "{}", {"Set-Cookie": "idsess=ID-SESSION; Path=/; Secure"}

        site.handlers[(BPM_HOST, "/autenticacion/login")] = lambda _: (
            302,
            "",
            {"Location": LOGIN_ID_RETURN, "Set-Cookie": "TS0191f197=ANON; Path=/; Secure"},
        )
        site.page(LOGIN_ID_RETURN, id_uruguay_page())
        site.on(f"https://{ID_HOST}/api/login", submit)
        site.page(
            HOME_PAGE,
            "<html>Inicio</html>",
            **{"Set-Cookie": "simple_bpm_session=TEST-SESSION; Path=/; Secure; HttpOnly"},
        )
        await warm_up_chromium(site)  # до сжатия таймаутов: холодный запуск может быть медленным
        monkeypatch.setattr(browser, "launch", routed_launch(site))
        monkeypatch.setattr(auth, "STEP_TIMEOUT_MS", 1500)
        monkeypatch.setattr(browser, "NAVIGATION_TIMEOUT_MS", 1500)  # клик по скрытому элементу
        monkeypatch.setattr(auth, "RETURN_TIMEOUT_MS", 2500)
        yield state
        for opened in site.browsers:
            assert not opened.is_connected(), "браузер входа остался открытым"


def assert_no_secrets(text: str) -> None:
    for secret in SECRETS:
        assert secret not in text


def assert_clean_error(error: BaseException) -> None:
    rendered = "".join(traceback.format_exception(error))
    assert_no_secrets(str(error))
    assert_no_secrets(repr(error))
    assert_no_secrets(rendered)


# --- успешный вход ---


async def test_вход_по_шагам_значения_совпадают_с_настройками_и_секретами(login: Login) -> None:
    cookies = await login.authenticator().login()
    [posted] = login.posted
    assert posted["pais"] == "Rusia"
    assert posted["tipo"] == "Pasaporte"
    assert posted["num"] == DOC_NUMBER
    assert posted["pw"] == PASSWORD
    assert posted["steps"] == ["gubuy", "nodoc", "continuar1", "continuar2"]
    assert {c.name for c in cookies} >= {"simple_bpm_session"}


async def test_раскрывающийся_список_вместо_select(login: Login) -> None:
    login.site.page(LOGIN_ID_RETURN, id_uruguay_page(custom_select=True))
    await login.authenticator().login()
    assert login.posted[0]["pais"] == "Rusia"
    assert login.posted[0]["tipo"] == "Pasaporte"


async def test_возвращаются_только_cookies_bpmgob_с_их_значениями(login: Login) -> None:
    cookies = await login.authenticator().login()
    by_name = {c.name: c for c in cookies}
    assert by_name["simple_bpm_session"].value == "TEST-SESSION"
    assert by_name["simple_bpm_session"].domain.lstrip(".") == BPM_HOST
    assert by_name["simple_bpm_session"].secure is True
    assert "idsess" not in by_name  # cookie ID Uruguay боту не нужны и не возвращаются
    assert all(c.domain.lstrip(".") == BPM_HOST for c in cookies)


async def test_cookie_анонимной_сессии_шлюза_не_выдаётся_за_вход(login: Login) -> None:
    # Адрес входа ID Uruguay содержит адрес bpmgob в запросе (redirect_uri): это ещё не возврат
    # на bpmgob. Вернув управление сразу, бот забрал бы анонимные cookies шлюза и счёл вход удачным
    cookies = await login.authenticator().login()
    assert "simple_bpm_session" in {c.name for c in cookies}


async def test_вход_после_переадресации_скриптом(login: Login) -> None:
    # Страница bpmgob сначала загружается сама, на вход уводит скрипт: ветка ожидания адреса ID
    login.site.page(
        LOGIN_BPM,
        f"<html><script>setTimeout(() => location.replace('{LOGIN_ID}'), 200)</script></html>",
    )
    login.site.page(LOGIN_ID, id_uruguay_page())
    cookies = await login.authenticator().login()
    assert "simple_bpm_session" in {c.name for c in cookies}
    assert login.posted[0]["pw"] == PASSWORD


async def test_браузер_закрывается_и_профиля_нет(login: Login) -> None:
    await login.authenticator().login()
    assert len(login.site.browsers) == 1
    assert not login.site.browsers[0].is_connected()


async def test_секреты_не_попадают_в_логи(login: Login, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.DEBUG):
        await login.authenticator().login()
    assert_no_secrets(caplog.text)
    assert "TEST-SESSION" not in caplog.text  # и значения cookies тоже
    assert any("вход выполнен" in r.getMessage() for r in caplog.records)


# --- отказы ---


@pytest.mark.parametrize("omit", ["gubuy", "nodoc", "pais", "tipo", "num"])
async def test_нет_ожидаемого_элемента_это_login_failed(login: Login, omit: str) -> None:
    login.site.page(LOGIN_ID_RETURN, id_uruguay_page(omit=omit))
    with pytest.raises(LoginFailed, match="не найден ожидаемый элемент") as info:
        await login.authenticator().login()
    assert_clean_error(info.value)
    assert login.posted == []  # пароль не вводился и не отправлялся


async def test_кнопка_continuar_не_появилась_это_site_unavailable_пароль_не_отправлен(
    login: Login,
) -> None:
    # Текстовый поиск находит и скрытую кнопку второго шага: ждёт её и упирается в таймаут
    login.site.page(LOGIN_ID_RETURN, id_uruguay_page(omit="continuar1"))
    with pytest.raises(SiteUnavailable) as info:
        await login.authenticator().login()
    assert_clean_error(info.value)
    assert login.posted == []


async def test_страница_входа_заблокирована_это_rate_limited(login: Login) -> None:
    login.site.page(LOGIN_ID_RETURN, BLOCK_HTML)
    with pytest.raises(RateLimited, match="заблокирована"):
        await login.authenticator().login()
    assert login.posted == []


@pytest.mark.parametrize("status", [403, 429])
async def test_статусы_блокировки_на_странице_входа(login: Login, status: int) -> None:
    login.site.page(LOGIN_ID_RETURN, id_uruguay_page(), status=status)
    with pytest.raises(RateLimited):
        await login.authenticator().login()
    assert login.posted == []


async def test_после_входа_нет_cookies_это_login_failed(login: Login) -> None:
    login.site.page(HOME_PAGE, "<html>Inicio</html>")  # сайт вернул на bpmgob, но сессии нет
    login.site.handlers[(BPM_HOST, "/autenticacion/login")] = lambda _: (
        302,
        "",
        {"Location": LOGIN_ID_RETURN},
    )
    with pytest.raises(LoginFailed, match="нет cookies") as info:
        await login.authenticator().login()
    assert_clean_error(info.value)


async def test_пароль_не_показан_это_site_unavailable_без_утечки(login: Login) -> None:
    login.site.page(LOGIN_ID_RETURN, id_uruguay_page(password_hidden=True))
    # В сообщении названо место, где вход застрял: по нему на живом сайте ясно, что чинить
    with pytest.raises(SiteUnavailable, match="вход не завершился на шаге «пароль»") as info:
        await login.authenticator().login()
    assert_clean_error(info.value)
    assert login.posted == []


async def test_возврат_на_bpmgob_не_случился_это_site_unavailable(
    login: Login, caplog: pytest.LogCaptureFixture
) -> None:
    # Сайт принял форму, но остался на странице ID (например, неверный пароль)
    login.site.page(LOGIN_ID_RETURN, id_uruguay_page(return_to_bpmgob=False))
    with caplog.at_level(logging.DEBUG), pytest.raises(SiteUnavailable) as info:
        await login.authenticator().login()
    assert_clean_error(info.value)
    assert_no_secrets(caplog.text)


async def test_обрыв_сети_на_странице_входа_это_site_unavailable(login: Login) -> None:
    login.site.on(LOGIN_BPM, lambda _: (-1, "", {}))
    with pytest.raises(SiteUnavailable) as info:
        await login.authenticator().login()
    assert_clean_error(info.value)
    assert BPM_HOST not in str(info.value)


async def test_браузер_не_запустился_это_site_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    # Здесь настоящий launch: канал, которого нет, — ошибка Playwright до любой сети
    authenticator = PlaywrightAuthenticator(
        LOGIN_BPM,
        "Rusia",
        "Pasaporte",
        lambda: Credentials(SecretStr(DOC_NUMBER), SecretStr(PASSWORD)),
        BrowserOptions(headless=True, channel="no-such-channel"),
    )
    with pytest.raises(SiteUnavailable) as info:
        await authenticator.login()
    assert_clean_error(info.value)


async def test_нет_секрета_в_хранилище_пробрасывается_и_браузер_не_запускается(
    login: Login,
) -> None:
    def missing() -> Credentials:
        raise SecretMissing("нет секрета: выполните `rcbot init`")

    with pytest.raises(SecretMissing):
        await login.authenticator(credentials=missing).login()
    assert login.site.browsers == []
    assert login.site.requests == []
