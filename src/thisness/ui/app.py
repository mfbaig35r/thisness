"""The Workbench: a local web app over the public ``Thisness`` API (design 04 §4).

Served in-process by whatever owns the store, since DuckDB allows one writer. Every
read and write goes through ``Thisness`` methods; nothing here touches the store's
tables. Pages are server-rendered Jinja templates; htmx swaps fragments (the side
panel, tabs, search) and posts answers.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime
from functools import cache
from importlib import resources
from pathlib import Path
from typing import Any

from fastapi import APIRouter, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup

from thisness.assistant import review_labels
from thisness.core.decisions import PairJudgment
from thisness.engine import Thisness
from thisness.storage.errors import NotFound, ThisnessError

HERE = Path(str(resources.files("thisness.ui")))
TEMPLATES = Jinja2Templates(directory=HERE / "templates")
router = APIRouter()

TYPE_ICONS = {"organization": "building", "person": "user", "taxon": "leaf"}
ATTRIBUTE_ICONS = {
    "name": "id", "registration_ids": "fingerprint", "domain": "link", "website": "link",
    "email": "mail", "country": "world", "city": "map-pin", "postal_code": "hash",
    "address_line": "home", "phone": "phone", "date_of_birth": "cake", "employer": "briefcase",
    "identifiers": "fingerprint", "given_name": "id", "family_name": "id",
    "authorship": "id", "rank": "list-check", "family": "git-branch", "kingdom": "world",
    "accepted_name": "scale", "taxonomic_status": "scale",
}  # fmt: skip
# Shown on a review card besides the name and location, in profile order.
NOT_ON_CARDS = frozenset({"name", "given_name", "family_name", "city", "country", "postal_code"})
STATE_TONES = {"active": "green", "provisional": "amber", "retired": "gray"}
VERDICT_TONES = {"MATCH": "green", "NO_MATCH": "red", "POSSIBLE_MATCH": "amber",
                 "REQUIRES_REVIEW": "amber"}  # fmt: skip


# -- template helpers ------------------------------------------------------------------------


@cache
def _svg(name: str) -> str:
    path = HERE / "static" / "icons" / f"{name}.svg"
    text = (
        path.read_text() if path.exists() else (HERE / "static/icons/circle-dashed.svg").read_text()
    )
    body = text[text.index(">", text.index("<svg")) + 1 : text.rindex("</svg>")]
    return " ".join(body.split())


def icon(name: str, size: int = 16, cls: str = "") -> Markup:
    return Markup(
        f'<svg class="icon {cls}" width="{size}" height="{size}" viewBox="0 0 24 24" fill="none" '
        f'stroke="currentColor" stroke-width="1.8" stroke-linecap="round" '
        f'stroke-linejoin="round" aria-hidden="true">{_svg(name)}</svg>'
    )


def fmt(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list | tuple):
        return ", ".join(fmt(v) for v in value)
    if isinstance(value, dict):
        return json.dumps(value, sort_keys=True)
    if isinstance(value, float):
        return f"{value:.2f}"
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M")
    return str(value)


IRREGULAR = {"entity": "entities", "body": "bodies", "person": "people", "taxon": "taxa"}


def plural(count: int, noun: str) -> str:
    return f"{count:,} {noun if count == 1 else IRREGULAR.get(noun, noun + 's')}"


def type_title(entity_type: str) -> str:
    noun = entity_type.replace("_", " ")
    return IRREGULAR.get(noun, noun if noun.endswith("s") else noun + "s").capitalize()


TEMPLATES.env.globals.update(
    icon=icon, fmt=fmt, plural=plural, type_title=type_title, TYPE_ICONS=TYPE_ICONS,
    ATTRIBUTE_ICONS=ATTRIBUTE_ICONS, STATE_TONES=STATE_TONES, VERDICT_TONES=VERDICT_TONES,
)  # fmt: skip


def engine_of(request: Request) -> Thisness:
    return request.app.state.engine


def render(request: Request, template: str, **context: Any) -> HTMLResponse:
    engine = engine_of(request)
    context.setdefault("nav", _nav(engine))
    context.setdefault("store_name", request.app.state.store_name)
    return TEMPLATES.TemplateResponse(request, template, context)


def _nav(engine: Thisness) -> dict[str, Any]:
    overview = engine.overview()
    return {
        "types": [
            (t, sum(n for s, n in overview["entities"].get(t, {}).items() if s != "retired"))
            for t in sorted(engine.profiles)
        ],
        "open_reviews": overview["open_reviews"],
        "candidates": len(engine.relationship_candidates()),
    }


HOODS_PER_PAGE = 50  # neighborhoods on one page of possible relationships
PAIRS_PER_HOOD = 50  # pairs shown in one neighborhood before "+N more"


def _name(engine: Thisness, entity_id: str) -> str:
    try:
        _, snapshot = engine.get_entity(entity_id)
    except NotFound:
        return entity_id
    name = snapshot.attributes.get("name") if snapshot else None
    return name if isinstance(name, str) and name else entity_id


def _redirect(request: Request, url: str) -> Response:
    if request.headers.get("HX-Request"):
        return Response(status_code=204, headers={"HX-Redirect": url})
    return RedirectResponse(url, status_code=303)


def _error(message: str) -> HTMLResponse:
    # htmx swaps 200 responses only; the form names this element as its error target.
    return HTMLResponse(
        f'<div class="form-error">{icon("alert-triangle")} {Markup.escape(message)}</div>'
    )


# -- entities --------------------------------------------------------------------------------


@router.get("/")
def home(request: Request) -> Response:
    engine = engine_of(request)
    first = sorted(engine.profiles)[0] if engine.profiles else None
    return RedirectResponse(f"/entities/{first}" if first else "/runs", status_code=303)


@router.get("/entities/{entity_type}", response_class=HTMLResponse)
def entities(request: Request, entity_type: str, state: str | None = None, q: str | None = None,
             page: int = 1) -> HTMLResponse:  # fmt: skip
    engine = engine_of(request)
    limit = 50
    listing = engine.entities(entity_type, state=state or None, query=q or None,
                              offset=(page - 1) * limit, limit=limit)  # fmt: skip
    counts = engine.overview()["entities"].get(entity_type, {})
    return render(request, "entities.html", entity_type=entity_type, listing=listing,
                  state=state or "", q=q or "", page=page, counts=counts)  # fmt: skip


@router.get("/entity/{entity_id}", response_class=HTMLResponse)
def entity(request: Request, entity_id: str, tab: str = "values") -> Response:
    engine = engine_of(request)
    dossier = engine.dossier(entity_id)
    if dossier.entity.entity_id != entity_id:
        return RedirectResponse(f"/entity/{dossier.entity.entity_id}?tab={tab}", status_code=303)
    return render(request, "entity.html", d=dossier, tab=tab, **_tab_context(engine, dossier, tab))


@router.get("/entity/{entity_id}/tab/{tab}", response_class=HTMLResponse)
def entity_tab(request: Request, entity_id: str, tab: str, page: int = 1,
               budget: int = 4000) -> HTMLResponse:  # fmt: skip
    engine = engine_of(request)
    limit = 50
    dossier = engine.dossier(entity_id, members_offset=(page - 1) * limit, members_limit=limit)
    context = _tab_context(engine, dossier, tab, page=page, budget=budget)
    return TEMPLATES.TemplateResponse(request, "entity_tab.html",
                                      {"d": dossier, "tab": tab, **context})  # fmt: skip


def _tab_context(engine: Thisness, d, tab: str, *, page: int = 1, budget: int = 4000) -> dict:
    context: dict[str, Any] = {"page": page, "budget": budget}
    if tab == "history":
        context["commits"] = _timeline(engine, d.entity.entity_id)
    if tab == "context":
        context["text"] = d.to_context(budget)
    return context


def _timeline(engine: Thisness, entity_id: str) -> list[dict[str, Any]]:
    """Ledger events for an entity, one item per commit, newest first."""
    by_commit: dict[str, list] = defaultdict(list)
    for event in engine.history(entity_id=entity_id):
        by_commit[event.commit_id].append(event)
    items = []
    for events in by_commit.values():
        lines, joined, left = [], 0, 0
        for e in events:
            t = e.event_type.value
            if t == "MEMBERSHIP_ACCEPTED":
                joined += 1
            elif t == "MEMBERSHIP_ENDED":
                left += 1
            elif t == "ENTITY_CREATED":
                lines.append(("plus", f"Created as {e.payload.get('state', 'active')}", None))
            elif t == "ENTITY_MERGED" and e.entity_id == entity_id:
                lines.append(("git-merge", "Merged into", e.related_entity_id))
            elif t == "ENTITY_MERGED":
                lines.append(("git-merge", "Absorbed", e.entity_id))
            elif t == "ENTITY_SPLIT":
                targets = e.payload.get("targets", [])
                lines.append(("arrows-split", f"Split into {plural(len(targets), 'entity')}", None))
            elif t == "ENTITY_PROMOTED":
                lines.append(("circle-check", "Promoted to active", None))
            elif t == "ENTITY_REACTIVATED":
                lines.append(("arrow-back-up", "Reactivated", None))
            elif t == "RELATIONSHIP_CREATED":
                other = e.related_entity_id if e.entity_id == entity_id else e.entity_id
                lines.append(("link", "Related to", other))
            elif t == "RELATIONSHIP_REVOKED":
                lines.append(("x", "Relationship revoked", None))
            else:
                lines.append(("circle-dashed", t.replace("_", " ").capitalize(), None))
        if joined:
            lines.append(("users", f"{plural(joined, 'observation')} joined", None))
        if left:
            lines.append(("users", f"{plural(left, 'observation')} left", None))
        first = events[0]
        items.append({
            "seq": first.seq, "last_seq": events[-1].seq, "at": first.recorded_at,
            "actor": first.actor, "run_id": first.run_id,
            "decisions": sorted({e.decision_id for e in events if e.decision_id}),
            "lines": [(i, text, ref, _name(engine, ref) if ref else None)
                      for i, text, ref in lines],
        })  # fmt: skip
    return sorted(items, key=lambda x: -x["seq"])


# -- side panels -----------------------------------------------------------------------------


@router.get("/panel/entity/{entity_id}", response_class=HTMLResponse)
def panel_entity(request: Request, entity_id: str) -> HTMLResponse:
    d = engine_of(request).dossier(entity_id, members_limit=8)
    return TEMPLATES.TemplateResponse(request, "panel_entity.html", {"d": d})


@router.get("/panel/observation/{observation_id}", response_class=HTMLResponse)
def panel_observation(request: Request, observation_id: str) -> HTMLResponse:
    engine = engine_of(request)
    obs = engine.observation(observation_id)
    entity_id = engine.entity_of(observation_id)
    history = engine.history(observation_id=observation_id)
    return TEMPLATES.TemplateResponse(request, "panel_observation.html", {
        "o": obs, "entity_id": entity_id, "entity_name": _name(engine, entity_id) if entity_id
        else None, "history": history})  # fmt: skip


@router.get("/panel/decision/{decision_id}", response_class=HTMLResponse)
def panel_decision(request: Request, decision_id: str) -> HTMLResponse:
    explained = engine_of(request).explain(decision_id)
    return TEMPLATES.TemplateResponse(request, "panel_decision.html", {"x": explained})


# -- reviews ---------------------------------------------------------------------------------


@router.get("/reviews", response_class=HTMLResponse)
def reviews(request: Request, kind: str | None = None) -> HTMLResponse:
    engine = engine_of(request)
    summaries = engine.reviews(kind or None)
    kinds = Counter(s.review.kind for s in engine.reviews())
    suggestion = {}  # review id -> the latest suggested resolution, if any
    for s in summaries:
        recommendations = engine.recommendations(s.review.review_id)
        if recommendations:
            suggestion[s.review.review_id] = recommendations[-1].resolution
    return render(
        request,
        "reviews.html",
        summaries=summaries,
        kind=kind or "",
        kinds=kinds,
        suggestion=suggestion,
    )


@router.get("/reviews/{review_id}", response_class=HTMLResponse)
def review(request: Request, review_id: str) -> HTMLResponse:
    engine = engine_of(request)
    case = engine.review_case(review_id)
    columns: dict[str, list] = {}
    for node in case.nodes:
        columns.setdefault(node.entity_id or node.node_id, []).append(node)
    names = {n.node_id: str(n.attributes.get("name") or n.node_id) for n in case.nodes}
    board = [
        {"entity_id": e, "name": _name(engine, e), "state": nodes[0].entity_state,
         "size": nodes[0].size, "nodes": nodes}
        for e, nodes in sorted(columns.items(), key=lambda kv: (-kv[1][0].size, kv[0]))
    ]  # fmt: skip
    column_of = {n.node_id: n.entity_id or n.node_id for n in case.nodes}
    judgments = sorted(case.judgments, key=_weight, reverse=True)
    recommendation = (engine.recommendations(review_id) or [None])[-1]
    cited = set(recommendation.cited_judgments) if recommendation else set()
    # The labels the review assistant's request used (R1.., C1..), so a suggestion's
    # rationale ("R11 and R13 share an email") reads against the board.
    refs, labelled = review_labels(engine, case)
    label_of = {j.decision_id: label for label, j, *_ in labelled}
    rows = [
        {"j": j, "left": names.get(j.left_id, j.left_id), "right": names.get(j.right_id, j.right_id),
         "left_ref": refs.get(j.left_id), "right_ref": refs.get(j.right_id),
         "label": label_of.get(j.decision_id),
         "weight": _weight(j), "across": column_of.get(j.left_id) != column_of.get(j.right_id),
         "model": _model_answer(j), "cited": j.decision_id in cited}
        for j in judgments if j.left_id in names and j.right_id in names
    ]  # fmt: skip
    rows.sort(key=lambda r: (not r["cited"], not r["across"], -r["weight"]))
    status = case.review.status
    entity_type = engine.get_entity(board[0]["entity_id"])[0].entity_type if board else None
    relations = engine.profiles[entity_type].relations if entity_type in engine.profiles else ()
    profile = engine.profiles.get(entity_type) if entity_type else None
    assertion = profile.assertions if profile else None
    # A source's assertion gets its own line on a card, so its fields are not repeated.
    skip = {assertion.status, assertion.accepted} if assertion else set()
    card_fields = list(dict.fromkeys(
        a for p in engine.profiles.values() for a in p.attributes
        if a not in NOT_ON_CARDS and a not in skip))  # fmt: skip
    return render(request, "review.html", case=case, board=board, rows=rows[:60], refs=refs,
                  card_fields=card_fields, relations=relations, recommendation=recommendation,
                  assertion=assertion, entity_type=entity_type,
                  can_ask=engine.assistant is not None,
                  suggested=json.dumps([list(g) for g in recommendation.groups])
                  if recommendation and recommendation.groups else "",
                  hidden=max(0, len(rows) - 60), status=status,
                  cut=json.dumps(case.suggested_cut) if case.suggested_cut else "")  # fmt: skip


def _model_answer(j: PairJudgment) -> str | None:
    """An adjudicator's answer, for judgments a model made (design 05 §4)."""
    if j.actor.kind != "agent":
        return None
    return next((str(s.value) for s in j.scores if s.kind == "llm_decision"), None)


