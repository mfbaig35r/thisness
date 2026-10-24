"""``thisness``: the command line (design 04 §5). Thin wrappers over ``Thisness``,
with every built-in profile (organization, person) registered.

The store is ``--store``, else ``$THISNESS_STORE``, else ``identity.duckdb`` in the
current directory.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from collections.abc import Callable, Sequence
from dataclasses import asdict
from pathlib import Path

from thisness.engine import Thisness
from thisness.profiles.base import ResolutionProfile
from thisness.profiles.organization import organization
from thisness.profiles.person import person
from thisness.profiles.taxon import taxon

PROFILES: dict[str, Callable[[], ResolutionProfile]] = {"organization": organization,
                                                        "person": person, "taxon": taxon}  # fmt: skip
GENERATED = ("organization", "person", "taxon")  # types with a benchmark


def _generator(entity_type: str):
    from thisness.benchmark import organizations, people, taxa

    return {"organization": organizations.generate, "person": people.generate,
            "taxon": taxa.standard}[entity_type]  # fmt: skip


DEMO_SOURCES = ("erp", "crm", "invoices", "contracts")
MULTI_SEPARATORS = (";", "|")


def _engine(store: str) -> Thisness:
    return Thisness(store, profiles=[make() for make in PROFILES.values()])


def _split(value: str) -> list[str]:
    for sep in MULTI_SEPARATORS:
        if sep in value:
            return [v.strip() for v in value.split(sep) if v.strip()]
    return [value.strip()]


def read_csv(path: Path, profile: ResolutionProfile) -> list[tuple[dict, dict]]:
    """Rows as (attributes, context): columns the profile declares become attributes
    (multi-valued ones split on ';' or '|'); the rest are kept as context."""
    rows = []
    with path.open(newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            attributes, context = {}, {}
            for column, raw in row.items():
                if column is None or raw is None or not raw.strip():
                    continue
                key = column.strip()
                definition = profile.attributes.get(key)
                if definition is None:
                    context[key] = raw.strip()
                elif definition.cardinality == "multi":
                    attributes[key] = _split(raw)
                else:
                    attributes[key] = raw.strip()
            if attributes:
                rows.append((attributes, context))
    return rows


# -- commands --------------------------------------------------------------------------------


def cmd_ingest(args: argparse.Namespace) -> int:
    engine = _engine(args.store)
    profile = engine.profiles[args.type]
    path = Path(args.file)
    rows = read_csv(path, profile)
    observations = [
        engine.observe(
            args.type,
            attributes,
            {"type": args.source_type, "id": path.name, "locator": {"row": i}},
            context=context,
        )
        for i, (attributes, context) in enumerate(rows)
    ]
    stored = engine.ingest(observations)
    new = sum(1 for o, s in zip(observations, stored, strict=True) if o.observation_id == s)
    print(
        f"{path.name}: {len(rows)} rows, {new} new observations, {len(rows) - new} already stored"
    )
    if args.resolve:
        return cmd_resolve(args, engine)
    return 0


def _name_only_plan(args: argparse.Namespace, engine: Thisness):
    if getattr(args, "plan", "default") != "name-only":
        return None
    from thisness.llm import LLMClient, LLMPolicy
    from thisness.name_only import NameOnlyPlan

    client = LLMClient(engine.store, _provider(args), ids=engine.ids, clock=engine.clock,
                       policy=LLMPolicy(budget_usd=args.budget))  # fmt: skip
    return NameOnlyPlan(client, model=args.group_model, verify_model=args.model)


def cmd_resolve(args: argparse.Namespace, engine: Thisness | None = None) -> int:
    engine = engine or _engine(args.store)
    types = [args.type] if getattr(args, "type", None) else sorted(engine.profiles)
    plan = _name_only_plan(args, engine)
    use_model = plan is None and getattr(args, "provider", "none") != "none"
    for entity_type in types:
        role = _model_role(args, engine.profiles[entity_type]) if use_model else "off"
        adjudicator = None
        if role in ("decide", "shadow"):
            from thisness.adjudicate import Adjudicator

            adjudicator = Adjudicator(_client(args, engine), model=args.model, mode=role)
        result = engine.resolve_many(entity_type, plan=plan, adjudicator=adjudicator)
        # Outcomes are per observation (every record of a new cluster is CREATE_ENTITY),
        # so report the entities the store now holds rather than a breakdown that reads
        # as if nothing merged.
        states = engine.store.entity_counts().get(entity_type, {})
        live = sum(n for s, n in states.items() if s != "retired")
        print(f"{entity_type}: {result.stats.get('scope', 0)} observations resolved; "
              f"{live} entities in the store; {len(result.reviews)} reviews opened")  # fmt: skip
        if adjudicator is not None:
            stats = result.stats.get("adjudication", {})
            print(f"  model ({role}): {stats.get('answered', 0)} pairs answered, "
                  f"{stats.get('applied', 0)} applied, ${stats.get('spent_usd', 0):.4f}")  # fmt: skip
        if role == "suggest" and result.reviews:
            from thisness.assistant import ReviewAssistant

            assistant = ReviewAssistant(_client(args, engine), model=args.model)
            suggested = assistant.recommend_many(engine, list(result.reviews))
            ok = sum(r.status == "ok" for r in suggested)
            print(f"  model (suggest): {ok} of {len(suggested)} new reviews have a suggestion, "
                  f"${assistant.client.spent_usd:.4f}")  # fmt: skip
    return 0


def _model_role(args: argparse.Namespace, profile) -> str:
    """The role a configured model plays for this type: ``--model-role``, or the
    profile's (design 06 §10.11), or shadow."""
    role = getattr(args, "model_role", "auto")
    if role != "auto":
        return role
    return profile.model_role or "shadow"


