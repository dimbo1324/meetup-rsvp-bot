"""Схема настроек (раздел 7) и чистая валидация: типы, диапазоны, HTML, шаблоны текстов."""

from typing import Any

import pytest

from meetup_bot.services.settings_schema import (
    DESCRIPTION_MAX_LEN,
    DESCRIPTION_WARN_LEN,
    MESSAGE_MAX_LEN,
    NAME_MAX_LEN,
    POPUP_MAX_LEN,
    SAMPLE_VARS,
    SCHEMA,
    WORST_CASE_VARS,
    Kind,
    SettingsValidationError,
    check_html,
    check_template,
    get_spec,
    parse_hhmm,
    validate,
    validate_capacity,
    validate_description,
    validate_place,
    validate_url,
)
from meetup_bot.texts.defaults import (
    ALLOWED_PLACEHOLDERS,
    DEFAULT_TEXTS,
    MESSAGE_TEXT_KEYS,
)

# Значения по умолчанию и диапазоны из раздела 7 спецификации
SPEC_DEFAULTS: dict[str, Any] = {
    "capacity": 10,
    "event_weekday": 5,
    "event_time": "17:00",
    "default_place": "",
    "default_place_url": "",
    "default_description": "",
    "default_photo_file_id": None,
    "auto_create_enabled": True,
    "auto_create_weekday": 6,
    "auto_create_time": "10:00",
    "cancel_deadline_days_before": 1,
    "cancel_deadline_time": "20:00",
    "registration_close_hours_before": 3,
    "reminder_enabled": True,
    "reminder_hours_before": 24,
    "attendance_prompt_hours_after": 5,
    "ban_after_no_shows": 1,
    "remove_banned_from_future_events": True,
    "notify_user_on_ban": False,
    "show_names_in_post": True,
    "plus_one_enabled": False,
    "waitlist_enabled": False,
    "late_promotion_hours": 24,
    "notify_admins_on_full": True,
    "notify_admins_on_cancel": True,
    "timezone": "America/Montevideo",
}

SPEC_RANGES: dict[str, tuple[int, int]] = {
    "capacity": (1, 200),
    "event_weekday": (0, 6),
    "auto_create_weekday": (0, 6),
    "cancel_deadline_days_before": (0, 7),
    "registration_close_hours_before": (0, 72),
    "reminder_hours_before": (1, 72),
    "attendance_prompt_hours_after": (1, 48),
    "ban_after_no_shows": (0, 10),
    "late_promotion_hours": (0, 72),
}


# ---- схема и значения по умолчанию ----------------------------------------------------


@pytest.mark.parametrize(("key", "default"), SPEC_DEFAULTS.items())
def test_умолчание_совпадает_с_разделом_7(key: str, default: Any) -> None:
    assert SCHEMA[key].default == default


def test_все_нетекстовые_ключи_раздела_7_есть_в_схеме() -> None:
    non_text = {key for key, spec in SCHEMA.items() if spec.kind is not Kind.TEXT}
    assert non_text == set(SPEC_DEFAULTS)


@pytest.mark.parametrize(("key", "bounds"), SPEC_RANGES.items())
def test_диапазоны_целых_совпадают_с_разделом_7(key: str, bounds: tuple[int, int]) -> None:
    assert (SCHEMA[key].min, SCHEMA[key].max) == bounds


def test_у_каждого_текста_схемы_умолчание_из_раздела_6() -> None:
    text_specs = {key: spec for key, spec in SCHEMA.items() if spec.kind is Kind.TEXT}
    assert {key: spec.default for key, spec in text_specs.items()} == DEFAULT_TEXTS


def test_всплывающие_тексты_ограничены_200_а_сообщения_лимитом_telegram() -> None:
    for key, spec in SCHEMA.items():
        if spec.kind is not Kind.TEXT:
            continue
        expected = MESSAGE_MAX_LEN if key in MESSAGE_TEXT_KEYS else POPUP_MAX_LEN
        assert spec.max_len == expected, key


