#!/usr/bin/env python3
"""
Test suite for MTL extended functionality.
"""

import subprocess
import json
import sys
import tempfile
import os

def run_mtl(csv_content, dsl_content, params=None):
    """Run MTL with given CSV and DSL content."""
    with tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False) as f:
        f.write(csv_content)
        csv_path = f.name

    with tempfile.NamedTemporaryFile(mode='w', suffix='.mtl', delete=False) as f:
        f.write(dsl_content)
        dsl_path = f.name

    cmd = ['python', 'mtl.py', '--csv', csv_path, '--dsl', dsl_path]
    if params:
        for key, value in params.items():
            cmd.extend(['--param', f'{key}={value}'])

    result = subprocess.run(cmd, capture_output=True, text=True)

    os.unlink(csv_path)
    os.unlink(dsl_path)

    return result


def test_basic_part1_compatibility():
    """Test that Part 1 functionality still works."""
    print("Testing Part 1 compatibility...")

    csv_content = """timestamp,location,price,quantity
2024-01-01T10:00:00Z,New York,10.0,2
2024-01-01T11:00:00Z,Boston,20.0,3
2024-01-01T12:00:00Z,New York,15.0,1
"""

    dsl_content = """
    filter(location == "New York")
    window(1h)
    group_by([location])
    aggregate sum(price) as total_price
    """

    result = run_mtl(csv_content, dsl_content)
    assert result.returncode == 0, f"Failed: {result.stderr}"

    output = json.loads(result.stdout)
    assert len(output) == 1
    assert output[0]['location'] == 'New York'
    assert output[0]['total_price'] == 25.0  # 10*2 + 15*1
    print("✓ Part 1 compatibility works")


def test_schema_with_inheritance():
    """Test schema definition with inheritance."""
    print("\nTesting schema with inheritance...")

    csv_content = """timestamp,location,item,price,quantity,unit,category,promo_code
2024-01-01T10:00:00Z,Store A,Widget,10.0,2,pcs,electronics,SUMMER2024
2024-01-01T11:00:00Z,Store B,Gadget,20.0,1,pcs,electronics,SPRING2024
2024-01-01T12:00:00Z,Store A,Thing,15.0,3,pcs,other,SUMMER2024
"""

    dsl_content = """
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

    pipeline TestPipeline using PromoSale {
        filter(promo_code == "SUMMER2024")
        window(1h)
        group_by([location])
        aggregate sum(revenue) as total_revenue,
                  count(*) as n
    }
    """

    result = run_mtl(csv_content, dsl_content)
    assert result.returncode == 0, f"Failed: {result.stderr}"

    output = json.loads(result.stdout)
    assert len(output) == 2  # Store A appears twice

    # Find Store A result
    store_a = next(o for o in output if o['location'] == 'Store A')
    assert store_a['total_revenue'] == 65.0  # (10*2) + (15*3)
    print("✓ Schema inheritance works")


def test_type_override_widening():
    """Test that numeric widening (int->float) is allowed."""
    print("\nTesting type override widening...")

    csv_content = """timestamp,location,item,price,quantity
2024-01-01T10:00:00Z,Store A,Widget,10.0,2
2024-01-01T11:00:00Z,Store B,Gadget,20.0,3
"""

    dsl_content = """
    schema BaseSale {
        timestamp: timestamp
        location: string
        item: string
        price: float
        quantity: int
    }

    schema ExtendedSale extends BaseSale {
        quantity: float  # int -> float widening allowed
    }

    pipeline TestPipeline using ExtendedSale {
        aggregate sum(quantity) as total_qty
    }
    """

    result = run_mtl(csv_content, dsl_content)
    assert result.returncode == 0, f"Failed: {result.stderr}"
    print("✓ Type override widening works")


