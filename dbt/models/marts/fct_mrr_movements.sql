-- One row per customer and month: MRR at the start and end of the month and the movement
-- that explains the change. Every month, for every customer:
--   ending = starting + new + expansion + contraction + churned + reactivation + fx
--
-- Classification uses local-currency MRR, so an exchange-rate change alone is never called
-- expansion or contraction. For a customer active in both months:
--   plan movement = (mrr_local - prev_mrr_local) x this month's rate
--   fx movement   = prev_mrr_local x (this month's rate - last month's rate)
-- which adds up to ending - starting exactly.
with customer_months as (
    select
        *,
        coalesce(lag(mrr_local) over w, 0) as prev_mrr_local,
        coalesce(lag(rate_to_eur) over w, rate_to_eur) as prev_rate_to_eur,
        coalesce(lag(mrr_local, 12) over w, 0) as mrr_local_12m_ago,
        coalesce(
            max(mrr_local) over (
                partition by customer_id order by month_start
                rows between unbounded preceding and 1 preceding
            ), 0
        ) > 0 as had_mrr_before,
        min(case when mrr_local > 0 then month_start end) over (
            partition by customer_id
        ) as cohort_month,
        last_value(plan_tier ignore nulls) over w_running as last_plan_tier,
        last_value(plan_interval ignore nulls) over w_running as last_plan_interval
    from {{ ref('int_customer_months') }}
    window
        w as (partition by customer_id order by month_start),
        w_running as (
            partition by customer_id order by month_start
            rows between unbounded preceding and current row
        )
),

classified as (
    select
        *,
        case
            when prev_mrr_local = 0 and mrr_local > 0 and not had_mrr_before then 'new'
            when prev_mrr_local = 0 and mrr_local > 0 then 'reactivation'
            when prev_mrr_local > 0 and mrr_local = 0 then 'churn'
            when prev_mrr_local > 0 and mrr_local > prev_mrr_local then 'expansion'
            when prev_mrr_local > 0 and mrr_local < prev_mrr_local then 'contraction'
            when prev_mrr_local > 0 then 'flat'
            else 'inactive'
        end as movement_type
    from customer_months
)

select
    customer_id || '-' || strftime(month_start, '%Y-%m') as customer_month_id,
    customer_id,
    month_start,
    currency,
    -- for a churned customer, the plan they left
    last_plan_tier as plan_tier,
    last_plan_interval as plan_interval,
    subscription_status,
    cohort_month,
    movement_type,
    rate_to_eur,
    prev_mrr_local,
    mrr_local,
    prev_mrr_local * prev_rate_to_eur as starting_mrr_eur,
    mrr_eur as ending_mrr_eur,
    case when movement_type = 'new' then mrr_eur else 0 end as new_mrr_eur,
    case
        when movement_type = 'expansion' then (mrr_local - prev_mrr_local) * rate_to_eur
        else 0
    end as expansion_mrr_eur,
    case
        when movement_type = 'contraction' then (mrr_local - prev_mrr_local) * rate_to_eur
        else 0
    end as contraction_mrr_eur,
    case
        when movement_type = 'churn' then -prev_mrr_local * prev_rate_to_eur else 0
    end as churned_mrr_eur,
    case when movement_type = 'reactivation' then mrr_eur else 0 end as reactivation_mrr_eur,
    case
        when prev_mrr_local > 0 and mrr_local > 0
            then prev_mrr_local * (rate_to_eur - prev_rate_to_eur)
        else 0
    end as fx_mrr_eur,
    -- 12-month net revenue retention inputs, both sides at this month's rate (FX-neutral)
    mrr_local_12m_ago,
    mrr_local_12m_ago * rate_to_eur as nrr_base_eur,
    case when mrr_local_12m_ago > 0 then mrr_eur else 0 end as nrr_retained_eur,
    case
        when mrr_local_12m_ago > 0 then least(mrr_local, mrr_local_12m_ago) * rate_to_eur
        else 0
    end as grr_retained_eur
from classified
