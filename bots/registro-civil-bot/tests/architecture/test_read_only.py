"""Инвариант 1 и 5 (SPEC 0.3): бот только читает, контрольный вопрос не решает.

Статические проверки по `ast` исходников. С решением R13 браузера в боте нет: действий над
страницей нет нигде, в строках адаптера нет кнопок записи и оплаты, HTTP-методы и тела запросов
ограничены, решателя контрольного вопроса нет.
"""

from __future__ import annotations

import ast
import re

from tests.architecture.srcscan import (
    Module,
    attr_name,
    calls,
    modules,
    receiver,
    string_constants,
)

ALL = modules()
GUBUY = [m for m in ALL if m.rel.startswith("infrastructure/gubuy/")]

# Имена действий над страницей (Playwright и подобные). «check», «clear», «type» совпадают с
# обычными методами (SafeHttp.check, Redactor.clear), поэтому в gubuy/ ищутся все, в остальном
# коде — без них
ACTIONS = {
    "click",
    "dblclick",
    "check",
    "uncheck",
    "set_checked",
    "fill",
    "type",
    "press",
    "press_sequentially",
    "select_option",
    "tap",
    "dispatch_event",
    "drag_to",
    "drag_and_drop",
    "set_input_files",
    "clear",
}
AMBIGUOUS = {"check", "clear", "type"}


def _actions(module: Module) -> list[tuple[str, str, str, int]]:
    names = ACTIONS if module.rel.startswith("infrastructure/gubuy/") else ACTIONS - AMBIGUOUS
    return [
        (func, attr_name(call), receiver(call), call.lineno)
        for call, func in calls(module)
        if attr_name(call) in names and not (attr_name(call) == "clear" and "jar" in receiver(call))
    ]


def test_действий_над_страницей_нет_нигде() -> None:
    violations = [
        f"{module.rel}:{line}: {func}() вызывает .{action}()"
        for module in ALL
        for func, action, _, line in _actions(module)
    ]
    assert violations == []


def test_браузерной_автоматизации_в_коде_нет() -> None:
    """Решение R13: бот читает страницу SAE обычным GET, браузер ему не нужен."""
    found = [
        f"{m.rel}:{i}"
        for m in ALL
        for i, line in enumerate(m.text.splitlines(), start=1)
        if re.search(r"\b(playwright|selenium|pyppeteer)\b", line.split("#", 1)[0], re.I)
    ]
    assert found == []


def test_адаптер_сайта_один_и_только_читает_страницу_sae() -> None:
    assert sorted(m.rel for m in GUBUY) == [
        "infrastructure/gubuy/__init__.py",
        "infrastructure/gubuy/sae_source.py",
    ]


FORBIDDEN_LOCATOR = re.compile(
    r"(?i)siguiente|guardar|volver|pagar|efectuar|\bpago\b|reservar|confirmar|agendar|enviar|submit"
)


def test_в_строках_gubuy_нет_кнопок_черновика_и_оплаты() -> None:
    """Докстринги исключены: они называют кнопки, которые бот НЕ трогает."""
    found = [
        f"{m.rel}:{line}: {text[:40]!r}"
        for m in GUBUY
        for text, line in string_constants(m)
        if FORBIDDEN_LOCATOR.search(text)
    ]
    assert found == []


def test_контроль_шаблон_запрещённых_локаторов_ловит_кнопки() -> None:
    for text in ("Siguiente", "Guardar y Cerrar", "Volver", "Pagar", "Efectuar pago", "submit"):
        assert FORBIDDEN_LOCATOR.search(text), text
    for text in ("Continuar", "Usuario Gub.uy", ".ui-datepicker-next", "Oficina Tramite"):
        assert not FORBIDDEN_LOCATOR.search(text), text


LOCATOR_CALLS = {"get_by_role", "get_by_text", "get_by_label", "locator", "wait_for_selector"}


