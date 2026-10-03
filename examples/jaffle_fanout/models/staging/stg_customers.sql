select
    id as customer_id,
    first_name,
    last_name,
    region
from {{ source('shop', 'customers') }}
