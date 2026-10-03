with payments_per_order as (
    select
        order_id,
        sum(amount) as amount_paid,
        count(*)    as payment_count
    from {{ ref('stg_payments') }}
    group by order_id
)

select
    o.customer_id,
    sum(o.amount)              as revenue,
    count(*)                   as order_count,
    sum(ppo.amount_paid)       as amount_paid,
    sum(ppo.payment_count)     as payment_count
from {{ ref('stg_orders') }} o
left join payments_per_order ppo
    on ppo.order_id = o.order_id
group by o.customer_id
