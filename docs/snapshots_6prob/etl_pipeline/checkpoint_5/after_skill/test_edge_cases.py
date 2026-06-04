#!/usr/bin/env python3
"""Test edge cases for sub-pipelines feature."""

import json
import subprocess

def run_test(test_name, input_data):
    """Run a test and print the result."""
    print(f"\n{'='*60}")
    print(f"Test: {test_name}")
    print(f"{'='*60}")
    print("Input:")
    print(json.dumps(input_data, indent=2))

    result = subprocess.run(
        ['python', 'etl_pipeline.py', '--execute'],
        input=json.dumps(input_data),
        capture_output=True,
        text=True
    )

    print("\nOutput:")
    print(result.stdout)
    if result.stderr:
        print("STDERR:", result.stderr)
    print("Return code:", result.returncode)
    return result

# Test 1: Definitions that are not used should be allowed
test1_input = {
    "defs": {
        "Unused": {
            "steps": [
                {"op": "map", "as": "x", "expr": "value * 2"}
            ]
        }
    },
    "pipeline": {
        "steps": [
            {"op": "select", "columns": ["id"]}
        ]
    },
    "dataset": [
        {"id": 1, "value": 10}
    ]
}

run_test("1) Unused definitions are allowed", test1_input)

# Test 2: Unknown top-level keys should be ignored
test2_input = {
    "defs": {
        "Test": {
            "steps": [
                {"op": "map", "as": "z", "expr": "value + 1"}
            ]
        }
    },
    "pipeline": {
        "steps": [
            {"op": "call", "name": "Test", "params": {}}
        ]
    },
    "dataset": [
        {"id": 1, "value": 5}
    ],
    "unknown_field": "should be ignored",
    "another_unknown": {"nested": "value"}
}

run_test("2) Unknown top-level keys are ignored", test2_input)

# Test 3: Call with params containing arrays
test3_input = {
    "defs": {
        "FilterIn": {
            "steps": [
                {"op": "filter", "where": "value in params.allowed"}
            ]
        }
    },
    "pipeline": {
        "steps": [
            {"op": "call", "name": "FilterIn", "params": {"allowed": [10, 20, 30]}}
        ]
    },
    "dataset": [
        {"id": 1, "value": 5},
        {"id": 2, "value": 10},
        {"id": 3, "value": 25}
    ]
}

run_test("3) Call with array params", test3_input)

# Test 4: Nested defs and calls
test4_input = {
    "defs": {
        "Outer": {
            "steps": [
                {"op": "map", "as": "transformed", "expr": "value * params.factor"},
                {"op": "call", "name": "Inner", "params": {}}
            ]
        },
        "Inner": {
            "steps": [
                {"op": "map", "as": "doubled", "expr": "transformed + 10"}
            ]
        }
    },
    "pipeline": {
        "steps": [
            {"op": "call", "name": "Outer", "params": {"factor": 2}}
        ]
    },
    "dataset": [
        {"id": 1, "value": 5}
    ]
}

run_test("4) Nested calls", test4_input)

# Test 5: Invalid def name (should fail validation)
test5_input = {
    "defs": {
        "123Invalid": {
            "steps": [
                {"op": "map", "as": "z", "expr": "value + 1"}
            ]
        }
    },
    "pipeline": {
        "steps": [
            {"op": "call", "name": "123Invalid", "params": {}}
        ]
    },
    "dataset": [
        {"id": 1}
    ]
}

run_test("5) Invalid definition name (starts with digit)", test5_input)

# Test 6: Call with missing params should work (use empty params)
test6_input = {
    "defs": {
        "NoParams": {
            "steps": [
                {"op": "map", "as": "result", "expr": "value * 2"}
            ]
        }
    },
    "pipeline": {
        "steps": [
            {"op": "call", "name": "NoParams"}
        ]
    },
    "dataset": [
        {"id": 1, "value": 5}
    ]
}

run_test("6) Call without params (should default to empty)", test6_input)

# Test 7: params reserved keyword - should return null when accessed from row
test7_input = {
    "defs": {
        "TestReserved": {
            "steps": [
                {"op": "map", "as": "row_params", "expr": "params"},
                {"op": "map", "as": "row_value", "expr": "value"}
            ]
        }
    },
    "pipeline": {
        "steps": [
            {"op": "call", "name": "TestReserved", "params": {"x": 10}}
        ]
    },
    "dataset": [
        {"id": 1, "value": 100}
    ]
}

run_test("7) 'params' is reserved keyword in row access", test7_input)

# Test 8: Complex multi-step definition with filter and map
test8_input = {
    "defs": {
        "FilterAndTransform": {
            "steps": [
                {"op": "filter", "where": "value > params.min"},
                {"op": "map", "as": "z", "expr": "(value - params.mean) / params.std"},
                {"op": "select", "columns": ["id", "z"]}
            ]
        }
    },
    "pipeline": {
        "steps": [
            {"op": "call", "name": "FilterAndTransform", "params": {"min": 0, "mean": 50, "std": 20}}
        ]
    },
    "dataset": [
        {"id": 1, "value": 10},
        {"id": 2, "value": 60},
        {"id": 3, "value": 90}
    ]
}

run_test("8) Complex multi-step with filter", test8_input)

print("\n" + "="*60)
print("All edge case tests completed!")
print("="*60)