def test_локаторы_во_всём_коде_не_называют_запрещённые_кнопки() -> None:
    found = []
    for module in ALL:
        for call, _ in calls(module):
            if attr_name(call) in LOCATOR_CALLS:
                for node in ast.walk(call):
                    if (
                        isinstance(node, ast.Constant)
                        and isinstance(node.value, str)
                        and FORBIDDEN_LOCATOR.search(node.value)
                    ):
                        found.append(f"{module.rel}:{call.lineno}: {node.value!r}")
    assert found == []


def test_add_init_script_и_подмена_отпечатка_не_используются() -> None:
    """SPEC 0.3, п. 2: без подмены отпечатка браузера (webdriver, user-agent, stealth)."""
    pattern = re.compile(
        r"add_init_script|set_extra_http_headers|user_agent\s*=|stealth|webdriver|"
        r"AutomationControlled|--user-agent|new_context\([^)]*(locale|timezone_id|viewport)",
        re.IGNORECASE,
    )
    found = [
        f"{m.rel}:{i}"
        for m in ALL
        for i, line in enumerate(m.text.splitlines(), start=1)
        if pattern.search(line.split("#", 1)[0])
    ]
    assert found == []


# --- HTTP только читает ---

WRITE_METHODS = re.compile(r"""["'](PUT|DELETE|PATCH)["']""")


def test_http_методы_get_head_и_post_только_к_ntfy() -> None:
    methods: dict[str, set[str]] = {}
    for module in ALL:
        for call, _ in calls(module):
            if attr_name(call) == "request" and call.args:
                first = call.args[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    methods.setdefault(first.value, set()).add(module.rel)
    assert set(methods) <= {"GET", "HEAD", "POST"}, methods
    assert methods["POST"] == {"infrastructure/notify/ntfy.py"}
    assert {"GET", "HEAD"} <= set(methods)


def test_нет_записывающих_http_вызовов_и_page_request() -> None:
    found = []
    for module in ALL:
        for call, _ in calls(module):
            name = attr_name(call)
            if name in {"put", "patch", "delete", "post", "fetch"} and "request" in receiver(call):
                found.append(f"{module.rel}:{call.lineno}: {receiver(call)}.{name}")
            if name in {"put", "patch", "delete"} and re.search(
                r"(?i)session|http|client", receiver(call)
            ):
                found.append(f"{module.rel}:{call.lineno}: {receiver(call)}.{name}")
        for i, line in enumerate(module.text.splitlines(), start=1):
            if WRITE_METHODS.search(line.split("#", 1)[0]):
                found.append(f"{module.rel}:{i}: метод записи")
    assert found == []


def test_тела_запросов_только_в_ntfy() -> None:
    """`json=`, `data=`, `body=` — запись на сайт; допустим только JSON для ntfy."""
    found = []
    for module in ALL:
        for call, _ in calls(module):
            if attr_name(call) == "request":
                kw = {k.arg for k in call.keywords}
                if kw & {"json", "data", "body"} and module.rel != "infrastructure/notify/ntfy.py":
                    found.append(f"{module.rel}:{call.lineno}")
    assert found == []


# --- контрольный вопрос шага 1 не решается (инвариант 5, D3) ---

SOLVER_NAMES = re.compile(r"(?i)solv|captcha|respuesta|security_question|auto_?pass|quiz")


def test_в_коде_нет_решателя_контрольного_вопроса() -> None:
    found = []
    for module in ALL:
        for node in ast.walk(module.tree):
            name = getattr(node, "name", None) or getattr(node, "id", None)
            if isinstance(name, str) and SOLVER_NAMES.search(name):
                found.append(f"{module.rel}:{node.lineno}: {name}")
    assert found == []


def test_тексты_контрольных_вопросов_не_используются_как_ответы() -> None:
    """Слова вопросов шага 1 в коде не встречаются вовсе: вход на bpmgob удалён (R13)."""
    pattern = re.compile(r"(?i)cu[aá]ntos minutos|n[uú]mero est[aá] antes|minutos tiene")
    found = [
        f"{m.rel}:{line}" for m in ALL for text, line in string_constants(m) if pattern.search(text)
    ]
    assert found == []