def _client(args: argparse.Namespace, engine: Thisness):
    from thisness.llm import LLMClient, LLMPolicy

    return LLMClient(engine.store, _provider(args), ids=engine.ids, clock=engine.clock,
                     policy=LLMPolicy(budget_usd=args.budget,
                                      concurrency=getattr(args, "concurrency", 4)))  # fmt: skip


def cmd_demo(args: argparse.Namespace) -> int:
    store = Path(args.store)
    if store.exists() and not args.force:
        print(f"{store} exists; pass --force to add the demo data to it", file=sys.stderr)
        return 1
    if (args.reference or args.col) and args.type != "taxon":
        print("--reference and --col apply to taxa only", file=sys.stderr)
        return 1
    engine = _engine(str(store))
    options = {k: True for k in ("reference", "col") if getattr(args, k)}
    records = _generator(args.type)(args.size, seed=args.seed, **options).records
    # Taxon records carry their own source: "wfo" is the reference authority.
    sources = [getattr(r, "source", None) or DEMO_SOURCES[i % len(DEMO_SOURCES)]
               for i, r in enumerate(records)]  # fmt: skip
    observations = [
        engine.observe(args.type, r.attributes, {"type": source, "id": r.record_id})
        for r, source in zip(records, sources, strict=True)
    ]
    engine.ingest(observations)
    print(f"{store}: {len(observations)} synthetic {args.type} records")
    return cmd_resolve(argparse.Namespace(type=args.type), engine)


def cmd_ui(args: argparse.Namespace) -> int:
    try:
        import uvicorn

        from thisness.ui import create_app
    except ImportError:
        print("The Workbench needs the ui extra: pip install 'thisness[ui]'", file=sys.stderr)
        return 1
    store = args.path or args.store
    engine = _engine(store)
    engine.assistant = _assistant(args, engine)
    url = f"http://{args.host}:{args.port}"
    print(f"Thisness Workbench on {url} ({store}). Ctrl-C stops it.")
    if not args.no_browser:
        import threading
        import webbrowser

        threading.Timer(1.0, webbrowser.open, [url]).start()
    uvicorn.run(create_app(engine, store_name=Path(store).name), host=args.host, port=args.port,
                log_level="warning")  # fmt: skip
    return 0


def cmd_history(args: argparse.Namespace) -> int:
    engine = _engine(args.store)
    key = "observation_id" if args.id.startswith("obs_") else "entity_id"
    for e in engine.history(**{key: args.id}):
        subject = e.entity_id or e.observation_id or ""
        print(f"{e.seq:>8}  {e.event_type.value:<22} {subject:<32} {e.decision_id or ''}")
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    """The mapping table (design 08 §6): CSV to standard output, or a file."""
    import pyarrow.csv as pacsv
    import pyarrow.parquet as pq

    if args.format == "parquet" and not args.output:
        print("--format parquet needs --output FILE", file=sys.stderr)
        return 1
    engine = _engine(args.store)
    table = engine.mapping(args.type, as_of_seq=args.as_of)
    if args.format == "parquet":
        pq.write_table(table, args.output)
    elif args.output:
        pacsv.write_csv(table, args.output)
    else:
        try:
            pacsv.write_csv(table, sys.stdout.buffer)
        except BrokenPipeError:  # the reader stopped early (`| head`): not an error
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
    if args.output:
        print(f"{table.num_rows:,} rows to {args.output}", file=sys.stderr)
    return 0


