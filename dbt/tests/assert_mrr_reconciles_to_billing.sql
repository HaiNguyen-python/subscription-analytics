-- MRR equals billed MRR plus the explained differences, to the cent, every month.
select *
from {{ ref('mart_revenue_reconciliation') }}
where abs(unexplained_eur) > {{ var('billing_tolerance_eur') }}
