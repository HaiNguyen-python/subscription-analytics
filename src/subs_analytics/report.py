"""Report: docs/results.md, two charts, and an HTML page per month.

Every MRR number comes from the semantic layer (`semantic.query`); cohort retention and the
reconciliation come from their marts. Nothing here re-derives a metric in Python.
"""

from __future__ import annotations

import base64
import html
import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import duckdb
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.colors import LinearSegmentedColormap  # noqa: E402
from matplotlib.patches import Rectangle  # noqa: E402
from PIL import Image  # noqa: E402

from . import semantic  # noqa: E402

BLUE, GREY, DARK, INK, INK_2, GRID = (
    "#2a78d6",
    "#b7b7b2",
    "#52514e",
    "#0b0b0b",
    "#52514e",
    "#e4e3df",
)
BRIDGE = [
    ("new_mrr", "New"),
    ("reactivation_mrr", "Reactivation"),
    ("expansion_mrr", "Expansion"),
    ("contraction_mrr", "Contraction"),
    ("churned_mrr", "Churn"),
    ("fx_mrr", "FX"),
]
KPIS = [
    "mrr",
    "arr",
    "net_new_mrr",
    "active_customers",
    "arpa",
    "logo_churn_rate",
    "revenue_churn_rate",
    "nrr",
]
TREND = [
    "mrr",
    "net_new_mrr",
    "active_customers",
    "logo_churn_rate",
    "revenue_churn_rate",
    "nrr",
    "arpa",
]
FOCUS_COHORT = date(2026, 1, 1)  # the planted weak cohort
CHECK_MONTH = 3  # months since start used to compare cohorts


@dataclass
class ReportData:
    month: date
    kpis: dict[str, Any]
    previous: dict[str, Any]
    trend: list[dict[str, Any]]
    by_interval: list[dict[str, Any]]
    cohorts: list[dict[str, Any]]
    reconciliation: list[dict[str, Any]]
    edge_cases: list[dict[str, Any]]
    labels: dict[str, str] = field(default_factory=dict)

    @property
    def change(self) -> float:
        return float(self.kpis["mrr"]) - float(self.kpis["starting_mrr"])

    @property
    def headline(self) -> str:
        pct = self.change / float(self.kpis["starting_mrr"]) if self.kpis["starting_mrr"] else 0
        verb = "grew" if self.change >= 0 else "fell"
        return (
            f"MRR {verb} {_eur(abs(self.change))} ({pct:+.1%}) in "
            f"{self.month:%B %Y} to {_eur(self.kpis['mrr'])}"
        )


def _rows(
    con: duckdb.DuckDBPyConnection, sql: str, params: list[Any] | None = None
) -> list[dict[str, Any]]:
    result = con.execute(sql, params or [])
    cols = [d[0] for d in result.description]
    return [dict(zip(cols, r, strict=True)) for r in result.fetchall()]


def collect(con: duckdb.DuckDBPyConnection, month: date) -> ReportData:
    layer = semantic.load()
    bridge = ["starting_mrr", *[m for m, _ in BRIDGE]]
    wanted = list(dict.fromkeys([*KPIS, *bridge, "churned_customers", "starting_customers"]))
    current = semantic.query(con, wanted, month=month, layer=layer)
    if not current:
        raise ValueError(f"no data for {month}; run the pipeline first")
    trend = semantic.query(con, TREND, layer=layer)
    previous = [r for r in trend if r[semantic.TIME_COLUMN] < month]
    return ReportData(
        month=month,
        kpis=current[0],
        previous=previous[-1] if previous else {},
        trend=[r for r in trend if r[semantic.TIME_COLUMN] <= month],
        by_interval=semantic.query(
            con,
            ["new_mrr", "churned_mrr", "churned_customers"],
            group_by=["plan_interval"],
            month=month,
            layer=layer,
        ),
        cohorts=_rows(
            con,
            "select * from analytics.mart_cohort_retention where cohort_month <= ? order by 1, 2",
            [month],
        ),
        reconciliation=_rows(
            con,
            "select * from analytics.mart_revenue_reconciliation where month_start <= ? order by 1",
            [month],
        ),
        edge_cases=_rows(
            con,
            """
            select customer_id, month_start, movement_type, mrr_local, ending_mrr_eur
            from analytics.fct_mrr_movements
            where customer_id like 'cus_edge_%'
              and month_start between date '2025-01-01' and date '2025-05-01'
            order by customer_id, month_start""",
        ),
        labels={m: layer.label(m) for m in layer.metrics},
    )