def _weight(j: PairJudgment) -> float:
    for s in j.scores:
        if s.kind == "fellegi_sunter_weight":
            return float(s.value)
    return 0.0


@router.post("/reviews/{review_id}/resolve")
def resolve_review(request: Request, review_id: str, resolution: str = Form(...),
                   reason: str = Form(...), groups: str = Form(""),
                   relation: str = Form(""), recommendation_id: str = Form("")) -> Response:  # fmt: skip
    engine = engine_of(request)
    if not reason.strip():
        return _error("Give a reason; it is stored with the decision.")
    try:
        parsed = json.loads(groups) if resolution == "groups" and groups else None
        engine.resolve_review(review_id, resolution, groups=parsed, relation=relation or None,
                              reason=reason.strip(),
                              recommendation_id=recommendation_id or None)  # fmt: skip
    except (ThisnessError, ValueError) as exc:
        return _error(str(exc))
    remaining = engine.reviews()
    return _redirect(request, f"/reviews/{remaining[0].review.review_id}" if remaining
                     else "/reviews")  # fmt: skip


@router.post("/reviews/{review_id}/recommend")
def recommend(request: Request, review_id: str) -> Response:
    engine = engine_of(request)
    try:
        result = engine.recommend(review_id)
    except ThisnessError as exc:
        return _error(str(exc))
    if result.recommendation is None:
        return _error(f"No usable suggestion ({result.status.replace('_', ' ')}). "
                      "The call is recorded under Model calls.")  # fmt: skip
    return _redirect(request, f"/reviews/{review_id}")


