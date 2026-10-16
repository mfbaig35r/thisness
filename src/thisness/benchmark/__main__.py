"""``python -m thisness.benchmark run``: resolve synthetic benchmarks and report.
``python -m thisness.benchmark starter``: rebuild a shipped starter model."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from thisness.benchmark import organizations, people, taxa
from thisness.benchmark.evaluate import PROFILES, run_benchmark
from thisness.starter import write_starter

GENERATORS = {"organization": organizations.generate, "person": people.generate,
              "taxon": taxa.standard}  # fmt: skip


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m thisness.benchmark")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="resolve benchmarks and print reports")
    run.add_argument("--type", default="organization", choices=sorted(GENERATORS))
    run.add_argument("--size", "--orgs", type=int, nargs="+", default=[805], dest="orgs",
                     help="entities per benchmark")  # fmt: skip
    run.add_argument("--seeds", type=int, nargs="+", default=[11])
    run.add_argument("--json", action="store_true", help="print full reports as JSON")
    run.add_argument("--reference", action="store_true",
                     help="taxon only: WFO's own rows as observations too (design 06 §5)")  # fmt: skip
    run.add_argument(
        "--col",
        action="store_true",
        help="taxon only: the Catalogue of Life's rows too, a second authority",
    )
    run.add_argument(
        "--authority",
        default=None,
        help="taxon only: the reference authority's source (default wfo)",
    )
    sub.add_parser("wfo-fixture", help="rebuild the shipped WFO fixture (fetches three families)")
    sub.add_parser("uk-stage", help="fetch GLEIF and Companies House and stage them (design 07)")
    sub.add_parser("uk-truth", help="count how good the GLEIF to Companies House join is")
    uk_audit = sub.add_parser("uk-audit", help="read a sample of the join")
    uk_audit.add_argument("--n", type=int, default=400)
    sub.add_parser("uk-fixture", help="rebuild the shipped UK fixture from the staged data")
    edgar_stage = sub.add_parser(
        "edgar-stage", help="stage EDGAR insiders from an edgar-persons-pool store (design 09)"
    )
    edgar_stage.add_argument("--store", type=Path, required=True,
                             help="the edgar-persons-pool DuckDB file (read only)")  # fmt: skip
    sub.add_parser("edgar-truth", help="count how good the CIK is as truth")
    edgar_audit = sub.add_parser("edgar-audit", help="read samples of the truth's doubts")
    edgar_audit.add_argument("--n", type=int, default=40)
    starter = sub.add_parser("starter", help="rebuild a starter model")
    starter.add_argument("--type", default="organization", choices=sorted(GENERATORS))
    starter.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    if args.command == "run":
        for n in args.orgs:
            for seed in args.seeds:
                options = {k: True for k in ("reference", "col") if getattr(args, k)}
                profile = (PROFILES[args.type](authority=args.authority)
                           if args.authority else None)  # fmt: skip
                report, _, _ = run_benchmark(GENERATORS[args.type](n, seed=seed, **options),
                                             profile=profile)  # fmt: skip
                print(json.dumps(report.as_dict(), indent=1) if args.json else report.summary())
    elif args.command.startswith("uk-"):
        from thisness.benchmark import uk

        if args.command == "uk-stage":
            print(f"staged {uk.stage()}")
        elif args.command == "uk-truth":
            print(json.dumps(uk.truth_report(), indent=1))
        elif args.command == "uk-audit":
            report = uk.audit(n=args.n)
            print(json.dumps({k: v for k, v in report.items() if k != "differing_names"}))
            for d in report["differing_names"][:25]:
                print(f"- {d['gleif']!r} | Companies House: {d['companies_house']!r}")
        else:
            print(f"wrote {uk.make_fixture()}")
    elif args.command.startswith("edgar-"):
        from thisness.benchmark import edgar

        if args.command == "edgar-stage":
            print(f"staged {edgar.stage(args.store)}")
        elif args.command == "edgar-truth":
            print(json.dumps(edgar.truth_report(), indent=1))
        else:
            print(json.dumps(edgar.audit(n=args.n), indent=1))
    elif args.command == "wfo-fixture":
        target = Path(taxa.__file__).parent / "data" / "wfo_fixture.csv.gz"
        taxa.write_rows(taxa.families(taxa.FIXTURE_FAMILIES), target)
        print(f"wrote {target}")
    else:
        import thisness.profiles

        profile = PROFILES[args.type]()
        default = Path(thisness.profiles.__file__).parent / "data" / profile.starter_model
        path = write_starter(profile, args.out or default)
        print(f"wrote {path}")


if __name__ == "__main__":
    main()
