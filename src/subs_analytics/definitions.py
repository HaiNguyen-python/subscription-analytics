"""Dagster software-defined assets: raw load -> dbt models -> monthly metrics -> report.

    raw/<table>  (8 assets, one multi-asset)   load the Stripe export files into DuckDB
    dbt models   (via dagster-dbt)             `dbt build`: models and their tests
    monthly_metrics  (monthly partitions)      semantic-layer metrics for one month
    monthly_report   (monthly partitions)      HTML report and charts for one month

dbt sources are named `raw.<table>`, so dagster-dbt links them to the raw assets by key
without a custom translator. dbt tests show up as asset checks; the metrics asset adds
three more (MRR matches the mart, bridge closes, billing reconciliation).

Run the UI with `dagster dev` (the module is set in pyproject.toml). `subs run-all` and the
tests materialise the same assets in-process with `dagster.materialize`.
"""

from collections.abc import Iterator
from datetime import date
from typing import Any

from dagster import (
    AssetCheckResult,
    AssetCheckSpec,
    AssetExecutionContext,
    AssetKey,
    AssetsDefinition,
    AssetSelection,
    AssetSpec,
    Definitions,
    MaterializeResult,
    MetadataValue,
    MonthlyPartitionsDefinition,
    asset,
    build_schedule_from_partitioned_job,
    define_asset_job,
    multi_asset,
)
from dagster_dbt import DbtCliResource, DbtProject, dbt_assets

from . import report, semantic
from .generator import RAW_FILES, START
from .warehouse import DBT_DIR, Settings, connect, dbt_executable, ensure_manifest, load_raw

MONTHLY = MonthlyPartitionsDefinition(start_date=START.strftime("%Y-%m-%d"), end_date="2026-10-01")
KPI_METRICS = [
    "mrr",
    "arr",
    "starting_mrr",
    "new_mrr",
    "expansion_mrr",
    "contraction_mrr",
    "churned_mrr",
    "reactivation_mrr",
    "fx_mrr",
    "net_new_mrr",
    "active_customers",
    "churned_customers",
    "logo_churn_rate",
    "revenue_churn_rate",
    "nrr",
    "arpa",
]
TOLERANCE_EUR = 0.01

DBT_PROJECT = DbtProject(project_dir=DBT_DIR, profiles_dir=DBT_DIR)
# One fixed folder for the latest build (run_results.json, compiled SQL) instead of a new
# folder per run; the report reads its test summary from here.
DBT_BUILD_DIR = DBT_DIR / "target" / "dagster_build"
RAW_KEYS = {table: AssetKey(["raw", table]) for table in RAW_FILES}


@multi_asset(
    specs=[
        AssetSpec(key, group_name="raw", description=f"Stripe export: {RAW_FILES[t]}")
        for t, key in RAW_KEYS.items()
    ],
    check_specs=[AssetCheckSpec("has_rows", asset=key) for key in RAW_KEYS.values()],
)
def raw_stripe_export(context: AssetExecutionContext) -> Iterator[Any]:
    """Load every export file into `raw.<table>`. Idempotent: tables are replaced."""
    settings = Settings.from_env()
    counts = load_raw(settings.raw_dir, settings.db_path)
    for table, key in RAW_KEYS.items():
        yield MaterializeResult(asset_key=key, metadata={"rows": counts[table]})
        yield AssetCheckResult(
            asset_key=key,
            check_name="has_rows",
            passed=counts[table] > 0,
            metadata={"rows": counts[table]},
        )
    context.log.info(f"loaded {sum(counts.values())} rows from {settings.raw_dir}")


@dbt_assets(manifest=ensure_manifest(), project=DBT_PROJECT)
def subs_dbt_models(context: AssetExecutionContext, dbt: DbtCliResource) -> Iterator[Any]:
    """`dbt build` runs models and tests in dependency order; failures stop downstream."""
    Settings.from_env().export()  # dbt's profile reads SUBS_DUCKDB_PATH
    yield from dbt.cli(["build"], context=context, target_path=DBT_BUILD_DIR).stream()


def _month(context: AssetExecutionContext) -> date:
    return date.fromisoformat(context.partition_key)


