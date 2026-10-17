"""Seeded synthetic Stripe-style exports for an online language school.

The school sells two tiers (Standard group classes, Premium 1:1 tutoring), each monthly or
annual, in EUR and USD. The generator works in two steps:

1. Draw a `SubPlan` per subscription: the business decisions (plan, trial, coupon, when the
   customer upgrades, cancels or fails a payment). Random customers get random plans; the
   documented edge cases in `EDGE_CASES` are written by hand.
2. `build_subscription` turns a plan into Stripe-shaped records with the same mechanics
   Stripe uses: billing periods from an anchor date, proration lines on mid-cycle plan
   changes, cancel at period end, past_due and recovery, coupons by duration.

Keeping decisions and mechanics apart means the hand-written edge cases go through exactly
the same code as the 2,000 random customers, so a test on an edge case tests the generator too.

Effects planted on purpose (so the analysis has something real to find):
- the January 2026 "New Year resolution" cohort churns 2.5x faster in its first four months;
- June and July have higher churn and fewer signups (summer break);
- September 2026 has a back-to-school campaign with 1.6x the usual signups;
- annual plans sold in September 2025 come up for renewal in September 2026.
"""

from __future__ import annotations

import calendar
import csv
import json
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

START = datetime(2024, 10, 1)
MONTHS = 24
EXPORT_AT = datetime(2026, 10, 1)  # first instant after the data: nothing at or after it exists
PAST_DUE_GRACE_DAYS = 21  # Stripe dunning setting: cancel 21 days after the first failed payment


@dataclass(frozen=True)
class Price:
    id: str
    product: str
    tier: str
    interval: str  # "month" or "year"
    currency: str  # "eur" or "usd"
    unit_amount: int  # minor units (cents), like Stripe


@dataclass(frozen=True)
class Coupon:
    id: str
    percent_off: int
    duration: str  # "once", "repeating" or "forever"
    duration_in_months: int | None = None


PRODUCTS = {
    "standard": ("prod_standard", "Standard: group classes"),
    "premium": ("prod_premium", "Premium: 1:1 tutoring"),
}
_LIST_PRICES = {  # (tier, interval, currency) -> unit amount in cents
    ("standard", "month", "eur"): 2900,
    ("standard", "year", "eur"): 29000,
    ("premium", "month", "eur"): 7900,
    ("premium", "year", "eur"): 79000,
    ("standard", "month", "usd"): 3500,
    ("standard", "year", "usd"): 35000,
    ("premium", "month", "usd"): 9500,
    ("premium", "year", "usd"): 95000,
}
PRICES: dict[str, Price] = {
    f"price_{t[:3]}_{i}_{c}": Price(f"price_{t[:3]}_{i}_{c}", PRODUCTS[t][0], t, i, c, amount)
    for (t, i, c), amount in _LIST_PRICES.items()
}
COUPONS: dict[str, Coupon] = {
    "WELCOME20": Coupon("WELCOME20", 20, "repeating", 3),
    "SCHOLAR100": Coupon("SCHOLAR100", 100, "forever"),  # teacher scholarship seats
    "FIRST50": Coupon("FIRST50", 50, "once"),
}


def price_id(tier: str, interval: str, currency: str) -> str:
    return f"price_{tier[:3]}_{interval}_{currency}"


def add_months(anchor: datetime, k: int) -> datetime:
    """Anchor date plus k months, clamping the day like Stripe (Jan 31 -> Feb 28 -> Mar 31)."""
    m = anchor.month - 1 + k
    year, month = anchor.year + m // 12, m % 12 + 1
    return anchor.replace(year=year, month=month, day=min(anchor.day, _days_in(year, month)))


def _days_in(year: int, month: int) -> int:
    return calendar.monthrange(year, month)[1]


def period_bounds(anchor: datetime, interval: str, k: int) -> tuple[datetime, datetime]:
    """Start and end of the k-th billing period. Always computed from the anchor, not chained,
    so a period never drifts after a short month."""
    step = 12 if interval == "year" else 1
    return add_months(anchor, k * step), add_months(anchor, (k + 1) * step)