@pytest.mark.parametrize("key", list(SCHEMA))
def test_умолчание_каждой_настройки_проходит_собственную_валидацию(key: str) -> None:
    spec = SCHEMA[key]
    normalized, _ = validate(spec, spec.default)
    assert normalized == spec.default


def test_умолчания_текстов_укладываются_в_худший_случай() -> None:
    for key, text in DEFAULT_TEXTS.items():
        if key in MESSAGE_TEXT_KEYS:
            continue
        assert len(text.format(**WORST_CASE_VARS)) <= POPUP_MAX_LEN, key


def test_неизвестный_ключ_даёт_ошибку_с_ключом() -> None:
    with pytest.raises(SettingsValidationError) as info:
        get_spec("нет_такого")
    assert info.value.key == "нет_такого"


# ---- числа и флаги --------------------------------------------------------------------


@pytest.mark.parametrize(("key", "bounds"), SPEC_RANGES.items())
def test_границы_диапазона_принимаются(key: str, bounds: tuple[int, int]) -> None:
    for value in bounds:
        assert validate(SCHEMA[key], value) == (value, [])


@pytest.mark.parametrize(("key", "bounds"), SPEC_RANGES.items())
def test_за_границей_диапазона_отклоняется(key: str, bounds: tuple[int, int]) -> None:
    for value in (bounds[0] - 1, bounds[1] + 1):
        with pytest.raises(SettingsValidationError):
            validate(SCHEMA[key], value)


@pytest.mark.parametrize("value", [True, False, "10", 10.0, None, [10]])
def test_целое_не_принимает_чужие_типы(value: object) -> None:
    with pytest.raises(SettingsValidationError, match="целое"):
        validate(SCHEMA["capacity"], value)


@pytest.mark.parametrize("value", [1, 0, "true", None, "вкл"])
def test_флаг_принимает_только_bool(value: object) -> None:
    with pytest.raises(SettingsValidationError):
        validate(SCHEMA["plus_one_enabled"], value)


@pytest.mark.parametrize("value", [True, False])
def test_флаг_принимает_bool(value: bool) -> None:
    assert validate(SCHEMA["plus_one_enabled"], value) == (value, [])


# ---- время, часовой пояс, строки ------------------------------------------------------


@pytest.mark.parametrize("value", ["00:00", "17:00", "23:59"])
def test_время_корректное(value: str) -> None:
    assert validate(SCHEMA["event_time"], value) == (value, [])


def test_время_обрезает_пробелы() -> None:
    assert validate(SCHEMA["event_time"], " 17:30 ")[0] == "17:30"


@pytest.mark.parametrize(
    "value", ["7:00", "24:00", "12:60", "ab:cd", "1700", "17:00:00", "", "-1:00"]
)
def test_время_некорректное(value: str) -> None:
    with pytest.raises(SettingsValidationError, match="ЧЧ:ММ"):
        validate(SCHEMA["event_time"], value)


def test_время_не_строка_отклоняется() -> None:
    with pytest.raises(SettingsValidationError, match="строка"):
        validate(SCHEMA["event_time"], 1700)


@pytest.mark.parametrize(
    ("value", "expected"),
    [("17:00", (17, 0)), ("00:00", (0, 0)), ("23:59", (23, 59)), ("24:00", None), ("9:00", None)],
)
def test_parse_hhmm(value: str, expected: tuple[int, int] | None) -> None:
    assert parse_hhmm(value) == expected


@pytest.mark.parametrize("value", ["America/Montevideo", "UTC", "Europe/Moscow"])
def test_часовой_пояс_корректный(value: str) -> None:
    assert validate(SCHEMA["timezone"], value) == (value, [])


@pytest.mark.parametrize("value", ["Mars/Phobos", "", "../etc/passwd", "Montevideo", "a" * 300])
def test_часовой_пояс_некорректный(value: str) -> None:
    with pytest.raises(SettingsValidationError, match="часовой пояс"):
        validate(SCHEMA["timezone"], value)


