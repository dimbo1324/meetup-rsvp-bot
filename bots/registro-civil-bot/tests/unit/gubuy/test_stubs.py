"""Заглушки до разведки: модель ответа API и выключенный источник SAE (SPEC 2.3, D1, D6)."""

from __future__ import annotations

import pytest
from tests.fakes import TARGET

from rcbot.application.ports import SessionCookie
from rcbot.domain.errors import SourceNotConfigured, UnexpectedResponse
from rcbot.domain.models import SourceKind
from rcbot.infrastructure.gubuy.sae_source import SaeSource
from rcbot.infrastructure.gubuy.schemas import parse_disponibilidades


@pytest.mark.parametrize(
    "payload", [None, {}, [], {"dias": [1, 2]}, "x", 5, [{"fecha": "2027-02-23"}]]
)
def test_разбор_ответа_api_нарочно_отказывает_пока_нет_дампа(payload: object) -> None:
    # Лучше переход на DomSource, чем тревога по неверно понятому ответу (SPEC 6.3)
    with pytest.raises(UnexpectedResponse, match="не разобран"):
        parse_disponibilidades(payload)


async def test_sae_выключен_и_ничего_не_держит() -> None:
    source = SaeSource()
    assert source.kind is SourceKind.SAE
    source.use_cookies([SessionCookie("simple_bpm_session", "TEST", "bpmgob.mec.gub.uy")])
    with pytest.raises(SourceNotConfigured, match="SAE"):
        await source.fetch(TARGET)
    await source.close()
    await source.close()