def cmd_dossier(args: argparse.Namespace) -> int:
    engine = _engine(args.store)
    d = engine.dossier(args.id)
    print(d.to_context(args.chars))
    return 0


def cmd_reviews(args: argparse.Namespace) -> int:
    engine = _engine(args.store)
    if args.action == "accept":
        return _accept_suggestions(args, engine)
    for s in engine.reviews():
        print(f"{s.review.review_id}  {s.review.kind:<8} {s.observations:>6} observations  "
              f"{len(s.review.members):>4} records")  # fmt: skip
    return 0


def _accept_suggestions(args: argparse.Namespace, engine: Thisness) -> int:
    """Accept open suggestions in bulk (design 10 §4.2): a person's answers, one per
    review, each reversible."""
    import getpass

    from thisness.core.decisions import Actor

    resolutions = args.resolution.split(",") if args.resolution else None
    result = engine.accept_recommendations(
        actor=Actor(kind="human", id=args.actor or getpass.getuser()),
        entity_type=args.type, resolutions=resolutions, dry_run=args.dry_run)  # fmt: skip
    verb = "would accept" if result.dry_run else "accepted"
    accepted = ", ".join(f"{k} {v}" for k, v in sorted(result.accepted.items())) or "none"
    print(f"{verb} {sum(result.accepted.values())} suggestions ({accepted})")
    for why, n in sorted(result.skipped.items()):
        print(f"  left open: {n} ({why})")
    return 0


def cmd_recommend(args: argparse.Namespace) -> int:
    engine = _engine(args.store)
    engine.assistant = _assistant(args, engine)
    results = engine.recommend_open(kind=args.kind, limit=args.limit)
    statuses: dict[str, int] = {}
    for r in results:
        statuses[r.status] = statuses.get(r.status, 0) + 1
        if r.recommendation:
            rec = r.recommendation
            print(f"{r.review_id}  {rec.resolution:<10} {rec.rationale or ''}"[:160])
    client = engine.assistant.client
    print(f"{len(results)} reviews: {statuses}; {client.calls} calls, ${client.spent_usd:.4f}")
    return 0


def cmd_labels(args: argparse.Namespace) -> int:
    """Display names (design 08 §5.2): ask a model for suggestions, list them, accept them,
    or clear an accepted name."""
    from thisness.display_names import LabelAssistant

    engine = _engine(args.store)
    if args.action == "suggest":
        assistant = LabelAssistant(_client(args, engine), model=args.model)
        counts = engine.suggest_labels(args.type, assistant=assistant, limit=args.limit)
        client = assistant.client
        print(f"{counts}; {client.calls} calls, ${client.spent_usd:.4f}")
        return 0
    if args.action == "clear":
        for entity_id in args.ids:
            engine.clear_label(entity_id, reason=args.reason or "cleared from the command line")
        print(f"{len(args.ids)} names cleared")
        return 0
    entities = [r.entity_id for r in engine.entities(args.type, limit=1_000_000).rows]
    labeled = engine.display_labels(entities)
    suggestions = {e: s for e, s in engine.label_suggestions(entities).items() if e not in labeled}
    if args.action == "list":
        names = engine.names(sorted(suggestions))
        for entity_id, s in list(suggestions.items())[: args.limit]:
            print(f"{s.decision_id}  {entity_id}  {names[entity_id]!r} -> {s.name!r}")
        print(f"{len(suggestions)} suggestions not accepted; {len(labeled)} names set")
        return 0
    chosen = list(suggestions.values()) if args.all else \
        [s for s in suggestions.values() if s.decision_id in set(args.ids)]  # fmt: skip
    if not chosen:
        print("nothing to accept: give suggestion ids or --all", file=sys.stderr)
        return 1
    for s in chosen:
        engine.accept_label_suggestion(s.decision_id, reason=args.reason or "accepted suggestion")
    print(f"{len(chosen)} suggestions accepted")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    from thisness.storage.duckdb.invariants import check

    engine = _engine(args.store)
    violations = check(engine.store)
    for v in violations:
        print(v)
    print("all invariants hold" if not violations else f"{len(violations)} violations")
    return 1 if violations else 0


