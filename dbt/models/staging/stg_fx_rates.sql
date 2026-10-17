-- Monthly average rates to EUR, the reporting currency. These months are also the
-- reporting calendar: a month is reported only if its rates are known.
select
    month as month_start,
    lower(currency) as currency,
    rate_to_eur
from {{ source('raw', 'fx_rates') }}