@dataclass
class SubPlan:
    """Business decisions for one subscription. All times are naive UTC."""

    sub_id: str
    customer_id: str
    start: datetime
    price_id: str
    trial_days: int = 0
    trial_converts: bool = True
    coupon_id: str | None = None
    changes: list[tuple[datetime, str]] = field(default_factory=list)  # (when, new price)
    cancel_at: datetime | None = None  # cancel_at_period_end set at this time
    payment_failure: tuple[int, int | None] | None = None  # (period, days to recover | None)
    refund_days_after_end: int | None = None


@dataclass
class Export:
    """Stripe-style records, one list per export file."""

    customers: list[dict[str, Any]] = field(default_factory=list)
    subscriptions: list[dict[str, Any]] = field(default_factory=list)
    invoices: list[dict[str, Any]] = field(default_factory=list)
    lines: list[dict[str, Any]] = field(default_factory=list)
    refunds: list[dict[str, Any]] = field(default_factory=list)
    discounts: list[dict[str, Any]] = field(default_factory=list)
    fx_rates: list[dict[str, Any]] = field(default_factory=list)
    planted: dict[str, Any] = field(default_factory=dict)
    _seq: dict[str, int] = field(default_factory=dict)

    def next_id(self, prefix: str) -> str:
        self._seq[prefix] = self._seq.get(prefix, 0) + 1
        return f"{prefix}_{self._seq[prefix]:06d}"


def _unix(ts: datetime | None) -> int | None:
    return None if ts is None else int((ts - datetime(1970, 1, 1)).total_seconds())


def _iso(ts: datetime | None) -> str:
    return "" if ts is None else ts.strftime("%Y-%m-%d %H:%M:%S")


def _invoice_percent(
    coupon: Coupon | None, discount_end: datetime | None, at: datetime, first_paid: bool
) -> int:
    """Percent off for an invoice created at `at`, following Stripe coupon durations."""
    if coupon is None:
        return 0
    if coupon.duration == "once":
        return coupon.percent_off if first_paid else 0
    if coupon.duration == "repeating" and discount_end is not None and at >= discount_end:
        return 0
    return coupon.percent_off


