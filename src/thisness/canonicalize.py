"""Canonicalizers build an entity's snapshot from its member observations.

A snapshot is what Thisness currently believes about an entity. It is derived,
versioned with the entity, and never used for scoring (design 03 §9 compares
through members, not snapshots).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from pydantic import JsonValue

from thisness._canonical import canonical_json
from thisness.core.components import ComponentRef
from thisness.core.observation import Observation


class Canonicalizer(Protocol):
    ref: ComponentRef

    def build(self, observations: Sequence[Observation]) -> dict[str, JsonValue]: ...


class MostFrequentValue:
    """For each attribute, the most frequent non-null value across members.

    Ties go to the value seen first in observation-id order, which is ingest order.
    Redacted observations contribute nothing.
    """

    ref = ComponentRef.of("canonicalizer", "most_frequent_value", "1", {})

    def build(self, observations: Sequence[Observation]) -> dict[str, JsonValue]:
        counts: dict[str, Counter[str]] = {}
        first_seen: dict[tuple[str, str], int] = {}
        values: dict[str, JsonValue] = {}
        ordered = sorted(observations, key=lambda o: o.observation_id)
        for position, obs in enumerate(ordered):
            for attribute, value in (obs.attributes or {}).items():
                if value is None:
                    continue
                key = canonical_json(value)
                counts.setdefault(attribute, Counter())[key] += 1
                first_seen.setdefault((attribute, key), position)
                values.setdefault(key, value)
        result: dict[str, JsonValue] = {}
        for attribute in sorted(counts):
            best = min(
                counts[attribute],
                key=lambda k: (-counts[attribute][k], first_seen[(attribute, k)]),
            )
            result[attribute] = values[best]
        return result


class ReadableSpelling:
    """Most frequent value for every attribute, but for ``attribute`` the most readable
    spelling of its most supported key (design 08 §5.1): among the records whose name has
    the entity's most frequent key, a spelling the key function needed to clean nothing
    from, then one in mixed case (most of its words with a lower-case letter), then the
    most frequent. "HALEON UK FINANCE LIMITED" (3)
    and "Haleon Uk Finance Limited" (1) share a key; the label is the second. When every
    spelling carried something cleaned off ("SQ *ACME"), the label is the cleaned text."""

    def __init__(self, attribute: str, key: Any, ref_name: str, version: str = "1") -> None:
        self.attribute = attribute
        self.key = key  # raw value -> (key, cleaned text) or None
        self.base = MostFrequentValue()
        self.ref = ComponentRef.of("canonicalizer", ref_name, version, {"attribute": attribute})

    def build(self, observations: Sequence[Observation]) -> dict[str, JsonValue]:
        result = self.base.build(observations)
        ordered = sorted(observations, key=lambda o: o.observation_id)
        spellings: list[tuple[str, str, str]] = []  # key, raw, cleaned
        for obs in ordered:
            raw = (obs.attributes or {}).get(self.attribute)
            if isinstance(raw, str) and (found := self.key(raw)) is not None:
                spellings.append((found[0], raw, found[1]))
        if not spellings:
            return result
        keys = Counter(k for k, _, _ in spellings)
        first = {k: i for i, (k, _, _) in reversed(list(enumerate(spellings)))}
        best = min(keys, key=lambda k: (-keys[k], first[k]))
        mine = [(raw, cleaned) for k, raw, cleaned in spellings if k == best]
        counts = Counter(raw for raw, _ in mine)
        order = {raw: i for i, (raw, _) in reversed(list(enumerate(mine)))}

        def readable(item: tuple[str, str]) -> tuple:
            raw, cleaned = item
            # Written in mixed case: most of its words have a lower-case letter, so a
            # capitals name with "Ltd." on the end does not count (design 08 §13.3).
            words = [w for w in raw.split() if sum(c.isalpha() for c in w) > 1]
            lower = sum(any(c.islower() for c in w) for w in words)
            mixed = bool(words) and lower * 2 > len(words) and raw != raw.lower()
            return (raw.strip() != cleaned, not mixed, -counts[raw], order[raw])

        raw, cleaned = min(set(mine), key=readable)
        result[self.attribute] = raw if raw.strip() == cleaned else cleaned
        return result


class ByProfile:
    """Each entity type's own canonicalizer (design 06 §6), else most frequent value. The
    store records the canonicalizer that actually built each snapshot (``pick``)."""

    ref = ComponentRef.of("canonicalizer", "by_profile", "1", {})

    def __init__(self, profiles: Mapping[str, Any], default: Canonicalizer | None = None) -> None:
        self.profiles = profiles  # the engine's registry, read at build time
        self.default = default or MostFrequentValue()

    def pick(self, observations: Sequence[Observation]) -> Canonicalizer:
        if observations:
            profile = self.profiles.get(observations[0].entity_type)
            if profile is not None and getattr(profile, "canonicalizer", None) is not None:
                return profile.canonicalizer
        return self.default

    def build(self, observations: Sequence[Observation]) -> dict[str, JsonValue]:
        return self.pick(observations).build(observations)


class AcceptedByAuthority:
    """The canonical name is the one a named authority accepts (design 06 §3): the name
    of a member the authority lists as accepted, or the accepted name the authority
    asserts for a member it lists as a synonym. The snapshot says which authority, in
    ``accepted_according_to``. Without such a member, the most frequent value.

    The other fields describe the accepted name too. A majority over members would
    describe its synonyms, which usually outnumber it: *Castanea dentata* Borkh. read as a
    variety authored by Marshall. With the authority's accepted record among the members,
    its fields win; with only its assertions, fields that describe a single name
    (``name_fields``) are left out rather than taken from a synonym."""

    def __init__(self, authority: str, *, attribute: str = "name",
                 authorship: str = "authorship",
                 name_fields: tuple[str, ...] = ("authorship", "rank", "taxonomic_status",
                                                 "accepted_name", "identifiers")) -> None:  # fmt: skip
        self.authority = authority
        self.attribute = attribute
        self.authorship = authorship
        self.name_fields = name_fields
        self.ref = ComponentRef.of("canonicalizer", "accepted_by_authority", "2",
                                   {"authority": authority, "attribute": attribute,
                                    "authorship": authorship,
                                    "name_fields": list(name_fields)})  # fmt: skip
        self._fallback = MostFrequentValue()

    def _full(self, attrs: Mapping[str, Any]) -> str | None:
        """The name with its authorship, which a record may hold in its own field."""
        name, author = attrs.get(self.attribute), attrs.get(self.authorship)
        if not isinstance(name, str) or not name.strip():
            return None
        if isinstance(author, str) and author.strip() and not name.rstrip().endswith(author):
            return f"{name.strip()} {author.strip()}"
        return name.strip()

    def build(self, observations: Sequence[Observation]) -> dict[str, JsonValue]:
        result = self._fallback.build(observations)
        votes: Counter[str] = Counter()
        first: dict[str, int] = {}
        records: dict[str, Mapping[str, Any]] = {}  # accepted name -> its own record
        ordered = sorted(observations, key=lambda o: o.observation_id)
        for position, obs in enumerate(ordered):
            attrs = obs.attributes or {}
            if obs.source.type != self.authority:
                continue
            status = str(attrs.get("taxonomic_status") or "").casefold()
            if status == "accepted":
                name = self._full(attrs)
                if name is not None:
                    records.setdefault(name, attrs)
            else:
                name = attrs.get("accepted_name")
            if isinstance(name, str) and name.strip():
                votes[name] += 1
                first.setdefault(name, position)
        if not votes:
            return result
        accepted = min(votes, key=lambda n: (-votes[n], first[n]))
        for field_name in self.name_fields:
            result.pop(field_name, None)
        if accepted in records:
            own = records[accepted]
            result.update({k: v for k, v in own.items() if v is not None})
            result.pop("accepted_name", None)
        result[self.attribute] = accepted
        result["accepted_according_to"] = self.authority
        return result
