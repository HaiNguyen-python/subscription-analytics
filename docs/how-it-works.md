# How it works

This page follows a subscription from the Stripe export to the MRR bridge and the report, and
works through every edge case by hand. Python is in `src/subs_analytics/`, SQL in `dbt/`.

## 1. The business and the export files

An online language school sells two products, each monthly or annual, in EUR and USD:

| Price id | Product | Interval | Amount |
| --- | --- | --- | --- |
| `price_sta_month_eur` / `_usd` | Standard: group classes | month | 29.00 EUR / 35.00 USD |
| `price_sta_year_eur` / `_usd` | Standard | year | 290.00 EUR / 350.00 USD |
| `price_pre_month_eur` / `_usd` | Premium: 1:1 tutoring | month | 79.00 EUR / 95.00 USD |
| `price_pre_year_eur` / `_usd` | Premium | year | 790.00 EUR / 950.00 USD |

Coupons: `WELCOME20` (20% off, repeating 3 months, monthly prices only), `SCHOLAR100` (100% off
forever, teacher scholarship seats), `FIRST50` (50% off once).

The export has eight files, shaped like the Stripe API objects:

| File | Grain | Notes |
| --- | --- | --- |
| `customers.csv` | customer | one currency per customer, like Stripe |
| `prices.json` | price | product expanded (`--expand data.product`) |
| `subscriptions.json` | subscription | current state plus a nested `history` of status changes, Unix timestamps |
| `invoices.csv` | invoice | status `paid`, `open`, `uncollectible` or `void`; amounts in cents |
| `invoice_line_items.csv` | line | `amount` before discount, `discount_amount`, `proration`, service period |
| `refunds.csv` | refund | refers to the invoice it refunds |
| `discounts.json` | discount | coupon embedded; start and end |
| `fx_rates.csv` | month x currency | monthly average rate to EUR |

## 2. Synthetic data and planted effects

`generator.py` works in two steps on purpose.

1. **Decisions**: `draw_subscription` draws a `SubPlan` for each subscription: price, trial,
   coupon, mid-cycle plan changes, when the customer schedules a cancellation, whether a renewal
   payment fails and whether it recovers. The 13 edge-case customers in `EDGE_CASES` are
   `SubPlan`s written by hand.
2. **Mechanics**: `build_subscription` turns any plan into Stripe-shaped records. Billing
   periods are computed from the anchor with Stripe's day clamping (`add_months`: 31 Jan ->
   28 Feb -> 31 Mar, never drifting). A plan change creates a `subscription_update` invoice with
   two proration lines. A cancellation sets `cancel_at_period_end` and ends the subscription at
   the period end. A failed payment makes the subscription `past_due`; it recovers after a few
   days or is canceled after 21 days with the invoice `uncollectible`. Anything at or after the
   export time (1 Oct 2026) is cut off, like a real export.

Because the edge cases go through the same mechanics as the 2,000 random customers, a test on
an edge case also tests the generator.

Random behaviour: 35% start with a 14-day trial (62% convert); 30% Premium; 25% annual; monthly
churn hazard 7.5% in the first three periods, 4.5% up to a year, 3% after; annual plans do not
renew 22-28% of the time; 2% of Standard customers upgrade and 1.2% of Premium downgrade per
period; 3% of renewals fail; 15% of churned customers come back after 1-7 months.

**Planted effects** (so there is something real to find):

