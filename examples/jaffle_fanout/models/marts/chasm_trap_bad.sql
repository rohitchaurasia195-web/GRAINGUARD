select
    c.region,
    sum(o.amount)          as revenue,
    sum(t.minutes_spent)   as support_minutes
from {{ ref('stg_customers') }} c
left join {{ ref('stg_orders') }} o
    on o.customer_id = c.customer_id
left join {{ ref('stg_support_tickets') }} t
    on t.customer_id = c.customer_id
group by c.region
