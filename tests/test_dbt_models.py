"""The dbt models on the tiny build: every edge case against hand-computed MRR, the bridge,
FX, cohorts and the reconciliation. Expected numbers are worked out in docs/how-it-works.md."""

from __future__ import annotations

import shutil
from datetime import date
from pathlib import Path

import duckdb
import pytest
from conftest import Built

from subs_analytics.definitions import DBT_BUILD_DIR

MONTHS = [date(2025, m, 1) for m in range(1, 6)]
# Local-currency MRR at the end of Jan..May 2025 and the movement in each month.
EDGE_EXPECTED: dict[str, list[tuple[float, str]]] = {
    "upgrade": [(29, "new"), (29, "flat"), (79, "expansion"), (79, "flat"), (79, "flat")],
    "downgrade": [(79, "new"), (29, "contraction"), (29, "flat"), (29, "flat"), (29, "flat")],
    "annual": [
        (0, "inactive"),
        (790 / 12, "new"),
        (790 / 12, "flat"),
        (790 / 12, "flat"),
        (790 / 12, "flat"),
    ],
    "refund": [(29, "new"), (29, "flat"), (29, "flat"), (29, "flat"), (0, "churn")],
    "coupon100": [(0, "inactive")] * 5,
    "coupon_once": [(29, "new")] + [(29, "flat")] * 4,
    "coupon_repeating": [
        (23.2, "new"),
        (23.2, "flat"),
        (23.2, "flat"),
        (29, "expansion"),
        (29, "flat"),
    ],
    "usd": [(35, "new")] + [(35, "flat")] * 4,
    "trial": [(0, "inactive"), (29, "new"), (29, "flat"), (29, "flat"), (29, "flat")],
    "trial_lost": [(0, "inactive")] * 5,
    "past_due": [(29, "new"), (29, "flat"), (29, "flat"), (0, "churn"), (0, "inactive")],
    "past_due_recovered": [(29, "new")] + [(29, "flat")] * 4,
    "reactivation": [
        (29, "new"),
        (29, "flat"),
        (0, "churn"),
        (0, "inactive"),
        (79, "reactivation"),
    ],
}


@pytest.mark.parametrize("name", sorted(EDGE_EXPECTED))
def test_edge_case_mrr_and_movement(built: Built, name: str) -> None:
    rows = built.rows(
        "select month_start, mrr_local, movement_type from analytics.fct_mrr_movements "
        "where customer_id = ? and month_start between ? and ? order by month_start",
        [f"cus_edge_{name}", MONTHS[0], MONTHS[-1]],
    )
    actual = {r[0]: (r[1], r[2]) for r in rows}
    for month, (mrr, movement) in zip(MONTHS, EDGE_EXPECTED[name], strict=True):
        got_mrr, got_movement = actual.get(month, (0.0, "inactive"))
        assert got_mrr == pytest.approx(mrr, abs=1e-6), (name, month)
        assert got_movement == movement, (name, month)


def test_past_due_counts_in_mrr_while_unpaid(built: Built) -> None:
    [(status, mrr)] = built.rows(
        "select subscription_status, mrr_local from analytics.fct_mrr_movements "
        "where customer_id = 'cus_edge_past_due' and month_start = '2025-03-01'"
    )
    assert (status, mrr) == ("past_due", 29)


def test_usd_mrr_converts_with_the_monthly_rate_and_fx_is_its_own_movement(built: Built) -> None:
    rates = dict(built.rows("select month, rate_to_eur from raw.fx_rates where currency = 'usd'"))
    jan, feb = date(2025, 1, 1), date(2025, 2, 1)
    [(ending, starting, fx, expansion)] = built.rows(
        "select ending_mrr_eur, starting_mrr_eur, fx_mrr_eur, expansion_mrr_eur "
        "from analytics.fct_mrr_movements where customer_id = 'cus_edge_usd' "
        "and month_start = ?",
        [feb],
    )
    assert starting == pytest.approx(35 * rates[jan])
    assert ending == pytest.approx(35 * rates[feb])
    assert fx == pytest.approx(35 * (rates[feb] - rates[jan]))
    assert expansion == 0  # an exchange-rate move is never called expansion


