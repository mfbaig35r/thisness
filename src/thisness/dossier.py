"""The dossier: everything Thisness believes about one entity (design 04 §3).

Built from stored records only, at one ledger position, so the same position always
gives the same dossier. ``to_context`` renders it as compact Markdown for a language
model, with a pointer back to an observation or decision on every statement.
"""

from __future__ import annotations

import unicodedata
from collections import Counter
from dataclasses import dataclass
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from thisness._canonical import canonical_json
from thisness.core.identity import Entity, EntitySnapshot, RelationshipAssertion
from thisness.core.observation import SourceRef
from thisness.core.review import ReviewItem
from thisness.llm.privacy import OMIT, shown
from thisness.relationships import RelationshipCandidate, relationship_candidates
from thisness.storage.errors import InvalidOperation, NotFound

if TYPE_CHECKING:
    from thisness.core.observation import Observation
    from thisness.engine import Thisness
    from thisness.profiles.base import ResolutionProfile

CITE = 3  # observation ids cited per statement before "+N"


@dataclass(frozen=True)
class ValueSupport:
    value: Any  # the most frequent spelling among the observations carrying it
    observations: tuple[str, ...]
    sources: tuple[tuple[str, int], ...]  # source type, observations from it


@dataclass(frozen=True)
class AttributeSupport:
    attribute: str
    canonical: Any  # the snapshot's value
    values: tuple[ValueSupport, ...]  # most supported first
    observed: int  # observations carrying the attribute
    # The group the snapshot's value normalizes into, found by the dossier's grouping
    # ("Castanea crenata Siebold & Zucc." is the group of records naming Castanea crenata).
    matched: ValueSupport | None = None

    @property
    def shown(self) -> ValueSupport | None:
        """The values supporting what the dossier shows: the snapshot's value when it
        has one, which need not be the most supported (a taxon's accepted name is
        usually outnumbered by its synonyms); None when no member carries it."""
        if self.matched is not None:
            return self.matched
        if self.canonical is None:
            return self.values[0] if self.values else None
        # A multi-valued attribute's snapshot is a list; its values are grouped per item.
        items = self.canonical if isinstance(self.canonical, list) else [self.canonical]
        wanted = {canonical_json(item) for item in items}
        return next((v for v in self.values if canonical_json(v.value) in wanted), None)


@dataclass(frozen=True)
class AuthorityView:
    """What one source says about where some of the members belong (design 06 §3)."""

    source: str  # source type
    accepted: str  # the accepted name it gives, as it most often writes it
    observations: tuple[str, ...]
    statuses: tuple[tuple[str, int], ...]  # status for the record's own name, count
    agrees: bool  # the accepted name is the entity's label
    reference: bool  # the profile's reference authority


@dataclass(frozen=True)
class Contradiction:
    attribute: str
    feature: str
    scheme: str | None
    sides: tuple[ValueSupport, ...]


@dataclass(frozen=True)
class Lineage:
    redirected_from: str | None  # the retired id the dossier was asked for
    merged_from: tuple[tuple[str, int], ...]  # (entity, seq retired into this one)
    split_into: tuple[tuple[str, int], ...]  # (entity, seq)
    split_from: tuple[tuple[str, int], ...]  # (entity, seq)


@dataclass(frozen=True)
class HistorySummary:
    created_seq: int
    last_seq: int
    events: tuple[tuple[str, int], ...]  # event type, count
    decisions: int


@dataclass(frozen=True)
class MemberView:
    observation_id: str
    source: SourceRef
    attributes: dict[str, Any] | None  # None when redacted
    observed_at: str | None


@dataclass(frozen=True)
class RelationshipView:
    relationship: RelationshipAssertion
    other_id: str
    other_label: str
    role: str  # "subject" when this entity is the subject


@dataclass(frozen=True)
class Dossier:
    entity: Entity
    snapshot: EntitySnapshot | None
    label: str
    as_of_seq: int
    lineage: Lineage
    history: HistorySummary
    attributes: tuple[AttributeSupport, ...]
    contradictions: tuple[Contradiction, ...]
    member_count: int
    redacted: int
    members: tuple[MemberView, ...]  # one page
    members_offset: int
    relationships: tuple[RelationshipView, ...]
    open_reviews: tuple[ReviewItem, ...]
    relationship_candidates: tuple[tuple[RelationshipCandidate, str], ...]  # with labels
    policies: dict[str, str]  # attribute -> external model policy
    authorities: tuple[AuthorityView, ...] = ()  # reference first

    def to_context(self, max_chars: int = 4000) -> str:
        return _render(self, max_chars)