# -- model calls -----------------------------------------------------------------------------


@router.get("/llm", response_class=HTMLResponse)
def model_calls(request: Request, run: str | None = None) -> HTMLResponse:
    engine = engine_of(request)
    calls = engine.invocations(run_id=run or None)
    totals = {
        "calls": len(calls),
        "status": Counter(c.status for c in calls),
        "input": sum(c.input_tokens or 0 for c in calls),
        "output": sum(c.output_tokens or 0 for c in calls),
        "cost": sum(c.cost_usd or 0 for c in calls),
        "priced": all(c.cost_usd is not None for c in calls if c.status != "cached"),
    }
    return render(request, "llm.html", calls=calls, totals=totals, run=run or "")


@router.get("/panel/invocation/{invocation_id}", response_class=HTMLResponse)
def panel_invocation(request: Request, invocation_id: str) -> HTMLResponse:
    call = engine_of(request).invocation(invocation_id)
    pretty = {k: json.dumps(getattr(call, k), indent=1, sort_keys=True, ensure_ascii=False)
              for k in ("payload", "output", "settings")}  # fmt: skip
    return TEMPLATES.TemplateResponse(request, "panel_invocation.html", {"c": call, **pretty})


# -- relationship candidates -----------------------------------------------------------------


@router.get("/relationships", response_class=HTMLResponse)
def relationships(request: Request, offset: int = 0) -> HTMLResponse:
    """A page of neighborhoods, largest first. On 46,565 real UK records there were
    66,978 candidates in 691 neighborhoods; rendering them all took 32 minutes
    (design 07 §13.4)."""
    engine = engine_of(request)
    groups: dict[str, list] = {}
    for c in engine.relationship_candidates():
        groups.setdefault(c.neighborhood, []).append(c)
    hoods = list(groups)
    offset = max(0, offset)
    page = {h: groups[h][:PAIRS_PER_HOOD] for h in hoods[offset : offset + HOODS_PER_PAGE]}
    hidden = {h: len(groups[h]) - len(page[h]) for h in page}
    names = engine.names(sorted({e for cs in page.values() for c in cs
                                 for e in (c.left_entity, c.right_entity)} | set(page)))  # fmt: skip
    return render(request, "relationships.html", groups=page, names=names, hidden=hidden,
                  offset=offset, total_hoods=len(hoods),
                  total_pairs=sum(len(cs) for cs in groups.values()),
                  page_size=HOODS_PER_PAGE)  # fmt: skip


