"""`subs` command line: generate data, run the Dagster assets, write the report."""

from __future__ import annotations

import argparse
import logging
from collections.abc import Sequence
from datetime import date
from pathlib import Path

from .generator import GeneratorConfig, generate, write_export
from .warehouse import DBT_DIR, Settings, connect, load_raw

log = logging.getLogger("subs")
LATEST_MONTH = "2026-09-01"


def cmd_generate(args: argparse.Namespace, settings: Settings) -> None:
    counts = write_export(
        generate(GeneratorConfig(seed=args.seed, n_customers=args.customers)), settings.raw_dir
    )
    log.info("wrote %s to %s", counts, settings.raw_dir)


def cmd_load(args: argparse.Namespace, settings: Settings) -> None:
    log.info("loaded %s", load_raw(Path(args.source_dir or settings.raw_dir), settings.db_path))


def materialize_all(month: str) -> bool:
    """Run every asset in-process for one monthly partition (raw and dbt are unpartitioned)."""
    from dagster import materialize

    from .definitions import ALL_ASSETS, RESOURCES

    result = materialize(ALL_ASSETS, partition_key=month, resources=RESOURCES)
    failed = [c for c in result.get_asset_check_evaluations() if not c.passed]
    for check in failed:
        log.error("asset check failed: %s %s", check.asset_key.to_user_string(), check.check_name)
    return result.success and not failed


def cmd_materialize(args: argparse.Namespace, settings: Settings) -> None:
    if not materialize_all(args.month):
        raise SystemExit(1)


def cmd_report(args: argparse.Namespace, settings: Settings) -> None:
    from . import report

    month = date.fromisoformat(args.month)
    with connect(settings.db_path) as con:
        data = report.collect(con, month)
    label = report.data_label(settings.raw_dir)
    paths = report.write_docs(
        data, Path(args.docs_dir), label, report.dbt_summary(DBT_DIR / "target" / "dagster_build")
    )
    paths.append(report.write_html(data, settings.reports_dir / month.strftime("%Y-%m")))
    log.info("%s. Wrote %s", data.headline, ", ".join(str(p) for p in paths))


def cmd_run_all(args: argparse.Namespace, settings: Settings) -> None:
    cmd_generate(args, settings)
    cmd_materialize(args, settings)
    cmd_report(args, settings)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="subs", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    gen = argparse.ArgumentParser(add_help=False)
    gen.add_argument("--seed", type=int, default=42)
    gen.add_argument("--customers", type=int, default=2000)
    month = argparse.ArgumentParser(add_help=False)
    month.add_argument("--month", default=LATEST_MONTH, help="partition, e.g. 2026-09-01")
    docs = argparse.ArgumentParser(add_help=False)
    docs.add_argument("--docs-dir", default="docs")
    sub.add_parser("generate", parents=[gen], help="write synthetic Stripe exports")
    load = sub.add_parser("load", help="load export files into raw.* (no dbt)")
    load.add_argument("--source-dir", help="folder with a real export (default: SUBS_RAW_DIR)")
    sub.add_parser("materialize", parents=[month], help="run all Dagster assets in-process")
    sub.add_parser("report", parents=[month, docs], help="docs/results.md, charts, HTML")
    sub.add_parser("run-all", parents=[gen, month, docs], help="generate, materialize, report")
    return parser


COMMANDS = {
    "generate": cmd_generate,
    "load": cmd_load,
    "materialize": cmd_materialize,
    "report": cmd_report,
    "run-all": cmd_run_all,
}


def main(argv: Sequence[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = build_parser().parse_args(argv)
    settings = Settings.from_env()
    settings.export()
    COMMANDS[args.command](args, settings)


if __name__ == "__main__":
    main()
