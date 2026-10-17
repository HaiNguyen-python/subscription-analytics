# Learning guide (for me, Hai)

Goal: be able to explain every part of this repo in an Analytics Engineer or Data Engineer
interview, and answer "why?" three levels deep. This is the reporting I want for HaiEduTech's
Stripe account, so I should be able to defend each MRR rule as a business decision too.

Start with [docs/how-it-works.md](docs/how-it-works.md) (the walkthrough and the edge cases by
hand) and [docs/decisions.md](docs/decisions.md) (the reasons). This file is the practice plan.

## 1. Run it and watch each step (1 hour)

```bash
make install
subs generate                       # data/raw/*.csv|json + planted.json: open subscriptions.json
subs load                           # raw.* only; look at the nested history column
subs materialize --month 2026-09-01 # every Dagster asset; read the asset check lines
subs report                         # docs/results.md, docs/img, reports/2026-09/report.html
dagster dev                         # http://localhost:3000: lineage from raw/customers to the report
```

Then open DuckDB (`duckdb data/warehouse.duckdb`) and follow one customer:

```sql
select * from analytics.int_subscription_states where customer_id = 'cus_edge_upgrade';
select * from analytics.fct_mrr_movements where customer_id = 'cus_edge_upgrade' limit 6;
select * from raw.invoice_line_items where subscription = 'sub_edge_upgrade_1' limit 5;
select * from analytics.mart_revenue_reconciliation order by month_start desc limit 3;
```

Run one metric through dbt's own engine: `cd dbt && DBT_PROFILES_DIR=. mf query --metrics
mrr,nrr --group-by metric_time__month` and compare with `docs/results.md`.

## 2. Read the code in this order

| File | What to understand | Question to answer yourself |
| --- | --- | --- |
| `generator.py`: `SubPlan`, `build_subscription` | decisions vs mechanics, proration, cut-off | Why are the edge cases `SubPlan`s and not hand-written rows? |
| `generator.py`: `add_months`, `period_bounds` | Stripe's day clamping | What goes wrong if each period is computed from the previous one? |
| `warehouse.py`: `load_raw` | explicit types, idempotent replace | What breaks with type inference on an export without refunds? |
| `dbt/models/staging/stg_subscriptions.sql` | `unnest` + `generate_subscripts` | Why keep the history nested in raw? |
| `int_subscription_states.sql` | validity intervals with `lead()` | Which state is "current" when `valid_to` is null? |
| `int_customer_months.sql` | month-end instant, MRR rules, dense spine | Why `valid_to >= next_month_start` and not `>`? |
| `fct_mrr_movements.sql` | movement classes, FX decomposition | Prove on paper that the bridge closes for a USD customer. |
| `mart_revenue_reconciliation.sql` | billed MRR, proration rule, tolerance check | Why is the revenue check a warning and billing an error? |
| `mart_cohort_retention.sql`, `mart_nrr.sql` | constant currency, 12-month lag | Why does `lag(mrr_local, 12)` need the dense spine? |
| `dbt/tests/*.sql` | singular tests | Which bug would each one catch? |
| `semantic.yml` | measures vs metrics, ratio vs derived | Why is NRR a ratio of two metrics and not a derived expression? |
| `semantic.py` | compile in two layers, safe derived expressions | What would `revenue; drop table x` do without the tokenizer? |
| `definitions.py` | multi-asset, dagster-dbt, partitions, checks | Why are dbt assets not partitioned? |
| `report.py` | everything read through `semantic.query` | How does the test prove the report uses the YAML? |

## 3. Key concepts in my own words (fill these in)

- **MRR, ARR, ARPA:** ...
- **MRR bridge / movements (new, expansion, contraction, churn, reactivation):** ...
- **Logo churn vs revenue churn vs NRR vs GRR:** ...
- **Proration and why it is not MRR:** ...
- **Month-end snapshot vs daily MRR:** ...
- **FX translation vs constant currency:** ...
- **Revenue recognition (accrual, daily spreading) vs billing vs cash:** ...
- **Cohort analysis, logo vs revenue retention:** ...
- **Semantic layer, measure vs metric, MetricFlow:** ...
- **Software-defined assets, partitions, asset checks:** ...
- **Validity intervals / SCD2-style history:** ...

## 4. Interview questions this repo answers

1. *How do you compute MRR from Stripe?* Month-end state, normalisation, coupons, trials,
   past_due; show the edge-case table and the tests.
2. *Why did MRR change this month?* The bridge: September 2026 grew €4,199, new +€5,800,
   churn -€1,975 (synthetic). FX is separate.
3. *How do you know your MRR is right?* Billed MRR from invoice lines reconciles to the cent
   with named differences; recognised revenue within tolerance; bridge-closure test; a test
   that breaks the bridge on purpose.
4. *What is a semantic layer and why use one?* One definition per metric; show the YAML, the
   compiler, the MetricFlow cross-check and the test that edits ARR.
5. *How would you orchestrate this?* Dagster assets, dagster-dbt, monthly partitions for the
   per-month outputs, asset checks, the schedule on the 2nd.
6. *How do you handle multiple currencies?* Monthly average rates, local-currency
   classification, FX bridge line, constant currency for retention.
7. *Which cohorts churn and how would you show it?* Cohort heatmap, first-paying-month
   cohorts; January 2026 at 67% vs 80% after three months (planted).
8. *What would change at 100x the data?* Incremental models with a lookback, partitioned dbt
   runs, a cloud warehouse, freshness checks.

## 5. Make it mine (do at least three)

- [ ] Export one month of HaiEduTech's Stripe data (test mode first) into the eight files and
      run `subs load --source-dir`; write down every mismatch with the synthetic format.
- [ ] Add daily MRR (`int_customer_days`) and compare its monthly average with the snapshot.
- [ ] Add a per-seat school licence price (`quantity > 1`) and extend the MRR rule and the tests.
- [ ] Add a `customer_country` dimension to the semantic model and a churn-by-country table.
- [ ] Make `int_customer_months` incremental with a two-month lookback and prove it gives the
      same numbers as the full build.
- [ ] Add a Dagster sensor that runs the job when a new export folder appears.

## 6. Quality checks I should be able to explain

- `make lint` (ruff), `mypy src`, `pytest` (29 test functions, 47 cases, about 96% coverage),
  `dbt parse` (validates the semantic layer), `dagster definitions validate`.
- Why the test session builds one tiny warehouse (53 customers) through the real Dagster
  assets and shares it between test files.
- Why `assert_revenue_within_tolerance` is `severity: warn`.

## 7. Honesty checklist before pushing

- [ ] I can explain every file without looking at it.
- [ ] Every number in the README comes from `docs/results.md` of a run I did myself and is
      labelled synthetic.
- [ ] I say clearly that the January cohort, summer churn and September campaign were planted.
- [ ] I say that the real Stripe loader has not been run against a real account yet.
- [ ] Anything I haven't built is under "What I'd do next".

Note: in the environment this repo was first built in, `dagster dev` was started and answered
on its web port, but the UI itself was not clicked through. Do that once before a demo.
