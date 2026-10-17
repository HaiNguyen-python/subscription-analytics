-- One row per customer. The cohort is the first month the customer paid (had MRR at a month
-- end), not the signup date: a trial that never converts has no cohort.
with months as (
    select
        customer_id,
        max(cohort_month) as cohort_month,
        arg_min(plan_tier, month_start) filter (where mrr_local > 0) as first_plan_tier,
        arg_min(plan_interval, month_start) filter (where mrr_local > 0) as first_plan_interval,
        arg_max(ending_mrr_eur, month_start) as current_mrr_eur,
        count(*) filter (where movement_type = 'reactivation') as reactivations
    from {{ ref('fct_mrr_movements') }}
    group by 1
),

subscriptions as (
    select
        customer_id,
        count(distinct subscription_id) as subscriptions,
        bool_or(status = 'trialing') as started_with_trial
    from {{ ref('stg_subscriptions') }}
    group by 1
),

coupons as (
    select customer_id, arg_min(coupon_id, starts_at) as first_coupon_id
    from {{ ref('stg_discounts') }}
    group by 1
)

select
    c.customer_id,
    c.country,
    c.currency,
    c.created_at,
    m.cohort_month,
    m.first_plan_tier,
    m.first_plan_interval,
    coalesce(s.started_with_trial, false) as started_with_trial,
    k.first_coupon_id,
    coalesce(s.subscriptions, 0) as subscriptions,
    coalesce(m.reactivations, 0) as reactivations,
    coalesce(m.current_mrr_eur, 0) as current_mrr_eur,
    case
        when m.cohort_month is null then 'never_paid'
        when m.current_mrr_eur > 0 then 'active'
        else 'churned'
    end as lifecycle_status
from {{ ref('stg_customers') }} as c
left join months as m using (customer_id)
left join subscriptions as s using (customer_id)
left join coupons as k using (customer_id)