def _provider(args: argparse.Namespace):
    """A real model provider from the command line, optionally recording to a cassette."""
    from thisness.llm.providers import RecordingProvider

    if getattr(args, "replay", None):
        # A recorded run answered again, free and offline, under the provider name the
        # recording was made with, so its invocations read as the original ones did.
        from thisness.llm.providers.replay import ReplayProvider

        name = "anthropic" if args.provider == "anthropic" else "openai_compatible"
        return ReplayProvider(args.replay, name=name, local=args.local)
    if args.provider == "anthropic":
        from thisness.llm.providers.anthropic import AnthropicProvider

        provider = AnthropicProvider()
    else:
        from thisness.llm.providers.openai_compatible import OpenAICompatibleProvider

        provider = OpenAICompatibleProvider(args.base_url, local=args.local,
                                            api_key=os.environ.get("OPENAI_API_KEY"))  # fmt: skip
    return RecordingProvider(provider, args.record) if args.record else provider


def _assistant(args: argparse.Namespace, engine: Thisness):
    if getattr(args, "provider", "none") == "none":
        return None
    from thisness.assistant import ReviewAssistant

    return ReviewAssistant(_client(args, engine), model=args.model)


# The model a run uses when --model is not given, by provider. gpt-6-luna is the model
# every published measurement used; the Anthropic provider works but is not measured.
DEFAULT_MODELS = {"openai": "gpt-6-luna", "anthropic": "claude-sonnet-5"}
DEFAULT_GROUP_MODELS = {"openai": "gpt-6-luna", "anthropic": "claude-haiku-4-5"}


def _default_models(args: argparse.Namespace) -> None:
    """Fill in --model and --group-model from the run's provider (eval names it with
    --adjudicator or --assistant), so the default always matches the provider."""
    named = (getattr(args, k, None) for k in ("provider", "adjudicator", "assistant"))
    provider = next((p for p in named if p in DEFAULT_MODELS), "openai")
    if getattr(args, "model", "") is None:
        args.model = DEFAULT_MODELS[provider]
    if getattr(args, "group_model", "") is None:
        args.group_model = DEFAULT_GROUP_MODELS[provider]


def _model_options(parser: argparse.ArgumentParser, *, required: bool = False) -> None:
    g = parser.add_argument_group("model (design 05)")
    g.add_argument("--provider", default="openai" if required else "none",
                   choices=(["anthropic", "openai"] if required
                            else ["none", "anthropic", "openai"]))  # fmt: skip
    g.add_argument("--model", default=None,
                   help="default gpt-6-luna (openai) or claude-sonnet-5 (anthropic)")  # fmt: skip
    g.add_argument("--budget", type=float, default=None, help="US dollars")
    g.add_argument("--base-url", default="https://api.openai.com/v1", help="for --provider openai")
    g.add_argument("--local", action="store_true", help="the openai-compatible server is yours")
    g.add_argument("--record", default=None, help="append every exchange to this cassette")
    g.add_argument(
        "--replay",
        default=None,
        help="answer from this cassette instead of calling the model: free, offline",
    )
    g.add_argument("--concurrency", type=int, default=4, help="model requests in flight")


def _adjudicator_factory(args: argparse.Namespace):
    """Builds the run's adjudicator (design 05 §4, §7): a simulated oracle, or a real model
    with a dollar budget, optionally recording every exchange to a cassette."""
    if args.adjudicator == "none":
        return None
    from thisness.adjudicate import Adjudicator
    from thisness.benchmark.oracle import benchmark_truth, oracle_provider
    from thisness.llm import LLMClient, LLMPolicy
    from thisness.llm.providers import RecordingProvider

    def build(engine, truth, bench):
        if args.adjudicator == "oracle":

            def related(a, b):
                return bench.group.get(a) is not None and bench.group.get(a) == bench.group.get(b)

            provider = oracle_provider(benchmark_truth(engine, truth), related,
                                       error=args.oracle_error, fold_related=args.oracle_fold)  # fmt: skip
            if args.record:
                provider = RecordingProvider(provider, args.record)
        else:
            args.provider = args.adjudicator
            provider = _provider(args)
        policy = LLMPolicy(budget_usd=args.budget, concurrency=args.concurrency)
        client = LLMClient(engine.store, provider, ids=engine.ids, clock=engine.clock,
                           policy=policy)  # fmt: skip
        return Adjudicator(client, model=args.model, mode=args.mode, max_pairs=args.max_pairs)

    return build


