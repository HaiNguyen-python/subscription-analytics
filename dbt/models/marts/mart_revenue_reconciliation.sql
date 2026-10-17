-- MRR checked against what Stripe actually invoiced, per month (all EUR).
--
-- 1. Billed MRR (exact). Take every invoice line whose service period covers the month-end
--    instant, turn it into a monthly amount, and add them up. It must equal MRR except for
--    two explained differences:
--      one_time_discounts  once coupons lower the invoice but not MRR
--      past_due_unpaid     past_due subscriptions count in MRR, their invoice is unpaid
--    `unexplained` must be zero (dbt/tests/assert_mrr_reconciles_to_billing.sql).
--    A proration line counts as its price's full monthly amount with its sign: after an
--    upgrade, old line + credit (-old) + charge (+new) = new MRR.
--
-- 2. Recognised revenue (tolerance). Paid invoice lines spread over their service period by
--    the second, minus refunds in the month they are paid out. Daily spreading gives February
--    fewer days than an average month, so recurring revenue is calendar-normalised
--    (x 365 / 12 / days in month) and compared with the average of opening and closing MRR
--    (dbt/tests/assert_revenue_within_tolerance.sql).
with months as (
    select
        month_start,
        month_start + interval 1 month as next_month_start,
        date_diff('day', month_start, month_start + interval 1 month) as days_in_month
    from {{ ref('stg_fx_rates') }}
    where currency = 'eur'
),

lines as (
    select
        l.*,
        i.invoice_status,
        i.customer_id,
        i.currency,
        p.unit_amount as price_unit_amount,
        p.interval_months,
        coalesce(d.duration = 'once', false) as has_once_coupon
    from {{ ref('stg_invoice_line_items') }} as l
    inner join {{ ref('stg_invoices') }} as i using (invoice_id)
    inner join {{ ref('stg_prices') }} as p using (price_id)
    left join {{ ref('stg_discounts') }} as d on l.subscription_id = d.subscription_id
    where i.invoice_status != 'void'
),

covering as (
    select
        m.month_start,
        lines.*,
        case
            when lines.is_proration
                then sign(lines.amount) * lines.price_unit_amount / lines.interval_months
                * (1 - round(lines.discount_amount / lines.amount, 2))
            else lines.net_amount / lines.interval_months
        end as monthly_amount
    from lines
    inner join months as m
        on lines.period_start < m.next_month_start and lines.period_end >= m.next_month_start
),

status_at_month_end as (
    select month_start, subscription_id, status
    from {{ ref('int_subscription_states') }} as st
    inner join months as m
        on st.valid_from < m.next_month_start
        and (st.valid_to is null or st.valid_to >= m.next_month_start)
),

billed as (
    select
        c.month_start,
        sum(c.monthly_amount * fx.rate_to_eur) filter (
            where c.invoice_status = 'paid'
        ) as billed_mrr_eur,
        sum(c.discount_amount / c.interval_months * fx.rate_to_eur) filter (
            where c.invoice_status = 'paid' and c.has_once_coupon and not c.is_proration
        ) as one_time_discounts_eur,
        sum(c.monthly_amount * fx.rate_to_eur) filter (
            where c.invoice_status in ('open', 'uncollectible') and s.status = 'past_due'
        ) as past_due_unpaid_eur
    from covering as c
    inner join {{ ref('stg_fx_rates') }} as fx
        on c.month_start = fx.month_start and c.currency = fx.currency
    left join status_at_month_end as s
        on c.month_start = s.month_start and c.subscription_id = s.subscription_id
    group by 1
),

recognised as (
    select
        m.month_start,
        sum(
            l.net_amount * fx.rate_to_eur
            * date_diff('second', greatest(l.period_start, m.month_start),
                        least(l.period_end, m.next_month_start))
            / date_diff('second', l.period_start, l.period_end)
        ) filter (where not l.is_proration) as recurring_revenue_eur,
        coalesce(sum(
            l.net_amount * fx.rate_to_eur
            * date_diff('second', greatest(l.period_start, m.month_start),
                        least(l.period_end, m.next_month_start))
            / date_diff('second', l.period_start, l.period_end)
        ) filter (where l.is_proration), 0) as proration_revenue_eur
    from lines as l
    inner join months as m
        on l.period_start < m.next_month_start and l.period_end > m.month_start
    inner join {{ ref('stg_fx_rates') }} as fx
        on m.month_start = fx.month_start and l.currency = fx.currency
    where l.invoice_status = 'paid' and l.period_end > l.period_start
    group by 1
),

refunds as (
    select date_trunc('month', r.refunded_at)::date as month_start,
        -sum(r.amount * fx.rate_to_eur) as refunds_eur
    from {{ ref('stg_refunds') }} as r
    inner join {{ ref('stg_fx_rates') }} as fx
        on date_trunc('month', r.refunded_at)::date = fx.month_start
        and r.currency = fx.currency
    group by 1
)

select
    mm.month_start,
    m.days_in_month,
    mm.starting_mrr_eur,
    mm.ending_mrr_eur as mrr_eur,
    coalesce(b.billed_mrr_eur, 0) as billed_mrr_eur,
    coalesce(b.one_time_discounts_eur, 0) as one_time_discounts_eur,
    coalesce(b.past_due_unpaid_eur, 0) as past_due_unpaid_eur,
    mm.ending_mrr_eur - coalesce(b.billed_mrr_eur, 0) - coalesce(b.one_time_discounts_eur, 0)
    - coalesce(b.past_due_unpaid_eur, 0) as unexplained_eur,
    coalesce(r.recurring_revenue_eur, 0) as recurring_revenue_eur,
    coalesce(r.proration_revenue_eur, 0) as proration_revenue_eur,
    coalesce(f.refunds_eur, 0) as refunds_eur,
    coalesce(r.recurring_revenue_eur, 0) + coalesce(r.proration_revenue_eur, 0)
    + coalesce(f.refunds_eur, 0) as recognised_revenue_eur,
    coalesce(r.recurring_revenue_eur, 0) * 365.0 / 12 / m.days_in_month
        as recurring_revenue_normalised_eur,
    (mm.starting_mrr_eur + mm.ending_mrr_eur) / 2 as average_mrr_eur
from {{ ref('mart_mrr_monthly') }} as mm
inner join months as m using (month_start)
left join billed as b using (month_start)
left join recognised as r using (month_start)
left join refunds as f using (month_start)
