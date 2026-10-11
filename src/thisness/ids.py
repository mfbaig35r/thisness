"""Prefixed, time-ordered identifiers (design 02 §3).

Every identifier is ``{prefix}_{ULID}``: a lowercase prefix naming the kind of
object, then a 26-character ULID (48-bit millisecond timestamp, 80 random bits,
Crockford base32). IDs from one factory sort in creation order, including several
within the same millisecond.

Entity prefixes are declared by profiles (``org``, ``person``). The other prefixes
are reserved for Thisness's own records.
"""

from __future__ import annotations

import random
import re
import secrets
import threading
import time
from collections.abc import Callable
from typing import Literal

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_DECODE = {c: i for i, c in enumerate(_CROCKFORD)}

OBSERVATION_PREFIX = "obs"
RESERVED_PREFIXES = frozenset(
    {"obs", "mem", "ev", "dec", "run", "stg", "prop", "rel", "rev", "evt", "cmt", "con", "llm"}
)

_PREFIX_RE = re.compile(r"^[a-z][a-z0-9]{0,15}$")
_ID_RE = re.compile(r"^([a-z][a-z0-9]{0,15})_([0-9A-HJKMNP-TV-Z]{26})$")

_TIMESTAMP_LIMIT = 1 << 48
_RANDOM_LIMIT = 1 << 80

NodeKind = Literal["observation", "entity"]


def _encode(value: int, length: int) -> str:
    chars = []
    for _ in range(length):
        value, remainder = divmod(value, 32)
        chars.append(_CROCKFORD[remainder])
    return "".join(reversed(chars))


def validate_prefix(prefix: str) -> str:
    if not _PREFIX_RE.match(prefix):
        raise ValueError(f"invalid id prefix {prefix!r}: expected [a-z][a-z0-9]{{0,15}}")
    return prefix


def validate_entity_prefix(prefix: str) -> str:
    validate_prefix(prefix)
    if prefix in RESERVED_PREFIXES:
        raise ValueError(f"id prefix {prefix!r} is reserved and cannot be used for entities")
    return prefix


def parse_id(value: str) -> tuple[str, str]:
    """Split an id into (prefix, ulid). Raises ValueError if it is not a Thisness id."""
    match = _ID_RE.match(value)
    if not match:
        raise ValueError(f"not a Thisness id: {value!r}")
    return match.group(1), match.group(2)


def id_prefix(value: str) -> str:
    return parse_id(value)[0]


def id_timestamp_ms(value: str) -> int:
    """Milliseconds since the Unix epoch encoded in an id's ULID."""
    ulid = parse_id(value)[1]
    result = 0
    for char in ulid[:10]:
        result = result * 32 + _DECODE[char]
    return result


def node_kind(value: str) -> NodeKind:
    """Whether an id names an observation or an entity. Other record ids are errors."""
    prefix = id_prefix(value)
    if prefix == OBSERVATION_PREFIX:
        return "observation"
    if prefix in RESERVED_PREFIXES:
        raise ValueError(f"{value!r} is a {prefix!r} record, not an observation or entity")
    return "entity"


class IdFactory:
    """Generates prefixed ULIDs, monotonically increasing within one factory.

    Inject a clock and a random source for reproducible ids; see ``seeded``.
    """

    def __init__(
        self,
        *,
        clock_ms: Callable[[], int] | None = None,
        randbits: Callable[[int], int] | None = None,
    ) -> None:
        self._clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)
        self._randbits = randbits or secrets.randbits
        self._lock = threading.Lock()
        self._last_ms = -1
        self._last_random = 0

    @classmethod
    def seeded(cls, seed: int, *, start_ms: int = 1_790_000_000_000) -> IdFactory:
        """A deterministic factory for tests: a fixed random seed and a clock that
        advances one millisecond per id."""
        rng = random.Random(seed)
        tick = iter(range(start_ms, _TIMESTAMP_LIMIT))
        return cls(clock_ms=lambda: next(tick), randbits=rng.getrandbits)

    def ulid(self) -> str:
        with self._lock:
            ms = self._clock_ms()
            if not 0 <= ms < _TIMESTAMP_LIMIT:
                raise ValueError(f"timestamp {ms} is outside the 48-bit ULID range")
            if ms > self._last_ms:
                self._last_ms = ms
                self._last_random = self._randbits(80)
            else:
                # Same (or earlier) millisecond: keep order by incrementing.
                self._last_random += 1
                if self._last_random >= _RANDOM_LIMIT:
                    self._last_ms += 1
                    self._last_random = self._randbits(80)
            return _encode(self._last_ms, 10) + _encode(self._last_random, 16)

    def new(self, prefix: str) -> str:
        return f"{validate_prefix(prefix)}_{self.ulid()}"

    def entity(self, prefix: str) -> str:
        return f"{validate_entity_prefix(prefix)}_{self.ulid()}"

    def observation(self) -> str:
        return self.new("obs")

    def membership(self) -> str:
        return self.new("mem")

    def evidence(self) -> str:
        return self.new("ev")

    def decision(self) -> str:
        return self.new("dec")

    def run(self) -> str:
        return self.new("run")

    def stage(self) -> str:
        return self.new("stg")

    def proposal(self) -> str:
        return self.new("prop")

    def relationship(self) -> str:
        return self.new("rel")

    def review(self) -> str:
        return self.new("rev")

    def event(self) -> str:
        return self.new("evt")

    def commit(self) -> str:
        return self.new("cmt")

    def constraint(self) -> str:
        return self.new("con")

    def invocation(self) -> str:
        return self.new("llm")
