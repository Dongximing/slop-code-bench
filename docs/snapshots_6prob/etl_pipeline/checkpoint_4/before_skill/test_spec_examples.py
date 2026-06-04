#!/usr/bin/env python3
"""Test exact examples from specification."""

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

# Example 1: Basic call with parameters (from spec)
example1_input = {
    "defs": {
        "Normalize": { "steps":[
            {"op":"map","as":"z","expr":"(value - params.mean) / params.std"}
        ]},
        "KeepCols": { "steps":[
            {"op":"select","columns":["id","z"]}
        ]}
    },
    "pipeline": { "steps": [
        {"op":"call","name":"Normalize","params":{"mean":100,"std":20}},
        {"op":"call","name":"KeepCols","params":{}}
    ]},
    "dataset":[
        {"id":1,"value":120},
        {"id":2,"value":80}
    ]
}

example1_result = run_test("Example 1: Basic call with parameters", example1_input)
expected_output_1 = {
    "status": "ok",
    "data": [
        {"id": 1, "z": 1},
        {"id": 2, "z": -1}
    ]
}
print("\nExpected output (data):", json.dumps(expected_output_1["data"]))

# Example 2: Missing definition (from spec)
example2_input = {
    "defs": {},
    "pipeline": { "steps": [ {"op":"call","name":"Missing","params":{}} ] },
    "dataset":[{"id":1}]
}

example2_result = run_test("Example 2: Missing definition", example2_input)

# Example 3: Direct recursion (from spec)
example3_input = {
    "defs": {
        "Loop": { "steps":[ {"op":"call","name":"Loop","params":{}} ] }
    },
    "pipeline": { "steps": [ {"op":"call","name":"Loop","params":{}} ] },
    "dataset":[{"id":1}]
}

example3_result = run_test("Example 3: Direct recursion", example3_input)

print("\n" + "="*60)
print("Checking against expected outputs from specification:")
print("="*60)

# Verify example 1 output
print("\nExample 1 output check:")
output1 = json.loads(example1_result.stdout)
if output1.get("data") == expected_output_1["data"]:
    print("✓ Data matches expected output")
else:
    print("✗ Data does not match")
    print(f"  Expected: {expected_output_1['data']}")
    print(f"  Got: {output1.get('data')}")

# Verify error code for example 2
print("\nExample 2 output check:")
output2 = json.loads(example2_result.stdout)
if output2.get("error_code") == "UNKNOWN_DEF":
    print("✓ Error code is UNKNOWN_DEF")
else:
    print(f"✗ Expected UNKNOWN_DEF, got: {output2.get('error_code')}")

# Verify error code for example 3
print("\nExample 3 output check:")
output3 = json.loads(example3_result.stdout)
if output3.get("error_code") == "RECURSION_FORBIDDEN":
    print("✓ Error code is RECURSION_FORBIDDEN")
else:
    print(f"✗ Expected RECURSION_FORBIDDEN, got: {output3.get('error_code')}")
