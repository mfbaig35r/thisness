from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from thisness._canonical import canonical_json, digest


def test_key_order_does_not_change_the_digest():
    assert digest({"b": 1, "a": [1, {"y": 2, "x": 3}]}) == digest(
        {"a": [1, {"x": 3, "y": 2}], "b": 1}
    )


def test_dates_serialize_as_iso():
    stamp = datetime(2026, 10, 7, tzinfo=UTC)
    assert canonical_json({"d": date(2026, 10, 7), "t": stamp}) == (
        '{"d":"2026-10-07","t":"2026-10-07T00:00:00+00:00"}'
    )


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ({"s": {1, 2}}, "no defined order"),
        ({"t": datetime(2026, 10, 7)}, "naive datetimes"),
        ({"o": object()}, "not JSON-serializable"),
    ],
)
def test_ambiguous_values_are_rejected(value, message):
    with pytest.raises(TypeError, match=message):
        canonical_json(value)


def test_nan_is_rejected():
    with pytest.raises(ValueError):
        canonical_json({"x": float("nan")})