def _key(value: Any) -> str:
    if isinstance(value, str):
        return " ".join(unicodedata.normalize("NFKC", value).casefold().split())
    return canonical_json(value)


def _support(
    groups: dict[str, list[tuple[str, Any]]],
    source_of: dict[str, SourceRef],
    preferred: Any = None,
) -> tuple[ValueSupport, ...]:
    """Values grouped by key, as ValueSupport, most supported first. A group shows its
    most frequent spelling, or ``preferred`` (the snapshot's value) when it has it."""
    out = []
    for items in groups.values():
        spellings = Counter(canonical_json(v) for _, v in items)
        first = {}
        for _, v in items:
            first.setdefault(canonical_json(v), v)
        best = min(spellings, key=lambda k: (-spellings[k], k))
        if preferred is not None and canonical_json(preferred) in spellings:
            best = canonical_json(preferred)
        observations = tuple(sorted({o for o, _ in items}))
        sources = Counter(source_of[o].type for o in observations)
        out.append(ValueSupport(first[best], observations, tuple(sorted(sources.items()))))
    out.sort(key=lambda v: (-len(v.observations), v.observations[0]))
    return tuple(out)


class _ValueKeys:
    """Groups attribute values by the profile's normalized feature (``value_keys``)."""

    def __init__(self, profile: ResolutionProfile | None) -> None:
        self.value_keys = profile.value_keys if profile else {}
        self.normalizers = profile.normalizers if profile else ()

    def key(self, attributes: dict[str, Any], attribute: str, item: Any) -> str:
        feature = self.value_keys.get(attribute)
        if feature is not None:
            # The record with only this one value of the attribute: the normalizer sees
            # the context it needs (a phone number's country) and nothing else varies.
            probe = SimpleNamespace(attributes={**attributes, attribute: item})
            found = {f.value for n in self.normalizers if feature in n.features
                     for f in n.normalize(probe) if f.feature == feature}  # fmt: skip
            if len(found) == 1:
                return "=" + found.pop()
        return _key(item)


def _label(snapshot: EntitySnapshot | None, entity_id: str) -> str:
    if snapshot is not None:
        name = snapshot.attributes.get("name")
        if isinstance(name, str) and name:
            return name
    return entity_id


def _snapshot(engine: Thisness, entity_id: str) -> EntitySnapshot | None:
    try:
        return engine.store.snapshot(entity_id)
    except NotFound:
        return None


