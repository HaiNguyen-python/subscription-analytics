-- The MRR bridge closes every month: start + movements = end, and this month's start is
-- last month's end. Returns the months where it does not.
with bridge as (
    select
        *,
        lag(ending_mrr_eur) over (order by month_start) as previous_ending_mrr_eur
    from {{ ref('mart_mrr_monthly') }}
)

select *
from bridge
where abs(starting_mrr_eur + new_mrr_eur + expansion_mrr_eur + contraction_mrr_eur
          + churned_mrr_eur + reactivation_mrr_eur + fx_mrr_eur - ending_mrr_eur) > 0.005
   or abs(starting_mrr_eur - coalesce(previous_ending_mrr_eur, 0)) > 0.005
