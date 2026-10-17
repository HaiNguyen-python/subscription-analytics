"""The semantic layer: compiler rules, agreement with the marts and with MetricFlow, and
proof that the report reads its numbers from the YAML definition."""

from __future__ import annotations

import os
import shutil
import subprocess
from datetime import date
from pathlib import Path

import duckdb
import pytest
import yaml
from conftest import Built

from subs_analytics import report, semantic
from subs_analytics.definitions import DBT_BUILD_DIR
from subs_analytics.warehouse import DB_ENV, DBT_DIR

LAYER_YAML = {
    "semantic_models": [
        {
            "name": "t",
            "model": "ref('facts')",
            "defaults": {"agg_time_dimension": "month_start"},
            "dimensions": [
                {"name": "month_start", "type": "time"},
                {"name": "tier", "type": "categorical"},
            ],
            "measures": [
                {"name": "amount", "agg": "sum"},
                {"name": "payers", "agg": "count_distinct", "expr": "customer"},
            ],
        }
    ],
    "metrics": [
        {"name": "revenue", "type": "simple", "type_params": {"measure": "amount"}},
        {"name": "payers", "type": "simple", "type_params": {"measure": "payers"}},
        {
            "name": "per_payer",
            "type": "ratio",
            "type_params": {"numerator": "revenue", "denominator": "payers"},
        },
        {
            "name": "doubled",
            "type": "derived",
            "type_params": {"expr": "revenue * 2", "metrics": [{"name": "revenue"}]},
        },
        {
            "name": "sneaky",
            "type": "derived",
            "type_params": {"expr": "revenue; drop table facts", "metrics": [{"name": "revenue"}]},
        },
        {
            "name": "loop_a",
            "type": "derived",
            "type_params": {"expr": "loop_b", "metrics": [{"name": "loop_b"}]},
        },
        {
            "name": "loop_b",
            "type": "derived",
            "type_params": {"expr": "loop_a", "metrics": [{"name": "loop_a"}]},
        },
    ],
}


@pytest.fixture
def toy(tmp_path: Path) -> tuple[semantic.SemanticLayer, duckdb.DuckDBPyConnection]:
    path = tmp_path / "semantic.yml"
    path.write_text(yaml.safe_dump(LAYER_YAML))
    con = duckdb.connect()
    con.execute(
        "create schema main_s; create table main_s.facts as select * from (values "
        "(date '2025-01-01', 'a', 'c1', 10.0), (date '2025-01-01', 'b', 'c2', 30.0), "
        "(date '2025-02-01', 'a', 'c1', 0.0)) t(month_start, tier, customer, amount)"
    )
    con.execute("delete from main_s.facts where month_start = '2025-02-01' and amount = 0")
    con.execute("insert into main_s.facts select date '2025-02-01', 'a', null, 0.0")
    return semantic.load(path, schema="main_s"), con


def test_simple_ratio_and_derived_metrics(toy: tuple) -> None:
    layer, con = toy
    rows = semantic.query(con, ["revenue", "per_payer", "doubled"], layer=layer)
    assert [(r["revenue"], r["per_payer"], r["doubled"]) for r in rows] == [
        (40.0, 20.0, 80.0),
        (0.0, None, 0.0),
    ]  # ratio is null, not an error, when 0 payers
    by_tier = semantic.query(
        con, ["revenue"], group_by=["tier"], month=date(2025, 1, 1), layer=layer
    )
    assert {r["tier"]: r["revenue"] for r in by_tier} == {"a": 10.0, "b": 30.0}


@pytest.mark.parametrize(
    ("metrics", "group_by", "message"),
    [
        (["sneaky"], [], "cannot parse"),
        (["loop_a"], [], "cycle"),
        (["nope"], [], "unknown metric"),
        (["revenue"], ["customer"], "unknown dimension"),
    ],
)
def test_compiler_rejects_bad_definitions(
    toy: tuple, metrics: list[str], group_by: list[str], message: str
) -> None:
    layer, _ = toy
    with pytest.raises(semantic.SemanticError, match=message):
        semantic.compile_sql(layer, metrics, group_by)