def build(
    engine: Thisness, entity_id: str, *, members_offset: int = 0, members_limit: int = 50
) -> Dossier:
    store = engine.store
    as_of = store.head_seq()
    redirected_from = None
    entity = store.entity(entity_id)
    if entity.state == "retired":
        live = store.resolve_id(entity_id)
        if len(live) != 1:
            raise InvalidOperation(
                -1, f"{entity_id} is retired and resolves to {len(live)} entities: {live}"
            )
        redirected_from, entity = entity_id, store.entity(live[0])
    eid = entity.entity_id
    profile: ResolutionProfile | None = engine.profiles.get(entity.entity_type)
    snapshot = _snapshot(engine, eid)

    member_ids = store.members(eid)
    observations = store.observations(member_ids)
    source_of = {o.observation_id: o.source for o in observations}
    live_obs = [o for o in observations if o.attributes is not None]

    # Attribute support, in profile order, then any attributes the profile does not declare.
    # Each value is grouped under what the profile's normalizer makes of it, read in its
    # record's context (so "US", "USA" and "United States" are one country, and three
    # spellings of a phone number are one number), else under a case- and space-folded key.
    keyer = _ValueKeys(profile)
    values: dict[str, dict[str, list[tuple[str, Any]]]] = {}
    for obs in live_obs:
        for attribute, value in (obs.attributes or {}).items():
            items = [v for v in (value if isinstance(value, list) else [value])
                     if v is not None and v != ""]  # fmt: skip
            for item in items:
                key = keyer.key(obs.attributes or {}, attribute, item)
                values.setdefault(attribute, {}).setdefault(key, []).append(
                    (obs.observation_id, item)
                )
    declared = list(profile.attributes) if profile else []
    order = [a for a in declared if a in values] + sorted(set(values) - set(declared))
    canonical = snapshot.attributes if snapshot else {}
    attributes = tuple(_attribute_support(a, values[a], source_of, canonical, keyer) for a in order)

    # Contradictions: members disagreeing on a feature the profile says they should share.
    contradictions = []
    if profile and profile.contradictions:
        features = store.features([o.observation_id for o in live_obs])
        for check in profile.contradictions:
            by_scheme: dict[str | None, dict[str, list[tuple[str, Any]]]] = {}
            for obs_id in sorted(features):
                for value in features[obs_id].get(check.feature, ()):
                    by_scheme.setdefault(check.scheme(value), {}).setdefault(value, []).append(
                        (obs_id, value)
                    )
            for scheme in sorted(by_scheme, key=lambda s: s or ""):
                if len(by_scheme[scheme]) > 1:
                    contradictions.append(
                        Contradiction(
                            check.attribute,
                            check.feature,
                            scheme,
                            _support(by_scheme[scheme], source_of),
                        )
                    )

    # Lineage and history, from the ledger.
    events = list(store.ledger(entity_id=eid))
    split_into, split_from = [], []
    for e in events:
        if e.event_type == "ENTITY_SPLIT" and e.entity_id == eid:
            split_into += [(t, e.seq) for t in e.payload.get("targets", [])]
        if e.event_type in ("ENTITY_CREATED", "ENTITY_REACTIVATED") and e.entity_id == eid:
            for other in store.ledger(decision_id=e.decision_id) if e.decision_id else ():
                if other.event_type == "ENTITY_SPLIT" and eid in other.payload.get("targets", []):
                    split_from.append((other.entity_id, other.seq))
    lineage = Lineage(
        redirected_from=redirected_from,
        merged_from=tuple((m.entity_id, m.retired_seq) for m in store.merged_from(eid)),
        split_into=tuple(split_into),
        split_from=tuple(sorted(set(split_from), key=lambda x: x[1])),
    )
    history = HistorySummary(
        created_seq=entity.created_seq,
        last_seq=max((e.seq for e in events), default=entity.created_seq),
        events=tuple(sorted(Counter(e.event_type.value for e in events).items())),
        decisions=len({e.decision_id for e in events if e.decision_id}),
    )

    page = observations[members_offset : members_offset + members_limit]
    members = tuple(
        MemberView(
            o.observation_id,
            o.source,
            o.attributes,
            o.observed_at.isoformat() if o.observed_at else None,
        )
        for o in page
    )

    relationships = []
    for rel in store.relationships(eid):
        subject = rel.subject_entity_id == eid
        other = rel.object_entity_id if subject else rel.subject_entity_id
        role = "subject" if subject else "object"
        relationships.append(
            RelationshipView(rel, other, _label(_snapshot(engine, other), other), role)
        )

    involved = {eid, *member_ids}
    reviews = tuple(r for r in store.open_reviews() if involved & set(r.members))
    candidates = tuple(
        (c, _label(_snapshot(engine, other), other))
        for c in relationship_candidates(engine, eid)
        for other in [c.right_entity if c.left_entity == eid else c.left_entity]
    )
    declared_attrs = profile.attributes if profile else {}
    policies = {a: d.external_model_policy for a, d in declared_attrs.items()}
    authorities = _authorities(profile, live_obs, keyer, canonical)
    return Dossier(
        entity=entity,
        snapshot=snapshot,
        label=_label(snapshot, eid),
        as_of_seq=as_of,
        lineage=lineage,
        history=history,
        attributes=attributes,
        contradictions=tuple(contradictions),
        member_count=len(member_ids),
        redacted=len(observations) - len(live_obs),
        members=members,
        members_offset=members_offset,
        relationships=tuple(relationships),
        open_reviews=reviews,
        relationship_candidates=candidates,
        policies=policies,
        authorities=authorities,
    )


