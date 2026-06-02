#!/usr/bin/env python3
"""Test script for library composition functionality."""

import json
import subprocess
import sys


def run_test(name, input_json, execute=False, expected_output=None):
    """Run a single test and check output."""
    print(f"\n=== Test: {name} ===")
    cmd = ["python", "etl_pipeline.py"]
    if execute:
        cmd.append("--execute")

    result = subprocess.run(
        cmd,
        input=json.dumps(input_json),
        capture_output=True,
        text=True
    )

    print(f"Exit code: {result.returncode}")
    print(f"Stdout: {result.stdout[:500]}")

    if result.returncode != 0:
        print(f"Stderr: {result.stderr[:500]}")

    if expected_output:
        try:
            actual = json.loads(result.stdout)
            expected = json.loads(expected_output)
            if actual == expected:
                print("✓ PASS")
                return True
            else:
                print("✗ FAIL - Output mismatch")
                print(f"Expected: {json.dumps(expected, indent=2)}")
                print(f"Actual: {json.dumps(actual, indent=2)}")
                return False
        except Exception as e:
            print(f"✗ FAIL - Error comparing output: {e}")
            return False
    else:
        # Just check if execution succeeded
        try:
            actual = json.loads(result.stdout)
            if actual.get("status") == "ok":
                print("✓ PASS (status=ok)")
                return True
            else:
                print(f"✗ FAIL - status not ok: {actual.get('status')}")
                return False
        except Exception as e:
            print(f"✗ FAIL - Error parsing output: {e}")
            return False


def main():
    """Run all tests."""
    tests_passed = 0
    total_tests = 0

    # Test 1: Basic library composition with params
    total_tests += 1
    test1_input = {
        "library": {
            "stats": {"defs": {
                "Normalize": {"steps":[{"op":"map","as":"z","expr":"(x - params.mu) / params.s"}]},
                "KeepZ": {"steps":[{"op":"select","columns":["id","z"]}]}
            }}
        },
        "compose": [
            {"ref":"stats:Normalize","params":{"mu":10,"s":5}},
            {"ref":"stats:KeepZ"}
        ],
        "dataset":[
            {"id":1,"x":15},
            {"id":2,"x":5}
        ]
    }
    test1_expected = {
        "status":"ok",
        "data":[
            {"id":1,"z":1},
            {"id":2,"z":-1}
        ],
        "metrics":{"rows_in":2,"rows_out":2}
    }
    if run_test("Basic library composition with params", test1_input, execute=True, expected_output=json.dumps(test1_expected)):
        tests_passed += 1

    # Test 2: Mix inline steps and library reference
    total_tests += 1
    test2_input = {
        "library": {
            "tags": {"defs": {
                "Label": {"steps":[{"op":"map","as":"tag","expr":"params.label"}]}
            }}
        },
        "compose": [
            {"steps":[{"op":"map","as":"y","expr":"x * 2"}]},
            {"ref":"tags:Label","params":{"label":"A"}}
        ],
        "dataset":[{"id":1,"x":3}]
    }
    test2_expected = {
        "status":"ok",
        "data":[
            {"id":1,"x":3,"y":6,"tag":"A"}
        ],
        "metrics":{"rows_in":1,"rows_out":1}
    }
    if run_test("Mix inline steps and library reference", test2_input, execute=True, expected_output=json.dumps(test2_expected)):
        tests_passed += 1

    # Test 3: Unknown namespace
    total_tests += 1
    test3_input = {
        "library": { "stats": { "defs": {} } },
        "compose": [ {"ref":"metrics:Normalize"} ],
        "dataset":[{"id":1,"x":0}]
    }
    # Test 3a: Unknown namespace error
    total_tests += 1
    if run_test("Unknown namespace (execute)", test3_input, execute=True):
        tests_passed += 1

    # Test 4: Normalize mode (--execute=false)
    total_tests += 1
    test4_input = {
        "library": {
            "ops": { "defs": {
                "AddX": { "steps":[{"op":"map","as":"x","expr":"1"}] }
            } }
        },
        "compose": [
            {"ref":"ops:AddX"},
            {"steps":[{"op":"select","columns":["x"]}]}
        ],
        "dataset":[]
    }
    test4_expected_steps = [
        {"op":"map","as":"x","expr":"1"},
        {"op":"select","columns":["x"]}
    ]
    test4_expected = {
        "status":"ok",
        "normalized":{
            "steps":test4_expected_steps
        }
    }
    if run_test("Normalize mode (--execute=false)", test4_input, execute=False, expected_output=json.dumps(test4_expected)):
        tests_passed += 1

    # Test 5: Empty compose array
    total_tests += 1
    test5_input = {
        "library": {},
        "compose": [],
        "dataset": [{"id":1, "x":10}]
    }
    test5_expected = {
        "status":"ok",
        "data": [{"id":1,"x":10}],
        "metrics":{"rows_in":1,"rows_out":1}
    }
    if run_test("Empty compose array", test5_input, execute=True, expected_output=json.dumps(test5_expected)):
        tests_passed += 1

    # Test 6: Library reference with call step and params
    total_tests += 1
    test6_input = {
        "library": {
            "calc": {"defs": {
                "AddAndSelect": {
                    "steps": [
                        {"op":"map","as":"a","expr":"params.base + 5"},
                        {"op":"select","columns":["a"]}
                    ]
                }
            }}
        },
        "compose": [
            {"ref":"calc:AddAndSelect","params":{"base":10}}
        ],
        "dataset":[{"id":1}]
    }
    test6_expected = {
        "status":"ok",
        "data": [{"a":15}],
        "metrics":{"rows_in":1,"rows_out":1}
    }
    if run_test("Library reference with params expression", test6_input, execute=True, expected_output=json.dumps(test6_expected)):
        tests_passed += 1

    print(f"\n\n=== Summary: {tests_passed}/{total_tests} tests passed ===")
    return 0 if tests_passed == total_tests else 1


if __name__ == "__main__":
    sys.exit(main())
