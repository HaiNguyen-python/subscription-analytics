-- Calendar-normalised recognised recurring revenue is close to average MRR. This is a
-- sanity check, not an identity (the exact check is assert_mrr_reconciles_to_billing), so it
-- warns instead of failing. The first three months are skipped: with a tiny opening base,
-- mid-month signups make the average of opening and closing MRR a poor estimate.
{{ config(severity='warn') }}

select *
from {{ ref('mart_revenue_reconciliation') }}
where month_start >= (
        select min(month_start) + interval 3 month
        from {{ ref('mart_revenue_reconciliation') }}
    )
  and abs(recurring_revenue_normalised_eur - average_mrr_eur)
      > {{ var('revenue_tolerance_pct') }} * average_mrr_eur
