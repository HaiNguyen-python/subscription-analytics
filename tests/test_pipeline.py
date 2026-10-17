"""Dagster definitions, the in-process materialisation, idempotent partitions, CLI, report."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import duckdb
import pytest
from conftest import PARTITION, Built
from dagster import AssetKey, materialize

from subs_analytics import cli, report
from subs_analytics.definitions import ALL_ASSETS, MONTHLY, defs, monthly_metrics


def test_definitions_load_with_job_schedule_and_linked_sources() -> None:
    assert defs.resolve_job_def("monthly_refresh") is not None
    assert [s.name for s in defs.schedules or []] == ["monthly_refresh_schedule"]
    graph = defs.resolve_asset_graph()
    stg = graph.get(AssetKey("stg_customers"))
    assert AssetKey(["raw", "customers"]) in stg.parent_keys  # dbt source -> raw asset
    assert MONTHLY.get_partition_keys()[-1] == "2026-09-01"
    assert len(MONTHLY.get_partition_keys()) == 24


def test_everything_materialises_and_every_check_passes(built: Built) -> None:
    assert built.result.success
    checks = built.result.get_asset_check_evaluations()
    names = {c.check_name for c in checks}
    assert {"has_rows", "mrr_matches_mart", "bridge_closes", "billing_reconciliation"} <= names
    assert any("assert_mrr_reconciles_to_billing" in n for n in names)  # dbt tests as checks
    failed = [(c.asset_key.to_user_string(), c.check_name) for c in checks if not c.passed]
    # The revenue tolerance test is a warning by design; on 53 customers it may fire.
    assert [f for f in failed if "revenue_within_tolerance" not in f[1]] == []


def test_metrics_partition_is_idempotent(built: Built) -> None:
    def kpis() -> list[tuple]:
        with duckdb.connect(str(built.db_path), read_only=True) as con:
            return con.execute(
                "select metric, value from metrics.monthly_kpis where month_start = ? order by 1",
                [PARTITION],
            ).fetchall()

    before = kpis()
    again = materialize([monthly_metrics], partition_key=PARTITION)
    assert again.success
    assert kpis() == before and len(before) == 16


def test_report_asset_and_cli_report(built: Built, tmp_path: Path) -> None:
    assert (built.reports_dir / "2025-05" / "report.html").exists()
    cli.main(["report", "--month", PARTITION, "--docs-dir", str(tmp_path)])
    text = (tmp_path / "results.md").read_text()
    assert "synthetic data, seed 7" in text and "MRR bridge" in text
    for png in ("mrr_bridge.png", "cohort_retention.png"):
        size = (tmp_path / "img" / png).stat().st_size
        assert 5_000 < size < 300_000


def test_headline_and_cohort_comparison(built: Built) -> None:
    with duckdb.connect(str(built.db_path), read_only=True) as con:
        data = report.collect(con, date(2025, 5, 1))
    assert data.headline.startswith("MRR ") and "May 2025" in data.headline
    assert data.change == pytest.approx(data.kpis["mrr"] - data.kpis["starting_mrr"])
    focus, others = report.cohort_comparison(data)
    assert focus is None and 0 < others <= 1  # Jan 2026 is after the report month


def test_cli_generate_writes_a_small_export(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SUBS_RAW_DIR", str(tmp_path / "raw"))
    cli.main(["generate", "--customers", "10", "--seed", "1"])
    assert (tmp_path / "raw" / "subscriptions.json").exists()
    assert len(ALL_ASSETS) == 4


def test_cli_load_reads_an_export_folder(
    built: Built, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The loader path for a real export: point `subs load` at a folder of files."""
    monkeypatch.setenv("SUBS_DUCKDB_PATH", str(tmp_path / "real.duckdb"))
    cli.main(["load", "--source-dir", str(built.raw_dir)])
    with duckdb.connect(str(tmp_path / "real.duckdb"), read_only=True) as con:
        [(customers,)] = con.execute("select count(*) from raw.customers").fetchall()
    assert customers == 40 + 13  # random customers plus the edge cases
