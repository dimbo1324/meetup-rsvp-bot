"""Часы стенда: `ManualClock` + пошаговый перебор сроков (между ними успевает настоящий ввод-вывод).

`ManualClock.advance` будит всех спящих «залпом» и лишь потом отдаёт управление; с настоящими
сокетами и SQLite проверка, начатая в момент пробуждения, не успевает закончиться до следующего
пробуждения, и расписание искажается. Здесь срок будится по одному, а стенд между ними ждёт
тишины (см. `Stand.advance`).
"""

from __future__ import annotations

import heapq

from tests.fakes import ManualClock


class StandClock(ManualClock):
    def next_deadline(self) -> float | None:
        """Ближайший срок живого спящего или None."""
        while self._sleepers and self._sleepers[0][2].done():
            heapq.heappop(self._sleepers)
        return self._sleepers[0][0] if self._sleepers else None

    def fire_next(self) -> None:
        """Перевести часы на ближайший срок и разбудить одного спящего."""
        deadline, _, future = heapq.heappop(self._sleepers)
        self._set_mono(deadline)
        future.set_result(None)

    def jump_to(self, mono: float) -> None:
        self._set_mono(mono)
