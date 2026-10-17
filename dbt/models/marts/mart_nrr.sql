-- 12-month net and gross revenue retention: of the MRR customers had 12 months ago, how much
-- do the same customers pay now? Net counts expansion; gross caps each customer at their old
-- MRR. Both sides at the current month's rate. Months without a 12-month history are left out.
select
    month_start,
    sum(nrr_base_eur) as base_mrr_12m_ago_eur,
    sum(nrr_retained_eur) as retained_mrr_eur,
    sum(grr_retained_eur) as retained_mrr_capped_eur,
    sum(nrr_retained_eur) / sum(nrr_base_eur) as nrr_12m,
    sum(grr_retained_eur) / sum(nrr_base_eur) as grr_12m
from {{ ref('fct_mrr_movements') }}
group by 1
having sum(nrr_base_eur) > 0
