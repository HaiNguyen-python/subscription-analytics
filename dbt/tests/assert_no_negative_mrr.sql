{{ config(meta={"dagster": {"ref": {"name": "fct_mrr_movements"}}}) }}

-- MRR is never negative, for a customer or in total. A negative value would mean a credit
-- (refund, proration) leaked into MRR.
select customer_month_id as id, ending_mrr_eur as mrr_eur
from {{ ref('fct_mrr_movements') }}
where mrr_local < 0 or ending_mrr_eur < 0
union all
select strftime(month_start, '%Y-%m') as id, ending_mrr_eur as mrr_eur
from {{ ref('mart_mrr_monthly') }}
where ending_mrr_eur < 0
