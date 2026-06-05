# Simple aggregate without group_by or window
aggregate
    sum(price) as total_revenue,
    average(price) as avg_price,
    count(*) as n
