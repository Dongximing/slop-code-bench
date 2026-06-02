#!/usr/bin/env python3
"""Unit tests for market_tools.py functions"""
import gzip

from market_tools import (
    calculate_volume_weighted_average_price,
    filter_outlier_orders,
    parse_gzip_csv,
    parse_station_gzip_csv,
)


def _test_price_csv_parsing():
    csv_str = (
        "order_id,duration,is_buy_order,issued,location_id,min_volume,price,range,"
        "system_id,type_id,volume_remain,volume_total\n"
        "1,30,False,2024-01-01T00:00:00,60000043,1,100.0,station,30000001,34,"
        "1000,1000\n"
        "2,30,True,2024-01-01T00:00:00,60000043,1,95.0,station,30000001,34,800,800\n"
    )
    gzipped = gzip.compress(csv_str.encode("utf-8"))
    fields = [
        "order_id",
        "duration",
        "is_buy_order",
        "issued",
        "location_id",
        "min_volume",
        "price",
        "range",
        "system_id",
        "type_id",
        "volume_remain",
        "volume_total",
    ]
    rows = parse_gzip_csv(gzipped, fields)
    assert len(rows) == 2, f"Expected 2 rows, got {len(rows)}"
    assert rows[0]["is_buy_order"] == "False"
    assert rows[1]["order_id"] == "2"
    print("  ✓ Price CSV parsing works")


def _test_station_csv_parsing():
    csv_str = (
        "location_id,type,name\n"
        "60000043,Station,Jita IV - Moon 4 - Caldari Navy Assembly Plant\n"
        "60008494,Station,Amarr VIII (Oris) - Emperor Family Academy\n"
    )
    gzipped = gzip.compress(csv_str.encode("utf-8"))
    rows = parse_station_gzip_csv(gzipped)
    assert len(rows) == 2, f"Expected 2 rows, got {len(rows)}"
    assert rows[0]["location_id"] == "60000043"
    assert "Jita" in rows[0]["name"]
    print("  ✓ Station CSV parsing works")


def _test_outlier_filtering():
    orders = [
        {"is_buy_order": True, "price": 100.0, "volume_remain": 100},
        {"is_buy_order": True, "price": 95.0, "volume_remain": 100},
        {"is_buy_order": True, "price": 500.0, "volume_remain": 100},  # Outlier
        {"is_buy_order": True, "price": 85.0, "volume_remain": 100},
    ]
    filtered = filter_outlier_orders(orders, True)
    assert len(filtered) == 3, f"Expected 3 orders after filtering, got {len(filtered)}"
    assert not any(o["price"] == 500.0 for o in filtered)
    print("  ✓ Buy order outlier filtering works")

    sell_orders = [
        {"is_buy_order": False, "price": 10.0, "volume_remain": 100},
        {"is_buy_order": False, "price": 15.0, "volume_remain": 100},
        {"is_buy_order": False, "price": 1000.0, "volume_remain": 100},  # Outlier
    ]
    filtered = filter_outlier_orders(sell_orders, False)
    assert len(filtered) == 2, f"Expected 2 orders after filtering, got {len(filtered)}"
    assert not any(o["price"] == 1000.0 for o in filtered)
    print("  ✓ Sell order outlier filtering works")


def _test_volume_weighted_price():
    orders = [
        {"is_buy_order": True, "price": 100.0, "volume_remain": 100},
        {"is_buy_order": True, "price": 90.0, "volume_remain": 50},
        {"is_buy_order": True, "price": 80.0, "volume_remain": 350},
    ]
    result = calculate_volume_weighted_average_price(orders)
    assert result == 100.0, f"Expected 100.0, got {result}"

    result = calculate_volume_weighted_average_price([])
    assert result == 0.0, f"Expected 0.0 for empty list, got {result}"
    print("  ✓ Volume weighted average price calculation works")


def run_all_tests():
    print("=" * 60)
    print("Running unit tests for market_tools")
    print("=" * 60)
    _test_price_csv_parsing()
    _test_station_csv_parsing()
    _test_outlier_filtering()
    _test_volume_weighted_price()
    print("\n" + "=" * 60)
    print("ALL TESTS PASSED!")
    print("=" * 60)


if __name__ == "__main__":
    run_all_tests()
