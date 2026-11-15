"""Рабочие команды: check, run, simulate."""

from __future__ import annotations

import argparse
import asyncio
import importlib
import sys
from collections import defaultdict
from collections.abc import Callable

from rcbot.application.ports import CheckRecord
from rcbot.bootstrap import Core, build_core, build_source, transient_target
from rcbot.cli.exclusive import run_exclusive
from rcbot.domain.errors import RcbotError
from rcbot.domain.models import AvailabilitySnapshot, DeliveryResult, Slot, SourceKind
from rcbot.infrastructure.win.power import KeepAwake
from rcbot.runner import run_app


def _print_snapshot(snapshot: AvailabilitySnapshot) -> None:
    print(f"Источник: {snapshot.source.value}, проверка заняла {snapshot.latency_ms:.0f} мс.")
    if not snapshot.slots:
        if snapshot.dates_unknown:
            print(
                "Места появились: надпись «нет мест» пропала, даты не разобраны — откройте запись."
            )
        else:
            print("Свободных мест нет.")
        return
    by_month: dict[tuple[int, int], list[Slot]] = defaultdict(list)
    for slot in sorted(snapshot.slots):
        by_month[(slot.day.year, slot.day.month)].append(slot)
    for (year, month), slots in sorted(by_month.items()):
        print(f"{month:02}.{year}: " + ", ".join(s.label() for s in slots))


async def _check_once(core: Core) -> tuple[SourceKind, AvailabilitySnapshot | RcbotError]:
    """Одна проверка; вид источника нужен и при ошибке: запись в журнале не должна его путать."""
    source = build_source(core)
    try:
        return source.kind, await source.fetch(transient_target(core))
    except RcbotError as error:
        return source.kind, error
    finally:
        await source.close()


def cmd_check(_: argparse.Namespace) -> int:
    with build_core(command="check") as core:
        return run_exclusive(core.paths, lambda: _check(core))


def _check(core: Core) -> int:
    kind, outcome = asyncio.run(_check_once(core))
    if isinstance(outcome, RcbotError):
        core.journal.check(
            CheckRecord(
                core.clock.now(),
                kind,
                False,
                frozenset(),
                0.0,
                "check-once",
                0.0,
                type(outcome).__name__,
                str(outcome),
            )
        )
        print(f"Проверка не удалась: {type(outcome).__name__}: {outcome}", file=sys.stderr)
        return 1
    core.journal.check(
        CheckRecord(
            outcome.observed_at,
            outcome.source,
            True,
            outcome.slots,
            outcome.latency_ms,
            "check-once",
            0.0,
        )
    )
    _print_snapshot(outcome)
    return 0


def _loop_factory() -> Callable[[], asyncio.AbstractEventLoop] | None:
    """uvloop — только на Linux; на Windows нужен стандартный ProactorEventLoop (SPEC 2.1)."""
    if sys.platform != "linux":
        return None
    try:
        # Динамический импорт: на Windows пакета нет, и проверка типов не должна его искать
        uvloop = importlib.import_module("uvloop")
    except ImportError:
        return None
    factory: Callable[[], asyncio.AbstractEventLoop] = uvloop.new_event_loop
    return factory


def _awake(body: Callable[[], int]) -> Callable[[], int]:
    def awake_body() -> int:
        with KeepAwake():
            return body()

    return awake_body


def cmd_run(_: argparse.Namespace) -> int:
    # pythonw.exe (Планировщик) запускается без консоли: пишем только в файл лога
    with build_core(console=sys.stderr is not None, command="run") as core:
        return run_exclusive(
            core.paths,
            _awake(lambda: asyncio.run(run_app(core), loop_factory=_loop_factory())),
        )


def _print_delivery(result: DeliveryResult) -> None:
    mark = "✅" if result.ok else "❌"
    error = f" — {result.error}" if result.error else ""
    channel, attempt = result.channel.value, result.attempt
    print(f"{mark} {channel}, попытка {attempt}: {result.latency_ms:.0f} мс{error}")


def cmd_simulate(args: argparse.Namespace) -> int:
    with build_core(command="simulate") as core:

        def body() -> int:
            return asyncio.run(
                run_app(core, simulate_wait_sec=args.wait, on_delivery=_print_delivery),
                loop_factory=_loop_factory(),
            )

        return run_exclusive(core.paths, _awake(body))