@asset(
    partitions_def=MONTHLY,
    group_name="metrics",
    deps=[
        AssetKey("fct_mrr_movements"),
        AssetKey("mart_mrr_monthly"),
        AssetKey("mart_revenue_reconciliation"),
    ],
    check_specs=[
        AssetCheckSpec("mrr_matches_mart", asset="monthly_metrics"),
        AssetCheckSpec("bridge_closes", asset="monthly_metrics"),
        AssetCheckSpec("billing_reconciliation", asset="monthly_metrics"),
    ],
)
def monthly_metrics(context: AssetExecutionContext) -> Iterator[Any]:
    """Compile the KPI metrics for one month from the semantic layer and store them in
    `metrics.monthly_kpis` (delete + insert, so re-running a partition is idempotent)."""
    month, settings = _month(context), Settings.from_env()
    with connect(settings.db_path, read_only=False) as con:
        rows = semantic.query(con, KPI_METRICS, month=month)
        values = {m: rows[0][m] for m in KPI_METRICS} if rows else {}
        con.execute("create schema if not exists metrics")
        con.execute(
            "create table if not exists metrics.monthly_kpis "
            "(month_start date, metric varchar, value double)"
        )
        con.execute("delete from metrics.monthly_kpis where month_start = ?", [month])
        con.executemany(
            "insert into metrics.monthly_kpis values (?, ?, ?)",
            [[month, m, v] for m, v in values.items()],
        )
        mart = con.execute(
            "select ending_mrr_eur, net_new_mrr_eur from analytics.mart_mrr_monthly "
            "where month_start = ?",
            [month],
        ).fetchone()
        rec = con.execute(
            "select unexplained_eur from analytics.mart_revenue_reconciliation "
            "where month_start = ?",
            [month],
        ).fetchone()
    yield MaterializeResult(
        metadata={
            "month": month.isoformat(),
            **{m: MetadataValue.float(float(v or 0)) for m, v in values.items()},
        }
    )
    mrr, net_new = float(values.get("mrr") or 0), float(values.get("net_new_mrr") or 0)
    start, fx = float(values.get("starting_mrr") or 0), float(values.get("fx_mrr") or 0)
    yield AssetCheckResult(
        check_name="mrr_matches_mart",
        passed=mart is not None
        and abs(mrr - mart[0]) <= TOLERANCE_EUR
        and abs(net_new - mart[1]) <= TOLERANCE_EUR,
        metadata={"semantic_mrr": mrr, "mart_mrr": float(mart[0]) if mart else -1.0},
    )
    yield AssetCheckResult(
        check_name="bridge_closes",
        passed=abs(start + net_new + fx - mrr) <= TOLERANCE_EUR,
        metadata={"gap_eur": start + net_new + fx - mrr},
    )
    unexplained = float(rec[0]) if rec else float("nan")
    yield AssetCheckResult(
        check_name="billing_reconciliation",
        passed=abs(unexplained) <= TOLERANCE_EUR,
        metadata={"unexplained_eur": unexplained},
    )


@asset(
    partitions_def=MONTHLY,
    group_name="metrics",
    deps=[
        monthly_metrics,
        AssetKey("mart_cohort_retention"),
        AssetKey("mart_revenue_reconciliation"),
    ],
)
def monthly_report(context: AssetExecutionContext) -> MaterializeResult:
    """HTML report with the MRR bridge and the cohort heatmap for one month."""
    month, settings = _month(context), Settings.from_env()
    with connect(settings.db_path) as con:
        data = report.collect(con, month)
    path = report.write_html(data, settings.reports_dir / month.strftime("%Y-%m"))
    return MaterializeResult(
        metadata={"path": MetadataValue.path(str(path)), "headline": data.headline}
    )


ALL_ASSETS: list[AssetsDefinition] = [
    raw_stripe_export,
    subs_dbt_models,
    monthly_metrics,
    monthly_report,
]
monthly_refresh = define_asset_job(
    "monthly_refresh", selection=AssetSelection.all(), partitions_def=MONTHLY
)

RESOURCES = {"dbt": DbtCliResource(project_dir=DBT_PROJECT, dbt_executable=dbt_executable())}

defs = Definitions(
    assets=ALL_ASSETS,
    jobs=[monthly_refresh],
    # On the 2nd of each month, refresh everything and report the month that just closed.
    schedules=[build_schedule_from_partitioned_job(monthly_refresh, day_of_month=2, hour_of_day=6)],
    resources=RESOURCES,
)