def test_illegal_type_override():
    """Test that illegal type overrides are rejected."""
    print("\nTesting illegal type override rejection...")

    csv_content = """timestamp,location,item,price,quantity
2024-01-01T10:00:00Z,Store A,Widget,10.0,2
"""

    dsl_content = """
    schema BaseSale {
        timestamp: timestamp
        location: string
        item: string
        price: float
        quantity: int
    }

    schema ExtendedSale extends BaseSale {
        quantity: string  # illegal: float -> string
    }

    pipeline TestPipeline using ExtendedSale {
        aggregate sum(quantity) as total_qty
    }
    """

    result = run_mtl(csv_content, dsl_content)
    assert result.returncode == 2, f"Should have failed with type error, got: {result.stderr}"
    assert "type_error" in result.stderr
    print("✓ Illegal type override correctly rejected")


def test_pipeline_parameters():
    """Test pipeline parameters."""
    print("\nTesting pipeline parameters...")

    csv_content = """timestamp,location,price,quantity
2024-01-01T10:00:00Z,Store A,10.0,2
2024-01-01T11:00:00Z,Store B,20.0,3
2024-01-01T12:00:00Z,Store C,5.0,1
"""

    dsl_content = """
    schema Sale {
        timestamp: timestamp
        location: string
        price: float
        quantity: float
    }

    pipeline FilteredPipeline using Sale {
        params {
            min_price: float = 0.0
        }

        filter(price >= param("min_price"))
        aggregate sum(price) as total_price
    }
    """

    # Test with parameter
    result = run_mtl(csv_content, dsl_content, params={'min_price': '15.0'})
    assert result.returncode == 0, f"Failed: {result.stderr}"

    output = json.loads(result.stdout)
    # Only rows with price >= 15: Store B (20), Store C is below
    # Actually all rows pass since min is 15 and we have 20 and 5
    # Let me check: 10 < 15, 20 >= 15, 5 < 15
    # So only Store B should remain
    assert len(output) == 1
    assert output[0]['total_price'] == 20.0
    print("✓ Pipeline parameters work")


def test_pre_stage_calculated_fields():
    """Test pre-stage calculated fields in pipeline."""
    print("\nTesting pre-stage calculated fields...")

    csv_content = """timestamp,location,price,quantity
2024-01-01T10:00:00Z,Store A,10.0,2
2024-01-01T11:00:00Z,Store B,20.0,3
"""

    dsl_content = """
    schema Sale {
        timestamp: timestamp
        location: string
        price: float
        quantity: float
    }

    pipeline TestPipeline using Sale {
        calc revenue = price * quantity : float @stage(pre)
        filter(revenue >= 30.0)
        group_by([location])
        aggregate sum(revenue) as total_revenue
    }
    """

    result = run_mtl(csv_content, dsl_content)
    assert result.returncode == 0, f"Failed: {result.stderr}"

    output = json.loads(result.stdout)
    # Store A: 10*2 = 20 (filtered out)
    # Store B: 20*3 = 60 (kept)
    assert len(output) == 1
    assert output[0]['total_revenue'] == 60.0
    print("✓ Pre-stage calculated fields work")


def test_post_agg_calculated_fields():
    """Test post-aggregation calculated fields."""
    print("\nTesting post-aggregation calculated fields...")

    csv_content = """timestamp,location,price,quantity
2024-01-01T10:00:00Z,Store A,10.0,2
2024-01-01T11:00:00Z,Store B,20.0,3
2024-01-01T12:00:00Z,Store A,15.0,1
2024-01-01T13:00:00Z,Store B,25.0,2
"""

    dsl_content = """
    schema Sale {
        timestamp: timestamp
        location: string
        price: float
        quantity: float
    }

    pipeline TestPipeline using Sale {
        group_by([location])
        aggregate sum(price * quantity) as revenue,
                  count(*) as transaction_count
        calc avg_per_tx = revenue / transaction_count : float @stage(post_agg)
    }
    """

    result = run_mtl(csv_content, dsl_content)
    assert result.returncode == 0, f"Failed: {result.stderr}"

    output = json.loads(result.stdout)
    assert len(output) == 2

    # Store A: (10*2 + 15*1) / 2 = 35/2 = 17.5
    # Store B: (20*3 + 25*2) / 2 = (60+50)/2 = 55
    store_a = next(o for o in output if o['location'] == 'Store A')
    store_b = next(o for o in output if o['location'] == 'Store B')

    assert abs(store_a['avg_per_tx'] - 17.5) < 0.001
    assert abs(store_b['avg_per_tx'] - 55.0) < 0.001
    print("✓ Post-aggregation calculated fields work")


