#!/usr/bin/env python3
"""
Tests for reprocessing API endpoints.
"""

import gzip
import io
import csv
import json
import requests
import time

BASE_URL = "http://127.0.0.1:5000"

def test_reprocess_basic():
    """Test basic reprocessing without drill-down."""
    print("\n=== Testing POST /v1/reprocess (basic) ===")

    # Test with Veldspar
    response = requests.post(f"{BASE_URL}/v1/reprocess", json={
        "items": {"Veldspar": 100},
        "efficiency": {
            "ore": 0.9063,
            "gas": 0.95,
            "scrap": 0.55
        },
        "drill_down": False
    })

    print(f"Status: {response.status_code}")
    result = response.json()
    print(f"Response: {json.dumps(result, indent=2)[:500]}")

    assert response.status_code == 201, f"Expected 201, got {response.status_code}"
    assert "products" in result, "Expected products in response"
    assert "yields" in result, "Expected yields in response"
    print("✓ Basic reprocessing test passed")

def test_reprocess_drill_down():
    """Test reprocessing with drill-down enabled."""
    print("\n=== Testing POST /v1/reprocess (drill down) ===")

    response = requests.post(f"{BASE_URL}/v1/reprocess", json={
        "items": {"Veldspar": 100},
        "efficiency": {
            "ore": 0.9063,
            "gas": 0.95,
            "scrap": 0.55
        },
        "drill_down": True
    })

    print(f"Status: {response.status_code}")
    result = response.json()
    print(f"Response keys: {list(result.get('products', {}).keys())}")

    assert response.status_code == 201, f"Expected 201, got {response.status_code}"
    print("✓ Drill-down reprocessing test passed")

def test_reprocess_unknown_item():
    """Test reprocessing with unknown item name."""
    print("\n=== Testing POST /v1/reprocess (unknown item) ===")

    response = requests.post(f"{BASE_URL}/v1/reprocess", json={
        "items": {"NonExistent Item": 100}
    })

    print(f"Status: {response.status_code}")
    result = response.json()
    print(f"Response: {json.dumps(result, indent=2)}")

    assert response.status_code == 404, f"Expected 404, got {response.status_code}"
    assert result.get("error") == "UNKNOWN_ITEMS", f"Expected UNKNOWN_ITEMS error, got {result.get('error')}"
    print("✓ Unknown item test passed")

def test_reprocess_invalid_quantity():
    """Test reprocessing with invalid quantity."""
    print("\n=== Testing POST /v1/reprocess (invalid quantity) ===")

    response = requests.post(f"{BASE_URL}/v1/reprocess", json={
        "items": {"Veldspar": -5},
        "drill_down": False
    })

    print(f"Status: {response.status_code}")
    result = response.json()
    print(f"Response: {json.dumps(result, indent=2)}")

    assert response.status_code == 400, f"Expected 400, got {response.status_code}"
    print("✓ Invalid quantity test passed")

def test_reprocess_with_market_data():
    """Test reprocessing with market data loaded."""
    print("\n=== Testing POST /v1/reprocess (with market) ===")

    # First ingest some test market data
    response = requests.post(f"{BASE_URL}/v1/prices?market=jita&mode=replace", data=b"test_data")

    # Now reprocess with market parameter
    response = requests.post(f"{BASE_URL}/v1/reprocess", json={
        "items": {"Veldspar": 100},
        "market": "Jita"
    })

    print(f"Status: {response.status_code}")
    result = response.json()
    print(f"Has buy price: {result.get('inputs', {}).get('buy') is not None}")

    # Should have null prices without actual price data loaded
    assert response.status_code == 201, f"Expected 201, got {response.status_code}"
    print("✓ Market parameter test passed")

def test_reprocess_config_api_key():
    """Test creating and using API keys for configuration-driven yields."""
    print("\n=== Testing POST /v1/config (API key creation) ===")

    # Create a config
    response = requests.post(f"{BASE_URL}/v1/config", json={
        "structure": {
            "type": "athanor",
            "rig": "t2",
            "security": "nullsec"
        },
        "skills": {
            "Reprocessing": 5,
            "Reprocessing Efficiency": 4
        },
        "implant": "RX-804"
    })

    print(f"Status: {response.status_code}")
    result = response.json()
    print(f"Response: {json.dumps(result, indent=2)}")

    assert response.status_code == 201, f"Expected 201, got {response.status_code}"
    assert "key" in result, "Expected key in response"

    api_key = result["key"]
    print(f"✓ API key created: {api_key[:20]}...")

    # Use the API key for reprocessing
    print("\n=== Testing reprocessing with API key ===")
    response = requests.post(f"{BASE_URL}/v1/reprocess", json={
        "items": {"Veldspar": 100}
    }, headers={"X-API-Key": api_key})

    print(f"Status: {response.status_code}")
    result = response.json()
    print(f"Yields: {result.get('yields')}")

    assert response.status_code == 201, f"Expected 201, got {response.status_code}"
    print("✓ API key usage test passed")

