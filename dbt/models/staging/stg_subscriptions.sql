-- One row per status change of a subscription (the export nests the history in each
-- subscription object). Stripe timestamps are Unix seconds; they become naive UTC here.
with unnested as (
    select
        id as subscription_id,
        customer as customer_id,
        make_timestamp(created * 1000000) as created_at,
        status as current_status,
        unnest(history) as h,
        generate_subscripts(history, 1) as event_index
    from {{ source('raw', 'subscriptions') }}
)

select
    subscription_id || '-' || lpad(event_index::varchar, 3, '0') as subscription_event_id,
    subscription_id,
    customer_id,
    created_at,
    current_status,
    event_index,
    make_timestamp(h.effective_at * 1000000) as effective_at,
    h.status as status,
    h.price as price_id,
    h.cancel_at_period_end as cancel_at_period_end
from unnested
