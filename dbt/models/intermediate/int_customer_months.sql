-- The subscription-month spine: one row per customer and month, from the customer's first
-- subscription month to the last reported month, with MRR at the month end.
--
-- MRR is a month-end snapshot: the state each subscription is in at the last instant before
-- the next month starts. Rules (docs/how-it-works.md has the worked examples):
--   * active and past_due count; trialing, canceled and anything else count 0
--   * monthly amount = unit amount / interval_months (annual = amount / 12)
--   * repeating and forever coupons reduce MRR while they last; once coupons do not
--   * EUR = local amount x that month's average rate
with months as (
    select month_start, month_start + interval 1 month as next_month_start
    from {{ ref('stg_fx_rates') }}
    where currency = 'eur'
),

state_at_month_end as (
    select m.month_start, st.*
    from {{ ref('int_subscription_states') }} as st
    inner join months as m
        on st.valid_from < m.next_month_start
        and (st.valid_to is null or st.valid_to >= m.next_month_start)
),

discount_at_month_end as (
    select m.month_start, d.subscription_id, max(d.percent_off) as percent_off
    from {{ ref('stg_discounts') }} as d
    inner join months as m
        on d.counts_in_mrr
        and d.starts_at < m.next_month_start
        and (d.ends_at is null or d.ends_at >= m.next_month_start)
    group by all
),

subscription_mrr as (
    select
        s.month_start,
        s.customer_id,
        s.subscription_id,
        s.status,
        s.plan_tier,
        s.plan_interval,
        case
            when s.status in ('active', 'past_due')
                then round(
                    s.unit_amount / s.interval_months
                    * (1 - coalesce(d.percent_off, 0) / 100.0), 6
                )
            else 0
        end as mrr_local
    from state_at_month_end as s
    left join discount_at_month_end as d using (month_start, subscription_id)
),

spine as (
    select c.customer_id, c.currency, m.month_start
    from {{ ref('stg_customers') }} as c
    inner join (
        select customer_id, min(date_trunc('month', valid_from)) as first_month
        from {{ ref('int_subscription_states') }}
        group by 1
    ) as f using (customer_id)
    inner join months as m on m.month_start >= f.first_month
)

select
    spine.customer_id,
    spine.month_start,
    spine.currency,
    fx.rate_to_eur,
    coalesce(sum(sm.mrr_local), 0) as mrr_local,
    coalesce(sum(sm.mrr_local), 0) * fx.rate_to_eur as mrr_eur,
    -- a customer has one live subscription at a time; take the one carrying the MRR
    arg_max(sm.status, sm.mrr_local) as subscription_status,
    arg_max(sm.plan_tier, sm.mrr_local) filter (where sm.mrr_local > 0) as plan_tier,
    arg_max(sm.plan_interval, sm.mrr_local) filter (where sm.mrr_local > 0) as plan_interval
from spine
inner join {{ ref('stg_fx_rates') }} as fx
    on spine.month_start = fx.month_start and spine.currency = fx.currency
left join subscription_mrr as sm
    on spine.customer_id = sm.customer_id and spine.month_start = sm.month_start
group by spine.customer_id, spine.month_start, spine.currency, fx.rate_to_eur
