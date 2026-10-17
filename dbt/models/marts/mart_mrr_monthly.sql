-- The MRR bridge per month. The report reads the same numbers through the semantic layer
-- (dbt/models/semantic.yml); tests/test_semantic.py asserts that both agree.
select
    month_start,
    sum(starting_mrr_eur) as starting_mrr_eur,
    sum(new_mrr_eur) as new_mrr_eur,
    sum(expansion_mrr_eur) as expansion_mrr_eur,
    sum(contraction_mrr_eur) as contraction_mrr_eur,
    sum(churned_mrr_eur) as churned_mrr_eur,
    sum(reactivation_mrr_eur) as reactivation_mrr_eur,
    sum(fx_mrr_eur) as fx_mrr_eur,
    sum(ending_mrr_eur) as ending_mrr_eur,
    sum(new_mrr_eur + expansion_mrr_eur + contraction_mrr_eur + churned_mrr_eur
        + reactivation_mrr_eur) as net_new_mrr_eur,
    12 * sum(ending_mrr_eur) as arr_eur,
    count(*) filter (where mrr_local > 0) as active_customers,
    count(*) filter (where movement_type = 'new') as new_customers,
    count(*) filter (where movement_type = 'churn') as churned_customers,
    count(*) filter (where movement_type = 'reactivation') as reactivated_customers
from {{ ref('fct_mrr_movements') }}
group by 1
