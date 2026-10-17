"""Warehouse plumbing: settings, the raw load into DuckDB, and calling dbt.

The raw layer stores the export files as they are (typed, nested JSON kept as STRUCT/LIST),
so every business rule lives in dbt where it is tested and documented. The same loader reads
a real Stripe export, as long as the files follow `RAW_FILES` (see docs/how-it-works.md).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import duckdb

from .generator import RAW_FILES

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DBT_DIR = PROJECT_ROOT / "dbt"
DB_ENV, RAW_ENV, REPORTS_ENV = "SUBS_DUCKDB_PATH", "SUBS_RAW_DIR", "SUBS_REPORTS_DIR"
ANALYTICS_SCHEMA = "analytics"


@dataclass(frozen=True)
class Settings:
    """Where things live. Read from the environment so dbt, Dagster and the CLI agree."""

    db_path: Path
    raw_dir: Path
    reports_dir: Path

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            db_path=Path(os.environ.get(DB_ENV, "data/warehouse.duckdb")).resolve(),
            raw_dir=Path(os.environ.get(RAW_ENV, "data/raw")).resolve(),
            reports_dir=Path(os.environ.get(REPORTS_ENV, "reports")).resolve(),
        )

    def export(self) -> None:
        """Put absolute paths into the environment for child processes (dbt reads DB_ENV)."""
        os.environ[DB_ENV] = str(self.db_path)
        os.environ[RAW_ENV] = str(self.raw_dir)
        os.environ[REPORTS_ENV] = str(self.reports_dir)


_TS = "TIMESTAMP"
# Explicit column types: a tiny export can have an all-empty column (or no refunds at all),
# and type inference would then guess VARCHAR or NULL and break the staging models.
_CSV_TYPES: dict[str, dict[str, str]] = {
    "customers": {
        "id": "VARCHAR",
        "email": "VARCHAR",
        "created": _TS,
        "country": "VARCHAR",
        "currency": "VARCHAR",
    },
    "invoices": {
        "id": "VARCHAR",
        "customer": "VARCHAR",
        "subscription": "VARCHAR",
        "created": _TS,
        "status": "VARCHAR",
        "currency": "VARCHAR",
        "billing_reason": "VARCHAR",
        "total": "BIGINT",
        "amount_paid": "BIGINT",
    },
    "invoice_line_items": {
        "id": "VARCHAR",
        "invoice": "VARCHAR",
        "subscription": "VARCHAR",
        "price": "VARCHAR",
        "amount": "BIGINT",
        "discount_amount": "BIGINT",
        "proration": "BOOLEAN",
        "period_start": _TS,
        "period_end": _TS,
        "description": "VARCHAR",
    },
    "refunds": {
        "id": "VARCHAR",
        "invoice": "VARCHAR",
        "amount": "BIGINT",
        "currency": "VARCHAR",
        "created": _TS,
        "reason": "VARCHAR",
    },
    "fx_rates": {"month": "DATE", "currency": "VARCHAR", "rate_to_eur": "DOUBLE"},
}
_JSON_TYPES: dict[str, dict[str, str]] = {
    "prices": {
        "id": "VARCHAR",
        "currency": "VARCHAR",
        "unit_amount": "BIGINT",
        "recurring": "STRUCT(interval VARCHAR, interval_count INTEGER)",
        "product": "STRUCT(id VARCHAR, name VARCHAR, tier VARCHAR)",
    },
    "subscriptions": {
        "id": "VARCHAR",
        "customer": "VARCHAR",
        "created": "BIGINT",
        "status": "VARCHAR",
        "trial_start": "BIGINT",
        "trial_end": "BIGINT",
        "cancel_at_period_end": "BOOLEAN",
        "canceled_at": "BIGINT",
        "ended_at": "BIGINT",
        "price": "VARCHAR",
        "history": "STRUCT(effective_at BIGINT, status VARCHAR, price VARCHAR, "
        "cancel_at_period_end BOOLEAN)[]",
    },
    "discounts": {
        "id": "VARCHAR",
        "subscription": "VARCHAR",
        "customer": "VARCHAR",
        "start": "BIGINT",
        "end": "BIGINT",
        "coupon": "STRUCT(id VARCHAR, percent_off INTEGER, duration VARCHAR, "
        "duration_in_months INTEGER)",
    },
}


def _columns_sql(types: dict[str, str]) -> str:
    return "{" + ", ".join(f"'{name}': '{kind}'" for name, kind in types.items()) + "}"


def load_raw(raw_dir: Path, db_path: Path) -> dict[str, int]:
    """(Re)create `raw.<table>` for every export file. Idempotent: a reload replaces tables."""
    missing = [f for f in RAW_FILES.values() if not (raw_dir / f).exists()]
    if missing:
        raise FileNotFoundError(
            f"missing export files in {raw_dir}: {missing}. Run `subs generate` first."
        )
    db_path.parent.mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = {}
    with duckdb.connect(str(db_path)) as con:
        con.execute("create schema if not exists raw")
        for table, file_name in RAW_FILES.items():
            path = str(raw_dir / file_name).replace("'", "''")
            if table in _CSV_TYPES:
                source = (
                    f"read_csv('{path}', header = true, "
                    f"columns = {_columns_sql(_CSV_TYPES[table])})"
                )
            else:
                source = (
                    f"read_json('{path}', format = 'array', "
                    f"columns = {_columns_sql(_JSON_TYPES[table])})"
                )
            con.execute(f"create or replace table raw.{table} as select * from {source}")
            row = con.execute(f"select count(*) from raw.{table}").fetchone()
            counts[table] = int(row[0]) if row else 0
    return counts


def connect(db_path: Path, read_only: bool = True) -> duckdb.DuckDBPyConnection:
    return duckdb.connect(str(db_path), read_only=read_only)


def dbt_executable() -> str:
    """Prefer the dbt next to the running Python, so a venv's dbt is used even if not on PATH."""
    local = Path(sys.executable).with_name("dbt")
    found = str(local) if local.exists() else shutil.which("dbt")
    if found is None:
        raise RuntimeError("dbt not found; install the package with `pip install -e .`")
    return found


def ensure_manifest(force: bool = False) -> Path:
    """Run `dbt parse` if the manifest is missing. Dagster needs it to build the asset graph.

    `dbt parse` does not touch the warehouse, so this is safe at import time.
    """
    manifest = DBT_DIR / "target" / "manifest.json"
    if force or not manifest.exists():
        env = {**os.environ, "DBT_SEND_ANONYMOUS_USAGE_STATS": "false"}
        proc = subprocess.run(
            [
                dbt_executable(),
                "parse",
                "--project-dir",
                str(DBT_DIR),
                "--profiles-dir",
                str(DBT_DIR),
                "--quiet",
            ],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"dbt parse failed:\n{(proc.stdout + proc.stderr)[-3000:]}")
    return manifest
