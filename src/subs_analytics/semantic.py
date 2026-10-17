"""A small compiler for the dbt semantic layer file (`dbt/models/semantic/semantic.yml`).

The YAML is a real dbt semantic model, so dbt validates it on every parse and MetricFlow can
query it. MetricFlow is a heavy dependency for one fact table, so the Dagster metrics asset
and the report use this compiler instead. It supports the subset the file uses:

- measures with `agg: sum | count | count_distinct | min | max | average` and an optional `expr`
- `simple` metrics (one measure), `ratio` metrics (numerator / denominator, null when the
  denominator is 0) and `derived` metrics (arithmetic over other metrics)
- grouping by month (`metric_time__month`) plus any categorical dimension

The point is a single definition: a metric is written once in YAML, and every number in the
report is compiled from it (tests/test_semantic.py checks this).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import duckdb
import yaml

from .warehouse import ANALYTICS_SCHEMA, DBT_DIR

SEMANTIC_FILE = DBT_DIR / "models" / "semantic" / "semantic.yml"
TIME_COLUMN = "metric_time__month"
_AGGS = {
    "sum": "sum({})",
    "count": "count({})",
    "count_distinct": "count(distinct {})",
    "min": "min({})",
    "max": "max({})",
    "average": "avg({})",
}
_TOKEN = re.compile(r"\s*(?:(\d+(?:\.\d+)?)|([A-Za-z_]\w*)|([-+*/()]))")


class SemanticError(ValueError):
    """The YAML or a query asks for something this compiler does not support."""


@dataclass(frozen=True)
class Measure:
    name: str
    agg: str
    expr: str


@dataclass(frozen=True)
class Metric:
    name: str
    label: str
    type: str
    params: Mapping[str, Any]


@dataclass(frozen=True)
class SemanticLayer:
    table: str
    time_dimension: str
    dimensions: frozenset[str]
    measures: Mapping[str, Measure]
    metrics: Mapping[str, Metric]

    def label(self, metric: str) -> str:
        return self.metrics[metric].label


def _ref_to_table(model: str, schema: str) -> str:
    match = re.fullmatch(r"ref\('(\w+)'\)", model.strip())
    if not match:
        raise SemanticError(f"model must be ref('name'), got {model!r}")
    return f"{schema}.{match.group(1)}"


def load(path: Path = SEMANTIC_FILE, schema: str = ANALYTICS_SCHEMA) -> SemanticLayer:
    """Read the semantic model and metrics from the dbt YAML file."""
    doc = yaml.safe_load(path.read_text())
    models = doc.get("semantic_models", [])
    if len(models) != 1:
        raise SemanticError("expected exactly one semantic model")
    model = models[0]
    time_dim = model["defaults"]["agg_time_dimension"]
    measures = {}
    for m in model["measures"]:
        if m["agg"] not in _AGGS:
            raise SemanticError(f"unsupported agg {m['agg']!r} in measure {m['name']}")
        measures[m["name"]] = Measure(m["name"], m["agg"], str(m.get("expr", m["name"])))
    metrics = {
        m["name"]: Metric(m["name"], m.get("label", m["name"]), m["type"], m["type_params"])
        for m in doc.get("metrics", [])
    }
    dims = frozenset(d["name"] for d in model["dimensions"] if d["type"] == "categorical")
    return SemanticLayer(_ref_to_table(model["model"], schema), time_dim, dims, measures, metrics)


def _metric_names(refs: Sequence[Any]) -> list[str]:
    return [r["name"] if isinstance(r, Mapping) else str(r) for r in refs]


def _expression(layer: SemanticLayer, name: str, used: set[str], seen: tuple[str, ...] = ()) -> str:
    """SQL expression for a metric over the measure columns; collects the measures it uses."""
    if name in seen:
        raise SemanticError(f"metric cycle: {' -> '.join((*seen, name))}")
    if name not in layer.metrics:
        raise SemanticError(f"unknown metric {name!r}")
    metric, seen = layer.metrics[name], (*seen, name)
    if metric.type == "simple":
        measure = metric.params["measure"]
        measure = measure["name"] if isinstance(measure, Mapping) else measure
        if measure not in layer.measures:
            raise SemanticError(f"metric {name} uses unknown measure {measure!r}")
        used.add(measure)
        return f'"{measure}"'
    if metric.type == "ratio":
        num, den = (_metric_names([metric.params[k]])[0] for k in ("numerator", "denominator"))
        return (
            f"({_expression(layer, num, used, seen)}) / "
            f"nullif({_expression(layer, den, used, seen)}, 0)"
        )
    if metric.type == "derived":
        allowed = set(_metric_names(metric.params.get("metrics", [])))
        return _derived(layer, str(metric.params["expr"]), allowed, used, seen)
    raise SemanticError(f"unsupported metric type {metric.type!r} ({name})")


def _derived(
    layer: SemanticLayer, expr: str, allowed: set[str], used: set[str], seen: tuple[str, ...]
) -> str:
    """Rewrite a derived expression token by token. Only numbers, + - * / ( ) and the
    metrics listed under `metrics:` are allowed, so the YAML cannot inject arbitrary SQL."""
    out, pos = [], 0
    while pos < len(expr.rstrip()):
        match = _TOKEN.match(expr, pos)
        if not match:
            raise SemanticError(f"cannot parse derived expression {expr!r} at {pos}")
        number, ident, op = match.groups()
        if ident is not None:
            if ident not in allowed:
                raise SemanticError(f"{ident!r} is not listed in the metrics of {expr!r}")
            out.append(f"({_expression(layer, ident, used, seen)})")
        else:
            out.append(number if number is not None else op)
        pos = match.end()
    return " ".join(out)


def compile_sql(
    layer: SemanticLayer,
    metrics: Sequence[str],
    group_by: Sequence[str] = (),
    month: date | None = None,
) -> str:
    """One SQL query: measures aggregated per month (and dimensions), metrics on top."""
    for dim in group_by:
        if dim not in layer.dimensions:
            raise SemanticError(f"unknown dimension {dim!r}")
    used: set[str] = set()
    expressions = {name: _expression(layer, name, used) for name in metrics}
    keys = [f"{layer.time_dimension} as {TIME_COLUMN}", *group_by]
    aggregates = [
        f'{_AGGS[layer.measures[m].agg].format(layer.measures[m].expr)} as "{m}"'
        for m in sorted(used)
    ]
    where = f"where {layer.time_dimension} = date '{month.isoformat()}'" if month else ""
    selected = [TIME_COLUMN, *group_by]
    return (
        f"with measures as (\n  select {', '.join(keys + aggregates)}\n"
        f"  from {layer.table}\n  {where}\n  group by all\n)\n"
        f"select {', '.join(selected)}, "
        + ", ".join(f'{sql} as "{name}"' for name, sql in expressions.items())
        + f"\nfrom measures\norder by {', '.join(selected)}"
    )


def query(
    con: duckdb.DuckDBPyConnection,
    metrics: Sequence[str],
    group_by: Sequence[str] = (),
    month: date | None = None,
    layer: SemanticLayer | None = None,
) -> list[dict[str, Any]]:
    """Run a compiled metric query and return one dict per row."""
    sql = compile_sql(layer or load(), metrics, group_by, month)
    result = con.execute(sql)
    columns = [d[0] for d in result.description]
    return [dict(zip(columns, row, strict=True)) for row in result.fetchall()]
