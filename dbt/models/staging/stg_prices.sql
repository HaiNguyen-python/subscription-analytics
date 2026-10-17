-- One row per price, with the product expanded. `interval_months` is what turns any billing
-- interval into a monthly amount: an annual price counts 1/12 of its amount as MRR.
select
    id as price_id,
    product.id as product_id,
    product.name as product_name,
    product.tier as plan_tier,
    recurring.interval as plan_interval,
    lower(currency) as currency,
    unit_amount / 100.0 as unit_amount,
    case recurring.interval
        when 'month' then recurring.interval_count
        when 'year' then 12 * recurring.interval_count
    end as interval_months
from {{ source('raw', 'prices') }}
