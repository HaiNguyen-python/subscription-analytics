-- Status history as validity intervals [valid_from, valid_to): the state a subscription was
-- in at any instant. valid_to is null for the current state.
select
    s.subscription_event_id,
    s.subscription_id,
    s.customer_id,
    s.status,
    s.price_id,
    s.cancel_at_period_end,
    p.plan_tier,
    p.plan_interval,
    p.currency,
    p.unit_amount,
    p.interval_months,
    s.effective_at as valid_from,
    lead(s.effective_at) over (
        partition by s.subscription_id order by s.effective_at, s.event_index
    ) as valid_to
from {{ ref('stg_subscriptions') }} as s
inner join {{ ref('stg_prices') }} as p using (price_id)