def test_часовой_пояс_не_строка_отклоняется() -> None:
    with pytest.raises(SettingsValidationError):
        validate(SCHEMA["timezone"], 3)


def test_место_обрезает_пробелы() -> None:
    assert validate_place("  Кафе  ") == "Кафе"


def test_место_пустое_допустимо() -> None:
    assert validate_place("") == ""


def test_место_ровно_200_допустимо_201_нет() -> None:
    assert validate_place("м" * 200) == "м" * 200
    with pytest.raises(SettingsValidationError, match="200"):
        validate_place("м" * 201)


@pytest.mark.parametrize("value", ["a\nb", "a\tb", "a\x00b", "a\x7fb"])
def test_место_без_управляющих_символов(value: str) -> None:
    with pytest.raises(SettingsValidationError, match="управляющих"):
        validate_place(value)


def test_место_не_строка_отклоняется() -> None:
    with pytest.raises(SettingsValidationError, match="строка"):
        validate_place(5)


def test_опциональная_строка_принимает_none() -> None:
    assert validate(SCHEMA["default_photo_file_id"], None) == (None, [])


def test_опциональная_строка_обрезает_пробелы() -> None:
    assert validate(SCHEMA["default_photo_file_id"], " AgACxyz ")[0] == "AgACxyz"


@pytest.mark.parametrize("value", ["", "   ", 5, "ab cd", "ab\ncd", "a" * 257])
def test_опциональная_строка_некорректная(value: object) -> None:
    with pytest.raises(SettingsValidationError):
        validate(SCHEMA["default_photo_file_id"], value)


# ---- ссылка и вместимость -------------------------------------------------------------


def test_ссылка_пустая_значит_без_ссылки() -> None:
    assert validate_url("") == ""


def test_ссылка_https_допустима_и_обрезается() -> None:
    assert validate_url("  https://maps.example/x?q=1  ") == "https://maps.example/x?q=1"


@pytest.mark.parametrize(
    "value",
    [
        "http://example.com",
        "https://",
        "ftp://example.com",
        "javascript:alert(1)",
        "example.com",
        "https://a.b/ c",
        "https://a.b/\nc",
        'https://a.b/"onclick',
        "https://a.b/'x",
        "https://a.b/<x>",
        "https://a.b/\\x",
    ],
)
def test_ссылка_некорректная(value: str) -> None:
    with pytest.raises(SettingsValidationError):
        validate_url(value)


def test_ссылка_длиннее_2000_отклоняется() -> None:
    with pytest.raises(SettingsValidationError, match="2000"):
        validate_url("https://a.b/" + "x" * 2000)


def test_вместимость_возвращает_int() -> None:
    assert validate_capacity(25) == 25


@pytest.mark.parametrize("value", [0, 201, -5, True, "10", 1.5])
def test_вместимость_некорректная(value: object) -> None:
    with pytest.raises(SettingsValidationError):
        validate_capacity(value)


# ---- HTML-описание --------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "",
        "просто текст",
        "<b>жирный</b> и <i>курсив</i>",
        "<b><i>вложенно</i></b>",
        "<strong>a</strong><em>b</em><u>c</u><s>d</s><del>e</del><ins>f</ins><strike>g</strike>",
        '<a href="https://example.com/path?q=1&amp;r=2">ссылка</a>',
        '<a href="tg://user?id=12345">человек</a>',
        '<span class="tg-spoiler">секрет</span>',
        "<tg-spoiler>секрет</tg-spoiler>",
        '<code class="language-python">x = 1</code>',
        "<pre>блок</pre>",
        "<code>код</code>",
        '<tg-emoji emoji-id="5368324170671202286">👍</tg-emoji>',
        "<blockquote>цитата</blockquote>",
        "1 &lt; 2 &amp; 3 &gt; 2 &quot;x&quot; &#169; &#xA9;",
    ],
)
def test_html_допустимый(value: str) -> None:
    assert check_html("default_description", value) == []


