schema BaseSale {
  timestamp: timestamp
  location: string
  item: string
  price: float
  quantity: float
  unit: string
  category: string
}

schema PromoSale extends BaseSale {
  quantity: float
  promo_code: string
  revenue: float = price * quantity
}

pipeline SalesSummary using PromoSale {
  filter(price >= 0.0 & starts_with(promo_code, 'Q3-'))
  window(1h)
  group_by(['location', 'category'])
  aggregate sum(price) as total_price, sum(revenue) as total_revenue, count(*) as n
}
