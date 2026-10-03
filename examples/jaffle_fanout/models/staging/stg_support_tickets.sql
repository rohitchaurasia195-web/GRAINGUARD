select
    id as ticket_id,
    customer_id,
    opened_at,
    minutes_spent
from {{ source('shop', 'support_tickets') }}