@pytest.mark.parametrize(
    "value",
    [
        "<script>alert(1)</script>",
        "<br>",
        "<div>x</div>",
        "<img src=x>",
        '<a href="javascript:alert(1)">x</a>',
        '<a href="JAVASCRIPT:alert(1)">x</a>',
        '<a href="http://example.com">x</a>',
        '<a href="https://a.b" onclick="x">x</a>',
        '<a href="https://a.b" href="https://c.d">x</a>',
        "<a>без ссылки</a>",
        '<a href="tg://user?id=abc">x</a>',
        '<a href="tg://resolve?domain=x">x</a>',
        '<b onclick="x">x</b>',
        "<span>без класса</span>",
        '<span class="other">x</span>',
        '<code class="evil">x</code>',
        '<pre style="x">x</pre>',
        '<tg-emoji emoji-id="abc">x</tg-emoji>',
        "<tg-emoji>x</tg-emoji>",
    ],
)
def test_html_запрещённые_теги_и_атрибуты(value: str) -> None:
    with pytest.raises(SettingsValidationError, match="HTML"):
        check_html("default_description", value)


@pytest.mark.parametrize(
    "value",
    [
        "<b>не закрыт",
        "</b>лишний закрывающий",
        "<b><i>перепутан</b></i>",
        "<b>a</i>",
        "текст <b",
        "текст <",
    ],
)
def test_html_незакрытые_и_перепутанные_теги(value: str) -> None:
    with pytest.raises(SettingsValidationError, match="HTML"):
        check_html("default_description", value)


@pytest.mark.parametrize(
    "value",
    [
        "<!-- комментарий -->",
        "<!DOCTYPE html>",
        "<?php echo 1 ?>",
        "<![CDATA[x]]>",
        "<b><!-- x --></b>",
    ],
)
def test_html_комментарии_и_декларации_запрещены(value: str) -> None:
    with pytest.raises(SettingsValidationError, match="HTML"):
        check_html("default_description", value)


@pytest.mark.parametrize("value", ["Tom & Jerry", "a &unknown; b", "a &amp b", "&#; x", "5 < 6"])
def test_html_голые_амперсанд_и_меньше_запрещены(value: str) -> None:
    with pytest.raises(SettingsValidationError, match="HTML"):
        check_html("default_description", value)


def test_html_ровно_максимальной_длины_допустим_с_предупреждением() -> None:
    warnings = check_html("default_description", "а" * DESCRIPTION_MAX_LEN)
    assert len(warnings) == 1


def test_html_длиннее_максимума_отклоняется() -> None:
    with pytest.raises(SettingsValidationError, match="слишком длинное"):
        check_html("default_description", "а" * (DESCRIPTION_MAX_LEN + 1))


def test_html_длиннее_порога_даёт_предупреждение() -> None:
    assert check_html("default_description", "а" * DESCRIPTION_WARN_LEN) == []
    assert check_html("default_description", "а" * (DESCRIPTION_WARN_LEN + 1))


def test_validate_description_возвращает_предупреждения_списком() -> None:
    assert validate_description("а" * (DESCRIPTION_WARN_LEN + 1))
    assert validate_description("<b>ok</b>") == []


def test_validate_description_ошибки_исключением() -> None:
    with pytest.raises(SettingsValidationError):
        validate_description("<script>")


# ---- шаблоны текстов (кейс 20) --------------------------------------------------------


@pytest.mark.parametrize("name", sorted(ALLOWED_PLACEHOLDERS))
def test_разрешённая_подстановка_принимается(name: str) -> None:
    check_template("text.full", "значение: {" + name + "}")


def test_неизвестная_подстановка_отклоняется() -> None:  # кейс 20
    with pytest.raises(SettingsValidationError, match="Неизвестная подстановка"):
        check_template("text.full", "Привет, {unknown}!")


