from __future__ import annotations

import pytest

from thisness.ids import (
    RESERVED_PREFIXES,
    IdFactory,
    id_prefix,
    id_timestamp_ms,
    node_kind,
    parse_id,
    validate_entity_prefix,
)


def test_ids_are_prefixed_ulids(ids):
    value = ids.observation()
    prefix, ulid = parse_id(value)
    assert prefix == "obs"
    assert len(ulid) == 26
    assert set(ulid) <= set("0123456789ABCDEFGHJKMNPQRSTVWXYZ")


def test_seeded_factories_reproduce_the_same_ids():
    a, b = IdFactory.seeded(42), IdFactory.seeded(42)
    assert [a.entity("org") for _ in range(5)] == [b.entity("org") for _ in range(5)]
    assert IdFactory.seeded(1).observation() != IdFactory.seeded(2).observation()


def test_ids_sort_in_creation_order_even_within_one_millisecond():
    factory = IdFactory(clock_ms=lambda: 1_790_000_000_000)
    values = [factory.ulid() for _ in range(1000)]
    assert values == sorted(values)
    assert len(set(values)) == 1000


def test_ids_stay_ordered_if_the_clock_goes_backwards():
    times = iter([2_000, 1_000, 1_000])
    factory = IdFactory(clock_ms=lambda: next(times))
    values = [factory.ulid() for _ in range(3)]
    assert values == sorted(values)


def test_random_overflow_within_a_millisecond_advances_the_timestamp():
    factory = IdFactory(clock_ms=lambda: 5_000, randbits=lambda bits: (1 << bits) - 1)
    first = factory.new("obs")
    second = factory.new("obs")
    assert second > first
    assert id_timestamp_ms(second) == 5_001


def test_timestamp_round_trips():
    factory = IdFactory(clock_ms=lambda: 1_790_000_123_456)
    assert id_timestamp_ms(factory.decision()) == 1_790_000_123_456


@pytest.mark.parametrize("prefix", sorted(RESERVED_PREFIXES))
def test_reserved_prefixes_cannot_name_entities(ids, prefix):
    with pytest.raises(ValueError, match="reserved"):
        ids.entity(prefix)


@pytest.mark.parametrize("prefix", ["", "Org", "1org", "org_x", "a" * 17])
def test_malformed_prefixes_are_rejected(prefix):
    with pytest.raises(ValueError, match="invalid id prefix"):
        validate_entity_prefix(prefix)


def test_node_kind_distinguishes_observations_entities_and_records(ids):
    assert node_kind(ids.observation()) == "observation"
    assert node_kind(ids.entity("org")) == "entity"
    with pytest.raises(ValueError, match="not an observation or entity"):
        node_kind(ids.decision())


@pytest.mark.parametrize("value", ["obs", "obs_123", "obs_01K9ZQ4T7M3X0B2V8N6C5D1E2I", "OBS_x"])
def test_parse_rejects_non_ids(value):
    with pytest.raises(ValueError, match="not a Thisness id"):
        id_prefix(value)


def test_out_of_range_clock_is_rejected():
    with pytest.raises(ValueError, match="48-bit"):
        IdFactory(clock_ms=lambda: 1 << 48).ulid()
