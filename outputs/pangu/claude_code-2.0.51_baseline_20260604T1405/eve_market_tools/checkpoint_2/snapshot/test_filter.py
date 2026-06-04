#!/usr/bin/env python3
"""Test outlier filtering."""

import sys
sys.path.insert(0, '/workspace')

from market_tools import filter_outlier_orders, Order
from datetime import datetime

# Test the actual function
orders = [
    Order(1, 30, True, datetime.fromisoformat("2024-01-01T00:00:00+00:00"), 60003760, 1, 9.5, "", 30000146, 34, 1000, 1000),
    Order(2, 30, True, datetime.fromisoformat("2024-01-01T00:00:00+00:00"), 60003760, 1, 9.0, "", 30000146, 34, 2000, 2000),
    Order(3, 30, True, datetime.fromisoformat("2024-01-01T00:00:00+00:00"), 60003760, 1, 8.5, "", 30000146, 34, 1500, 1500),
    Order(4, 30, True, datetime.fromisoformat("2024-01-01T00:00:00+00:00"), 60003760, 1, 8.0, "", 30000146, 34, 500, 500),
    Order(5, 30, True, datetime.fromisoformat("2024-01-01T00:00:00+00:00"), 60003760, 1, 20.0, "", 30000146, 34, 500, 500),
]

print("Buy orders test (5 orders, 10% = 0.5 -> ceiling 1 order removed):")
print(f"Original prices: {[o.price for o in orders]}")
filtered = filter_outlier_orders(orders, True)
print(f"Filtered prices: {[o.price for o in filtered]}")
print(f"Expected: Remove highest one (20.0)")
print(f"Test result: {'PASS' if 20.0 not in [o.price for o in filtered] else 'FAIL'}")
print()

# Test sell orders
sell_orders = [
    Order(1, 30, False, datetime.fromisoformat("2024-01-01T00:00:00+00:00"), 60003760, 1, 10.0, "", 30000146, 34, 1000, 1000),
    Order(2, 30, False, datetime.fromisoformat("2024-01-01T00:00:00+00:00"), 60003760, 1, 10.5, "", 30000146, 34, 2000, 2000),
    Order(3, 30, False, datetime.fromisoformat("2024-01-01T00:00:00+00:00"), 60003760, 1, 11.0, "", 30000146, 34, 1500, 1500),
    Order(4, 30, False, datetime.fromisoformat("2024-01-01T00:00:00+00:00"), 60003760, 1, 100.0, "", 30000146, 34, 500, 500),
]

print("Sell orders test:")
print(f"Original prices: {[o.price for o in sell_orders]}")
filtered_sell = filter_outlier_orders(sell_orders, False)
print(f"Filtered prices: {[o.price for o in filtered_sell]}")
print(f"Expected: Remove 100.0 (10x 10.0 = 100.0)")
print(f"Test result: {'PASS' if 100.0 not in [o.price for o in filtered_sell] else 'FAIL'}")

# Test with 15 orders
orders_many = [Order(i, 30, True, datetime.fromisoformat("2024-01-01T00:00:00+00:00"), 60003760, 1, 10.0 + i, "", 30000146, 34, 100, 100) for i in range(15)]
# Set the last one to be an outlier
orders_many[-1] = Order(15, 30, True, datetime.fromisoformat("2024-01-01T00:00:00+00:00"), 60003760, 1, 100.0, "", 30000146, 34, 100, 100)

print("\nBuy orders test (15 orders, 10% = 1.5 -> ceiling 2 orders removed):")
print(f"Original count: {len(orders_many)}")
filtered_many = filter_outlier_orders(orders_many, True)
print(f"Filtered count: {len(filtered_many)}")
print(f"Expected: Remove highest 2 (100.0 and 24.0)")
print(f"Test result: {'PASS' if len(filtered_many) == 13 else 'FAIL'}")
