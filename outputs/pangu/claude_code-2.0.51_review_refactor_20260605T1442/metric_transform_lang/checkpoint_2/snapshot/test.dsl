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
  params {
    price_min: float = 0.0
    promo_prefix: string = 'Q3-'
  }

  filter(price >= param("price_min") & promo_code starts_with param("promo_prefix"))
  window(1h)
  group_by(['location', 'category'])
  aggregate sum(price) as total_price,
            sum(revenue) as total_revenue,
            count(*) as n
}
