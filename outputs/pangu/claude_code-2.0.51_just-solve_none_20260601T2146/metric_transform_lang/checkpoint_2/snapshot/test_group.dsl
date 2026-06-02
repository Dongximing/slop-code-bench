group_by(location)
aggregate sum(price) as total_revenue, count(*) as n, average(price) as avg_price
