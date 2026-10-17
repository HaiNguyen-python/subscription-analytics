select
    id as invoice_id,
    customer as customer_id,
    subscription as subscription_id,
    created as created_at,
    status as invoice_status,
    lower(currency) as currency,
    billing_reason,
    total / 100.0 as total,
    amount_paid / 100.0 as amount_paid
from {{ source('raw', 'invoices') }}
