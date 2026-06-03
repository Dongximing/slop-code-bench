#!/usr/bin/env python3
"""Test script for sub-pipelines feature."""

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

# Test 1: Basic call with parameters
test1_input = {
    "defs": {
        "Normalize": {
            "steps": [
                {
                    "op": "map",
                    "as": "z",
                    "expr": "(value - params.mean) / params.std"
                }
            ]
        },
        "KeepCols": {
            "steps": [
                {
                    "op": "select",
                    "columns": ["id", "z"]
                }
            ]
        }
    },
    "pipeline": {
        "steps": [
            {"op": "call", "name": "Normalize", "params": {"mean": 100, "std": 20}},
            {"op": "call", "name": "KeepCols", "params": {}}
        ]
    },
    "dataset": [
        {"id": 1, "value": 120},
        {"id": 2, "value": 80}
    ]
}

run_test("1) Basic call with parameters", test1_input)

# Test 2: Missing definition
test2_input = {
    "defs": {},
    "pipeline": {
        "steps": [
            {"op": "call", "name": "Missing", "params": {}}
        ]
    },
    "dataset": [
        {"id": 1}
    ]
}

run_test("2) Missing definition", test2_input)

# Test 3: Direct recursion
test3_input = {
    "defs": {
        "Loop": {
            "steps": [
                {"op": "call", "name": "Loop", "params": {}}
            ]
        }
    },
    "pipeline": {
        "steps": [
            {"op": "call", "name": "Loop", "params": {}}
        ]
    },
    "dataset": [
        {"id": 1}
    ]
}

run_test("3) Direct recursion", test3_input)

# Test 4: Indirect recursion (A calls B, B calls A)
test4_input = {
    "defs": {
        "A": {
            "steps": [
                {"op": "call", "name": "B", "params": {}}
            ]
        },
        "B": {
            "steps": [
                {"op": "call", "name": "A", "params": {}}
            ]
        }
    },
    "pipeline": {
        "steps": [
            {"op": "call", "name": "A", "params": {}}
        ]
    },
    "dataset": [
        {"id": 1}
    ]
}

run_test("4) Indirect recursion", test4_input)

# Test 5: Call with filter
test5_input = {
    "defs": {
        "FilterValue": {
            "steps": [
                {"op": "filter", "where": "value > params.threshold"}
            ]
        }
    },
    "pipeline": {
        "steps": [
            {"op": "call", "name": "FilterValue", "params": {"threshold": 50}}
        ]
    },
    "dataset": [
        {"id": 1, "value": 10},
        {"id": 2, "value": 60},
        {"id": 3, "value": 100}
    ]
}

run_test("5) Call with filter and params", test5_input)

# Test 6: Multiple calls in sequence
test6_input = {
    "defs": {
        "AddOne": {
            "steps": [
                {"op": "map", "as": "value", "expr": "value + 1"}
            ]
        },
        "MultiplyByTwo": {
            "steps": [
                {"op": "map", "as": "value", "expr": "value * 2"}
            ]
        }
    },
    "pipeline": {
        "steps": [
            {"op": "call", "name": "AddOne", "params": {}},
            {"op": "call", "name": "MultiplyByTwo", "params": {}}
        ]
    },
    "dataset": [
        {"id": 1, "value": 5}
    ]
}

run_test("6) Multiple calls in sequence", test6_input)

# Test 7: Call with empty params
test7_input = {
    "defs": {
        "Double": {
            "steps": [
                {"op": "map", "as": "result", "expr": "value * params.multiplier"}
            ]
        }
    },
    "pipeline": {
        "steps": [
            {"op": "call", "name": "Double", "params": {}}
        ]
    },
    "dataset": [
        {"id": 1, "value": 10}
    ]
}

run_test("7) Call with empty params (should return null for params.multiplier)", test7_input)

# Test 8: Definition with multiple steps
test8_input = {
    "defs": {
        "Transform": {
            "steps": [
                {"op": "map", "as": "z", "expr": "(value - params.mean) / params.std"},
                {"op": "select", "columns": ["id", "z"]}
            ]
        }
    },
    "pipeline": {
        "steps": [
            {"op": "call", "name": "Transform", "params": {"mean": 100, "std": 20}}
        ]
    },
    "dataset": [
        {"id": 1, "value": 120},
        {"id": 2, "value": 80}
    ]
}

run_test("8) Definition with multiple steps", test8_input)

print("\n" + "="*60)
print("All tests completed!")
print("="*60)
