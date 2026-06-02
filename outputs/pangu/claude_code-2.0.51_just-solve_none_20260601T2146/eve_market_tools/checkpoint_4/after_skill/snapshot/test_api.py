#!/usr/bin/env python3
"""Tests for market_tools API endpoints."""
import csv
import gzip
import io
import requests

BASE_URL = "http://127.0.0.1:8000"


def _create_price_csv():
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['order_id', 'duration', 'is_buy_order', 'issued', 'location_id',
                     'min_volume', 'price', 'range', 'system_id', 'type_id',
                     'volume_remain', 'volume_total'])
    for order in [
        [1, 30, 'False', '2024-01-01T00:00:00', 60000043, 1, 100.0, 'station', 30000001, 34, 1000, 1000],
        [2, 30, 'False', '2024-01-01T00:00:00', 60000043, 1, 105.0, 'station', 30000001, 34, 500, 500],
        [3, 30, 'True', '2024-01-01T00:00:00', 60000043, 1, 95.0, 'station', 30000001, 34, 800, 800],
        [4, 30, 'True', '2024-01-01T00:00:00', 60000043, 1, 90.0, 'station', 30000001, 34, 1200, 1200],
    ]:
        writer.writerow(order)
    return gzip.compress(output.getvalue().encode('utf-8'))


def _create_station_csv():
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['location_id', 'type', 'name'])
    writer.writerow([60000043, 'Station', 'Jita IV - Moon 4 - Caldari Navy Assembly Plant'])
    writer.writerow([60008494, 'Station', 'Amarr VIII (Oris) - Emperor Family Academy'])
    return gzip.compress(output.getvalue().encode('utf-8'))


def run_tests():
    print("Testing Market Tools API...")

    print("\n1. Testing POST /v1/prices...")
    r = requests.post(f"{BASE_URL}/v1/prices?market=jita&mode=replace", data=_create_price_csv())
    print(f"   Status: {r.status_code}, Response: {r.json()}")

    print("\n2. Testing POST /v1/stations...")
    r = requests.post(f"{BASE_URL}/v1/stations", data=_create_station_csv())
    print(f"   Status: {r.status_code}, Response: {r.json()}")

    print("\n3. Testing GET /v1/market/10000002 (The Forge)...")
    r = requests.get(f"{BASE_URL}/v1/market/10000002")
    print(f"   Status: {r.status_code}, Response: {r.json()}")

    print("\n4. Testing GET /v1/market/10000002?type_ids=34...")
    r = requests.get(f"{BASE_URL}/v1/market/10000002?type_ids=34")
    print(f"   Status: {r.status_code}, Response: {r.json()}")

    print("\n5. Testing GET /v1/market/10000002/34...")
    r = requests.get(f"{BASE_URL}/v1/market/10000002/34")
    print(f"   Status: {r.status_code}, Response: {r.json()}")

    print("\n6. Testing GET /v1/hub-compare/34...")
    r = requests.get(f"{BASE_URL}/v1/hub-compare/34")
    print(f"   Status: {r.status_code}, Response: {r.json()}")

    print("\nAll tests completed!")


if __name__ == "__main__":
    import time
    time.sleep(2)
    run_tests()
