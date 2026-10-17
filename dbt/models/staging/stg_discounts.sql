-- One row per discount (a coupon applied to a subscription). Only repeating and forever
-- coupons change MRR; a once coupon is a one-off price cut on the first invoice.
select
    id as discount_id,
    subscription as subscription_id,
    customer as customer_id,
    make_timestamp(start * 1000000) as starts_at,
    make_timestamp("end" * 1000000) as ends_at,
    coupon.id as coupon_id,
    coupon.percent_off as percent_off,
    coupon.duration as duration,
    coupon.duration in ('repeating', 'forever') as counts_in_mrr
from {{ source('raw', 'discounts') }}
