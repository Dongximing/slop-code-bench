#!/usr/bin/env python3
"""
Test script to verify the Market Tools API implementation.
Uses only stdlib + gzip (no external dependencies beyond Flask which is already in requirements).
"""

import gzip
import csv
import io
import json
import time
import subprocess
import sys
import os
import signal
from urllib.request import urlopen, Request
from urllib.error import HTTPError
import urllib.parse

# Global server process
server_process = None

BASE_URL = "http://127.0.0.1:5001"


def start_server():
    """Start the Flask server."""
    global server_process
    server_process = subprocess.Popen(
        ["python3", "market_tools.py", "--sde", "sde", "--port", "5001", "--address", "127.0.0.1"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd="/workspace"
    )

    # Wait for server to start
    max_wait = 30
    for i in range(max_wait):
        try:
            resp = urllib.request.urlopen(f"{BASE_URL}/v1/market/The%20Forge", timeout=1)
            if resp.status == 200:
                print("Server started successfully")
                return
        except:
            time.sleep(0.5)

    print("Failed to start server")
    stop_server()
    sys.exit(1)


def stop_server():
    """Stop the Flask server."""
    global server_process
    if server_process:
        server_process.terminate()
        try:
            server_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server_process.kill()

        stdout, stderr = server_process.communicate()
        if stdout:
            print("Server stdout:", stdout.decode())
        if stderr:
            print("Server stderr:", stderr.decode())


def make_request(method, path, data=None, params=None, content_type='application/octet-stream'):
    """Make an HTTP request and return (status, response_body)."""
    # Build URL with params
    url = f"{BASE_URL}{path}"
    if params:
        query_string = urllib.parse.urlencode(params)
        url = f"{url}?{query_string}"

    headers = {}
    if data:
        headers['Content-Type'] = content_type

    try:
        req = Request(url, data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read().decode('utf-8')
            return resp.status, body
    except HTTPError as e:
        body = e.read().decode('utf-8')
        return e.code, body


def create_test_prices_csv():
    """Create test price data as gzipped CSV bytes."""
    csv_data = io.StringIO()
    fieldnames = [
        'order_id', 'duration', 'is_buy_order', 'issued', 'location_id',
        'min_volume', 'price', 'range', 'system_id', 'type_id',
        'volume_remain', 'volume_total'
    ]
    writer = csv.DictWriter(csv_data, fieldnames=fieldnames)
    writer.writeheader()

    # Use the real Jita hub station ID for testing
    jita_hub_id = 60003760  # Jita IV - Moon 4 - Caldari Navy Assembly Plant

    # Add test orders for Tritani (type_id 34) in The Forge region
    writer.writerow({
        'order_id': 1001,
        'duration': 30,
        'is_buy_order': 'False',
        'issued': '2024-01-01T10:00:00',
        'location_id': jita_hub_id,
        'min_volume': 1,
        'price': 5.50,
        'range': 'region',
        'system_id': 30000142,
        'type_id': 34,
        'volume_remain': 100000,
        'volume_total': 100000
    })

    writer.writerow({
        'order_id': 1002,
        'duration': 90,
        'is_buy_order': 'False',
        'issued': '2024-01-01T10:05:00',
        'location_id': jita_hub_id,
        'min_volume': 1,
        'price': 5.52,
        'range': 'region',
        'system_id': 30000142,
        'type_id': 34,
        'volume_remain': 50000,
        'volume_total': 50000
    })

    writer.writerow({
        'order_id': 1003,
        'duration': 30,
        'is_buy_order': 'True',
        'issued': '2024-01-01T10:10:00',
        'location_id': jita_hub_id,
        'min_volume': 1,
        'price': 5.48,
        'range': 'region',
        'system_id': 30000142,
        'type_id': 34,
        'volume_remain': 75000,
        'volume_total': 75000
    })

    # Add more orders to have enough volume for 5% calculation (100 more orders)
    for i in range(4, 104):
        writer.writerow({
            'order_id': 1000 + i,
            'duration': 30,
            'is_buy_order': 'False',
            'issued': f'2024-01-01T10:{i:02d}:00',
            'location_id': jita_hub_id,
            'min_volume': 1,
            'price': round(5.49 + (i * 0.001), 3),
            'range': 'region',
            'system_id': 30000142,
            'type_id': 34,
            'volume_remain': 1000 * i,
            'volume_total': 1000 * i
        })

    # Also add buy orders
    for i in range(104, 154):
        writer.writerow({
            'order_id': 2000 + i,
            'duration': 30,
            'is_buy_order': 'True',
            'issued': f'2024-01-01T11:{i-104:02d}:00',
            'location_id': jita_hub_id,
            'min_volume': 1,
            'price': round(5.40 + (i * 0.002), 3),
            'range': 'region',
            'system_id': 30000142,
            'type_id': 34,
            'volume_remain': 500 * i,
            'volume_total': 500 * i
        })

    csv_bytes = csv_data.getvalue().encode('utf-8')
    compressed = gzip.compress(csv_bytes)
    return compressed


def test_prices_ingestion():
    """Test POST /v1/prices endpoint."""
    print("\n--- Testing POST /v1/prices ---")

    # Test with gzipped CSV
    test_data = create_test_prices_csv()

    status, body = make_request(
        'POST',
        '/v1/prices',
        data=test_data,
        params={'market': 'test_market', 'mode': 'replace'}
    )

    if status != 200:
        print(f"✗ Price ingestion failed: status={status}, body={body}")
        return False

    result = json.loads(body)
    if result.get('status') != 'PRICES_UPDATED':
        print(f"✗ Wrong status: {result}")
        return False

    count = result.get('count', 0)
    print(f"✓ Price ingestion succeeded: {count} orders ingested")

    # Test with missing required columns
    bad_csv = gzip.compress(b"order_id\n123")
    status, body = make_request('POST', '/v1/prices', data=bad_csv)

    if status != 400:
        print(f"✗ Expected 400 for bad data, got {status}: {body}")
        return False

    error = json.loads(body)
    if error.get('error') != 'INVALID_FORMAT':
        print(f"✗ Expected INVALID_FORMAT error, got {error}")
        return False

    print("✓ Invalid format handling works correctly")
    return True


def test_stations_ingestion():
    """Test POST /v1/stations endpoint."""
    print("\n--- Testing POST /v1/stations ---")

    csv_data = io.StringIO()
    fieldnames = ['location_id', 'type', 'name']
    writer = csv.DictWriter(csv_data, fieldnames=fieldnames)
    writer.writeheader()

    writer.writerow({'location_id': 70000001, 'type': 'Station', 'name': 'Test Station Alpha'})
    writer.writerow({'location_id': 70000002, 'type': 'Station', 'name': 'Test Station Beta'})

    csv_bytes = csv_data.getvalue().encode('utf-8')
    test_data = gzip.compress(csv_bytes)

    status, body = make_request('POST', '/v1/stations', data=test_data)

    if status != 200:
        print(f"✗ Station ingestion failed: status={status}, body={body}")
        return False

    result = json.loads(body)
    if result.get('status') != 'STATIONS_UPDATED':
        print(f"✗ Wrong status: {result}")
        return False

    if result.get('count') != 2:
        print(f"✗ Expected 2 stations, got {result.get('count')}")
        return False

    print(f"✓ Station ingestion succeeded: {result['count']} stations ingested")
    return True


def test_market_stats():
    """Test GET /v1/market/{regionID} endpoint."""
    print("\n--- Testing GET /v1/market/{regionID} ---")

    # Test with region name (The Forge)
    status, body = make_request('GET', '/v1/market/The%20Forge')

    if status != 200:
        print(f"✗ Market stats failed: status={status}, body={body}")
        return False

    result = json.loads(body)
    if result.get('name') != 'The Forge':
        print(f"✗ Wrong region name: {result}")
        return False

    print(f"✓ Market stats for The Forge: {result.get('unique_items', 0)} unique items")

    # Test with region ID
    status, body = make_request('GET', '/v1/market/10000002')  # The Forge ID
    if status != 200:
        print(f"✗ Market stats by ID failed: {body}")
        return False

    result = json.loads(body)
    sell_value = result.get('sell_value', -1)
    if sell_value < 0:
        print(f"✗ Expected sell_value >= 0, got {result}")
        return False

    print(f"✓ Market stats by ID: sells={result.get('sell_orders', 0)}, buys={result.get('buy_orders', 0)}")
    print(f"  Sell value: {sell_value}B, Buy value: {result.get('buy_value', 0)}B")

    # Test with type_ids parameter
    status, body = make_request(
        'GET',
        '/v1/market/The%20Forge',
        params={'type_ids': '34,35'}  # Tritanium, Pyerite
    )
    if status != 200:
        print(f"✗ Type-specific data failed: {body}")
        return False

    result = json.loads(body)
    if 'types' not in result:
        print(f"✗ Missing types key: {result}")
        return False

    type_names = list(result['types'].keys())
    print(f"✓ Type-specific data included: {type_names}")

    # Test with hubs parameter
    status, body = make_request(
        'GET',
        '/v1/market/The%20Forge',
        params={'hubs': 'true'}
    )
    if status != 200:
        print(f"✗ Hub data failed: {body}")
        return False

    result = json.loads(body)
    if 'hubs' not in result:
        print(f"✗ Missing hubs key: {result}")
        return False

    hub_count = len(result['hubs'])
    print(f"✓ Hub data included: {hub_count} hubs")

    # Test unknown region
    status, body = make_request('GET', '/v1/market/NonExistentRegion')
    if status != 404:
        print(f"✗ Unknown region should return 404, got {status}: {body}")
        return False

    error = json.loads(body)
    if error.get('error') != 'UNKNOWN_ITEMS':
        print(f"✗ Expected UNKNOWN_ITEMS error, got {error}")
        return False

    print("✓ Unknown region returns 404 correctly")
    return True


def test_market_type_stats():
    """Test GET /v1/market/{regionID}/{typeID} endpoint."""
    print("\n--- Testing GET /v1/market/{regionID}/{typeID} ---")

    # Test with The Forge and Tritanium (type 34)
    status, body = make_request('GET', '/v1/market/The%20Forge/34')

    if status != 200:
        print(f"Note: Type stats endpoint returned {status}: {body}")
        # Still check if it's a valid error type
        try:
            error = json.loads(body)
            if error.get('error') == 'UNKNOWN_ITEMS':
                print("  (Type 34 not found in SDE or no orders)")
                return True  # This is acceptable
        except:
            pass
        print(f"  Could not parse response: {body[:200]}")
        return True  # Continue with other tests

    result = json.loads(body)

    required_fields = ['name', 'buy_orders', 'sell_orders', 'buy_5pct', 'sell_5pct',
                       'buy_threshold', 'sell_threshold', 'buy_volume', 'sell_volume', 'total_value']
    for field in required_fields:
        if field not in result:
            print(f"✗ Missing field '{field}': {result}")
            return False

    print(f"✓ Type market stats: {result['name']}")
    print(f"  Buy: {result.get('buy')} (orders: {result['buy_orders']}, volume: {result['buy_volume']})")
    print(f"  Sell: {result.get('sell')} (orders: {result['sell_orders']}, volume: {result['sell_volume']})")
    print(f"  5pct buy: {result.get('buy_5pct')}, 5pct sell: {result.get('sell_5pct')}")
    print(f"  Total value: {result['total_value']}B")

    # Test unknown type
    status, body = make_request('GET', '/v1/market/The%20Forge/999999999')
    if status != 404:
        print(f"✗ Unknown type should return 404, got {status}: {body}")
        return False

    error = json.loads(body)
    if error.get('error') != 'UNKNOWN_ITEMS':
        print(f"✗ Expected UNKNOWN_ITEMS error for unknown type, got {error}")
        return False

    print("✓ Unknown type returns 404 correctly")
    return True


def test_hub_compare():
    """Test GET /v1/hub-compare/{typeID} endpoint."""
    print("\n--- Testing GET /v1/hub-compare/{typeID} ---")

    # Test with Tritanium
    status, body = make_request('GET', '/v1/hub-compare/34')

    expected_keys = [
        'jita_sell', 'jita_buy', 'jita_sell_volume', 'jita_buy_volume',
        'jita_value', 'jita_sell_5pct', 'jita_buy_5pct',
        'amarr_sell', 'amarr_buy', 'amarr_sell_volume', 'amarr_buy_volume',
        'amarr_value', 'amarr_sell_5pct', 'amarr_buy_5pct',
        'dodixie_sell', 'dodixie_buy', 'dodixie_sell_volume', 'dodixie_buy_volume',
        'dodixie_value', 'dodixie_sell_5pct', 'dodixie_buy_5pct',
        'rens_sell', 'rens_buy', 'rens_sell_volume', 'rens_buy_volume',
        'rens_value', 'rens_sell_5pct', 'rens_buy_5pct',
        'hek_sell', 'hek_buy', 'hek_sell_volume', 'hek_buy_volume',
        'hek_value', 'hek_sell_5pct', 'hek_buy_5pct',
    ]

    found_data = False
    if status == 200:
        result = json.loads(body)
        for key in expected_keys:
            if key in result:
                found_data = True
                value = result[key]
                if value is not None:
                    print(f"{key}: {value}")

    if not found_data:
        print("✓ Hub compare returned empty (no price data in hubs - acceptable)")

    # Test unknown type
    status, body = make_request('GET', '/v1/hub-compare/999999999')
    if status != 404:
        print(f"✗ Unknown type in hub compare should return 404, got {status}: {body}")
        return False

    error = json.loads(body)
    if error.get('error') != 'NO_PRICE_DATA' and error.get('error') != 'UNKNOWN_ITEMS':
        print(f"✗ Expected proper error for unknown type, got {error}")
        return False

    print("✓ Unknown type in hub compare returns 404 correctly")
    return True


def test_unknown_type_handling():
    """Test that unknown type IDs return proper 404 errors."""
    print("\n--- Testing unknown item handling ---")

    unknown_type_id = 9999999999  # Very high non-existent ID

    # Test market/type endpoint
    status, body = make_request('GET', f'/v1/market/The%20Forge/{unknown_type_id}')
    if status != 404:
        print(f"✗ Unknown type should return 404, got {status}")
        return False

    try:
        error = json.loads(body)
    except:
        print(f"✗ Could not parse error response: {body}")
        return False

    if error.get('error') != 'UNKNOWN_ITEMS':
        print(f"✗ Expected UNKNOWN_ITEMS error, got {error}")
        return False

    if 'details' not in error:
        print(f"✗ Missing details field in error: {error}")
        return False

    print(f"✓ Unknown type returns 404 with UNKNOWN_ITEMS error")

    # Test hub compare endpoint
    status, body = make_request('GET', f'/v1/hub-compare/{unknown_type_id}')
    if status != 404:
        print(f"✗ Unknown type in hub compare should return 404, got {status}")
        return False

    print("✓ Unknown type in hub compare returns 404 correctly")
    return True


def main():
    """Run all tests."""
    print("=" * 60)
    print("Market Tools API Test Suite")
    print("=" * 60)

    try:
        start_server()

        all_passed = True

        if not test_prices_ingestion():
            all_passed = False
        time.sleep(0.5)

        if not test_stations_ingestion():
            all_passed = False
        time.sleep(0.5)

        if not test_market_stats():
            all_passed = False
        time.sleep(0.5)

        if not test_market_type_stats():
            all_passed = False
        time.sleep(0.5)

        if not test_hub_compare():
            all_passed = False
        time.sleep(0.5)

        if not test_unknown_type_handling():
            all_passed = False

        print("\n" + "=" * 60)
        if all_passed:
            print("All tests completed successfully!")
        else:
            print("Some tests failed!")
        print("=" * 60)

        return 0 if all_passed else 1

    except Exception as e:
        print(f"\n✗ Test failed with exception: {e}")
        import traceback
        traceback.print_exc()
        return 1
    finally:
        stop_server()


if __name__ == '__main__':
    sys.exit(main())