def _name_only_factory(args: argparse.Namespace):
    """The name-only plan for an evaluation run, grouping with ``--adjudicator``'s model."""
    if args.plan != "name-only":
        return None
    from thisness.benchmark.oracle import benchmark_truth, oracle_provider
    from thisness.llm import LLMClient, LLMPolicy
    from thisness.llm.providers import RecordingProvider
    from thisness.name_only import NameOnlyPlan

    def build(engine, truth, bench):
        if args.adjudicator in ("none", "oracle"):

            def related(a, b):
                return bench.group.get(a) is not None and bench.group.get(a) == bench.group.get(b)

            provider = oracle_provider(benchmark_truth(engine, truth), related,
                                       error=args.oracle_error, fold_related=args.oracle_fold)  # fmt: skip
            if args.record:
                provider = RecordingProvider(provider, args.record)
        else:
            args.provider = args.adjudicator
            provider = _provider(args)
        client = LLMClient(engine.store, provider, ids=engine.ids, clock=engine.clock,
                           policy=LLMPolicy(budget_usd=args.budget, concurrency=args.concurrency))  # fmt: skip
        return NameOnlyPlan(client, model=args.group_model, verify_model=args.model)

    return build


def _eval_rewrite(args: argparse.Namespace, bench) -> None:
    """The rewrite-and-hash baseline on one benchmark (design 05 §11.5)."""
    from thisness.benchmark import rewrite
    from thisness.llm import LLMClient, LLMPolicy
    from thisness.llm.providers import RecordingProvider

    engine = Thisness(":memory:")
    if args.adjudicator in ("none", "oracle"):
        provider = rewrite.oracle(bench)
        if args.record:
            provider = RecordingProvider(provider, args.record)
        model = "oracle"
    else:
        args.provider = args.adjudicator
        provider, model = _provider(args), args.model
    client = LLMClient(engine.store, provider, ids=engine.ids, clock=engine.clock,
                       policy=LLMPolicy(budget_usd=args.budget, concurrency=args.concurrency))  # fmt: skip
    result = rewrite.run(bench, client, model=model, order=args.rewrite_order,
                         concurrency=args.concurrency)  # fmt: skip
    m = result.metrics
    if args.json:
        print(json.dumps({"benchmark": bench.config, "clusters": asdict(m), "run": result.stats},
                         indent=1))  # fmt: skip
        return
    print(f"{m.observations} records: B3 P {m.b3_precision:.4f} R {m.b3_recall:.4f} "
          f"F1 {m.b3_f1:.4f} | false-merge clusters {m.false_merge_clusters} "
          f"(contaminated {m.contaminated}, largest {m.largest_bad_cluster})")  # fmt: skip
    keep = ("order", "keys", "intermediate_groups", "final_groups", "calls", "failed", "spent_usd")
    print("  rewrite:", json.dumps({k: result.stats[k] for k in keep}, sort_keys=True))


