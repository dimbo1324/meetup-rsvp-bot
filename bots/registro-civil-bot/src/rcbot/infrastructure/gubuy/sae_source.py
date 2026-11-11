"""`SaeSource` — необязательный вторичный источник, в v1 выключен (SPEC 2.3, D6).

Чтение публичной страницы SAE не реализовано: формат страницы не разобран, а находки SAE по
SPEC — только «неподтверждённые» и без звонка. Если владелец решит включить источник (вопрос
об этом — на этапе аудита), разбор пишется по снимку страницы, как для `ApiSource`.
"""

from __future__ import annotations

from collections.abc import Sequence

from rcbot.application.ports import SessionCookie
from rcbot.domain.errors import SourceNotConfigured
from rcbot.domain.models import AvailabilitySnapshot, SourceKind, WatchTarget


class SaeSource:
    @property
    def kind(self) -> SourceKind:
        return SourceKind.SAE

    def use_cookies(self, cookies: Sequence[SessionCookie]) -> None:
        """SAE открывается без входа: cookies сессии bpmgob ему не нужны."""

    async def fetch(self, target: WatchTarget) -> AvailabilitySnapshot:
        raise SourceNotConfigured("источник SAE в v1 не реализован (D6)")

    async def close(self) -> None:
        """Ресурсов не держит."""