def build_subscription(plan: SubPlan, out: Export, export_at: datetime = EXPORT_AT) -> None:
    """Emit the subscription, its status history, invoices, lines, discount and refund.

    Anything that would happen at or after `export_at` is cut off, exactly like a real export
    taken at that moment: a scheduled cancellation stays scheduled, an unpaid invoice stays open.
    """
    price = PRICES[plan.price_id]
    coupon = COUPONS[plan.coupon_id] if plan.coupon_id else None
    history: list[dict[str, Any]] = []
    ended_at: datetime | None = None
    canceled_at: datetime | None = None

    def event(ts: datetime, status: str, pid: str, cancel_at_period_end: bool = False) -> None:
        if ts < export_at:
            history.append(
                {
                    "effective_at": _unix(ts),
                    "status": status,
                    "price": pid,
                    "cancel_at_period_end": cancel_at_period_end,
                }
            )

    def invoice(
        created: datetime,
        reason: str,
        status: str,
        lines: list[tuple[Price, datetime, datetime, int, int, bool]],
    ) -> str | None:
        if created >= export_at:
            return None
        inv_id = out.next_id("in")
        total = sum(amount - disc for _, _, _, amount, disc, _ in lines)
        for p, ps, pe, amount, disc, proration in lines:
            name = f"{PRODUCTS[p.tier][1]} ({p.interval}ly)"
            if proration:
                name = ("Unused time on " if amount < 0 else "Remaining time on ") + name
            out.lines.append(
                {
                    "id": out.next_id("il"),
                    "invoice": inv_id,
                    "subscription": plan.sub_id,
                    "price": p.id,
                    "amount": amount,
                    "discount_amount": disc,
                    "proration": proration,
                    "period_start": _iso(ps),
                    "period_end": _iso(pe),
                    "description": name,
                }
            )
        out.invoices.append(
            {
                "id": inv_id,
                "customer": plan.customer_id,
                "subscription": plan.sub_id,
                "created": _iso(created),
                "status": status,
                "currency": price.currency,
                "billing_reason": reason,
                "total": total,
                "amount_paid": total if status == "paid" else 0,
            }
        )
        return inv_id

    # Coupons attach when the paid plan starts (after a trial), so a repeating coupon ends
    # exactly on a billing period boundary and every invoice is either fully discounted or not.
    paid_start = plan.start + timedelta(days=plan.trial_days)
    discount_end = None
    if coupon is not None:
        if coupon.duration == "repeating" and coupon.duration_in_months:
            discount_end = add_months(paid_start, coupon.duration_in_months)
        out.discounts.append(
            {
                "id": out.next_id("di"),
                "subscription": plan.sub_id,
                "customer": plan.customer_id,
                "start": _unix(paid_start),
                "end": _unix(discount_end),
                "coupon": {
                    "id": coupon.id,
                    "percent_off": coupon.percent_off,
                    "duration": coupon.duration,
                    "duration_in_months": coupon.duration_in_months,
                },
            }
        )

    anchor = plan.start
    if plan.trial_days:
        trial_end = plan.start + timedelta(days=plan.trial_days)
        event(plan.start, "trialing", price.id)
        invoice(
            plan.start, "subscription_create", "paid", [(price, plan.start, trial_end, 0, 0, False)]
        )
        anchor = trial_end
        if not plan.trial_converts:
            event(trial_end, "canceled", price.id)
            ended_at = canceled_at = trial_end if trial_end < export_at else None
    if plan.trial_converts:
        event(anchor, "active", price.id)

    current = price
    last_invoice: str | None = None
    k = 0
    while plan.trial_converts and ended_at is None:
        ps, pe = period_bounds(anchor, current.interval, k)
        if ps >= export_at:
            break
        pct = _invoice_percent(coupon, discount_end, ps, first_paid=k == 0)
        status = "paid"
        if plan.payment_failure and plan.payment_failure[0] == k:
            event(ps, "past_due", current.id)
            recover_days = plan.payment_failure[1]
            if recover_days is not None:
                recovered = ps + timedelta(days=recover_days)
                event(recovered, "active", current.id)
                status = "paid" if recovered < export_at else "open"
            else:
                cancel = ps + timedelta(days=PAST_DUE_GRACE_DAYS)
                event(cancel, "canceled", current.id)
                status = "uncollectible" if cancel < export_at else "open"
                if cancel < export_at:
                    ended_at = canceled_at = cancel
        reason = "subscription_create" if k == 0 and not plan.trial_days else "subscription_cycle"
        disc = round(current.unit_amount * pct / 100)
        last_invoice = (
            invoice(ps, reason, status, [(current, ps, pe, current.unit_amount, disc, False)])
            or last_invoice
        )
        if ended_at is not None:
            break
        change = next(((when, new) for when, new in plan.changes if ps <= when < pe), None)
        if change and change[0] < export_at:
            when, new_price = change[0], PRICES[change[1]]
            remaining = (pe - when).total_seconds() / (pe - ps).total_seconds()
            pct_now = _invoice_percent(coupon, discount_end, when, first_paid=False)
            credit, charge = (
                -round(current.unit_amount * remaining),
                round(new_price.unit_amount * remaining),
            )
            invoice(
                when,
                "subscription_update",
                "paid",
                [
                    (current, when, pe, credit, round(credit * pct_now / 100), True),
                    (new_price, when, pe, charge, round(charge * pct_now / 100), True),
                ],
            )
            event(when, "active", new_price.id)
            current = new_price
        if plan.cancel_at is not None and ps <= plan.cancel_at < pe:
            event(plan.cancel_at, "active", current.id, cancel_at_period_end=True)
            event(pe, "canceled", current.id)
            if plan.cancel_at < export_at:
                canceled_at = plan.cancel_at
            if pe < export_at:
                ended_at = pe
            break
        k += 1

    if ended_at is not None and plan.refund_days_after_end is not None and last_invoice:
        refunded_at = ended_at + timedelta(days=plan.refund_days_after_end)
        last = next(i for i in out.invoices if i["id"] == last_invoice)
        if refunded_at < export_at and last["amount_paid"] > 0:
            out.refunds.append(
                {
                    "id": out.next_id("re"),
                    "invoice": last_invoice,
                    "amount": last["amount_paid"],
                    "currency": last["currency"],
                    "created": _iso(refunded_at),
                    "reason": "requested_by_customer",
                }
            )

    last_event = history[-1]
    out.subscriptions.append(
        {
            "id": plan.sub_id,
            "customer": plan.customer_id,
            "created": _unix(plan.start),
            "status": last_event["status"],
            "trial_start": _unix(plan.start) if plan.trial_days else None,
            "trial_end": _unix(plan.start + timedelta(days=plan.trial_days))
            if plan.trial_days
            else None,
            "cancel_at_period_end": last_event["cancel_at_period_end"],
            "canceled_at": _unix(canceled_at),
            "ended_at": _unix(ended_at),
            "price": last_event["price"],
            "history": history,
        }
    )


