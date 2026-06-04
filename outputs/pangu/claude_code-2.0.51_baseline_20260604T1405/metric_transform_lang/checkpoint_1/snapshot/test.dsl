# Simple aggregation without grouping or window
aggregate sum(price) as total_revenue,
        average(price) as avg_price,
        count(*) as n
