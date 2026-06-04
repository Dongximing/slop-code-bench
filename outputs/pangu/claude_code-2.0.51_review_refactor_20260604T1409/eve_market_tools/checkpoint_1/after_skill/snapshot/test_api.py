#!/usr/bin/env python3
"""
Test script for market_tools.py API.
Tests all endpoints with mock data.
"""

import gzip
import io
import csv
import json
import requests

BASE_URL = "http://127.0.0.1:5000"

def create_test_orders_csv():
    """Create a gzipped CSV of test orders."""
    output = io.BytesIO()
    string_buffer = io.StringIO()
    writer = csv.writer(string_buffer)
    # Header row
    writer.writerow([
        'order_id', 'duration', 'is_buy_order', 'issued', 'location_id',
        'min_volume', 'price', 'range', 'system_id', 'type_id',
        'volume_remain', 'volume_total'
    ])
    # Test buy orders
    writer.writerow([1, 30, True, '2024-01-01T00:00:00Z', 60000361, 1, 100.0, 'station', 30000142, 34, 100, 1000])
    writer.writerow([2, 30, True, '2024-01-01T00:00:00Z', 60000361, 1, 95.0, 'station', 30000142, 34, 50, 500])
    writer.writerow([3, 30, True, '2024-01-01T00:00:00Z', 60000361, 1, 90.0, 'station', 30000142, 34, 200, 500])
    # Test sell orders
    writer.writerow([4, 30, False, '2024-01-01T00:00:00Z', 60000361, 1, 110.0, 'station', 30000142, 34, 150, 150])
    writer.writerow([5, 30, False, '2024-01-01T00:00:00Z', 60000361, 1, 115.0, 'station', 30000142, 34, 75, 100])
    writer.writerow([6, 30, False, '2024-01-01T00:00:00Z', 60000361, 1, 105.0, 'station', 30000142, 34, 300, 300])

    csv_data = string_buffer.getvalue().encode('utf-8')
    with gzip.GzipFile(fileobj=output, mode='wb') as f:
        f.write(csv_data)
    return output.getvalue()


def create_test_stations_csv():
    """Create a gzipped CSV of test stations."""
    output = io.BytesIO()
    string_buffer = io.StringIO()
    writer = csv.writer(string_buffer)
    writer.writerow(['location_id', 'type', 'name'])
    writer.writerow([60000361, 'Station', 'Jita IV - Moon 6 - Ytiri Storage'])
    writer.writerow([60000364, 'Station', 'Jita V - Moon 14 - Ytiri Storage'])
    writer.writerow([60000274, 'Structure', 'Test Structure'])

    csv_data = string_buffer.getvalue().encode('utf-8')
    with gzip.GzipFile(fileobj=output, mode='wb') as f:
        f.write(csv_data)
    return output.getvalue()


def test_endpoint(method, path, data=None, params=None):
    """Test a single endpoint."""
    url = f"{BASE_URL}{path}"
    headers = {'Content-Type': 'application/octet-stream'}

    print(f"\n=== Testing {method} {path} ===")
    print(f"Params: {params}")

    try:
        if data:
            response = requests.request(method, url, headers=headers, data=data, params=params, timeout=10)
        else:
            response = requests.request(method, url, params=params, timeout=10)

        print(f"Status: {response.status_code}")
        if response.content:
            try:
                result = response.json()
                print(f"Response: {json.dumps(result, indent=2)[:500]}")
                return result
            except:
                print(f"Raw response: {response.text[:200]}")
        return None
    except Exception as e:
        print(f"Error: {e}")
        return None