@dataclass(frozen=True)
class EdgeCase:
    customer_id: str
    currency: str
    what: str
    plans: tuple[SubPlan, ...]


def _t(day: str) -> datetime:
    return datetime.fromisoformat(day + " 09:00:00")


def _edge(name: str, what: str, *plans: SubPlan, currency: str = "eur") -> EdgeCase:
    return EdgeCase(f"cus_edge_{name}", currency, what, plans)


def _plan(name: str, start: str, pid: str, n: int = 1, **kw: Any) -> SubPlan:
    return SubPlan(f"sub_edge_{name}_{n}", f"cus_edge_{name}", _t(start), pid, **kw)


STD_M, PRE_M, PRE_Y = "price_sta_month_eur", "price_pre_month_eur", "price_pre_year_eur"

# Hand-written customers with known MRR. The expected values are computed by hand in
# docs/how-it-works.md and asserted in tests/test_dbt_models.py.
EDGE_CASES: tuple[EdgeCase, ...] = (
    _edge(
        "upgrade",
        "Standard monthly from 10 Jan 2025, upgrades to Premium on 20 Mar: "
        "proration credit and charge for the 21 of 31 days left",
        _plan("upgrade", "2025-01-10", STD_M, changes=[(_t("2025-03-20"), PRE_M)]),
    ),
    _edge(
        "downgrade",
        "Premium monthly from 20 Jan 2025, downgrades to Standard on 25 Feb",
        _plan("downgrade", "2025-01-20", PRE_M, changes=[(_t("2025-02-25"), STD_M)]),
    ),
    _edge(
        "annual",
        "Premium annual from 1 Feb 2025: MRR is 790 / 12",
        _plan("annual", "2025-02-01", PRE_Y),
    ),
    _edge(
        "refund",
        "Cancels at period end (scheduled 20 Apr, ends 5 May 2025), refunded "
        "10 days after the end: MRR churns in May, the refund only touches revenue",
        _plan("refund", "2025-01-05", STD_M, cancel_at=_t("2025-04-20"), refund_days_after_end=10),
    ),
    _edge(
        "coupon100",
        "Premium monthly with a 100% forever scholarship coupon: MRR 0",
        _plan("coupon100", "2025-01-15", PRE_M, coupon_id="SCHOLAR100"),
    ),
    _edge(
        "coupon_once",
        "50% off the first invoice only: MRR stays at list price",
        _plan("coupon_once", "2025-01-14", STD_M, coupon_id="FIRST50"),
    ),
    _edge(
        "coupon_repeating",
        "20% off for 3 months: MRR 23.20 until April, then 29.00 (the coupon ending is expansion)",
        _plan("coupon_repeating", "2025-01-14", STD_M, coupon_id="WELCOME20"),
    ),
    _edge(
        "usd",
        "Standard monthly in USD (35.00): MRR in EUR follows the monthly FX rate",
        _plan("usd", "2025-01-03", "price_sta_month_usd"),
        currency="usd",
    ),
    _edge(
        "trial",
        "14-day trial from 25 Jan 2025, converts on 8 Feb: no MRR in January",
        _plan("trial", "2025-01-25", STD_M, trial_days=14),
    ),
    _edge(
        "trial_lost",
        "Trial from 20 Jan 2025 that never converts: never in MRR",
        _plan("trial_lost", "2025-01-20", STD_M, trial_days=14, trial_converts=False),
    ),
    _edge(
        "past_due",
        "Payment fails on 12 Mar 2025 renewal, canceled 21 days later: "
        "past_due still counts in March MRR, churn in April",
        _plan("past_due", "2025-01-12", STD_M, payment_failure=(2, None)),
    ),
    _edge(
        "past_due_recovered",
        "Payment fails on 28 Feb 2025, paid 5 days later: "
        "past_due over the month end, still in February MRR",
        _plan("past_due_recovered", "2025-01-28", STD_M, payment_failure=(1, 5)),
    ),
    _edge(
        "reactivation",
        "Churns at period end on 8 Mar 2025, comes back on Premium on 16 May",
        _plan("reactivation", "2025-01-08", STD_M, cancel_at=_t("2025-02-20")),
        _plan("reactivation", "2025-05-16", PRE_M, n=2),
    ),
)


