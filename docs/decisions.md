# Design decisions

Short records of the main choices: what was decided, why, and what it costs. Format inspired by
Architecture Decision Records (ADRs).

## 1. MRR is a month-end snapshot of subscription state

- **Decision:** MRR for a month is the normalised price of every subscription that is `active`
  or `past_due` at the last instant of the month, after repeating and forever coupons.
- **Why:** it answers "what recurring revenue do we have right now?" and makes the bridge well
  defined (one start and one end value per month). Invoices are the wrong source: prorations,
  annual prepayments and refunds would all show up as MRR swings.
- **Cost:** a customer who starts and churns inside one month never appears in MRR (their
  revenue still shows in the reconciliation). Stripe's own dashboard uses daily MRR; the
  numbers will differ slightly.
- **Alternatives:** daily MRR averaged per month; MRR from invoices.

## 2. past_due counts, trialing does not, once coupons do not

- **Decision:** `past_due` is MRR until the subscription is canceled; `trialing` is 0; a once
  coupon does not reduce MRR, a repeating or forever coupon does; a 100% coupon gives MRR 0 and
  the customer is not counted as paying.
- **Why:** past_due is still a contract and most failed payments recover (the generator recovers
  55% of them). A trial has no price commitment. A once coupon is a one-off price cut, not a lower
  recurring price.
- **Cost:** MRR includes some revenue at risk; the reconciliation shows it as
  `past_due_unpaid`, so it is visible, not hidden.

## 3. Classify in local currency, FX as its own bridge line

- **Decision:** expansion/contraction compare local-currency MRR; the FX effect
  `prev_local x (rate_now - rate_prev)` is a separate movement. Rates are monthly averages.
- **Why:** with plain EUR comparisons every USD customer would "expand" or "contract" every
  month. The decomposition still closes exactly.
- **Cost:** one more line in the bridge to explain. `net_new_mrr` excludes FX, so ending minus
  starting MRR is net new plus FX.
- **Alternatives:** a fixed budget rate (no FX line, but MRR drifts from reality), constant
  currency reporting only.

## 4. Two reconciliations: exact and tolerance

- **Decision:** billed MRR (invoice lines covering the month end) must match MRR to the cent
  after two named differences, and this fails the build. Recognised revenue (daily spreading,
  calendar-normalised) must be within 5% of average MRR, and this only warns.
- **Why:** the exact check catches real bugs (a missed state, a wrong normalisation, a coupon
  applied twice). Revenue and MRR measure different things, so an exact revenue check would
  need so many adjustments that it would just restate the model.
- **Cost:** the billed MRR rule for proration lines (full monthly price with the line's sign)
  assumes one proration pair per change, which is how Stripe's `always_invoice` works but not
  every proration setting.

## 5. Repeating coupons only on monthly prices

- **Decision:** `WELCOME20` (3 months) applies to monthly prices only; coupons attach when the
  paid plan starts (after a trial).
- **Why:** three months of discount on a twelve-month invoice has no clean monthly meaning (is
  MRR discounted for 3 months or for the whole year it was billed?). Restricting the coupon is
  what a business would do with Stripe's `applies_to`, and it keeps the exact reconciliation
  exact. Attaching at the paid start makes a repeating coupon end on a period boundary.
- **Cost:** a real export with such coupons would need a rule, and the reconciliation would
  show it as unexplained until then.

## 6. The dbt semantic layer is the definition; a small compiler runs it

- **Decision:** metrics live in `dbt/models/semantic/semantic.yml` (dbt semantic models and
  metrics). The Dagster asset and the report use `semantic.py` to compile them to SQL.
  MetricFlow is an optional extra and a test checks it returns the same numbers.
- **Why:** one definition, validated by dbt, readable by any MetricFlow-compatible tool. The
  compiler runs in-process on a read-only DuckDB connection with no extra dependency, in
  milliseconds; `mf query` takes a few seconds per call and needs its own environment.
- **Cost:** the compiler supports only what the file uses (simple, ratio and derived metrics,
  grouping by month and a categorical dimension). New metric features need either compiler
  work or a switch to MetricFlow at runtime; the cross-check test tells us when they diverge.

## 7. Full dbt rebuild; metrics and report partitioned by month

- **Decision:** the raw and dbt assets are unpartitioned; `monthly_metrics` and
  `monthly_report` use `MonthlyPartitionsDefinition`.
- **Why:** MRR movements need the previous month and cohorts need all history, and a full
  build takes about 3 seconds. Monthly partitions are where they help: a re-run for one month
  only rewrites that month's KPIs (delete + insert, tested to be idempotent), and the schedule
  maps naturally to "report the month that just closed".
- **Cost:** a backfill of 24 partitions rebuilds dbt 24 times. At real scale I would make the
  big models incremental with a lookback window.

## 8. Cohort = first paying month

- **Decision:** a customer's cohort is the first month they had MRR at a month end, not their
  signup date. Reactivated customers stay in their original cohort.
- **Why:** trials and free seats would otherwise inflate cohort sizes with people who never
  paid, and retention would mix "did not convert" with "churned".
- **Cost:** a trial that starts on 25 January lands in the February cohort, which surprises
  people until it is explained (it is a test case).

## 9. dagster-dbt with the default translator

- **Decision:** name the dbt source `raw` and its tables after the files, so dagster-dbt's
  default keys (`raw/<table>`) match the raw multi-asset. dbt artifacts go to a fixed
  `dbt/target/dagster_build` folder.
- **Why:** no custom translator code to maintain; lineage from file to report is visible in
  the Dagster UI. The fixed folder gives the report one place to read the test summary from.
- **Cost:** asset keys depend on dbt naming; renaming a source table renames the asset.

## 10. DuckDB, and dbt in Dagster's process tree

- **Decision:** DuckDB for local runs, tests and CI; dbt is invoked through `DbtCliResource`.
- **Why:** no server, a 2,000-customer build in seconds, and the same SQL would run on
  MotherDuck or, with small changes, on Postgres or a cloud warehouse via dbt adapters.
- **Cost:** one writer at a time, so assets must close connections before dbt runs; the dbt
  profile reads the database path from `SUBS_DUCKDB_PATH`, which the assets set.
