# With group_by
aggregate
    sum(price) as total_revenue,
    average(price) as avg_price,
    count(*) as n
group_by(category)
