"""One tiny end-to-end build per test session, shared by the warehouse tests.

The 13 hand-written edge-case customers plus 40 random ones go through the real Dagster
assets (raw load, dbt build with all dbt tests, metrics, report) in about 20 seconds.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import duckdb
import pytest

from subs_analytics.generator import GeneratorConfig, generate, write_export
from subs_analytics.warehouse import DB_ENV, RAW_ENV, REPORTS_ENV

PARTITION = "2025-05-01"


@dataclass
class Built:
    db_path: Path
    raw_dir: Path
    reports_dir: Path
    result: Any  # dagster ExecuteInProcessResult

    def rows(self, sql: str, params: list[Any] | None = None) -> list[tuple[Any, ...]]:
        with duckdb.connect(str(self.db_path), read_only=True) as con:
            return con.execute(sql, params or []).fetchall()


@pytest.fixture(scope="session")
def built(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Built]:
    root = tmp_path_factory.mktemp("built")
    env = {
        DB_ENV: str(root / "warehouse.duckdb"),
        RAW_ENV: str(root / "raw"),
        REPORTS_ENV: str(root / "reports"),
    }
    old = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        write_export(generate(GeneratorConfig(seed=7, n_customers=40)), root / "raw")
        from dagster import materialize

        from subs_analytics.definitions import ALL_ASSETS, RESOURCES

        result = materialize(ALL_ASSETS, partition_key=PARTITION, resources=RESOURCES)
        yield Built(root / "warehouse.duckdb", root / "raw", root / "reports", result)
    finally:
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
