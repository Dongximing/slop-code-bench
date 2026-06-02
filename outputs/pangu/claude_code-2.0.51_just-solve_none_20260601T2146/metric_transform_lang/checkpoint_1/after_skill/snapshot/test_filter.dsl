filter(price > 1.0)
aggregate sum(price) as total_revenue, count(*) as n
