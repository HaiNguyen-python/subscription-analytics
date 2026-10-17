select
    id as refund_id,
    invoice as invoice_id,
    amount / 100.0 as amount,
    lower(currency) as currency,
    created as refunded_at,
    reason
from {{ source('raw', 'refunds') }}
