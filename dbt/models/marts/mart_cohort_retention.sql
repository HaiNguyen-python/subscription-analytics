-- Logo and revenue retention by cohort (first paying month) and months since then.
-- Revenue is in constant currency (every month at the cohort month's rate), so a weaker
-- dollar does not look like churn. Reactivated customers count again in their old cohort.
with cohort_rows as (
    select
        f.customer_id,
        f.cohort_month,
        date_diff('month', f.cohort_month, f.month_start) as months_since_start,
        f.mrr_local > 0 as is_active,
        f.mrr_local * fx.rate_to_eur as mrr_constant_eur
    from {{ ref('fct_mrr_movements') }} as f
    inner join {{ ref('stg_fx_rates') }} as fx
        on f.cohort_month = fx.month_start and f.currency = fx.currency
    where f.month_start >= f.cohort_month
),

cohort_size as (
    select
        cohort_month,
        count(*) as cohort_customers,
        sum(mrr_constant_eur) as cohort_mrr_eur
    from cohort_rows
    where months_since_start = 0
    group by 1
)

select
    r.cohort_month,
    r.months_since_start,
    s.cohort_customers,
    count(*) filter (where r.is_active) as active_customers,
    s.cohort_mrr_eur,
    sum(r.mrr_constant_eur) as mrr_eur,
    count(*) filter (where r.is_active) / s.cohort_customers as logo_retention,
    sum(r.mrr_constant_eur) / s.cohort_mrr_eur as revenue_retention
from cohort_rows as r
inner join cohort_size as s using (cohort_month)
group by r.cohort_month, r.months_since_start, s.cohort_customers, s.cohort_mrr_eur