def test_bridge_closes_every_month(built: Built) -> None:
    rows = built.rows(
        "select starting_mrr_eur, new_mrr_eur + expansion_mrr_eur + "
        "contraction_mrr_eur + churned_mrr_eur + reactivation_mrr_eur + fx_mrr_eur,"
        " ending_mrr_eur from analytics.mart_mrr_monthly order by month_start"
    )
    assert len(rows) >= 20  # from the first month with a subscription to Sep 2026
    previous_end = 0.0
    for start, movements, end in rows:
        assert start + movements == pytest.approx(end, abs=1e-6)
        assert start == pytest.approx(previous_end, abs=1e-6)
        previous_end = end


def test_bridge_test_catches_a_broken_bridge(built: Built, tmp_path: Path) -> None:
    """The singular dbt test is only useful if it fails on bad data: break one month."""
    broken = tmp_path / built.db_path.name  # same name: DuckDB uses it as the catalog
    shutil.copy(built.db_path, broken)
    sql = (
        DBT_BUILD_DIR / "compiled" / "subs_analytics" / "tests" / "assert_mrr_bridge_closes.sql"
    ).read_text()
    with duckdb.connect(str(broken)) as con:
        assert con.execute(sql).fetchall() == []
        con.execute(
            "update analytics.mart_mrr_monthly set new_mrr_eur = new_mrr_eur + 1 "
            "where month_start = '2025-03-01'"
        )
        failing = con.execute(sql).fetchall()
    assert len(failing) == 1


def test_billing_reconciliation_is_exact_and_explains_the_edge_cases(built: Built) -> None:
    rows = built.rows(
        "select month_start, unexplained_eur, one_time_discounts_eur, "
        "past_due_unpaid_eur, refunds_eur from analytics.mart_revenue_reconciliation"
    )
    by_month = {r[0]: r[1:] for r in rows}
    assert all(abs(unexplained) < 0.01 for unexplained, *_ in by_month.values())
    assert by_month[date(2025, 1, 1)][1] >= 14.5  # FIRST50 on cus_edge_coupon_once
    assert by_month[date(2025, 3, 1)][2] >= 29.0  # cus_edge_past_due, invoice uncollectible
    assert by_month[date(2025, 5, 1)][3] <= -29.0  # refund after churn hits May revenue


def test_cohort_is_the_first_paying_month(built: Built) -> None:
    cohorts = dict(
        built.rows(
            "select customer_id, cohort_month from analytics.dim_customer "
            "where customer_id like 'cus_edge_%'"
        )
    )
    assert cohorts["cus_edge_trial"] == date(2025, 2, 1)  # signed up in January, paid in Feb
    assert cohorts["cus_edge_reactivation"] == date(2025, 1, 1)  # keeps its first cohort
    assert cohorts["cus_edge_trial_lost"] is None
    assert cohorts["cus_edge_coupon100"] is None  # free seats are not paying customers
    [(active,)] = built.rows(
        "select active_customers from analytics.mart_cohort_retention "
        "where cohort_month = '2025-01-01' and months_since_start = 0"
    )
    [(active_m2, active_m4)] = built.rows(
        "select count(*) filter (where months_since_start = 2 and mrr_local > 0), "
        "count(*) filter (where months_since_start = 4 and mrr_local > 0) "
        "from (select *, date_diff('month', cohort_month, month_start) as months_since_start "
        "      from analytics.fct_mrr_movements where customer_id = 'cus_edge_reactivation')"
    )
    assert active >= 1 and (active_m2, active_m4) == (0, 1)


def test_no_negative_mrr_anywhere(built: Built) -> None:
    [(negative,)] = built.rows(
        "select count(*) from analytics.fct_mrr_movements where ending_mrr_eur < 0 or mrr_local < 0"
    )
    assert negative == 0