@router.get("/panel/candidate", response_class=HTMLResponse)
def panel_candidate(request: Request, left: str, right: str) -> HTMLResponse:
    engine = engine_of(request)
    candidate = next((c for c in engine.relationship_candidates(left)
                      if {c.left_entity, c.right_entity} == {left, right}), None)  # fmt: skip
    if candidate is None:
        raise NotFound(f"no open relationship candidate between {left} and {right}")
    a, b = engine.dossier(left, members_limit=0), engine.dossier(right, members_limit=0)
    attributes = list(dict.fromkeys([x.attribute for x in a.attributes]
                                    + [x.attribute for x in b.attributes]))  # fmt: skip
    relations = engine.profiles[a.entity.entity_type].relations
    return TEMPLATES.TemplateResponse(request, "panel_candidate.html", {
        "c": candidate, "left": a, "right": b, "attributes": attributes,
        "relations": relations})  # fmt: skip


@router.post("/relationships/decide")
def decide_candidate(request: Request, left: str = Form(...), right: str = Form(...),
                     action: str = Form(...), relation: str = Form("sibling_of"),
                     reason: str = Form(...), evidence: str = Form("")) -> Response:  # fmt: skip
    engine = engine_of(request)
    if not reason.strip():
        return _error("Give a reason; it is stored with the decision.")
    try:
        if action == "merge":
            survivor, other = sorted((left, right), key=lambda e: -len(engine.members(e)))
            engine.merge_entities(survivor, [other], reason=reason.strip())
        else:
            engine.relate(left, relation, right, reason=reason.strip(),
                          evidence_ids=[e for e in evidence.split(",") if e])  # fmt: skip
    except ThisnessError as exc:
        return _error(str(exc))
    return _redirect(request, "/relationships")