def _attribute_support(
    attribute: str,
    groups: dict[str, list[tuple[str, Any]]],
    source_of: dict[str, SourceRef],
    canonical: dict[str, Any],
    keyer: _ValueKeys,
) -> AttributeSupport:
    value = canonical.get(attribute)
    support = _support(groups, source_of, value)
    matched = None
    if value is not None and not isinstance(value, list):
        group = groups.get(keyer.key(canonical, attribute, value))
        if group:
            members = tuple(sorted({o for o, _ in group}))
            matched = next((v for v in support if v.observations == members), None)
    return AttributeSupport(
        attribute=attribute,
        canonical=value,
        values=support,
        observed=len({o for items in groups.values() for o, _ in items}),
        matched=matched,
    )


def _authorities(
    profile: ResolutionProfile | None,
    observations: list[Observation],
    keyer: _ValueKeys,
    canonical: dict[str, Any],
) -> tuple[AuthorityView, ...]:
    """Members grouped by source and by the accepted name the source gives them, the
    name itself when the source accepts it. Names are grouped as the profile normalizes
    them, so 'Castanea dentata Borkh.' and 'Castanea dentata (Marshall) Borkh.' agree."""
    spec = profile.assertions if profile else None
    if spec is None:
        return ()
    label = canonical.get(spec.name)
    label_key = keyer.key({}, spec.name, label) if isinstance(label, str) else None
    groups: dict[tuple[str, str], list[tuple[str, str, str]]] = {}
    for obs in observations:
        attrs = obs.attributes or {}
        status = attrs.get(spec.status)
        if not isinstance(status, str) or not status.strip():
            continue
        if status.casefold() in spec.accepted_statuses:
            accepted = attrs.get(spec.name)
            author = attrs.get(spec.authorship) if spec.authorship else None
            if isinstance(accepted, str) and isinstance(author, str) and author.strip() \
                    and not accepted.rstrip().endswith(author.strip()):  # fmt: skip
                accepted = f"{accepted.strip()} {author.strip()}"
        else:
            accepted = attrs.get(spec.accepted)
        if not isinstance(accepted, str) or not accepted.strip():
            continue
        key = keyer.key({}, spec.name, accepted)
        groups.setdefault((obs.source.type, key), []).append(
            (obs.observation_id, accepted.strip(), status.casefold())
        )
    views = []
    for (source, key), items in groups.items():
        spellings = Counter(a for _, a, _ in items)
        views.append(AuthorityView(
            source=source,
            accepted=min(spellings, key=lambda a: (-spellings[a], a)),
            observations=tuple(sorted(o for o, _, _ in items)),
            statuses=tuple(sorted(Counter(s for _, _, s in items).items())),
            agrees=label_key is not None and key == label_key,
            reference=source == spec.reference,
        ))  # fmt: skip
    views.sort(key=lambda v: (not v.reference, v.source, not v.agrees, -len(v.observations)))
    return tuple(views)


# -- rendering --------------------------------------------------------------------------------


def _n(count: int, noun: str) -> str:
    return f"{count} {noun}{'' if count == 1 else 's'}"


def _cite(ids: tuple[str, ...] | list[str]) -> str:
    shown = ", ".join(ids[:CITE])
    return f"[{shown}{f', +{len(ids) - CITE}' if len(ids) > CITE else ''}]"


def _shown(value: Any, policy: str) -> str | None:
    """A value as an external model may see it, or None when it may not (the shared
    rule of design 05 §3.5)."""
    result = shown(value, policy)
    if result is OMIT:
        return None
    return result if isinstance(result, str) else canonical_json(result)


