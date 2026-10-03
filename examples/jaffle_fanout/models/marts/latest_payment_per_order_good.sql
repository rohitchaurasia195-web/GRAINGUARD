with latest_payment as (
    select
        order_id,
        payment_method,
        amount
    from {{ ref('stg_payments') }}
    qualify row_number() over (partition by order_id order by payment_id desc) = 1
)

select
    o.customer_id,
    sum(o.amount)   as revenue,
    sum(lp.amount)  as latest_payment_amount
from {{ ref('stg_orders') }} o
left join latest_payment lp
    on lp.order_id = o.order_id
group by o.customer_id
