#!/usr/bin/env python3
"""
Unit tests for market_tools.py functions
"""

import gzip
import csv
import io

# Add current directory to path
import sys
sys.path.insert(0, '/workspace')

from market_tools import (
    parse_gzip_csv,
    parse_station_gzip_csv,
    filter_outlier_orders,
    calculate_volume_weighted_average_price
)

def test_price_csv_parsing():
    """Test parsing gzipped CSV for prices."""
    print("Test 1: Price CSV Parsing")

    # Create a test CSV
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['order_id', 'duration', 'is_buy_order', 'issued', 'location_id',
                     'min_volume', 'price', 'range', 'system_id', 'type_id',
                     'volume_remain', 'volume_total'])
    writer.writerow([1, 30, 'False', '2024-01-01T00:00:00', 60000043, 1, 100.0, 'station', 30000001, 34, 1000, 1000])
    writer.writerow([2, 30, 'True', '2024-01-01T00:00:00', 60000043, 1, 95.0, 'station', 30000001, 34, 800, 800])

    csv_string = output.getvalue()
    gzipped = gzip.compress(csv_string.encode('utf-8'))

    rows = parse_gzip_csv(gzipped, [
        'order_id', 'duration', 'is_buy_order', 'issued', 'location_id',
        'min_volume', 'price', 'range', 'system_id', 'type_id',
        'volume_remain', 'volume_total'
    ])

    assert len(rows) == 2, f"Expected 2 rows, got {len(rows)}"
    assert rows[0]['order_id'] == 1
    assert rows[0]['is_buy_order'] == False
    assert rows[1]['order_id'] == 2
    assert rows[1]['is_buy_order'] == True
    print("  ✓ Price CSV parsing works")

def test_station_csv_parsing():
    """Test parsing gzipped CSV for stations."""
    print("Test 2: Station CSV Parsing")

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['location_id', 'type', 'name'])
    writer.writerow([60000043, 'Station', 'Jita IV - Moon 4 - Caldari Navy Assembly Plant'])
    writer.writerow([60008494, 'Station', 'Amarr VIII (Oris) - Emperor Family Academy'])

    csv_string = output.getvalue()
    gzipped = gzip.compress(csv_string.encode('utf-8'))

    rows = parse_station_gzip_csv(gzipped)

    assert len(rows) == 2, f"Expected 2 rows, got {len(rows)}"
    assert rows[0]['location_id'] == 60000043
    assert rows[0]['type'] == 'Station'
    assert 'Jita' in rows[0]['name']
    print("  ✓ Station CSV parsing works")

def test_outlier_filtering():
    """Test filtering outlier orders."""
    print("Test 3: Outlier Filtering")

    # Create test buy orders
    orders = [
        {'is_buy_order': True, 'price': 100.0, 'volume_remain': 100},
        {'is_buy_order': True, 'price': 95.0, 'volume_remain': 100},
        {'is_buy_order': True, 'price': 90.0, 'volume_remain': 100},
        {'is_buy_order': True, 'price': 500.0, 'volume_remain': 100},  # Outlier - way too high
        {'is_buy_order': True, 'price': 85.0, 'volume_remain': 100},
    ]

    filtered = filter_outlier_orders(orders, True)

    # Should filter out the 500.0 price (10% cutoff)
    assert len(filtered) == 4, f"Expected 4 orders after filtering, got {len(filtered)}"
    assert all(o['price'] <= 100 for o in filtered)
    assert not any(o['price'] == 500.0 for o in filtered)
    print("  ✓ Buy order outlier filtering works")

    # Test sell order filtering
    sell_orders = [
        {'is_buy_order': False, 'price': 10.0, 'volume_remain': 100},  # Lowest
        {'is_buy_order': False, 'price': 15.0, 'volume_remain': 100},
        {'is_buy_order': False, 'price': 1000.0, 'volume_remain': 100},  # Outlier - 100x lowest
        {'is_buy_order': False, 'price': 20.0, 'volume_remain': 100},
    ]

    filtered_sell = filter_outlier_orders(sell_orders, False)

    # Should filter out the 1000.0 price (> 10 * 10.0)
    assert len(filtered_sell) == 3, f"Expected 3 orders after filtering, got {len(filtered_sell)}"
    assert not any(o['price'] == 1000.0 for o in filtered_sell)
    print("  ✓ Sell order outlier filtering works")

def test_volume_weighted_price():
    """Test volume weighted average price calculation."""
    print("Test 4: Volume Weighted Average Price")

    # Test buy orders
    orders = [
        {'is_buy_order': True, 'price': 100.0, 'volume_remain': 100},
        {'is_buy_order': True, 'price': 90.0, 'volume_remain': 50},
        {'is_buy_order': True, 'price': 80.0, 'volume_remain': 350},
    ]

    # Total volume: 500, 5% = 25 units
    # Should take from first order: 25 units @ 100 = 25 * 100 / 25 = 100.0
    result = calculate_volume_weighted_average_price(orders)

    assert result == 100.0, f"Expected 100.0, got {result}"
    print("  ✓ Volume weighted average price calculation works")

def test_edge_cases():
    """Test edge cases."""
    print("Test 5: Edge Cases")

    # Empty orders list
    result = calculate_volume_weighted_average_price([])
    assert result == 0.0, f"Expected 0.0 for empty list, got {result}"

    # Single order
    orders = [{'is_buy_order': True, 'price': 50.0, 'volume_remain': 100}]
    result = calculate_volume_weighted_average_price(orders)
    assert result == 50.0, f"Expected 50.0, got {result}"

    print("  ✓ Edge cases handled correctly")

def run_all_tests():
    """Run all tests."""
    print("=" * 60)
    print("Running unit tests for market_tools")
    print("=" * 60)

    test_price_csv_parsing()
    test_station_csv_parsing()
    test_outlier_filtering()
    test_volume_weighted_price()
    test_edge_cases()

    print("\n" + "=" * 60)
    print("ALL TESTS PASSED!")
    print("=" * 60)

if __name__ == "__main__":
    run_all_tests()
