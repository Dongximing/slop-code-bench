#!/usr/bin/env python3
"""
Test script for market_tools.py API endpoints.
"""

import gzip
import csv
import io
import requests
import json

API_URL = "http://127.0.0.1:5000"

def create_test_price_csv():
    """Create test price data CSV."""
    output = io.StringIO()
    writer = csv.writer(output)

    # Headers
    writer.writerow([
        'order_id', 'duration', 'is_buy_order', 'issued', 'location_id',
        'min_volume', 'price', 'range', 'system_id', 'type_id',
        'volume_remain', 'volume_total'
    ])

    # Sample orders - using known station IDs from Jita region
    writer.writerow([1001, 30, False, "2024-01-01T00:00:00", 60003761, 1, 100.0, "station", 30000142, 34, 1000, 1000])
    writer.writerow([1002, 30, False, "2024-01-01T00:00:00", 60003761, 1, 102.0, "station", 30000142, 34, 500, 500])
    writer.writerow([1003, 30, True, "2024-01-01T00:00:00", 60003761, 1, 95.0, "station", 30000142, 34, 200, 200])
    writer.writerow([1004, 30, True, "2024-01-01T00:00:00", 60003761, 1, 93.0, "station", 30000142, 34, 300, 300])

    # Different type
    writer.writerow([1005, 30, False, "2024-01-01T00:00:00", 60003761, 1, 50.0, "station", 30000142, 35, 800, 800])
    writer.writerow([1006, 30, False, "2024-01-01T00:00:00", 60003761, 1, 52.0, "station", 30000142, 35, 600, 600])

    return gzip.compress(output.getvalue().encode())


def create_test_station_csv():
    """Create test station data CSV."""
    output = io.StringIO()
    writer = csv.writer(output)

    writer.writerow(['location_id', 'type', 'name'])
    writer.writerow([60003761, 'Station', 'Jita IV - Moon 4 - Caldari Navy Assembly Plant'])
    writer.writerow([60008494, 'Station', 'Amarr VIII (Oris) - Emperor Family Academy'])
    writer.writerow([60014718, 'Station', 'Dodixie IX - Moon 20 - Federation Navy Assembly Plant'])

    return gzip.compress(output.getvalue().encode())


def test_price_ingestion():
    """Test price ingestion endpoint."""
    print("\n=== Testing Price Ingestion ===")

    # Test invalid format - no data
    response = requests.post(f"{API_URL}/v1/prices", data=b'')
    print(f"Empty data: {response.status_code} - {response.json()}")

    # Test invalid format - bad gzip
    response = requests.post(f"{API_URL}/v1/prices", data=b'not gzip')
    print(f"Bad gzip: {response.status_code} - {response.json()}")

    # Test valid price ingestion
    data = create_test_price_csv()
    response = requests.post(
        f"{API_URL}/v1/prices",
        data=data,
        headers={'Content-Type': 'application/octet-stream'},
        params={'market': 'test_market'}
    )
    print(f"Price ingestion: {response.status_code} - {response.json()}")

    return response.json()


def test_station_ingestion():
    """Test station ingestion endpoint."""
    print("\n=== Testing Station Ingestion ===")

    data = create_test_station_csv()
    response = requests.post(
        f"{API_URL}/v1/stations",
        data=data,
        headers={'Content-Type': 'application/octet-stream'}
    )
    print(f"Station ingestion: {response.status_code} - {response.json()}")

    return response.json()


def test_market_region():
    """Test market region endpoint."""
    print("\n=== Testing Market Region ===")

    # Test basic region query
    response = requests.get(f"{API_URL}/v1/market/10000002")  # The Forge
    print(f"Region stats: {response.status_code} - {json.dumps(response.json(), indent=2)}")

    # Test with type_ids
    response = requests.get(
        f"{API_URL}/v1/market/10000002",
        params={'type_ids': '34,35'}
    )
    print(f"Region with types: {response.status_code} - {json.dumps(response.json(), indent=2)}")

    # Test with hubs
    response = requests.get(
        f"{API_URL}/v1/market/10000002",
        params={'hubs': '1'}
    )
    print(f"Region with hubs: {response.status_code} - {json.dumps(response.json(), indent=2)}")


def test_market_type():
    """Test market type endpoint."""
    print("\n=== Testing Market Type ===")

    response = requests.get(f"{API_URL}/v1/market/10000002/34")
    print(f"Type stats: {response.status_code} - {json.dumps(response.json(), indent=2)}")


def test_hub_compare():
    """Test hub comparison endpoint."""
    print("\n=== Testing Hub Compare ===")

    response = requests.get(f"{API_URL}/v1/hub-compare/34")
    print(f"Hub compare: {response.status_code} - {json.dumps(response.json(), indent=2)}")

    # Test unknown type
    response = requests.get(f"{API_URL}/v1/hub-compare/99999999")
    print(f"Hub compare unknown: {response.status_code} - {response.json()}")


def run_all_tests():
    """Run all tests."""
    print("\n" + "="*60)
    print("Running Market Tools API Tests")
    print("="*60)

    try:
        test_station_ingestion()
        test_price_ingestion()
        test_market_region()
        test_market_type()
        test_hub_compare()

        print("\n" + "="*60)
        print("All tests completed!")
        print("="*60 + "\n")

    except Exception as e:
        print(f"\nError running tests: {e}")
        raise


if __name__ == '__main__':
    run_all_tests()
