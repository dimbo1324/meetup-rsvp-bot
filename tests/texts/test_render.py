"""Подстановка значений в тексты: экранирование и следы пустых подстановок."""

from meetup_bot.texts.defaults import DEFAULT_TEXTS
from meetup_bot.texts.render import fill, tidy


def test_кавычка_в_значении_внутри_href_не_закрывает_атрибут() -> None:
    template = '<a href="tg://user?id=1">{name}</a> <a href="{place}">место</a>'
    text = fill(template, {"name": 'Вася "x"', "place": 'a" onclick="evil'}, escape_html=True)
    assert 'href="a&quot; onclick=&quot;evil"' in text
    assert "Вася &quot;x&quot;" in text
    assert '" onclick=' not in text


def test_без_экранирования_значения_остаются_как_есть() -> None:
    assert fill("{name}", {"name": '"<b>"'}, escape_html=False) == '"<b>"'


def test_тире_диапазона_без_конца_убирается() -> None:
    values = {"weekday": "пятница", "date": "30 октября", "time": "16:30", "deadline": "вечера"}
    text = fill(DEFAULT_TEXTS["text.registered"], values, escape_html=True)
    assert "16:30." in text
    assert "–" not in text


def test_диапазон_с_концом_сохраняется() -> None:
    text = fill(
        DEFAULT_TEXTS["text.promoted"],
        {"weekday": "пятница", "date": "30 октября", "time": "16:30", "end_time": "18:30"},
        escape_html=True,
    )
    assert text == "Освободилось место — вы записаны на встречу: пятница, 30 октября, 16:30–18:30!"


def test_тире_между_словами_не_трогается() -> None:
    assert tidy("Место — парк. Время 10–12 часов.") == "Место — парк. Время 10–12 часов."


def test_день_недели_в_текстах_записи_стоит_в_именительном_падеже() -> None:
    for key in ("text.registered", "text.registered_late", "text.promoted", "text.reminder"):
        assert " на {weekday}" not in DEFAULT_TEXTS[key]