def test_сообщение_об_ошибке_перечисляет_доступные_подстановки() -> None:
    with pytest.raises(SettingsValidationError) as info:
        check_template("text.full", "{unknown}")
    assert all("{" + name + "}" in info.value.message for name in ALLOWED_PLACEHOLDERS)


@pytest.mark.parametrize(
    "template",
    [
        "{date.__class__}",
        "{date.__class__.__mro__}",
        "{date.upper}",
        "{0}",
        "{}",
        "{x!r}",
        "{date!r}",
        "{date!s}",
        "{date:>10}",
        "{date:{time}}",
        "{date[0]}",
        "{place[__class__]}",
        "{__import__}",
    ],
)
def test_формат_инъекции_отклоняются(template: str) -> None:  # кейс 20
    with pytest.raises(SettingsValidationError):
        check_template("text.full", template)


@pytest.mark.parametrize("template", ["{", "}", "a {date", "date}", "{{}", "{date}}"])
def test_несбалансированные_скобки_отклоняются(template: str) -> None:
    with pytest.raises(SettingsValidationError, match="скобки"):
        check_template("text.full", template)


def test_экранированные_скобки_допустимы() -> None:
    check_template("text.full", "{{не подстановка}}")


@pytest.mark.parametrize(
    "template",
    ["{unknown}", "{date.__class__}", "{0}", "{x!r}", "{date:>5}", "{"],
)
def test_validate_текста_отклоняет_плохой_шаблон(template: str) -> None:  # кейс 20
    with pytest.raises(SettingsValidationError):
        validate(SCHEMA["text.full"], template)


def test_validate_текста_нормализует_без_изменений() -> None:
    assert validate(SCHEMA["text.full"], "Мест нет, {name}") == ("Мест нет, {name}", [])


@pytest.mark.parametrize("value", ["", "   ", "\n"])
def test_пустой_текст_отклоняется(value: str) -> None:
    with pytest.raises(SettingsValidationError, match="пустым"):
        validate(SCHEMA["text.full"], value)


def test_текст_не_строка_отклоняется() -> None:
    with pytest.raises(SettingsValidationError, match="строка"):
        validate(SCHEMA["text.full"], 42)


def test_всплывающий_текст_ровно_200_допустим() -> None:
    assert validate(SCHEMA["text.full"], "я" * 200)[0] == "я" * 200


def test_всплывающий_текст_201_отклоняется() -> None:
    with pytest.raises(SettingsValidationError, match="слишком длинный"):
        validate(SCHEMA["text.full"], "я" * 201)


def test_длина_считается_по_худшему_случаю_а_не_по_образцу() -> None:
    # Образец «Имя» даёт короткую строку, но имя может быть 64 символа
    template = "{name}{name}{name}{name}"
    assert len(template.format(**SAMPLE_VARS)) < POPUP_MAX_LEN
    assert NAME_MAX_LEN * 4 > POPUP_MAX_LEN
    with pytest.raises(SettingsValidationError, match="слишком длинный"):
        validate(SCHEMA["text.full"], template)


def test_длина_места_в_худшем_случае_200_символов() -> None:
    assert validate(SCHEMA["text.full"], "{place}")[0] == "{place}"
    with pytest.raises(SettingsValidationError):
        validate(SCHEMA["text.full"], "{place} ")  # 200 + пробел, пробел не обрезается


def test_длина_причины_учитывается_в_худшем_случае() -> None:
    with pytest.raises(SettingsValidationError):
        validate(SCHEMA["text.full"], "{reason}{reason}")


def test_сообщение_не_ограничено_200_символами() -> None:
    long_text = "я" * 1000 + " {name}"
    assert validate(SCHEMA["text.reminder"], long_text)[0] == long_text


def test_сообщение_всё_равно_проверяет_подстановки() -> None:
    with pytest.raises(SettingsValidationError):
        validate(SCHEMA["text.reminder"], "{unknown}")
