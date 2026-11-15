"""Заглушки до разведки: модель ответа API и выключенный источник SAE (SPEC 2.3, D1, D6)."""

from __future__ import annotations

import pytest

from rcbot.domain.errors import UnexpectedResponse
from rcbot.infrastructure.gubuy.schemas import parse_disponibilidades


@pytest.mark.parametrize(
    "payload", [None, {}, [], {"dias": [1, 2]}, "x", 5, [{"fecha": "2027-02-23"}]]
)
def test_разбор_ответа_api_нарочно_отказывает_пока_нет_дампа(payload: object) -> None:
    # Лучше переход на DomSource, чем тревога по неверно понятому ответу (SPEC 6.3)
    with pytest.raises(UnexpectedResponse, match="не разобран"):
        parse_disponibilidades(payload)
