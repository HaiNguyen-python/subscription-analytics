"""Generator mechanics and the raw load, without dbt."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import duckdb
import pytest

from subs_analytics.generator import (
    EXPORT_AT,
    Export,
    GeneratorConfig,
    SubPlan,
    add_months,
    build_subscription,
    generate,
    period_bounds,
    write_export,
)
from subs_analytics.warehouse import load_raw

SMALL = GeneratorConfig(seed=3, n_customers=60)


def test_same_seed_same_export_and_different_seed_differs() -> None:
    a, b, c = generate(SMALL), generate(SMALL), generate(GeneratorConfig(seed=4, n_customers=60))
    assert a.invoices == b.invoices and a.subscriptions == b.subscriptions
    assert a.invoices != c.invoices


@pytest.mark.parametrize(
    ("anchor", "k", "expected"),
    [
        ("2025-01-31", 1, "2025-02-28"),  # clamp to the end of February
        ("2025-01-31", 2, "2025-03-31"),  # ... and back to the anchor day, no drift
        ("2024-02-29", 12, "2025-02-28"),
        ("2025-11-15", 3, "2026-02-15"),
    ],
)
def test_add_months_clamps_like_stripe(anchor: str, k: int, expected: str) -> None:
    assert add_months(datetime.fromisoformat(anchor), k) == datetime.fromisoformat(expected)


def test_annual_period_is_twelve_months() -> None:
    start, end = period_bounds(datetime(2025, 2, 1, 9), "year", 1)
    assert (start, end) == (datetime(2026, 2, 1, 9), datetime(2027, 2, 1, 9))


def test_mid_cycle_upgrade_creates_prorated_credit_and_charge() -> None:
    out = Export()
    build_subscription(
        SubPlan(
            "sub_x",
            "cus_x",
            datetime(2025, 1, 10, 9),
            "price_sta_month_eur",
            changes=[(datetime(2025, 3, 20, 9), "price_pre_month_eur")],
        ),
        out,
    )
    proration = [line for line in out.lines if line["proration"]]
    # 21 of 31 days left in the 10 Mar - 10 Apr period: -29 x 21/31 and +79 x 21/31, in cents
    assert [line["amount"] for line in proration] == [-1965, 5352]
    assert {line["period_start"] for line in proration} == {"2025-03-20 09:00:00"}
    invoice = next(i for i in out.invoices if i["billing_reason"] == "subscription_update")
    assert invoice["total"] == 5352 - 1965


def test_coupons_follow_stripe_durations() -> None:
    out = Export()
    for n, coupon in enumerate(["FIRST50", "WELCOME20", "SCHOLAR100"]):
        build_subscription(
            SubPlan(
                f"sub_{n}",
                "cus_x",
                datetime(2025, 1, 14, 9),
                "price_sta_month_eur",
                coupon_id=coupon,
            ),
            out,
        )
    discounts = {}
    for line in out.lines:
        discounts.setdefault(line["subscription"], []).append(line["discount_amount"])
    assert discounts["sub_0"][:3] == [1450, 0, 0]  # once: first invoice only
    assert discounts["sub_1"][:5] == [580, 580, 580, 0, 0]  # repeating: three months
    assert set(discounts["sub_2"]) == {2900}  # forever 100%


def test_export_contains_nothing_at_or_after_the_export_time() -> None:
    export = generate(SMALL)
    cutoff = EXPORT_AT.strftime("%Y-%m-%d %H:%M:%S")
    assert all(i["created"] < cutoff for i in export.invoices)
    epoch = int((EXPORT_AT - datetime(1970, 1, 1)).total_seconds())
    for sub in export.subscriptions:
        times = [h["effective_at"] for h in sub["history"]]
        assert times == sorted(times) and max(times) < epoch
        assert sub["status"] == sub["history"][-1]["status"]


def test_invoice_total_is_sum_of_net_lines() -> None:
    export = generate(SMALL)
    net: dict[str, int] = {}
    for line in export.lines:
        net[line["invoice"]] = (
            net.get(line["invoice"], 0) + line["amount"] - line["discount_amount"]
        )
    assert all(net[i["id"]] == i["total"] for i in export.invoices)


def test_raw_load_is_idempotent_and_typed_even_without_refunds(tmp_path: Path) -> None:
    export = generate(GeneratorConfig(seed=1, n_customers=5, edge_cases=False))
    export.refunds = []
    counts = write_export(export, tmp_path / "raw")
    assert counts["refunds"] == 0
    db = tmp_path / "wh.duckdb"
    first = load_raw(tmp_path / "raw", db)
    assert load_raw(tmp_path / "raw", db) == first  # replaced, not appended
    with duckdb.connect(str(db)) as con:
        kinds = dict(
            con.execute(
                "select column_name, data_type from information_schema.columns "
                "where table_name = 'refunds'"
            ).fetchall()
        )
    assert kinds["amount"] == "BIGINT" and kinds["created"] == "TIMESTAMP"
    planted = json.loads((tmp_path / "raw" / "planted.json").read_text())
    assert planted["seed"] == 1


def test_missing_files_give_a_clear_error(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="subs generate"):
        load_raw(tmp_path, tmp_path / "wh.duckdb")