def main():
    print("=" * 60)
    print("Market Tools API Tests")
    print("=" * 60)

    # Test 1: Ingest prices
    print("\n" + "=" * 60)
    print("TEST 1: POST /v1/prices - Price ingestion")
    print("=" * 60)
    orders_data = create_test_orders_csv()
    result = test_endpoint('POST', '/v1/prices?market=jita&mode=replace', data=orders_data)
    if result and result.get('status') == 'PRICES_UPDATED':
        print("✓ Price ingestion successful")
    else:
        print("✗ Price ingestion failed")

    # Test 2: Ingest stations
    print("\n" + "=" * 60)
    print("TEST 2: POST /v1/stations - Station ingestion")
    print("=" * 60)
    stations_data = create_test_stations_csv()
    result = test_endpoint('POST', '/v1/stations', data=stations_data)
    if result and result.get('status') == 'STATIONS_UPDATED':
        print("✓ Station ingestion successful")
    else:
        print("✗ Station ingestion failed")

    # Test 3: Get regional stats
    print("\n" + "=" * 60)
    print("TEST 3: GET /v1/market/jita - Regional stats")
    print("=" * 60)
    result = test_endpoint('GET', '/v1/market/jita')
    if result:
        print(f"✓ Regional stats retrieved: {result.get('name')}, {result.get('sell_orders')} sell orders, {result.get('buy_orders')} buy orders")
    else:
        print("✗ Failed to get regional stats")

    # Test 4: Get type details
    print("\n" + "=" * 60)
    print("TEST 4: GET /v1/market/jita/34 - Type details")
    print("=" * 60)
    result = test_endpoint('GET', '/v1/market/jita/34')
    if result:
        print(f"✓ Type details retrieved: {result.get('name')}, buy={result.get('buy')}, sell={result.get('sell')}")
    else:
        print("✗ Failed to get type details")

    # Test 5: Hub compare
    print("\n" + "=" * 60)
    print("TEST 5: GET /v1/hub-compare/34 - Hub comparison")
    print("=" * 60)
    result = test_endpoint('GET', '/v1/hub-compare/34')
    if result:
        print(f"✓ Hub comparison retrieved. Keys: {list(result.keys())[:5]}...")
    else:
        print("✗ Failed to get hub comparison")

    # Test 6: Test append mode
    print("\n" + "=" * 60)
    print("TEST 6: POST /v1/prices with append mode")
    print("=" * 60)
    result = test_endpoint('POST', '/v1/prices?market=jita&mode=append', data=orders_data)
    if result and result.get('status') == 'PRICES_UPDATED':
        print("✓ Append mode successful")
    else:
        print("✗ Append mode failed")

    # Test 7: Test with location_id filter
    print("\n" + "=" * 60)
    print("TEST 7: POST /v1/prices with location_id filter")
    print("=" * 60)
    result = test_endpoint('POST', '/v1/prices?market=jita&location_id=60000361', data=orders_data)
    if result:
        print(f"✓ Location filter successful: {result.get('count')} rows")
    else:
        print("✗ Location filter failed")

    # Test 8: Test with type_ids parameter
    print("\n" + "=" * 60)
    print("TEST 8: GET /v1/market/jita with type_ids parameter")
    print("=" * 60)
    result = test_endpoint('GET', '/v1/market/jita?type_ids=34')
    if result and 'types' in result:
        print(f"✓ Type IDs parameter successful. Types key present.")
        if result['types']:
            type_name = list(result['types'].keys())[0]
            print(f"  Found type: {type_name}")
    else:
        print("✗ Type IDs parameter failed")

    # Test 9: Test with hubs parameter
    print("\n" + "=" * 60)
    print("TEST 9: GET /v1/market/jita with hubs parameter")
    print("=" * 60)
    result = test_endpoint('GET', '/v1/market/jita?hubs=1')
    if result and 'hubs' in result:
        print(f"✓ Hubs parameter successful. Hub stats present.")
        if result['hubs']:
            hub = result['hubs'][0]
            print(f"  Top hub: {hub.get('station')} with {hub.get('orders')} orders")
    else:
        print("✗ Hubs parameter failed")

    # Test 10: Error handling - missing data
    print("\n" + "=" * 60)
    print("TEST 10: POST /v1/prices with no data")
    print("=" * 60)
    result = test_endpoint('POST', '/v1/prices?market=jita')
    if result and result.get('error') == 'INVALID_FORMAT':
        print("✓ Correctly returned 400 for empty data")
    else:
        print("✗ Did not correctly handle empty data")

    # Test 11: Error handling - invalid CSV
    print("\n" + "=" * 60)
    print("TEST 11: POST /v1/prices with invalid CSV")
    print("=" * 60)
    invalid_data = gzip.compress(b"not,csv,data,at,all")
    result = test_endpoint('POST', '/v1/prices?market=jita', data=invalid_data)
    if result and result.get('error') == 'INVALID_FORMAT':
        print("✓ Correctly returned 400 for invalid CSV")
    else:
        print("✗ Did not correctly handle invalid CSV")

    # Test 12: Unknown region
    print("\n" + "=" * 60)
    print("TEST 12: GET /v1/market/unknown - Unknown region")
    print("=" * 60)
    result = test_endpoint('GET', '/v1/market/unknown')
    if result and result.get('error') == 'UNKNOWN_ITEMS':
        print("✓ Correctly returned 404 for unknown region")
    else:
        print("✗ Did not correctly handle unknown region")

    print("\n" + "=" * 60)
    print("Tests Complete!")
    print("=" * 60)


if __name__ == '__main__':
    main()