def test_lag_function():
    """Test LAG function in post-aggregation."""
    print("\nTesting LAG function...")

    csv_content = """timestamp,location,price,quantity
2024-01-01T10:00:00Z,Store A,10.0,1
2024-01-01T11:00:00Z,Store B,20.0,1
2024-01-01T12:00:00Z,Store A,30.0,1
2024-01-01T13:00:00Z,Store B,40.0,1
"""

    dsl_content = """
    schema Sale {
        timestamp: timestamp
        location: string
        price: float
        quantity: float
    }

    pipeline TestPipeline using Sale {
        group_by([location])
        aggregate sum(price) as total_price
        calc prev_price = lag(total_price, 1) : float @stage(post_agg)
    }
    """

    result = run_mtl(csv_content, dsl_content)
    assert result.returncode == 0, f"Failed: {result.stderr}"

    output = json.loads(result.stdout)
    assert len(output) == 2

    # Results are sorted by location
    # Store A: total_price = 10+30 = 40, prev_price = null (first)
    # Store B: total_price = 20+40 = 60, prev_price = 40
    store_a = next(o for o in output if o['location'] == 'Store A')
    store_b = next(o for o in output if o['location'] == 'Store B')

    assert store_a['total_price'] == 40.0
    assert store_a['prev_price'] is None  # First in sorted order

    assert store_b['total_price'] == 60.0
    assert store_b['prev_price'] == 40.0
    print("✓ LAG function works")


def test_coalesce_function():
    """Test coalesce function."""
    print("\nTesting coalesce function...")

    csv_content = """timestamp,location,price,quantity
2024-01-01T10:00:00Z,Store A,10.0,2
2024-01-01T11:00:00Z,Store B,0.0,3
"""

    dsl_content = """
    schema Sale {
        timestamp: timestamp
        location: string
        price: float
        quantity: float
    }

    pipeline TestPipeline using Sale {
        calc safe_price = coalesce(price, 5.0) : float @stage(pre)
        group_by([location])
        aggregate sum(safe_price) as total_price
    }
    """

    result = run_mtl(csv_content, dsl_content)
    assert result.returncode == 0, f"Failed: {result.stderr}"

    output = json.loads(result.stdout)
    assert len(output) == 2

    store_a = next(o for o in output if o['location'] == 'Store A')
    store_b = next(o for o in output if o['location'] == 'Store B')

    assert store_a['total_price'] == 10.0
    assert store_b['total_price'] == 0.0  # 0.0 is not None, so price is used
    print("✓ Coalesce function works")


def test_schema_calculated_field():
    """Test calculated fields defined in schema."""
    print("\nTesting schema-level calculated fields...")

    csv_content = """timestamp,location,price,quantity
2024-01-01T10:00:00Z,Store A,10.0,2
2024-01-01T11:00:00Z,Store B,20.0,3
"""

    dsl_content = """
    schema Sale {
        timestamp: timestamp
        location: string
        price: float
        quantity: float
        revenue: float = price * quantity
    }

    pipeline TestPipeline using Sale {
        aggregate sum(revenue) as total_revenue
    }
    """

    result = run_mtl(csv_content, dsl_content)
    assert result.returncode == 0, f"Failed: {result.stderr}"

    output = json.loads(result.stdout)
    # Store A: 20, Store B: 60, total: 80
    assert output[0]['total_revenue'] == 80.0
    print("✓ Schema-level calculated fields work")


def test_parameter_type_checking():
    """Test that parameter types are enforced."""
    print("\nTesting parameter type checking...")

    csv_content = """timestamp,location,price,quantity
2024-01-01T10:00:00Z,Store A,10.0,2
"""

    dsl_content = """
    schema Sale {
        timestamp: timestamp
        location: string
        price: float
        quantity: float
    }

    pipeline TestPipeline using Sale {
        params {
            min_price: float = 0.0
        }
        filter(price >= param("min_price"))
        aggregate sum(price) as total_price
    }
    """

    # Provide wrong type (string where float expected)
    result = run_mtl(csv_content, dsl_content, params={'min_price': 'not_a_number'})
    assert result.returncode == 2, f"Should have failed with type error: {result.stderr}"
    assert "type_error" in result.stderr
    print("✓ Parameter type checking works")