def _eur(value: Any) -> str:
    v = float(value)
    return f"-€{abs(v):,.0f}" if v < 0 else f"€{v:,.0f}"


def _signed(value: Any) -> str:
    v = float(value)
    return f"+€{v:,.0f}" if v >= 0 else f"-€{abs(v):,.0f}"


def _fmt(metric: str, value: Any) -> str:
    if value is None:
        return "n/a"
    if metric.endswith("_rate") or metric == "nrr":
        return f"{float(value):.1%}"
    if metric in ("active_customers", "churned_customers", "starting_customers"):
        return f"{int(value):,}"
    if metric in ("net_new_mrr", "fx_mrr"):
        return _signed(value)
    if metric == "arpa":
        return f"€{float(value):,.2f}"
    return _eur(value)


def cohort_comparison(data: ReportData, k: int = CHECK_MONTH) -> tuple[float | None, float]:
    """(focus cohort logo retention at month k, average of all other cohorts at month k)."""
    at_k = [r for r in data.cohorts if r["months_since_start"] == k]
    focus = next(
        (float(r["logo_retention"]) for r in at_k if r["cohort_month"] == FOCUS_COHORT), None
    )
    others = [float(r["logo_retention"]) for r in at_k if r["cohort_month"] != FOCUS_COHORT]
    return focus, sum(others) / len(others) if others else float("nan")


def _style(ax: Any) -> None:
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(GRID)
    ax.tick_params(colors=INK_2, labelsize=9)
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)


def _titles(fig: Any, title: str, subtitle: str) -> None:
    fig.text(0.03, 0.955, title, fontsize=13.5, weight="bold", color=INK, ha="left")
    fig.text(0.03, 0.905, subtitle, fontsize=9.5, color=INK_2, ha="left")


