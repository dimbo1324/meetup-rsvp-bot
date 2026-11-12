"""Настройки (SPEC 0.3 п. 3, 2.6, 4.4): жёсткий минимум интервала, только https, границы полей."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from hypothesis import example, given
from hypothesis import strategies as st
from pydantic import ValidationError

from rcbot.config.settings import (
    MAX_POLL_INTERVAL_SEC,
    MIN_DOM_POLL_INTERVAL_SEC,
    MIN_POLL_INTERVAL_SEC,
    Settings,
    load_settings,
)
from tests.fakes import T0

# --- интервал опроса: жёсткий минимум ---


def test_константы_интервала_соответствуют_spec() -> None:
    assert MIN_POLL_INTERVAL_SEC == 10.0
    assert MIN_DOM_POLL_INTERVAL_SEC == 60.0
    assert MAX_POLL_INTERVAL_SEC == 3600.0


def test_интервал_по_умолчанию_двадцать_секунд() -> None:
    assert Settings().poll_interval_sec == 20.0


@pytest.mark.parametrize("value", [10, 10.0, 10.01, 20, 3599.9, 3600])
def test_интервал_от_десяти_секунд_допустим(value: float) -> None:
    assert Settings(poll_interval_sec=value).poll_interval_sec == value


@pytest.mark.parametrize("value", [9.99, 9, 1, 0.5, 0, -1, -20, float("-inf")])
def test_интервал_меньше_десяти_секунд_отклоняется(value: float) -> None:
    with pytest.raises(ValidationError, match="интервал"):
        Settings(poll_interval_sec=value)


@pytest.mark.parametrize("value", [3600.5, 86400, float("inf")])
def test_интервал_больше_верхнего_предела_обрезается_до_3600(value: float) -> None:
    assert Settings(poll_interval_sec=value).poll_interval_sec == 3600.0


def test_интервал_не_число_nan_не_обходит_минимум() -> None:
    # NaN не меньше десяти ни в одном сравнении: без явной проверки он прошёл бы мимо минимума
    with pytest.raises(ValidationError):
        Settings(poll_interval_sec=float("nan"))


@pytest.mark.parametrize("raw", ["nan", "NaN", "abc", "", "9.99", "-5", "0"])
def test_интервал_из_окружения_с_плохим_значением_отклоняется(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    monkeypatch.setenv("RCBOT_POLL_INTERVAL_SEC", raw)
    with pytest.raises(ValidationError):
        Settings()


def test_интервал_из_окружения_читается_строкой(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RCBOT_POLL_INTERVAL_SEC", "45.5")
    assert Settings().poll_interval_sec == 45.5


@given(st.floats(allow_nan=True, allow_infinity=True))
@example(float("nan"))
@example(9.999999)
@example(10.0)
@example(3600.0000001)
def test_свойство_любой_принятый_интервал_в_допустимых_пределах(value: float) -> None:
    try:
        accepted = Settings(poll_interval_sec=value).poll_interval_sec
    except ValidationError:
        return
    assert MIN_POLL_INTERVAL_SEC <= accepted <= MAX_POLL_INTERVAL_SEC


# --- https ---


@pytest.mark.parametrize("field", ["agenda_url", "login_url", "ntfy_server", "sae_url"])
@pytest.mark.parametrize(
    "value",
    ["http://bpmgob.mec.gub.uy/x", "ftp://x.example/a", "file:///etc/passwd", "//x.example", ""],
)
def test_обязательные_адреса_только_https(field: str, value: str) -> None:
    with pytest.raises(ValidationError, match="https"):
        Settings(**{field: value})


@pytest.mark.parametrize("field", ["api_url", "watchdog_url"])
@pytest.mark.parametrize("value", ["http://x.example/a", "ftp://x.example", "ws://x.example"])
def test_необязательные_адреса_http_отклоняются(field: str, value: str) -> None:
    with pytest.raises(ValidationError, match="https"):
        Settings(**{field: value})


@pytest.mark.parametrize("field", ["api_url", "watchdog_url"])
def test_необязательные_адреса_могут_быть_пустыми(field: str) -> None:
    assert getattr(Settings(**{field: ""}), field) == ""
    assert getattr(Settings(), field) == ""


@pytest.mark.parametrize(
    "field", ["agenda_url", "login_url", "ntfy_server", "sae_url", "api_url", "watchdog_url"]
)
def test_схема_https_в_любом_регистре_допустима(field: str) -> None:
    # API календаря допустим только на bpmgob (SPEC 2.6): для него проверяется регистр схемы
    host = "bpmgob.mec.gub.uy" if field == "api_url" else "host.example"
    assert getattr(Settings(**{field: f"HTTPS://{host}/a"}), field)


@pytest.mark.parametrize("field", ["agenda_url", "login_url", "ntfy_server", "sae_url", "api_url"])
@pytest.mark.parametrize("value", ["https://", "https:///path", "https://:443/x"])
def test_https_без_хоста_отклоняется(field: str, value: str) -> None:
    # Пустой хост попал бы в список разрешённых адресов как «» (SPEC 2.6)
    with pytest.raises(ValidationError):
        Settings(**{field: value})


def test_адреса_по_умолчанию_https_и_пустые_хосты_не_попадают_в_список() -> None:
    hosts = Settings().allowed_hosts()
    assert "" not in hosts


# --- часовой пояс ---


def test_часовой_пояс_по_умолчанию_монтевидео_utc_минус_три() -> None:
    settings = Settings()
    assert settings.timezone == "America/Montevideo"
    local = T0.astimezone(settings.tz)
    assert (local.hour, local.minute) == (12, 0)
    assert local.utcoffset() is not None
    assert local.utcoffset().total_seconds() == -3 * 3600  # type: ignore[union-attr]


@pytest.mark.parametrize("name", ["UTC", "Europe/Moscow", "America/Montevideo"])
def test_известный_часовой_пояс_принимается(name: str) -> None:
    assert Settings(timezone=name).tz.key == name


@pytest.mark.parametrize("name", ["Mars/Olympus", "", "../etc/passwd", "Montevideo", "UTC+3"])
def test_неизвестный_часовой_пояс_это_ошибка_валидации_а_не_сырой_keyerror(name: str) -> None:
    # CLI ловит ValidationError и печатает понятное сообщение; KeyError дал бы трассировку
    with pytest.raises(ValidationError):
        Settings(timezone=name)


def test_пояс_влияет_на_локальное_время() -> None:
    moment = datetime(2026, 11, 12, 15, 0, tzinfo=UTC)
    assert moment.astimezone(Settings(timezone="UTC").tz).hour == 15
    assert moment.astimezone(Settings(timezone="Europe/Moscow").tz).hour == 18


# --- границы Field ---

BOUNDS: list[tuple[str, Any, Any, Any, Any]] = [
    # поле, ниже минимума, минимум, максимум, выше максимума (None — предела нет)
    ("months_ahead", 0, 1, 12, 13),
    ("poll_jitter", -0.01, 0.0, 0.5, 0.51),
    ("request_timeout_sec", 0, 0.001, 60, 60.01),
    ("relogin_max_per_hour", 0, 1, 6, 7),
    ("tg_repeat_sec", 29.99, 30, 1e6, None),
    ("tg_repeat_max", -1, 0, 100, 101),
    ("ntfy_repeat_sec", 59.99, 60, 1e6, None),
    ("ntfy_repeat_max", -1, 0, 50, 51),
    ("call_repeat_sec", 119.99, 120, 1e6, None),
    ("call_repeat_max", -1, 0, 10, 11),
    ("daily_report_hour", -1, 0, 23, 24),
]


@pytest.mark.parametrize(("field", "below", "low", "high", "above"), BOUNDS)
def test_границы_полей(field: str, below: Any, low: Any, high: Any, above: Any) -> None:
    with pytest.raises(ValidationError):
        Settings(**{field: below})
    assert getattr(Settings(**{field: low}), field) == low
    assert getattr(Settings(**{field: high}), field) == high
    if above is not None:
        with pytest.raises(ValidationError):
            Settings(**{field: above})


def test_значения_по_умолчанию_совпадают_с_env_example_spec() -> None:
    s = Settings()
    assert (s.months_ahead, s.poll_jitter, s.request_timeout_sec) == (6, 0.10, 8.0)
    assert s.relogin_max_per_hour == 6
    assert (s.tg_repeat_sec, s.tg_repeat_max) == (60.0, 30)
    assert (s.ntfy_server, s.ntfy_repeat_sec, s.ntfy_repeat_max) == ("https://ntfy.sh", 120.0, 10)
    assert (s.call_repeat_sec, s.call_repeat_max, s.call_lang) == (180.0, 5, "ru-RU-Standard-A")
    assert (s.daily_report_hour, s.timezone) == (9, "America/Montevideo")
    assert (s.sentry_enabled, s.sae_source_enabled, s.headless) == (False, False, True)
    assert s.watchdog_url == "" and s.api_url == ""
    assert (s.doc_country, s.doc_type) == ("Rusia", "Pasaporte")
    assert s.location_text == "Oficina Tramite Extranjeros"


def test_повторы_нельзя_сделать_чаще_чем_разрешено_spec() -> None:
    # Не засыпать владельца и сторонние сервисы: пол интервалов повтора зашит в поля
    for field, floor in (("tg_repeat_sec", 30), ("ntfy_repeat_sec", 60), ("call_repeat_sec", 120)):
        with pytest.raises(ValidationError):
            Settings(**{field: floor - 1})


# --- журнал наблюдения ---


def test_журнал_по_умолчанию_10_и_5_гб() -> None:
    s = Settings()
    assert (s.journal_max_gb, s.journal_trim_to_gb) == (10.0, 5.0)
    assert s.journal_enabled


def test_порог_очистки_журнала_строго_меньше_квоты() -> None:
    with pytest.raises(ValidationError, match="TRIM_TO_GB"):
        Settings(journal_max_gb=5, journal_trim_to_gb=5)
    with pytest.raises(ValidationError, match="TRIM_TO_GB"):
        Settings(journal_max_gb=5, journal_trim_to_gb=6)
    assert Settings(journal_max_gb=5, journal_trim_to_gb=4.99).journal_trim_to_gb == 4.99


def test_квота_журнала_меньше_порога_по_умолчанию_требует_явного_порога() -> None:
    # max=3 при trim по умолчанию 5 — несогласованная пара, а не молчаливое «принято»
    with pytest.raises(ValidationError):
        Settings(journal_max_gb=3)


@pytest.mark.parametrize(
    ("field", "bad", "good"),
    [("journal_max_gb", 0, 1000), ("journal_trim_to_gb", 0, 4.9)],
)
def test_границы_размеров_журнала(field: str, bad: float, good: float) -> None:
    with pytest.raises(ValidationError):
        Settings(**{field: bad})
    with pytest.raises(ValidationError):
        Settings(**{field: 1000.01})
    assert getattr(Settings(**{field: good}), field) == good


# --- agenda_configured и allowed_hosts ---


def test_адрес_этапа_по_умолчанию_это_метка_а_не_настроенный() -> None:
    assert not Settings().agenda_configured
    assert Settings(agenda_url="https://bpmgob.mec.gub.uy/etapas/ejecutar/TEST/1").agenda_configured


def test_любая_угловая_скобка_в_адресе_означает_ненастроенный() -> None:
    assert not Settings(agenda_url="https://bpmgob.mec.gub.uy/<X>/1").agenda_configured


def test_список_хостов_по_умолчанию_точно_по_spec_2_6() -> None:
    assert Settings().allowed_hosts() == frozenset(
        {
            "bpmgob.mec.gub.uy",
            "mi.iduruguay.gub.uy",
            "api.telegram.org",
            "ntfy.sh",
            "api.callmebot.com",
        }
    )


def test_хост_сторожа_добавляется_только_когда_задан() -> None:
    hosts = Settings(watchdog_url="https://Hc.Example.org:8443/ping/abc").allowed_hosts()
    assert "hc.example.org" in hosts
    assert len(hosts) == 6
    assert not any(":" in h or "/" in h for h in hosts)


def test_api_живёт_на_bpmgob_и_список_хостов_не_растёт() -> None:
    hosts = Settings(api_url="https://BPMGOB.mec.gub.uy/disp").allowed_hosts()
    assert hosts == Settings().allowed_hosts() and len(hosts) == 5


@pytest.mark.parametrize(
    "url",
    ["https://api.example.org/disp", "https://bpmgob.mec.gub.uy.evil.example/x"],
)
def test_api_на_чужом_хосте_отклоняется(url: str) -> None:
    with pytest.raises(ValidationError, match=r"bpmgob\.mec\.gub\.uy"):
        Settings(api_url=url)


def test_sae_попадает_в_список_только_когда_включён() -> None:
    assert "sae.mec.gub.uy" not in Settings().allowed_hosts()
    enabled = Settings(sae_source_enabled=True).allowed_hosts()
    assert "sae.mec.gub.uy" in enabled and len(enabled) == 6


def test_свой_сервер_ntfy_заменяет_ntfy_sh_в_списке() -> None:
    hosts = Settings(ntfy_server="https://ntfy.example.org:8443").allowed_hosts()
    assert "ntfy.example.org" in hosts
    assert "ntfy.sh" not in hosts


def test_все_необязательные_источники_вместе() -> None:
    hosts = Settings(
        watchdog_url="https://w.example.org/p",
        api_url="https://bpmgob.mec.gub.uy/x",
        sae_source_enabled=True,
    ).allowed_hosts()
    assert len(hosts) == 7
    assert {"w.example.org", "sae.mec.gub.uy"} <= hosts


def test_хосты_не_зависят_от_адреса_этапа_и_входа() -> None:
    # Хост агенды и входа — из фиксированного списка gub.uy, а не из `.env`: подмена адреса
    # в настройках не расширяет список разрешённых хостов.
    hosts = Settings(
        agenda_url="https://evil.example/etapas/1", login_url="https://evil.example/login"
    ).allowed_hosts()
    assert "evil.example" not in hosts


# --- загрузка из .env и окружения ---


def test_load_settings_читает_env_файл(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text(
        "# комментарий\n"
        "RCBOT_POLL_INTERVAL_SEC=33\n"
        "RCBOT_TRAMITE_ID=TEST-TRAMITE\n"
        'RCBOT_LOCATION_TEXT="Oficina Prueba"\n'
        "RCBOT_SENTRY_ENABLED=true\n"
        "RCBOT_UNKNOWN_KEY=1\n"
        "OTHER_VAR=ignored\n",
        encoding="utf-8",
    )
    settings = load_settings(env)
    assert settings.poll_interval_sec == 33.0
    assert settings.tramite_id == "TEST-TRAMITE"
    assert settings.location_text == "Oficina Prueba"
    assert settings.sentry_enabled is True


def test_переменная_окружения_главнее_env_файла(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = tmp_path / ".env"
    env.write_text("RCBOT_POLL_INTERVAL_SEC=33\nRCBOT_MONTHS_AHEAD=3\n", encoding="utf-8")
    monkeypatch.setenv("RCBOT_POLL_INTERVAL_SEC", "44")
    settings = load_settings(env)
    assert settings.poll_interval_sec == 44.0
    assert settings.months_ahead == 3


def test_несуществующий_env_файл_даёт_значения_по_умолчанию(tmp_path: Path) -> None:
    settings = load_settings(tmp_path / "нет-такого" / ".env")
    assert settings.poll_interval_sec == 20.0
    assert settings.tramite_id == ""


def test_пустой_env_файл_даёт_значения_по_умолчанию(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("", encoding="utf-8")
    assert load_settings(env).months_ahead == 6


def test_плохое_значение_в_env_файле_это_ошибка_валидации(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("RCBOT_POLL_INTERVAL_SEC=5\n", encoding="utf-8")
    with pytest.raises(ValidationError, match="интервал"):
        load_settings(env)


def test_обычный_settings_не_читает_env_из_текущего_каталога(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".env").write_text("RCBOT_POLL_INTERVAL_SEC=77\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert Settings().poll_interval_sec == 20.0


def test_префикс_rcbot_обязателен(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POLL_INTERVAL_SEC", "99")
    assert Settings().poll_interval_sec == 20.0


def test_env_файл_в_кодировке_utf8_с_кириллицей(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("RCBOT_LOCATION_TEXT=Oficina Тест\n", encoding="utf-8")
    assert load_settings(env).location_text == "Oficina Тест"