@dataclass(frozen=True)
class GeneratorConfig:
    seed: int = 42
    n_customers: int = 2000
    edge_cases: bool = True


def _signup_weight(month: datetime) -> float:
    """Growth plus seasonality of a language school, with the planted campaigns."""
    weight = 1.0 + 0.03 * ((month.year - START.year) * 12 + month.month - START.month)
    weight *= {1: 1.6, 6: 0.6, 7: 0.6, 8: 1.3, 9: 1.4}.get(month.month, 1.0)
    if (month.year, month.month) == (2026, 9):
        weight *= 1.6 / 1.4  # back-to-school campaign: 1.6x instead of the usual 1.4x
    return weight


def _monthly_hazard(k: int, tier: str, period_end: datetime, cohort: tuple[int, int]) -> float:
    """Probability that a monthly subscriber cancels during billing period k."""
    hazard = 0.075 if k < 3 else 0.045 if k < 12 else 0.03
    if tier == "premium":
        hazard *= 0.8
    hazard *= {6: 1.7, 7: 1.7, 12: 1.2}.get(period_end.month, 1.0)
    if cohort == (2026, 1) and k < 4:
        hazard *= 2.5  # planted: New Year resolution signups give up early
    return hazard


def _random_time_in(rng: random.Random, start: datetime, end: datetime) -> datetime:
    """A random minute between two times, kept away from the edges by 10%."""
    span = (end - start).total_seconds()
    return start + timedelta(minutes=int(span * rng.uniform(0.1, 0.9) / 60))