def _save(fig: Any, path: Path) -> Path:
    """Save as a 128-colour PNG: same look, a third of the size (the repo is uploaded by hand)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, facecolor="white")
    plt.close(fig)
    with Image.open(path) as img:
        img.convert("RGB").quantize(colors=128).save(path, optimize=True)
    return path


def bridge_chart(data: ReportData, path: Path) -> Path:
    """Waterfall from starting to ending MRR. Increases blue, decreases grey."""
    k = data.kpis
    steps = [(label, float(k[m] or 0)) for m, label in BRIDGE]
    fig, ax = plt.subplots(figsize=(10, 5.4), dpi=150)
    fig.subplots_adjust(left=0.08, right=0.98, top=0.82, bottom=0.1)
    labels = ["Start", *[s[0] for s in steps], "End"]
    level = float(k["starting_mrr"])
    span = max(float(k["mrr"]), level)
    ax.bar(0, level, color=DARK, width=0.62)
    ax.text(0, level + span * 0.012, _eur(level), ha="center", va="bottom", fontsize=9, color=INK)
    for i, (_, value) in enumerate(steps, start=1):
        bottom = level if value >= 0 else level + value
        ax.bar(i, abs(value), bottom=bottom, color=BLUE if value >= 0 else GREY, width=0.62)
        ax.text(
            i,
            max(level, level + value) + span * 0.012,
            _signed(value),
            ha="center",
            va="bottom",
            fontsize=9,
            color=INK,
        )
        ax.plot([i - 1 + 0.31, i - 0.31], [level, level], color=INK_2, linewidth=0.7)
        level += value
    ax.plot([len(steps) + 0.31, len(steps) + 0.69], [level, level], color=INK_2, linewidth=0.7)
    ax.bar(len(labels) - 1, float(k["mrr"]), color=DARK, width=0.62)
    ax.text(
        len(labels) - 1,
        float(k["mrr"]) + span * 0.012,
        _eur(k["mrr"]),
        ha="center",
        va="bottom",
        fontsize=9,
        color=INK,
    )
    lows = [float(k["starting_mrr"])]
    for _, value in steps:
        lows.append(lows[-1] + value)
    floor = min(lows) * 0.9
    ax.set_ylim(floor, span * 1.06)
    ax.set_xticks(range(len(labels)), labels)
    ax.yaxis.set_major_formatter(lambda v, _: f"€{v / 1000:,.0f}k")
    ax.set_ylabel("MRR (EUR)", color=INK_2, fontsize=9)
    _style(ax)
    lost = -(float(k["churned_mrr"] or 0) + float(k["contraction_mrr"] or 0))
    _titles(
        fig,
        data.headline,
        f"New customers added {_eur(k['new_mrr'])}; churn and downgrades took "
        f"{_eur(lost)}. The y-axis starts at {_eur(floor)}, not zero. Synthetic data.",
    )
    return _save(fig, path)


def cohort_chart(data: ReportData, path: Path, max_months: int = 12) -> Path:
    """Heatmap of logo retention: cohorts down, months since first payment across."""
    cohorts = sorted({r["cohort_month"] for r in data.cohorts})
    grid = [[float("nan")] * (max_months + 1) for _ in cohorts]
    for r in data.cohorts:
        if r["months_since_start"] <= max_months:
            grid[cohorts.index(r["cohort_month"])][r["months_since_start"]] = float(
                r["logo_retention"]
            )
    fig, ax = plt.subplots(figsize=(10, 7.2), dpi=150)
    fig.subplots_adjust(left=0.1, right=0.98, top=0.84, bottom=0.03)
    cmap = LinearSegmentedColormap.from_list("blues", ["#f4f7fc", BLUE], N=32).with_extremes(
        bad="white"
    )
    ax.imshow(grid, cmap=cmap, vmin=0.3, vmax=1.0, aspect="auto", interpolation="nearest")
    for i, row in enumerate(grid):
        for j, value in enumerate(row):
            if value == value:  # not NaN
                ax.text(
                    j,
                    i,
                    f"{value:.0%}",
                    ha="center",
                    va="center",
                    fontsize=6.5,
                    color="white" if value > 0.78 else INK,
                )
    ax.set_xticks(range(max_months + 1))
    ax.set_yticks(range(len(cohorts)), [f"{c:%b %Y}" for c in cohorts])
    ax.tick_params(colors=INK_2, labelsize=8, length=0)
    ax.xaxis.tick_top()
    ax.set_xlabel("Months since first payment", color=INK_2, fontsize=9)
    ax.xaxis.set_label_position("top")
    for side in ax.spines.values():
        side.set_visible(False)
    if FOCUS_COHORT in cohorts:
        i = cohorts.index(FOCUS_COHORT)
        ax.add_patch(
            Rectangle((-0.5, i - 0.5), max_months + 1, 1, fill=False, edgecolor=INK, linewidth=1.4)
        )
    focus, others = cohort_comparison(data)
    title = (
        f"January 2026 signups: {focus:.0%} still paying after {CHECK_MONTH} months, "
        f"vs {others:.0%} for other cohorts"
        if focus is not None
        else "Logo retention by cohort"
    )
    _titles(
        fig,
        title,
        "Logo retention by cohort (first paying month). The January 2026 "
        "churn was planted in the generator. Synthetic data.",
    )
    return _save(fig, path)


def _table(header: list[str], rows: list[list[str]]) -> list[str]:
    return [
        "| " + " | ".join(header) + " |",
        "|" + " --- |" * len(header),
        *["| " + " | ".join(r) + " |" for r in rows],
    ]


def _why(data: ReportData) -> list[str]:
    """Plain-language answer to 'why did MRR change this month?', from the bridge."""
    k = data.kpis
    moves = sorted(((float(k[m] or 0), label) for m, label in BRIDGE), key=lambda x: -abs(x[0]))
    churn = {r["plan_interval"]: r for r in data.by_interval if r["plan_interval"]}
    lines = [
        "- Biggest movements: " + ", ".join(f"{label} {_signed(v)}" for v, label in moves[:3]) + "."
    ]
    if churn:
        parts = [
            f"{int(r['churned_customers'])} {name}ly ({_signed(r['churned_mrr'])})"
            for name, r in sorted(churn.items())
        ]
        lines.append(f"- Churned customers by plan interval: {', '.join(parts)}.")
    prev = data.previous
    if prev:
        lines.append(
            f"- Net new MRR {_signed(k['net_new_mrr'])} vs {_signed(prev['net_new_mrr'])}"
            f" the month before; logo churn {_fmt('logo_churn_rate', k['logo_churn_rate'])}"
            f" vs {_fmt('logo_churn_rate', prev['logo_churn_rate'])}."
        )
    return lines


def _revenue_gap(row: dict[str, Any]) -> str:
    """Calendar-normalised recurring revenue vs average MRR, as a signed percentage."""
    average = float(row["average_mrr_eur"])
    if not average:
        return "n/a"
    return f"{float(row['recurring_revenue_normalised_eur']) / average - 1:+.1%}"


def results_markdown(data: ReportData, label: str, dbt_summary: str = "") -> str:
    k = data.kpis
    out = [
        f"# Results ({label})",
        "",
        "Generated by `subs report` from the warehouse built by the Dagster assets. Every "
        "MRR figure is compiled from the semantic layer (`dbt/models/semantic/semantic.yml`)."
        " All data is synthetic; effects were planted on purpose (see "
        "[how-it-works.md](how-it-works.md#2-synthetic-data-and-planted-effects)).",
        "",
        f"## {data.month:%B %Y} at a glance",
        "",
        f"**{data.headline}.**",
        "",
    ]
    out += _table(["Metric", "Value"], [[data.labels[m], _fmt(m, k[m])] for m in KPIS])
    out += ["", "## MRR bridge", ""]
    rows = [["Starting MRR", _eur(k["starting_mrr"])]]
    rows += [[label, _signed(k[m])] for m, label in BRIDGE]
    rows += [["Ending MRR", _eur(k["mrr"])], ["Net new MRR (excl. FX)", _signed(k["net_new_mrr"])]]
    out += _table(["Step", "EUR"], rows)
    out += ["", "![MRR bridge](img/mrr_bridge.png)", "", "## Why did MRR change?", "", *_why(data)]
    out += ["", "## Monthly trend", ""]
    out += _table(
        ["Month", "MRR", "Net new", "Paying", "Logo churn", "Revenue churn", "NRR 12m", "ARPA"],
        [
            [
                f"{r[semantic.TIME_COLUMN]:%Y-%m}",
                _eur(r["mrr"]),
                _signed(r["net_new_mrr"]),
                _fmt("active_customers", r["active_customers"]),
                _fmt("logo_churn_rate", r["logo_churn_rate"]),
                _fmt("revenue_churn_rate", r["revenue_churn_rate"]),
                _fmt("nrr", r["nrr"]),
                _fmt("arpa", r["arpa"]),
            ]
            for r in data.trend
        ],
    )
    focus, others = cohort_comparison(data)
    out += [
        "",
        "## Cohort retention",
        "",
        f"Logo retention {CHECK_MONTH} months after the first payment: January 2026 cohort "
        f"**{_fmt('nrr', focus)}**, average of the other cohorts **{others:.1%}**. "
        "Revenue retention per cohort is in `analytics.mart_cohort_retention`.",
        "",
        "![Cohort retention](img/cohort_retention.png)",
        "",
        "## Reconciliation: MRR vs Stripe invoices",
        "",
        "Billed MRR is rebuilt from the invoice lines covering each month end. The difference"
        " to MRR must be fully explained (unexplained = 0, a failing dbt test otherwise). "
        "Recognised revenue is a sanity check within 5% (calendar-normalised).",
        "",
    ]
    out += _table(
        [
            "Month",
            "MRR",
            "Billed MRR",
            "Once coupons",
            "Past due unpaid",
            "Unexplained",
            "Recognised revenue",
            "Refunds",
            "Revenue vs avg MRR",
        ],
        [
            [
                f"{r['month_start']:%Y-%m}",
                _eur(r["mrr_eur"]),
                _eur(r["billed_mrr_eur"]),
                _eur(r["one_time_discounts_eur"]),
                _eur(r["past_due_unpaid_eur"]),
                f"{float(r['unexplained_eur']):.2f}",
                _eur(r["recognised_revenue_eur"]),
                _eur(r["refunds_eur"]),
                _revenue_gap(r),
            ]
            for r in data.reconciliation[-6:]
        ],
    )
    out += [
        "",
        "(Last six months; the full table is `analytics.mart_revenue_reconciliation`.)",
        "",
        "## Edge cases (January to May 2025, local currency MRR)",
        "",
    ]
    by_customer: dict[str, dict[str, str]] = {}
    for r in data.edge_cases:
        cell = f"{float(r['mrr_local']):.2f}"
        if r["movement_type"] not in ("flat", "inactive"):
            cell += f" ({r['movement_type']})"
        by_customer.setdefault(r["customer_id"], {})[f"{r['month_start']:%b}"] = cell
    months = ["Jan", "Feb", "Mar", "Apr", "May"]
    out += _table(
        ["Customer", *months],
        [
            [c.removeprefix("cus_edge_"), *[v.get(m, "-") for m in months]]
            for c, v in sorted(by_customer.items())
        ],
    )
    if dbt_summary:
        out += ["", "## Data tests", "", dbt_summary]
    return "\n".join(out) + "\n"


def data_label(raw_dir: Path) -> str:
    planted = raw_dir / "planted.json"
    if not planted.exists():
        return "data from an export"
    info = json.loads(planted.read_text())
    return f"synthetic data, seed {info['seed']}, {info['n_customers']:,} customers"


def dbt_summary(target_dir: Path) -> str:
    """Count model and test results from dbt's run_results.json (written by `dbt build`)."""
    path = target_dir / "run_results.json"
    if not path.exists():
        return ""
    results = json.loads(path.read_text())["results"]
    models = [r for r in results if r["unique_id"].startswith("model.")]
    tests = [r for r in results if r["unique_id"].startswith("test.")]
    passed = sum(r["status"] == "pass" for r in tests)
    warned = sum(r["status"] == "warn" for r in tests)
    failed = sum(r["status"] in ("fail", "error") for r in tests)
    return (
        f"Last `dbt build`: {sum(r['status'] == 'success' for r in models)} of "
        f"{len(models)} models built, {passed} tests passed, {warned} warned, "
        f"{failed} failed."
    )