- the January 2026 cohort has a 2.5x churn hazard in its first four billing months ("New Year
  resolution" signups);
- June and July have 1.7x churn and 0.6x signups (summer break). This is why the February 2026
  cohort also drops in months 4-5 of the heatmap: those are June and July;
- September 2026 has a back-to-school campaign with 1.6x the usual signups (normally 1.4x);
- growth of about 3% more signups each month.

## 3. Raw load: `warehouse.py`

`load_raw` creates `raw.<table>` with `read_csv` / `read_json` and **explicit column types**.
Type inference would guess `VARCHAR` for an all-empty column, and a small export may have no
refunds at all; explicit types keep the staging models working. Nested JSON stays as
`STRUCT`/`LIST`, so the business logic stays in dbt. A reload replaces the tables (idempotent).

## 4. dbt staging and intermediate models

Staging (`dbt/models/staging/`) renames, converts cents to units and Unix seconds to
timestamps. `stg_subscriptions` unnests the history into one row per status change.
`stg_customers` drops the email: no metric needs it, so it never reaches `analytics`.

`int_subscription_states` turns the history into validity intervals `[valid_from, valid_to)`
with `lead()`. `int_customer_months` is the **subscription-month spine**: one row per customer
and month from their first subscription month to the last reported month (the months in the
FX table). For each month it finds the state at the **month-end instant** (the last moment
before the next month starts):

```sql
on st.valid_from < m.next_month_start
and (st.valid_to is null or st.valid_to >= m.next_month_start)
```

and computes MRR in local currency:

| Rule | Why |
| --- | --- |
| `active` and `past_due` count, `trialing` and `canceled` count 0 | past_due is still a contract; it shows up separately in the reconciliation |
| monthly amount = unit amount / `interval_months` | an annual plan counts 1/12 |
| repeating and forever coupons reduce MRR while active | they change what the customer pays every month |
| once coupons do not | a one-off price cut is not recurring |
| EUR = local x the month's average rate | one rate per month and currency |

## 5. `fct_mrr_movements`: the bridge per customer

With the dense spine, `lag()` gives last month's MRR. Each customer-month gets one movement:

| Condition (local currency) | Movement | Amount (EUR) |
| --- | --- | --- |
| 0 -> >0, never paid before | new | ending MRR |
| 0 -> >0, paid before | reactivation | ending MRR |
| >0 -> 0 | churn | -starting MRR |
| >0 -> more | expansion | (now - before) x this month's rate |
| >0 -> less | contraction | (now - before) x this month's rate |
| both >0 | FX (in addition) | before x (rate now - rate before) |

For a customer active in both months the plan movement plus the FX movement is
`now x r1 - before x r0`, the change in EUR MRR, exactly. So the bridge closes for every
customer and every month, and FX never shows up as churn or expansion.
`dbt/tests/assert_mrr_bridge_closes.sql` checks start + movements = end and start = last end.

## 6. The edge cases, by hand

All EUR unless noted; MRR at the end of each month (`tests/test_dbt_models.py` asserts every
cell; the live values are in [results.md](results.md#edge-cases-january-to-may-2025-local-currency-mrr)).

| Customer | What happens | Jan | Feb | Mar | Apr | May |
| --- | --- | --- | --- | --- | --- | --- |
| upgrade | Standard from 10 Jan, Premium from 20 Mar | 29 new | 29 | 79 expansion +50 | 79 | 79 |
| downgrade | Premium from 20 Jan, Standard from 25 Feb | 79 new | 29 contraction -50 | 29 | 29 | 29 |
| annual | Premium annual from 1 Feb | - | 65.83 new | 65.83 | 65.83 | 65.83 |
| refund | cancel scheduled 20 Apr, ends 5 May, refund 15 May | 29 new | 29 | 29 | 29 | 0 churn |
| coupon100 | Premium with SCHOLAR100 | 0 | 0 | 0 | 0 | 0 |
| coupon_once | FIRST50 on the first invoice | 29 new | 29 | 29 | 29 | 29 |
| coupon_repeating | WELCOME20 for 3 months from 14 Jan | 23.20 new | 23.20 | 23.20 | 29 expansion +5.80 | 29 |
| usd | Standard in USD from 3 Jan | 35 USD new | 35 USD | 35 USD | 35 USD | 35 USD |
| trial | trial 25 Jan - 8 Feb, then paid | 0 | 29 new | 29 | 29 | 29 |
| trial_lost | trial from 20 Jan, never converts | 0 | 0 | 0 | 0 | 0 |
| past_due | renewal 12 Mar fails, canceled 2 Apr | 29 new | 29 | 29 (past_due) | 0 churn | 0 |
| past_due_recovered | renewal 28 Feb fails, paid 5 Mar | 29 new | 29 (past_due) | 29 | 29 | 29 |
| reactivation | ends 8 Mar, Premium again from 16 May | 29 new | 29 | 0 churn | 0 | 79 reactivation |

Worked details:

- **Proration.** The period is 10 Mar - 10 Apr (31 days); the change on 20 Mar leaves 21 days.
  Stripe credits the unused Standard time and charges the Premium time:
  `-29 x 21/31 = -19.65` and `+79 x 21/31 = +53.52` on a separate invoice. Neither line is
  MRR; MRR at the end of March is simply the Premium price, 79.
- **Annual.** `790 / 12 = 65.83` every month, although one invoice of 790 was paid in February.
- **Refund after churn.** The subscription ended on 5 May, so May churns -29. The refund on
  15 May reduces May's recognised revenue by 29 and never touches MRR.
- **100% coupon.** MRR 0, so the customer is not a paying customer, never "new", has no cohort.
- **Coupon ending.** When WELCOME20 ends, MRR goes from 23.20 to 29.00: +5.80 expansion (the
  same convention ChartMogul uses).
- **FX.** With the seed-42 rates (0.9141 in January 2025, 0.9237 in February), the USD customer
  is `35 x 0.9141 = €31.99` in January and `35 x 0.9237 = €32.33` in February. The +€0.34 is
  an FX movement, not expansion.
- **Trial.** On 31 Jan the subscription is `trialing`: MRR 0. The cohort is February, the first
  paying month, not the January signup.
- **past_due.** On 31 Mar the subscription is `past_due`: MRR still 29. It is canceled on 2 Apr,
  so April churns.

## 7. Marts

- `mart_mrr_monthly`: the bridge per month, customer counts, ARR.
- `dim_customer`: cohort (first paying month), first plan, trial, first coupon, lifecycle.
- `mart_cohort_retention`: per cohort and months since the first payment, logo retention
  (still paying / cohort size) and revenue retention in **constant currency** (every month at
  the cohort month's rate). Reactivated customers count again in their old cohort.
- `mart_nrr`: 12-month net and gross revenue retention: MRR now of the customers who paid 12
  months ago, over their MRR then, both at the current rate. Gross caps each customer at the
  old MRR.
- `mart_revenue_reconciliation`, two checks:
  1. **Billed MRR (exact).** Every invoice line whose service period covers the month-end
     instant becomes a monthly amount: `net / interval_months`, or for a proration line its
     price's full monthly amount with the line's sign. After the upgrade above, on 31 Mar:
     old line 29 + credit -29 + charge +79 = 79 = MRR. Only paid invoices count. MRR minus
     billed MRR must equal `one_time_discounts` (once coupons) plus `past_due_unpaid` (MRR of
     past_due subscriptions whose invoice is open or uncollectible); the rest is
     `unexplained`, and `assert_mrr_reconciles_to_billing` fails the build above €0.01.
  2. **Recognised revenue (tolerance).** Paid lines are spread over their service period by
     the second; refunds count in the month they are paid out. Daily spreading gives February
     28 days of a 365-day year, so recurring revenue is multiplied by `365 / 12 / days` before
     it is compared with the average of opening and closing MRR. Within 5% from the fourth
     month on; this test warns instead of failing.

## 8. Loading a real Stripe export

The loader reads any folder with the eight files in section 1. From a Stripe account they can
be produced with the Stripe CLI or API (`customers`, `prices --expand data.product`,
`subscriptions --status all`, `invoices` with lines, `refunds`) plus the subscription status
history from `customer.subscription.*` events and a monthly FX table. Then:

```bash
subs load --source-dir path/to/export
SUBS_RAW_DIR=path/to/export subs materialize --month 2026-09-01
```

This path has only been tested with the synthetic files. A real export will need small changes
(Stripe's CSV exports name columns differently, and multi-item or per-seat subscriptions are
not modelled).

## 9. The semantic layer: `dbt/models/semantic/semantic.yml` and `semantic.py`

The YAML is a dbt semantic model over `fct_mrr_movements` (one row per customer and month) with
measures (`mrr_eur`, `new_mrr_eur`, `lost_mrr_eur`, `active_customers`, ...) and metrics:

| Metric | Type | Definition |
| --- | --- | --- |
| `mrr` | simple | sum of month-end MRR |
| `arr` | derived | `mrr * 12` |
| `net_new_mrr` | derived | new + expansion + contraction + churned + reactivation (FX excluded) |
| `logo_churn_rate` | ratio | churned customers / paying customers at the start |
| `revenue_churn_rate` | ratio | (churned + contraction MRR) / starting MRR |
| `nrr` | ratio | MRR now of customers paying 12 months ago / their MRR then |
| `arpa` | ratio | MRR / paying customers |

`dbt parse` validates the file. `semantic.compile_sql` turns metric names into one query: an
inner `select` aggregates the measures per month (and an optional dimension), the outer
`select` combines them. Ratios use `nullif(denominator, 0)`. A derived expression is rewritten
token by token: only numbers, `+ - * / ( )` and the metrics it lists are allowed, so the YAML
cannot inject SQL, and a metric cycle is an error.

```python
semantic.query(con, ["mrr", "nrr"], month=date(2026, 9, 1))
semantic.query(con, ["churned_mrr"], group_by=["plan_interval"], month=date(2026, 9, 1))
```

`tests/test_semantic.py` checks that the compiled metrics equal the marts, that MetricFlow
(`mf query`) returns the same numbers from the same YAML, and that the report changes when the
YAML changes.

## 10. Dagster: `definitions.py`

| Asset | Partitions | What it does | Checks |
| --- | --- | --- | --- |
| `raw/<table>` (8, one multi-asset) | none | `load_raw` | `has_rows` |
| dbt models (17) | none | `dbt build` via dagster-dbt | every dbt test is an asset check |
| `monthly_metrics` | monthly | semantic metrics for the month into `metrics.monthly_kpis` (delete + insert) | `mrr_matches_mart`, `bridge_closes`, `billing_reconciliation` |
| `monthly_report` | monthly | HTML report and charts in `reports/<month>/` | |

dbt sources are named `raw.<table>`, so dagster-dbt links them to the raw asset keys without a
custom translator. dbt writes its artifacts to `dbt/target/dagster_build`. The job
`monthly_refresh` runs everything; a schedule runs it on the 2nd of each month for the month
that just closed. dbt and the raw load ignore the partition (MRR needs the full history), only
the metrics and the report are per month.

`subs materialize` and the test fixture call `dagster.materialize(ALL_ASSETS, partition_key=...)`
in-process. DuckDB allows one writer, so the raw asset closes its connection before dbt starts
and the metrics asset opens its own afterwards.

## 11. Report: `report.py`

`collect` reads the KPIs, the bridge and the trend through `semantic.query`, cohorts and the
reconciliation from their marts, and the edge-case rows from the fact table. `bridge_chart`
draws the waterfall (increases blue, decreases grey, the y-axis does not start at zero and the
subtitle says so); `cohort_chart` draws the logo-retention heatmap with the January 2026 row
outlined. `results_markdown` writes [results.md](results.md); `write_html` writes a single
self-contained page to the gitignored `reports/` folder.