def draw_subscription(
    rng: random.Random,
    sub_id: str,
    customer_id: str,
    start: datetime,
    pid: str,
    trial_days: int,
    coupon_id: str | None,
) -> tuple[SubPlan, datetime | None]:
    """Random decisions for one subscription. Returns the plan and when it ends (or None)."""
    plan = SubPlan(sub_id, customer_id, start, pid, trial_days=trial_days, coupon_id=coupon_id)
    if trial_days:
        plan.trial_converts = rng.random() < 0.62
        if not plan.trial_converts:
            return plan, start + timedelta(days=trial_days)
    anchor = start + timedelta(days=trial_days)
    current = PRICES[pid]
    cohort = (anchor.year, anchor.month)
    k = 0
    while True:
        ps, pe = period_bounds(anchor, current.interval, k)
        if ps >= EXPORT_AT:
            return plan, None
        if (
            k >= 1
            and current.interval == "month"
            and not plan.payment_failure
            and rng.random() < 0.03
        ):
            # One failed payment per subscription at most; no other decisions in that period,
            # so the status history stays in time order.
            recover = rng.randint(2, 10) if rng.random() < 0.55 else None
            plan.payment_failure = (k, recover)
            if recover is None:
                return plan, ps + timedelta(days=PAST_DUE_GRACE_DAYS)
            k += 1
            continue
        if current.interval == "month":
            p_change = 0.02 if current.tier == "standard" else 0.012
            if rng.random() < p_change:
                other = "premium" if current.tier == "standard" else "standard"
                new_id = price_id(other, current.interval, current.currency)
                plan.changes.append((_random_time_in(rng, ps, pe), new_id))
                current = PRICES[new_id]
            churn = rng.random() < _monthly_hazard(k, current.tier, pe, cohort)
            when = _random_time_in(rng, ps, pe)
        else:
            churn = rng.random() < (0.22 if current.tier == "premium" else 0.28)
            when = pe - timedelta(days=rng.randint(10, 60), hours=rng.randint(0, 12))
        if churn:
            plan.cancel_at = (
                max(when, plan.changes[-1][0] + timedelta(hours=1))
                if plan.changes and ps <= plan.changes[-1][0] < pe
                else when
            )
            if rng.random() < 0.06:
                plan.refund_days_after_end = rng.randint(2, 20)
            return plan, pe
        k += 1


def _fx_table(rng: random.Random) -> list[dict[str, Any]]:
    """Monthly average EUR per USD: a seeded random walk around 0.92."""
    rows: list[dict[str, Any]] = []
    rate = 0.92
    for m in range(MONTHS):
        month = add_months(START, m)
        rows.append({"month": month.date().isoformat(), "currency": "eur", "rate_to_eur": 1.0})
        rows.append(
            {"month": month.date().isoformat(), "currency": "usd", "rate_to_eur": round(rate, 4)}
        )
        rate *= 1 + rng.gauss(0, 0.015)
    return rows


def generate(cfg: GeneratorConfig | None = None) -> Export:
    cfg = cfg or GeneratorConfig()
    rng = random.Random(cfg.seed)
    out = Export()
    out.fx_rates = _fx_table(rng)
    months = [add_months(START, m) for m in range(MONTHS)]
    weights = [_signup_weight(m) for m in months]
    signup_months = sorted(rng.choices(months, weights=weights, k=cfg.n_customers))
    for n, month in enumerate(signup_months, start=1):
        days = _days_in(month.year, month.month)
        signup = month + timedelta(
            days=rng.randrange(days), hours=rng.randint(6, 20), minutes=rng.randrange(60)
        )
        currency = "usd" if rng.random() < 0.3 else "eur"
        country = (
            rng.choice(["US", "CA", "VN"])
            if currency == "usd"
            else rng.choice(["FI", "FI", "SE", "DE", "EE"])
        )
        customer_id = f"cus_{n:05d}"
        out.customers.append(
            {
                "id": customer_id,
                "email": f"learner{n:05d}@example.com",
                "created": _iso(signup),
                "country": country,
                "currency": currency,
            }
        )
        tier = "premium" if rng.random() < 0.3 else "standard"
        interval = "year" if rng.random() < 0.25 else "month"
        draw = rng.random()
        coupon = (
            "WELCOME20"
            if draw < 0.06
            else "SCHOLAR100"
            if draw < 0.07
            else "FIRST50"
            if draw < 0.11
            else None
        )
        if coupon == "WELCOME20" and interval == "year":
            # WELCOME20 is restricted to monthly prices (Stripe `applies_to`): three months of
            # discount on a twelve-month invoice has no clean monthly meaning.
            coupon = None
        trial = 14 if rng.random() < 0.35 else 0
        start, sub_n = signup, 1
        while True:
            plan, ended = draw_subscription(
                rng,
                f"sub_{n:05d}_{sub_n}",
                customer_id,
                start,
                price_id(tier, interval, currency),
                trial,
                coupon,
            )
            build_subscription(plan, out)
            lost_trial = bool(plan.trial_days) and not plan.trial_converts
            if ended is None or lost_trial or sub_n >= 3 or rng.random() >= 0.15:
                break
            start = ended + timedelta(days=rng.randint(30, 200))
            if start >= EXPORT_AT:
                break
            sub_n += 1
            tier = "premium" if rng.random() < 0.35 else "standard"
            interval, coupon, trial = "month", None, 0
    if cfg.edge_cases:
        for case in EDGE_CASES:
            first = case.plans[0].start
            out.customers.append(
                {
                    "id": case.customer_id,
                    "email": f"{case.customer_id}@example.com",
                    "created": _iso(first),
                    "country": "US" if case.currency == "usd" else "FI",
                    "currency": case.currency,
                }
            )
            for plan in case.plans:
                build_subscription(plan, out)
    out.planted = {
        "seed": cfg.seed,
        "n_customers": len(out.customers),
        "edge_cases": {c.customer_id: c.what for c in EDGE_CASES} if cfg.edge_cases else {},
        "effects": [
            "January 2026 cohort: 2.5x churn hazard in the first four billing months",
            "June and July: 1.7x churn hazard, 0.6x signups",
            "September 2026: back-to-school campaign, 1.6x signups",
            "Annual plans: 22-28% do not renew; September 2025 annual sales renew in Sep 2026",
        ],
    }
    return out


