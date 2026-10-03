select
    o.customer_id,
    sum(o.amount * r.rate) as revenue_usd
from {{ ref('stg_orders') }} o
left join raw.exchange_rates r
    on r.currency = 'INR'
   and r.rate_date = o.order_date
group by o.customer_id