def test_required_parameter_missing():
    """Test that missing required parameters cause error."""
    print("\nTesting required parameter validation...")

    csv_content = """timestamp,location,price,quantity
2024-01-01T10:00:00Z,Store A,10.0,2
"""

    dsl_content = """
    schema Sale {
        timestamp: timestamp
        location: string
        price: float
        quantity: float
    }

    pipeline TestPipeline using Sale {
        params {
            min_price: float  # No default, required
        }
        filter(price >= param("min_price"))
        aggregate sum(price) as total_price
    }
    """

    # Don't provide required parameter
    result = run_mtl(csv_content, dsl_content, params={})
    assert result.returncode == 1, f"Should have failed: {result.stderr}"
    assert "bad_dsl" in result.stderr
    print("✓ Required parameter validation works")


def test_multiple_calcs_with_lag():
    """Test multiple post-agg calcs with lag."""
    print("\nTesting multiple calcs with lag...")

    csv_content = """timestamp,location,price,quantity
2024-01-01T10:00:00Z,Store A,10.0,1
2024-01-01T11:00:00Z,Store A,20.0,1
2024-01-01T12:00:00Z,Store A,30.0,1
"""

    dsl_content = """
    schema Sale {
        timestamp: timestamp
        location: string
        price: float
        quantity: float
    }

    pipeline TestPipeline using Sale {
        aggregate sum(price) as total
        calc prev_total = lag(total, 1) : float @stage(post_agg)
        calc diff = total - coalesce(prev_total, 0.0) : float @stage(post_agg)
    }
    """

    result = run_mtl(csv_content, dsl_content)
    assert result.returncode == 0, f"Failed: {result.stderr}"

    output = json.loads(result.stdout)
    assert len(output) == 1  # No grouping, single result
    assert output[0]['total'] == 60.0  # 10+20+30
    assert output[0]['prev_total'] is None
    assert output[0]['diff'] == 60.0
    print("✓ Multiple calcs with lag work")


def test_cast_function():
    """Test explicit cast function."""
    print("\nTesting cast function...")

    csv_content = """timestamp,location,price,quantity
2024-01-01T10:00:00Z,Store A,10.9,2
"""

    dsl_content = """
    schema Sale {
        timestamp: timestamp
        location: string
        price: float
        quantity: float
    }

    pipeline TestPipeline using Sale {
        calc price_int = cast(price as int) : int @stage(pre)
        aggregate sum(price_int) as total_price_int
    }
    """

    result = run_mtl(csv_content, dsl_content)
    assert result.returncode == 0, f"Failed: {result.stderr}"

    output = json.loads(result.stdout)
    assert output[0]['total_price_int'] == 10  # 10.9 cast to int = 10
    print("✓ Cast function works")


def run_all_tests():
    """Run all tests."""
    tests = [
        test_basic_part1_compatibility,
        test_schema_with_inheritance,
        test_type_override_widening,
        test_illegal_type_override,
        test_pipeline_parameters,
        test_pre_stage_calculated_fields,
        test_post_agg_calculated_fields,
        test_lag_function,
        test_coalesce_function,
        test_schema_calculated_field,
        test_parameter_type_checking,
        test_required_parameter_missing,
        test_multiple_calcs_with_lag,
        test_cast_function,
    ]

    passed = 0
    failed = 0

    for test in tests:
        try:
            test()
            passed += 1
        except Exception as e:
            print(f"✗ {test.__name__} failed: {e}")
            import traceback
            traceback.print_exc()
            failed += 1

    print(f"\n{'='*50}")
    print(f"Results: {passed} passed, {failed} failed")
    print(f"{'='*50}")

    return failed == 0


if __name__ == "__main__":
    success = run_all_tests()
    sys.exit(0 if success else 1)
