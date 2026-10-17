-- Amounts are before discount in Stripe; `net_amount` is what the customer was charged.
-- Proration credits are negative lines.
select
    id as line_id,
    invoice as invoice_id,
    subscription as subscription_id,
    price as price_id,
    amount / 100.0 as amount,
    discount_amount / 100.0 as discount_amount,
    (amount - discount_amount) / 100.0 as net_amount,
    proration as is_proration,
    period_start,
    period_end,
    description
from {{ source('raw', 'invoice_line_items') }}
