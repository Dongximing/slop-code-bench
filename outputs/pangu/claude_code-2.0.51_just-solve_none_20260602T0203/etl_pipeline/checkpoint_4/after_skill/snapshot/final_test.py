#!/usr/bin/env python3
"""Final verification test for sub-pipelines feature."""

import json
import subprocess
import sys

def run_test(name, input_data):
    """Run a test and return parsed output."""
    result = subprocess.run(
        [sys.executable, "etl_pipeline.py", "--execute"],
        input=json.dumps(input_data),
        capture_output=True,
        text=True
    )
    return json.loads(result.stdout)

# Test 1: Basic call with parameters
output = run_test("Example 1", {
    "defs": {
        "Normalize": {"steps": [
            {"op":"map","as":"z","expr":"(value - params.mean) / params.std"}
        ]},
        "KeepCols": {"steps": [
            {"op":"select","columns":["id","z"]}
        ]}
    },
    "pipeline": {"steps": [
        {"op":"call","name":"Normalize","params":{"mean":100,"std":20}},
        {"op":"call","name":"KeepCols","params":{}}
    ]},
    "dataset":[{"id":1,"value":120},{"id":2,"value":80}]
})
assert output["status"] == "ok"
assert output["data"] == [{"id":1,"z":1.0},{"id":2,"z":-1.0}]
print("✓ Example 1 passed")

# Test 2: Missing definition
output = run_test("Example 2", {
    "defs": {},
    "pipeline": {"steps": [{"op":"call","name":"Missing","params":{}}]},
    "dataset":[{"id":1}]
})
assert output["status"] == "error"
assert output["error_code"] == "UNKNOWN_DEF"
print("✓ Example 2 passed")

# Test 3: Direct recursion
output = run_test("Example 3", {
    "defs": {
        "Loop": {"steps":[{"op":"call","name":"Loop","params":{}}]}
    },
    "pipeline": {"steps":[{"op":"call","name":"Loop","params":{}}]},
    "dataset":[{"id":1}]
})
assert output["status"] == "error"
assert output["error_code"] == "RECURSION_FORBIDDEN"
print("✓ Example 3 passed")

# Test 4: Unknown top-level keys are ignored
output = run_test("Unknown keys", {
    "defs": {
        "Test": {"steps":[{"op":"map","as":"z","expr":"value + 1"}]}
    },
    "pipeline": {"steps":[{"op":"call","name":"Test","params":{}}]},
    "dataset":[{"id":1,"value":5}],
    "unknown_field": "should be ignored",
    "another_unknown": {"nested": "value"}
})
assert output["status"] == "ok"
assert output["data"][0]["z"] == 6
print("✓ Unknown keys test passed")

# Test 5: params reserved keyword
output = run_test("Reserved params", {
    "defs": {
        "Test": {"steps":[{"op":"map","as":"x","expr":"params"}]}
    },
    "pipeline": {"steps":[{"op":"call","name":"Test","params":{"x":1}}]},
    "dataset":[{"id":1,"value":5}]
})
assert output["status"] == "ok"
assert output["data"][0]["x"] is None
print("✓ Reserved params test passed")

# Test 6: Nested calls
output = run_test("Nested calls", {
    "defs": {
        "Inner": {"steps":[{"op":"map","as":"result","expr":"value + 1"}]},
        "Outer": {"steps":[
            {"op":"map","as":"value","expr":"value * params.factor"},
            {"op":"call","name":"Inner","params":{}}
        ]}
    },
    "pipeline": {"steps":[{"op":"call","name":"Outer","params":{"factor":2}}]},
    "dataset":[{"id":1,"value":5}]
})
assert output["status"] == "ok"
assert output["data"][0]["result"] == 11
print("✓ Nested calls test passed")

print("\nAll tests passed!")