def _render(d: Dossier, max_chars: int) -> str:
    def policy(attribute: str) -> str:
        return d.policies.get(attribute, "allow")

    withheld = sorted(a.attribute for a in d.attributes if _shown("", policy(a.attribute)) is None)
    e = d.entity
    sources = Counter(m.source.type for m in d.members)
    header = [
        f"# {d.label} ({e.entity_id})",
        f"{e.entity_type}, {e.state}, version {e.version}, as of ledger seq {d.as_of_seq}. "
        f"{_n(d.member_count, 'observation')}"
        + (f" ({d.redacted} redacted)" if d.redacted else "")
        + (f"; withheld by policy: {', '.join(withheld)}" if withheld else "")
        + ".",
    ]
    if d.lineage.redirected_from:
        header.append(f"{d.lineage.redirected_from} was merged into this entity.")

    sections: list[tuple[str, list[str]]] = []
    lines = []
    for c in d.contradictions:
        shown = [(_shown(v.value, policy(c.attribute)), v) for v in c.sides]
        if any(s is None for s, _ in shown):
            continue
        scheme = f" ({c.scheme})" if c.scheme else ""
        sides = " vs ".join(f"{s} {_cite(v.observations)}" for s, v in shown)
        lines.append(f"- {c.attribute}{scheme}: {sides}")
    sections.append((f"## Contradictions ({len(lines)})", lines))

    lines = []
    for a in d.attributes:
        if _shown("", policy(a.attribute)) is None:
            continue
        top, *rest = a.values
        line = (f"- {a.attribute}: {_shown(top.value, policy(a.attribute))} "
                f"({len(top.observations)} of {a.observed}) {_cite(top.observations)}")  # fmt: skip
        if rest:
            others = "; ".join(
                f"{_shown(v.value, policy(a.attribute))} ({len(v.observations)}) "
                f"{_cite(v.observations)}"
                for v in rest[:3]
            )
            more = f"; +{len(rest) - 3} more values" if len(rest) > 3 else ""
            line += f". Also: {others}{more}"
        lines.append(line)
    sections.append(("## Attributes", lines))

    lines = []
    if d.authorities and _shown("", policy("accepted_name")) is not None:
        for v in d.authorities:
            who = f"{v.source} (reference)" if v.reference else v.source
            statuses = ", ".join(f"{n} {s}" for s, n in v.statuses)
            note = "" if v.agrees else "; disagrees with the label"
            lines.append(f"- {who}: accepted name {v.accepted} for "
                         f"{_n(len(v.observations), 'record')} ({statuses}){note} "
                         f"{_cite(list(v.observations))}")  # fmt: skip
        sections.append(("## Authorities", lines))

    lines = []
    for r in d.relationships:
        rel = r.relationship
        verb = (f"{rel.relation} {r.other_label} ({r.other_id})" if r.role == "subject"
                else f"{r.other_label} ({r.other_id}) {rel.relation} this entity")  # fmt: skip
        lines.append(f"- {verb} [{rel.basis_decision_id}]")
    for c, label in d.relationship_candidates:
        other = c.right_entity if c.left_entity == e.entity_id else c.left_entity
        lines.append(f"- possibly related to {label} ({other}): {', '.join(c.signals)}, "
                     f"{_n(c.pairs, 'pair')} {_cite(list(c.evidence_ids))}")  # fmt: skip
    sections.append(("## Relationships", lines))

    lines = [f"- {r.review_id}: open {r.kind} review" for r in d.open_reviews]
    sections.append(("## Open reviews", lines))

    lines = []
    for m in d.members:
        if m.attributes is None:
            lines.append(f"- {m.observation_id} ({m.source.type}:{m.source.id}): redacted")
            continue
        parts = []
        for attribute in sorted(m.attributes):
            value = m.attributes[attribute]
            if value is None or value == "":
                continue
            shown = _shown(value, policy(attribute))
            if shown is not None:
                parts.append(f"{attribute}={shown}")
        lines.append(f"- {m.observation_id} ({m.source.type}:{m.source.id}): {'; '.join(parts)}")
    shown_sources = ", ".join(f"{t} {n}" for t, n in sorted(sources.items()))
    sections.append((f"## Observations (sources on this page: {shown_sources})", lines))

    # Fill in priority order; stop at the first record that does not fit, so nothing of
    # lower priority appears while something of higher priority is missing.
    footer_room = 60
    out = list(header)
    used = sum(len(x) + 1 for x in out)
    omitted = 0
    stopped = False
    for heading, records in sections:
        if not records:
            continue
        if stopped:
            omitted += len(records)
            continue
        cost = len(heading) + 2
        taken = []
        for record in records:
            if used + cost + len(record) + 1 > max_chars - footer_room:
                stopped = True
                break
            taken.append(record)
            cost += len(record) + 1
        omitted += len(records) - len(taken)
        if taken:
            out += ["", heading, *taken]
            used += cost
    if omitted:
        out += ["", f"({omitted} more lines omitted for length.)"]
    return "\n".join(out)[:max_chars]
