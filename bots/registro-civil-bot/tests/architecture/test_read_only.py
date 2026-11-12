"""Инвариант 1 и 5 (SPEC 0.3): бот только читает, контрольный вопрос не решает.

Статические проверки по `ast` исходников: действия над страницей разрешены только там и только
в тех функциях, где они нужны для входа или листания месяцев; в локаторах нет кнопок черновика;
JS, который выполняется на странице, ничего не меняет; HTTP-методы и тела запросов ограничены.
"""

from __future__ import annotations

import ast
import re

import pytest

from rcbot.infrastructure.gubuy.parsers import READ_MONTH_JS
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

# Имена действий Playwright. «check», «clear», «type» совпадают с обычными методами
# (SafeHttp.check, Redactor.clear), поэтому в gubuy/ ищутся все, а в остальном коде — без них
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

# Где что разрешено: вход (auth), радиокнопка офиса и стрелка месяца (dom_source, discovery)
ALLOWED = {
    "infrastructure/gubuy/auth.py": {
        "_choose": {"select_option", "click"},
        "_click_text": {"click"},
        "_fill_identity": {"fill"},
    },
    "infrastructure/gubuy/dom_source.py": {
        "_select_location": {"check"},
        "_read_months": {"click"},
    },
    "infrastructure/gubuy/discovery.py": {"_walk_calendar": {"check", "click"}},
}


def _actions(module: Module) -> list[tuple[str, str, str, int]]:
    names = ACTIONS if module.rel.startswith("infrastructure/gubuy/") else ACTIONS - AMBIGUOUS
    return [
        (func, attr_name(call), receiver(call), call.lineno)
        for call, func in calls(module)
        if attr_name(call) in names and not (attr_name(call) == "clear" and "jar" in receiver(call))
    ]


def test_действия_над_страницей_только_в_разрешённых_файлах_и_функциях() -> None:
    violations = []
    for module in ALL:
        allowed = ALLOWED.get(module.rel, {})
        for func, action, _, line in _actions(module):
            if action not in allowed.get(func, set()):
                violations.append(f"{module.rel}:{line}: {func}() вызывает .{action}()")
    assert violations == []


def test_разрешённые_действия_действительно_есть_контроль_что_сканер_не_слеп() -> None:
    found = {(m.rel, func, action) for m in ALL for func, action, _, _ in _actions(m)}
    expected = {
        (rel, func, action)
        for rel, funcs in ALLOWED.items()
        for func, actions in funcs.items()
        for action in actions
    }
    assert expected <= found


def test_клик_в_dom_source_и_разведке_только_по_стрелке_следующего_месяца() -> None:
    for rel in ("infrastructure/gubuy/dom_source.py", "infrastructure/gubuy/discovery.py"):
        module = next(m for m in ALL if m.rel == rel)
        clicks = [r for _, a, r, _ in _actions(module) if a == "click"]
        assert clicks, rel
        for target in clicks:
            assert "NEXT_MONTH" in target or "ui-datepicker-next" in target, (rel, target)


def test_check_в_dom_source_и_разведке_только_по_радиокнопке_офиса() -> None:
    for rel in ("infrastructure/gubuy/dom_source.py", "infrastructure/gubuy/discovery.py"):
        module = next(m for m in ALL if m.rel == rel)
        checks = [r for _, a, r, _ in _actions(module) if a == "check"]
        assert checks, rel
        assert all(r.startswith("radio") for r in checks), (rel, checks)


def test_в_поля_входа_вводятся_только_номер_документа_и_пароль() -> None:
    module = next(m for m in ALL if m.rel == "infrastructure/gubuy/auth.py")
    values = [ast.unparse(call.args[0]) for call, _ in calls(module) if attr_name(call) == "fill"]
    assert len(values) == 2
    assert any("doc_number" in v for v in values)
    assert any("password" in v for v in values)
    assert all("get_secret_value" in v for v in values)


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


MUTATING_JS = re.compile(
    r"\.(click|submit|focus|blur|dispatchEvent|scrollIntoView|remove|append\w*|insert\w*|"
    r"setAttribute|removeAttribute|reset)\s*\(|\.\w+\s*=(?!=)|\bfetch\s*\(|XMLHttpRequest|"
    r"\blocation\b|\bwindow\.open|innerHTML|document\.cookie|localStorage|sessionStorage"
)


def _js_sources() -> dict[str, str]:
    found = {"READ_MONTH_JS": READ_MONTH_JS}
    for module in ALL:
        for call, _ in calls(module):
            if attr_name(call) in {"evaluate", "wait_for_function", "add_init_script"}:
                for node in call.args:
                    if isinstance(node, ast.Constant) and isinstance(node.value, str):
                        found[f"{module.rel}:{call.lineno}"] = node.value
    return found


def test_js_на_странице_ничего_не_меняет() -> None:
    sources = _js_sources()
    assert len(sources) >= 3, "JS из evaluate не найден: проверка ничего не проверила бы"
    bad = {name: m.group(0) for name, js in sources.items() if (m := MUTATING_JS.search(js))}
    assert bad == {}


def test_контроль_шаблон_мутирующего_js_ловит_изменения() -> None:
    for js in (
        "el.click()",
        "form.submit()",
        "input.value = '1'",
        "document.body.innerHTML = ''",
        "fetch('/x', {method: 'POST'})",
        "el.dispatchEvent(new Event('click'))",
        "window.location = '/x'",
    ):
        assert MUTATING_JS.search(js), js
    assert not MUTATING_JS.search("() => document.querySelector('.a').textContent")


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
    """Слова вопросов шага 1 встречаются только как признаки страницы (pages.py)."""
    pattern = re.compile(r"(?i)cu[aá]ntos minutos|n[uú]mero est[aá] antes|minutos tiene")
    found = [
        f"{m.rel}:{line}" for m in ALL for text, line in string_constants(m) if pattern.search(text)
    ]
    assert found == []


def test_страница_шага_1_зовёт_владельца_и_не_ведёт_к_вводу() -> None:
    from rcbot.infrastructure.gubuy.pages import STEP1_MARKERS, PageKind, classify

    for marker in STEP1_MARKERS:
        assert classify("https://bpmgob.mec.gub.uy/x", f"<p>{marker}</p>", 200) is PageKind.STEP1


def test_dom_source_на_шаге_1_поднимает_manualsteprequired_а_не_действует() -> None:
    from rcbot.domain.errors import ManualStepRequired
    from rcbot.infrastructure.gubuy.dom_source import DomSource

    with pytest.raises(ManualStepRequired):
        DomSource._raise_for_page(
            "https://bpmgob.mec.gub.uy/etapas/ejecutar/TEST-ETAPA",
            "Verificación de seguridad ¿Cuántos minutos tiene una hora?",
            200,
        )