def cmd_eval(args: argparse.Namespace) -> int:
    from thisness.benchmark.evaluate import run_benchmark
    from thisness.benchmark.organizations import messy, names_only

    if (args.reference or args.col) and args.type != "taxon":
        print("--reference and --col apply to taxa only", file=sys.stderr)
        return 1
    real = ("anthropic", "openai")
    if args.replay and args.adjudicator not in real and args.assistant not in real:
        print("--replay answers from a recording of --adjudicator or --assistant anthropic "
              "or openai", file=sys.stderr)  # fmt: skip
        return 1
    if args.replay and args.record:
        print("--replay and --record together would record the recording", file=sys.stderr)
        return 1
    if args.save and (len(args.size) > 1 or len(args.seeds) > 1):
        print("--save keeps one run: give one --size and one --seeds", file=sys.stderr)
        return 1
    if args.save and Path(args.save).exists():
        print(f"{args.save} exists; choose another path", file=sys.stderr)
        return 1
    if args.messy and not args.names_only:
        print("--messy applies to --names-only", file=sys.stderr)
        return 1
    if args.accept and args.assistant == "none":
        print("--accept accepts the assistant's suggestions: add --assistant", file=sys.stderr)
        return 1
    if args.plan == "rewrite" and (args.save or args.type != "organization"):
        print("--plan rewrite scores organizations' names and keeps no store", file=sys.stderr)
        return 1
    factory = _adjudicator_factory(args)
    plan = _name_only_factory(args)
    if args.source == "uk" and args.type != "organization":
        print("--source uk is a benchmark of organizations", file=sys.stderr)
        return 1
    if args.source == "edgar" and args.type != "person":
        print("--source edgar is a benchmark of people: add --type person", file=sys.stderr)
        return 1
    if args.source == "edgar" and args.identifiers == "visible":
        print("--source edgar: --identifiers is hidden or partial", file=sys.stderr)
        return 1
    options = {k: True for k in ("reference", "col") if getattr(args, k)}
    for n in args.size:
        for seed in args.seeds:
            if args.source == "uk":
                from thisness.benchmark import uk

                bench = uk.standard(n, seed=seed, mode=args.identifiers)
            elif args.source == "edgar":
                from thisness.benchmark import edgar

                bench = edgar.standard(n, seed=seed, mode=args.identifiers)
            else:
                bench = _generator(args.type)(n, seed=seed, **options)
            if args.names_only:
                bench = names_only(bench)
            if args.messy:
                bench = messy(bench)
            if args.plan == "rewrite":
                _eval_rewrite(args, bench)
                continue
            report, engine, truth = run_benchmark(bench, adjudicator=factory, plan=plan,
                                                  store=args.save or ":memory:")  # fmt: skip
            assisted = (_eval_assistant(args, engine, truth, bench)
                        if args.assistant != "none" else None)  # fmt: skip
            if args.save:
                engine.store.close()
                print(f"saved to {args.save}; open it with: thisness ui {args.save}")
            print(json.dumps(report.as_dict(), indent=1) if args.json else report.summary())
            if not args.json and report.run.get("plan") == "name_only":
                keep = ("nodes", "collapsed_groups", "proposals", "edges", "calls", "spent_usd")
                print("  name-only:", json.dumps({k: report.run[k] for k in keep}, sort_keys=True))
            if not args.json and "adjudication" in report.run:
                print("  adjudication:", json.dumps(report.run["adjudication"], sort_keys=True))
            if assisted is not None:
                print("  assistant:", json.dumps(assisted, sort_keys=True))
    return 0


def _eval_assistant(args: argparse.Namespace, engine, truth, bench) -> dict:
    """The review assistant answers every review the run left open, and its suggestions
    are scored against the truth as if accepted (design 06 §10.10, design 09 §6)."""
    from thisness.assistant import ReviewAssistant
    from thisness.benchmark.evaluate import recommendation_accuracy
    from thisness.benchmark.oracle import benchmark_truth, oracle_provider
    from thisness.llm import LLMClient, LLMPolicy
    from thisness.llm.providers import RecordingProvider

    if args.assistant == "oracle":

        def related(a, b):
            return bench.group.get(a) is not None and bench.group.get(a) == bench.group.get(b)

        provider = oracle_provider(benchmark_truth(engine, truth), related)
        if args.record:
            provider = RecordingProvider(provider, args.record)
    else:
        args.provider = args.assistant
        provider = _provider(args)
    client = LLMClient(engine.store, provider, ids=engine.ids, clock=engine.clock,
                       policy=LLMPolicy(budget_usd=args.budget, concurrency=args.concurrency))  # fmt: skip
    assistant = ReviewAssistant(client, model=args.model)
    results = engine.recommend_open(limit=1_000_000, assistant=assistant)
    out = {**recommendation_accuracy(engine, truth, results), "calls": client.calls,
           "spent_usd": round(client.spent_usd, 4)}  # fmt: skip
    if args.accept:
        out["accepted"] = _eval_accept(engine, truth, bench)
    return out


def _eval_accept(engine, truth, bench) -> dict:
    """Accept the suggestions each profile accepts in bulk, then measure what is left
    (design 10 §4.2): precision and recall after, and the reviews still open."""
    from thisness.benchmark.evaluate import cluster_metrics
    from thisness.core.decisions import Actor

    result = engine.accept_recommendations(actor=Actor(kind="human", id="eval"))
    after = cluster_metrics(engine.store.entities_of(list(truth)), truth, bench.kind)
    left = len(engine.reviews())
    return {"accepted": result.accepted, "skipped": result.skipped, "reviews_left": left,
            "reviews_left_per_1000": round(1000 * left / len(truth), 1),
            "b3_precision": round(after.b3_precision, 4), "b3_recall": round(after.b3_recall, 4),
            "false_merge_clusters": after.false_merge_clusters}  # fmt: skip