def test_config_invalid_values():
    """Test config endpoint with invalid values."""
    print("\n=== Testing POST /v1/config (invalid values) ===")

    # Test invalid structure type
    response = requests.post(f"{BASE_URL}/v1/config", json={
        "structure": {
            "type": "invalid_type"
        }
    })

    print(f"Status: {response.status_code}")
    result = response.json()
    print(f"Response: {json.dumps(result, indent=2)}")

    assert response.status_code == 404, f"Expected 404, got {response.status_code}"
    assert result.get("code") == "INVALID_VALUE", f"Expected INVALID_VALUE error"

    # Test invalid rig for npc_station
    response = requests.post(f"{BASE_URL}/v1/config", json={
        "structure": {
            "type": "npc_station",
            "rig": "t2"
        }
    })

    print(f"Status for npc_station with rig: {response.status_code}")
    result = response.json()
    print(f"Response: {json.dumps(result, indent=2)}")

    assert response.status_code == 404, f"Expected 404, got {response.status_code}"
    print("✓ Invalid config values test passed")

def test_reprocess_scrap_items():
    """Test reprocessing scrap items (non-ore/gas)."""
    print("\n=== Testing POST /v1/reprocess (scrap items) ===")

    # Test with a module that has materials
    response = requests.post(f"{BASE_URL}/v1/reprocess", json={
        "items": {"Prototype Cloaking Device I": 1},
        "efficiency": {
            "scrap": 0.55
        }
    })

    print(f"Status: {response.status_code}")
    result = response.json()
    print(f"Products: {list(result.get('products', {}).keys())[:5]}")

    assert response.status_code == 201, f"Expected 201, got {response.status_code}"
    print("✓ Scrap reprocessing test passed")

def test_reprocess_portion_sizes():
    """Test reprocessing with portion sizes (leftovers)."""
    print("\n=== Testing POST /v1/reprocess (portion sizes) ===")

    # Ore has portion size of 100, so 50 units should return as leftovers
    response = requests.post(f"{BASE_URL}/v1/reprocess", json={
        "items": {"Veldspar": 50},
        "efficiency": {
            "ore": 0.9063
        }
    })

    print(f"Status: {response.status_code}")
    result = response.json()

    # 50 units is less than portion size, so should remain as Veldspar
    if "products" in result:
        products = result["products"]
        print(f"Products: {list(products.keys())}")
        if "Veldspar" in products:
            print(f"Veldspar remains: {products['Veldspar']['quantity']} units")

    assert response.status_code == 201, f"Expected 201, got {response.status_code}"
    print("✓ Portion size test passed")

def test_multiple_items():
    """Test reprocessing multiple items."""
    print("\n=== Testing POST /v1/reprocess (multiple items) ===")

    response = requests.post(f"{BASE_URL}/v1/reprocess", json={
        "items": {
            "Veldspar": 100,
            "Scordite": 50
        },
        "efficiency": {
            "ore": 0.9063
        }
    })

    print(f"Status: {response.status_code}")
    result = response.json()
    print(f"Input volume: {result.get('inputs', {}).get('volume')}")
    print(f"Products count: {len(result.get('products', {}))}")

    assert response.status_code == 201, f"Expected 201, got {response.status_code}"
    print("✓ Multiple items test passed")

def main():
    """Run all tests."""
    print("=" * 60)
    print("Reprocessing API Tests")
    print("=" * 60)

    tests = [
        test_reprocess_basic,
        test_reprocess_drill_down,
        test_reprocess_unknown_item,
        test_reprocess_invalid_quantity,
        test_reprocess_with_market_data,
        test_reprocess_config_api_key,
        test_config_invalid_values,
        test_reprocess_scrap_items,
        test_reprocess_portion_sizes,
        test_multiple_items
    ]

    for test in tests:
        try:
            test()
        except Exception as e:
            print(f"✗ Test failed with error: {e}")
            raise

    print("\n" + "=" * 60)
    print("All tests passed!")
    print("=" * 60)


if __name__ == "__main__":
    main()
