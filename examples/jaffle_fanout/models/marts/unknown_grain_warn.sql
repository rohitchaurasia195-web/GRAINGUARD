select
    c.region,
    count(*) as customers_with_events
from {{ ref('stg_customers') }} c
join {{ source('shop', 'web_events') }} e
    on e.customer_id = c.customer_id
group by c.region
