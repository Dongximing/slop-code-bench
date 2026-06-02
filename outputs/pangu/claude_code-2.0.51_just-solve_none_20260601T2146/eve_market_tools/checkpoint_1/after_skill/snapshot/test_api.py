#!/usr/bin/env python3
"""
Test script for market_tools API
"""

import gzip
import csv
import io
import requests
import time
import json

BASE_URL = "http://127.0.0.1:8000"

def create_test_price_csv():
    """Create test price data."""
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['order_id', 'duration', 'is_buy_order', 'issued', 'location_id',
                     'min_volume', 'price', 'range', 'system_id', 'type_id',
                     'volume_remain', 'volume_total'])

    # Add some test orders
    writer.writerow([1, 30, 'False', '2024-01-01T00:00:00', 60000043, 1, 100.0, 'station', 30000001, 34, 1000, 1000])
    writer.writerow([2, 30, 'False', '2024-01-01T00:00:00', 60000043, 1, 105.0, 'station', 30000001, 34, 500, 500])
    writer.writerow([3, 30, 'True', '2024-01-01T00:00:00', 60000043, 1, 95.0, 'station', 30000001, 34, 800, 800])
    writer.writerow([4, 30, 'True', '2024-01-01T00:00:00', 60000043, 1, 90.0, 'station', 30000001, 34, 1200, 1200])

    return gzip.compress(output.getvalue().encode('utf-8'))

def create_test_station_csv():
    """Create test station data."""
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['location_id', 'type', 'name'])

    writer.writerow([60000043, 'Station', 'Jita IV - Moon 4 - Caldari Navy Assembly Plant'])
    writer.writerow([60008494, 'Station', 'Amarr VIII (Oris) - Emperor Family Academy'])

    return gzip.compress(output.getvalue().encode('utf-8'))

def test_api():
    """Run API tests."""
    print("Testing Market Tools API...")

    # Test 1: Ingest prices
    print("\n1. Testing POST /v1/prices...")
    price_data = create_test_price_csv()
    response = requests.post(f"{BASE_URL}/v1/prices?market=jita&mode=replace", data=price_data)
    print(f"   Status: {response.status_code}")
    print(f"   Response: {response.json()}")

    # Test 2: Ingest stations
    print("\n2. Testing POST /v1/stations...")
    station_data = create_test_station_csv()
    response = requests.post(f"{BASE_URL}/v1/stations", data=station_data)
    print(f"   Status: {response.status_code}")
    print(f"   Response: {response.json()}")

    # Test 3: Get market stats
    print("\n3. Testing GET /v1/market/10000002 (The Forge)...")
    response = requests.get(f"{BASE_URL}/v1/market/10000002")
    print(f"   Status: {response.status_code}")
    print(f"   Response: {json.dumps(response.json(), indent=2)}")

    # Test 4: Get market stats with type_ids
    print("\n4. Testing GET /v1/market/10000002?type_ids=34...")
    response = requests.get(f"{BASE_URL}/v1/market/10000002?type_ids=34")
    print(f"   Status: {response.status_code}")
    print(f"   Response: {json.dumps(response.json(), indent=2)}")

    # Test 5: Get type market stats
    print("\n5. Testing GET /v1/market/10000002/34...")
    response = requests.get(f"{BASE_URL}/v1/market/10000002/34")
    print(f"   Status: {response.status_code}")
    print(f"   Response: {json.dumps(response.json(), indent=2)}")

    # Test 6: Get hub compare
    print("\n6. Testing GET /v1/hub-compare/34...")
    response = requests.get(f"{BASE_URL}/v1/hub-compare/34")
    print(f"   Status: {response.status_code}")
    print(f"   Response: {json.dumps(response.json(), indent=2)}")

    print("\nAll tests completed!")

if __name__ == "__main__":
    # Wait for server to start
    time.sleep(2)
    test_api()
