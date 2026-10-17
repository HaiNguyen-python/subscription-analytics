-- One row per Stripe customer. The email is dropped here on purpose: no metric needs it,
-- so it never reaches the analytics schema (data minimisation).
select
    id as customer_id,
    lower(currency) as currency,
    country,
    created as created_at
from {{ source('raw', 'customers') }}