RAW_FILES = {
    "customers": "customers.csv",
    "prices": "prices.json",
    "subscriptions": "subscriptions.json",
    "invoices": "invoices.csv",
    "invoice_line_items": "invoice_line_items.csv",
    "refunds": "refunds.csv",
    "discounts": "discounts.json",
    "fx_rates": "fx_rates.csv",
}
_CSV_COLUMNS = {
    "customers": ["id", "email", "created", "country", "currency"],
    "invoices": [
        "id",
        "customer",
        "subscription",
        "created",
        "status",
        "currency",
        "billing_reason",
        "total",
        "amount_paid",
    ],
    "invoice_line_items": [
        "id",
        "invoice",
        "subscription",
        "price",
        "amount",
        "discount_amount",
        "proration",
        "period_start",
        "period_end",
        "description",
    ],
    "refunds": ["id", "invoice", "amount", "currency", "created", "reason"],
    "fx_rates": ["month", "currency", "rate_to_eur"],
}


def _prices_json() -> list[dict[str, Any]]:
    """Prices with the product expanded, like `stripe prices list --expand data.product`."""
    return [
        {
            "id": p.id,
            "currency": p.currency,
            "unit_amount": p.unit_amount,
            "recurring": {"interval": p.interval, "interval_count": 1},
            "product": {"id": p.product, "name": PRODUCTS[p.tier][1], "tier": p.tier},
        }
        for p in PRICES.values()
    ]


def write_export(export: Export, raw_dir: Path) -> dict[str, int]:
    """Write the export files and `planted.json`. Returns row counts per file."""
    raw_dir.mkdir(parents=True, exist_ok=True)
    tables: dict[str, list[dict[str, Any]]] = {
        "customers": export.customers,
        "invoices": export.invoices,
        "invoice_line_items": export.lines,
        "refunds": export.refunds,
        "fx_rates": export.fx_rates,
        "subscriptions": export.subscriptions,
        "discounts": export.discounts,
        "prices": _prices_json(),
    }
    for name, rows in tables.items():
        path = raw_dir / RAW_FILES[name]
        if path.suffix == ".json":
            path.write_text(json.dumps(rows, indent=None, separators=(",", ":")))
        else:
            with path.open("w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=_CSV_COLUMNS[name])
                writer.writeheader()
                writer.writerows(rows)
    (raw_dir / "planted.json").write_text(json.dumps(export.planted, indent=2))
    return {name: len(rows) for name, rows in tables.items()}