def parser() -> argparse.ArgumentParser:
    from thisness import __version__

    p = argparse.ArgumentParser(prog="thisness", description="Identity resolution with evidence.")
    p.add_argument("--version", action="version", version=f"thisness {__version__}")
    p.add_argument("--store", default=os.environ.get("THISNESS_STORE", "identity.duckdb"),
                   help="DuckDB store file (default: $THISNESS_STORE or identity.duckdb)")  # fmt: skip
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("ingest", help="ingest a CSV of observations")
    s.add_argument("file")
    s.add_argument("--type", default="organization", choices=sorted(PROFILES))
    s.add_argument("--source-type", default="csv", help="source system name (default: csv)")
    s.add_argument("--resolve", action="store_true", help="resolve after ingesting")
    s.set_defaults(func=cmd_ingest)

    s = sub.add_parser("resolve", help="resolve every unresolved observation")
    s.add_argument("--type", choices=sorted(PROFILES))
    s.add_argument("--plan", default="default", choices=["default", "name-only"],
                   help="name-only: group names with a model (design 05 §6)")  # fmt: skip
    s.add_argument("--group-model", default=None, help="for the grouping passes")
    s.add_argument("--model-role", default="auto",
                   choices=["auto", "decide", "shadow", "suggest", "off"],
                   help="with --provider: what the model does; auto follows each profile "
                        "(organizations and taxa decide, people suggest)")  # fmt: skip
    _model_options(s)
    s.set_defaults(func=cmd_resolve)

    s = sub.add_parser("ui", help="open the Workbench")
    s.add_argument("path", nargs="?", help="store file (overrides --store)")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--no-browser", action="store_true")
    _model_options(s)
    s.set_defaults(func=cmd_ui)

    s = sub.add_parser("recommend", help="ask a model to recommend answers to open reviews")
    s.add_argument("--kind", choices=["cluster", "boundary", "merge", "split"])
    s.add_argument("--limit", type=int, default=20, help="most impactful reviews first")
    _model_options(s, required=True)
    s.set_defaults(func=cmd_recommend)

    s = sub.add_parser("labels", help="display names: suggest, list, accept, clear")
    s.add_argument("action", choices=["suggest", "list", "accept", "clear"])
    s.add_argument("ids", nargs="*", help="accept: suggestion ids; clear: entity ids")
    s.add_argument("--type", default="organization", choices=sorted(PROFILES))
    s.add_argument("--limit", type=int, default=100)
    s.add_argument("--all", action="store_true", help="accept: every suggestion not accepted")
    s.add_argument("--reason", default=None)
    _model_options(s, required=True)
    s.set_defaults(func=cmd_labels)

    s = sub.add_parser("demo", help="build a store from synthetic organizations or people")
    s.add_argument("--type", default="organization", choices=GENERATED)
    s.add_argument("--reference", action="store_true",
                   help="taxa only: add WFO's own rows, so entities are labelled by WFO")  # fmt: skip
    s.add_argument("--col", action="store_true",
                   help="taxa only: add the Catalogue of Life's rows, a second authority "
                        "(fetched with attribution, CC BY 4.0)")  # fmt: skip
    s.add_argument("--size", "--orgs", type=int, default=300, dest="size",
                   help="entities to generate (default 300)")  # fmt: skip
    s.add_argument("--seed", type=int, default=11)
    s.add_argument("--force", action="store_true")
    s.set_defaults(func=cmd_demo)

    s = sub.add_parser("history", help="ledger events for an entity or observation")
    s.add_argument("id")
    s.set_defaults(func=cmd_history)

    s = sub.add_parser("dossier", help="an entity's dossier as LLM context")
    s.add_argument("id")
    s.add_argument("--chars", type=int, default=4000)
    s.set_defaults(func=cmd_dossier)

    s = sub.add_parser("export", help="export the mapping of records to entities")
    s.add_argument("what", choices=["mapping"])
    s.add_argument("--type", default="organization", choices=sorted(PROFILES))
    s.add_argument("--format", default="csv", choices=["csv", "parquet"])
    s.add_argument("--output", default=None, help="a file; CSV goes to standard output without one")
    s.add_argument("--as-of", type=int, default=None, help="the mapping as of this ledger seq")
    s.set_defaults(func=cmd_export)

    s = sub.add_parser("reviews", help="open reviews, most impactful first; or accept "
                                        "suggestions in bulk")  # fmt: skip
    s.add_argument("action", nargs="?", default="list", choices=["list", "accept"])
    s.add_argument("--type", choices=sorted(PROFILES), help="accept: only this type")
    s.add_argument("--resolution", default=None,
                   help="accept: kinds to accept, comma-separated (same, different, related, "
                        "groups); default the profile's")  # fmt: skip
    s.add_argument("--dry-run", action="store_true", help="accept: count, change nothing")
    s.add_argument("--actor", default=None, help="accept: who accepts (default: your login)")
    s.set_defaults(func=cmd_reviews)

    s = sub.add_parser("check", help="run the invariant suite over the store")
    s.set_defaults(func=cmd_check)

    s = sub.add_parser("eval", help="resolve synthetic benchmarks and report")
    s.add_argument("--type", default="organization", choices=GENERATED)
    s.add_argument("--size", "--orgs", type=int, nargs="+", default=[805], dest="size",
                   help="entities per benchmark")  # fmt: skip
    s.add_argument("--seeds", type=int, nargs="+", default=[11])
    s.add_argument("--json", action="store_true")
    g = s.add_argument_group("adjudicator (design 05 §4)")
    g.add_argument("--adjudicator", default="none",
                   choices=["none", "oracle", "anthropic", "openai"])  # fmt: skip
    g.add_argument("--mode", default="shadow", choices=["shadow", "decide"])
    g.add_argument("--model", default=None,
                   help="default gpt-6-luna (openai) or claude-sonnet-5 (anthropic)")  # fmt: skip
    g.add_argument("--budget", type=float, default=None, help="US dollars for the run")
    g.add_argument("--max-pairs", type=int, default=500)
    g.add_argument("--concurrency", type=int, default=4)
    g.add_argument(
        "--base-url", default="https://api.openai.com/v1", help="for --adjudicator openai"
    )
    g.add_argument("--local", action="store_true", help="the openai-compatible server is yours")
    g.add_argument("--record", default=None, help="append every exchange to this cassette")
    g.add_argument(
        "--replay",
        default=None,
        help="answer from this cassette instead of calling the model: free, offline",
    )
    s.add_argument(
        "--save", default=None, help="keep the run in this DuckDB file, to open with `thisness ui`"
    )
    g.add_argument("--oracle-error", type=float, default=0.0)
    g.add_argument("--oracle-fold", type=float, default=0.0)
    s.add_argument("--accept", action="store_true",
                   help="with --assistant: accept the suggestions each profile accepts in bulk, "
                        "then report what is left (design 10 §4.2)")  # fmt: skip
    s.add_argument("--assistant", default="none", choices=["none", "oracle", "anthropic", "openai"],
                   help="after the run, the review assistant answers every open review and "
                        "its suggestions are scored against the truth")  # fmt: skip
    s.add_argument("--names-only", action="store_true", help="the supplier-names benchmark")
    s.add_argument("--messy", action="store_true",
                   help="with --names-only: names as real extracts write them (design 08 §8)")  # fmt: skip
    s.add_argument("--reference", action="store_true", help="taxa only: WFO's own rows too")
    s.add_argument(
        "--source",
        default="synthetic",
        choices=["synthetic", "uk", "edgar"],
        help="uk: real UK companies from GLEIF and Companies House (design 07); "
        "stage them first with python -m thisness.benchmark uk-stage. edgar: real SEC "
        "insiders (design 09, --type person); stage them with edgar-stage",
    )
    s.add_argument(
        "--identifiers",
        default="hidden",
        choices=["hidden", "partial", "visible"],
        help="with --source uk: whether records carry company numbers and LEIs; with "
        "--source edgar: whether EDGAR records carry CIKs (hidden or partial)",
    )
    s.add_argument("--col", action="store_true", help="taxa only: the Catalogue of Life's rows")
    s.add_argument("--plan", default="default", choices=["default", "name-only", "rewrite"],
                   help="name-only: group names with --adjudicator's model (or the oracle); "
                        "rewrite: the rewrite-and-hash baseline (design 05 §11.5)")  # fmt: skip
    s.add_argument("--group-model", default=None)
    s.add_argument("--rewrite-order", default="as_built", choices=["as_built", "sorted"],
                   help="with --plan rewrite: later passes ordered by key hash, or by name")  # fmt: skip
    s.set_defaults(func=cmd_eval)
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    _default_models(args)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