def write_docs(data: ReportData, docs_dir: Path, label: str, summary: str = "") -> list[Path]:
    paths = [
        bridge_chart(data, docs_dir / "img" / "mrr_bridge.png"),
        cohort_chart(data, docs_dir / "img" / "cohort_retention.png"),
    ]
    results = docs_dir / "results.md"
    results.write_text(results_markdown(data, label, summary))
    return [results, *paths]


def write_html(data: ReportData, out_dir: Path) -> Path:
    """A single self-contained HTML page (charts embedded) for sharing outside the repo."""
    out_dir.mkdir(parents=True, exist_ok=True)
    charts = [
        bridge_chart(data, out_dir / "mrr_bridge.png"),
        cohort_chart(data, out_dir / "cohort_retention.png"),
    ]
    images = "".join(
        f'<img alt="{p.stem}" src="data:image/png;base64,'
        f'{base64.b64encode(p.read_bytes()).decode()}">'
        for p in charts
    )
    rows = "".join(
        f"<tr><td>{html.escape(data.labels[m])}</td><td>{_fmt(m, data.kpis[m])}</td></tr>"
        for m in KPIS
    )
    page = (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>MRR report {data.month:%Y-%m}</title><style>"
        "body{font-family:system-ui,sans-serif;max-width:960px;margin:2rem auto;padding:0 1rem;"
        "color:#0b0b0b;background:#fff}img{max-width:100%;margin:1rem 0}"
        "td{padding:.25rem 1rem .25rem 0;border-bottom:1px solid #e4e3df}"
        "</style></head><body>"
        f"<h1>{html.escape(data.headline)}</h1><p>Synthetic data. Metrics compiled from the "
        f"semantic layer.</p><table>{rows}</table>{images}</body></html>"
    )
    path = out_dir / "report.html"
    path.write_text(page)
    return path