# -- runs, model, search ---------------------------------------------------------------------


@router.get("/runs", response_class=HTMLResponse)
def runs(request: Request) -> HTMLResponse:
    return render(request, "runs.html", runs=engine_of(request).runs())


@router.get("/model/{entity_type}", response_class=HTMLResponse)
def model(request: Request, entity_type: str) -> HTMLResponse:
    engine = engine_of(request)
    profile = engine.profiles[entity_type]
    fs = engine.model(entity_type)
    comparisons = []
    if fs is not None:
        for c in profile.comparisons:
            if c.name not in fs.levels:
                continue
            levels = []
            for i, label in enumerate(fs.levels[c.name]):
                cap = fs.caps.get(c.name, {}).get(label)
                levels.append({"label": label, "m": fs.m[c.name][i], "u": fs.u[c.name][i],
                               "weight": fs.level_weight(c.name, i), "cap": cap,
                               "relationship": (c.name, label) in profile.relationship_level_pairs})  # fmt: skip
            comparisons.append({"name": c.name, "features": c.features, "levels": levels,
                                "unlearned": c.name in fs.unlearned})  # fmt: skip
    threshold = engine.edge_policy.threshold(fs, profile.min_strong_weight) if fs else None
    return render(request, "model.html", entity_type=entity_type, profile=profile, fs=fs,
                  comparisons=comparisons, threshold=threshold,
                  t_low=(engine.edge_policy.t_low if profile.max_negative_weight is None
                         else profile.max_negative_weight),
                  source=engine.model_source(entity_type))  # fmt: skip


@router.get("/search", response_class=HTMLResponse)
def search(request: Request, q: str = "") -> HTMLResponse:
    hits = engine_of(request).search(q, limit=8)
    return TEMPLATES.TemplateResponse(request, "search_results.html", {"hits": hits, "q": q})


# -- the app ---------------------------------------------------------------------------------


def create_app(engine: Thisness, *, store_name: str = "in-memory") -> FastAPI:
    app = FastAPI(title="Thisness Workbench", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.engine = engine
    app.state.store_name = store_name
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    app.include_router(router)

    @app.exception_handler(NotFound)
    def not_found(request: Request, exc: NotFound) -> HTMLResponse:
        response = render(request, "not_found.html", message=str(exc))
        response.status_code = 404
        return response

    return app