def test_semantic_metrics_match_the_mart_every_month(built: Built) -> None:
    with duckdb.connect(str(built.db_path), read_only=True) as con:
        metrics = semantic.query(
            con, ["mrr", "arr", "net_new_mrr", "churned_mrr", "active_customers", "fx_mrr"]
        )
        mart = con.execute(
            "select month_start, ending_mrr_eur, arr_eur, net_new_mrr_eur, "
            "churned_mrr_eur, active_customers, fx_mrr_eur "
            "from analytics.mart_mrr_monthly order by 1"
        ).fetchall()
        nrr = dict(con.execute("select month_start, nrr_12m from analytics.mart_nrr").fetchall())
        nrr_semantic = semantic.query(con, ["nrr"])
    assert len(metrics) == len(mart) >= 20
    for m, row in zip(metrics, mart, strict=True):
        assert m[semantic.TIME_COLUMN] == row[0]
        assert [
            m["mrr"],
            m["arr"],
            m["net_new_mrr"],
            m["churned_mrr"],
            m["active_customers"],
            m["fx_mrr"],
        ] == pytest.approx(list(row[1:]))
    for r in nrr_semantic:
        assert r["nrr"] == pytest.approx(nrr.get(r[semantic.TIME_COLUMN]))


def test_report_numbers_come_from_the_yaml_definition(
    built: Built, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Change ARR's definition in a copy of the YAML: the report must follow it."""
    edited = tmp_path / "semantic.yml"
    edited.write_text(
        semantic.SEMANTIC_FILE.read_text().replace("expr: mrr * 12", "expr: mrr * 13")
    )
    real_load = semantic.load
    monkeypatch.setattr(
        semantic, "load", lambda path=edited, schema="analytics": real_load(path, schema)
    )
    with duckdb.connect(str(built.db_path), read_only=True) as con:
        data = report.collect(con, date(2025, 5, 1))
    assert data.kpis["arr"] == pytest.approx(13 * data.kpis["mrr"])
    assert report._eur(data.kpis["arr"]) in report.results_markdown(data, "test")


@pytest.mark.skipif(
    shutil.which("mf") is None and not (Path(os.sys.executable).with_name("mf").exists()),
    reason="dbt-metricflow not installed",
)
def test_metricflow_reads_the_same_definitions_and_agrees(built: Built) -> None:
    """dbt's own engine on the same YAML returns the same numbers as our compiler."""
    mf = Path(os.sys.executable).with_name("mf")
    env = {
        **os.environ,
        DB_ENV: str(built.db_path),
        "DBT_PROFILES_DIR": str(DBT_DIR),
        "DBT_TARGET_PATH": str(DBT_BUILD_DIR),
    }
    proc = subprocess.run(
        [
            str(mf) if mf.exists() else "mf",
            "query",
            "--metrics",
            "mrr,logo_churn_rate,nrr,arpa",
            "--group-by",
            "metric_time__month",
            "--order",
            "metric_time__month",
            "--csv",
            str(built.db_path.with_suffix(".csv")),
        ],
        cwd=DBT_DIR,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    lines = built.db_path.with_suffix(".csv").read_text().splitlines()
    header, body = lines[0].split(","), [line.split(",") for line in lines[1:]]
    with duckdb.connect(str(built.db_path), read_only=True) as con:
        ours = semantic.query(con, ["mrr", "logo_churn_rate", "nrr", "arpa"])
    assert len(body) == len(ours)
    for row, mine in zip(body, ours, strict=True):
        values = dict(zip(header, row, strict=True))
        for metric in ("mrr", "logo_churn_rate", "nrr", "arpa"):
            theirs = float(values[metric]) if values[metric] else None
            assert theirs == pytest.approx(mine[metric]), metric
