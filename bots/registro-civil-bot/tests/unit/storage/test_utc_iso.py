"""UtcIso: время в БД — только aware, только UTC, строки сортируются как моменты (SPEC 2.7)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from hypothesis import given
from hypothesis import strategies as st

from rcbot.infrastructure.db.tables import UtcIso

TYPE = UtcIso()
MONTEVIDEO = timezone(timedelta(hours=-3))


def to_db(value: datetime | None) -> str | None:
    return TYPE.process_bind_param(value, None)  # type: ignore[arg-type]


def from_db(value: object) -> datetime | None:
    return TYPE.process_result_value(value, None)  # type: ignore[arg-type]


def test_aware_utc_туда_и_обратно_без_потерь() -> None:
    moment = datetime(2026, 11, 12, 15, 0, 7, 123456, tzinfo=UTC)
    assert from_db(to_db(moment)) == moment


def test_формат_строки_фиксированный_с_микросекундами_и_смещением_utc() -> None:
    assert to_db(datetime(2026, 11, 12, 15, 0, 7, tzinfo=UTC)) == "2026-11-12T15:00:07.000000+00:00"


def test_naive_время_отклоняется() -> None:
    with pytest.raises(ValueError, match="aware"):
        to_db(datetime(2026, 11, 12, 15, 0, 0))


def test_смещённое_время_нормализуется_к_utc() -> None:
    local = datetime(2026, 11, 12, 12, 0, 0, tzinfo=MONTEVIDEO)
    assert to_db(local) == "2026-11-12T15:00:00.000000+00:00"
    restored = from_db(to_db(local))
    assert restored == local
    assert restored is not None and restored.utcoffset() == timedelta(0)


def test_none_остаётся_none() -> None:
    assert to_db(None) is None
    assert from_db(None) is None


def test_строка_без_смещения_читается_как_utc_а_не_как_местное_время_машины() -> None:
    # Значение могли записать вручную или старым кодом; зависеть от часового пояса ноутбука нельзя
    assert from_db("2026-11-12T15:00:00.000000") == datetime(2026, 11, 12, 15, 0, tzinfo=UTC)


def test_сутки_в_местном_времени_не_совпадают_с_utc_но_момент_тот_же() -> None:
    near_midnight = datetime(2026, 11, 12, 23, 30, tzinfo=MONTEVIDEO)  # уже 13-е по UTC
    assert to_db(near_midnight) == "2026-11-13T02:30:00.000000+00:00"


# Фиксированные смещения: в зонах с переводом часов бывают несуществующие и неоднозначные
# «моменты», равенство для которых определено плохо и к БД отношения не имеет
offsets = st.builds(
    timezone,
    st.timedeltas(min_value=timedelta(hours=-14), max_value=timedelta(hours=14)).map(
        lambda delta: timedelta(minutes=delta // timedelta(minutes=1))
    ),
)
moments = st.datetimes(
    min_value=datetime(1990, 1, 1),
    max_value=datetime(2100, 1, 1),
    timezones=offsets | st.just(UTC),
)


@given(moments)
def test_туда_и_обратно_сохраняет_момент(moment: datetime) -> None:
    restored = from_db(to_db(moment))
    assert restored == moment
    assert restored is not None and restored.tzinfo is UTC


@given(moments, moments)
def test_порядок_строк_равен_порядку_моментов(a: datetime, b: datetime) -> None:
    sa, sb = to_db(a), to_db(b)
    assert sa is not None and sb is not None
    assert (sa < sb) == (a < b)
    assert (sa == sb) == (a == b)
