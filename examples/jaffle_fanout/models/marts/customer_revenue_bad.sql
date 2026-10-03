select
    o.customer_id,
    sum(o.amount)                 as revenue,
    count(*)                      as order_count,
    sum(p.amount)                 as amount_paid,
    count(distinct o.order_id)    as distinct_orders,
    max(o.order_date)             as last_order_date
from {{ ref('stg_orders') }} o
left join {{ ref('stg_payments') }} p
    on p.order_id = o.order_id
group by o.customer_id
