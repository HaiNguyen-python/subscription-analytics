-- Daily calendar that dbt's semantic layer (MetricFlow) requires. Covers the FX months.
select cast(d.date_day as date) as date_day
from (
    select unnest(generate_series(
        (select min(month_start) from {{ ref('stg_fx_rates') }})::timestamp,
        (select max(month_start) from {{ ref('stg_fx_rates') }})::timestamp + interval 1 month
        - interval 1 day,
        interval 1 day
    )) as date_day
) as d
