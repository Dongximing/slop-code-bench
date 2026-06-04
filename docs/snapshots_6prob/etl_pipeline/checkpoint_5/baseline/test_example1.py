#!/usr/bin/env python3
"""Test script for ETL pipeline examples."""

import json
import subprocess
import sys

def run_pipeline(input_json, execute=False):
    """Run the pipeline with given input and return output."""
    cmd = ["python3", "/workspace/etl_pipeline.py"]
    if execute:
        cmd.append("--execute=true")

    result = subprocess.run(
        cmd,
        input=json.dumps(input_json),
        capture_output=True,
        text=True
    )
    return json.loads(result.stdout)

def test_example1_basic_composition():
    """Test 1: Basic library composition with params"""
    input_data = {
        "library": {
            "stats": { "defs": {
                "Normalize": { "steps":[{ "op":"map", "as":"z", "expr":"(x - params.mu) / params.s" }] },
                "KeepZ": { "steps":[{ "op":"select", "columns":["id","z"] }] }
            } }
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

    expected = {
        "status":"ok",
        "data":[
            {"id":1,"z":1},
            {"id":2,"z":-1}
        ],
        "metrics":{"rows_in":2,"rows_out":2}
    }

    output = run_pipeline(input_data, execute=True)
    print("Test 1 - Basic library composition:")
    print(json.dumps(output, indent=2))
    assert output == expected, f"Expected {expected}, got {output}"
    print("PASSED!\n")

def test_example2_mixed_inline():
    """Test 2: Mix inline steps and library reference"""
    input_data = {
        "library": {
            "tags": { "defs": {
                "Label": { "steps":[{ "op":"map", "as":"tag", "expr":"params.label" }] }
            } }
        },
        "compose": [
            {"steps":[{"op":"map","as":"y","expr":"x * 2"}]},
            {"ref":"tags:Label","params":{"label":"A"}}
        ],
        "dataset":[{"id":1,"x":3}]
    }

    expected = {
        "status":"ok",
        "data":[
            {"id":1,"x":3,"y":6,"tag":"A"}
        ],
        "metrics":{"rows_in":1,"rows_out":1}
    }

    output = run_pipeline(input_data, execute=True)
    print("Test 2 - Mix inline steps and library reference:")
    print(json.dumps(output, indent=2))
    assert output == expected, f"Expected {expected}, got {output}"
    print("PASSED!\n")

def test_example3_unknown_namespace():
    """Test 3: Unknown namespace"""
    input_data = {
        "library": { "stats": { "defs": {} } },
        "compose": [ {"ref":"metrics:Normalize"} ],
        "dataset":[{"id":1,"x":0}]
    }

    output = run_pipeline(input_data, execute=True)
    print("Test 3 - Unknown namespace:")
    print(json.dumps(output, indent=2))
    assert output["status"] == "error", f"Expected error, got {output}"
    assert output["error_code"] == "UNKNOWN_NAMESPACE", f"Expected UNKNOWN_NAMESPACE, got {output}"
    assert output["path"] == "compose[0].ref", f"Expected path compose[0].ref, got {output}"
    print("PASSED!\n")

def test_example4_normalize_mode():
    """Test 4: Normalize mode (--execute=false)"""
    input_data = {
        "library": {
            "ops": { "defs": {
                "AddX": { "steps":[{ "op":"map", "as":"x", "expr":"1" }] }
            } }
        },
        "compose": [
            {"ref":"ops:AddX"},
            {"steps":[{ "op":"select", "columns":["x"] }]}
        ],
        "dataset":[]
    }

    expected = {
        "status":"ok",
        "normalized":{
            "steps":[
                {"op":"map","as":"x","expr":"1"},
                {"op":"select","columns":["x"]}
            ]
        }
    }

    output = run_pipeline(input_data, execute=False)
    print("Test 4 - Normalize mode:")
    print(json.dumps(output, indent=2))
    assert output == expected, f"Expected {expected}, got {output}"
    print("PASSED!\n")

def test_example5_unknown_ref():
    """Test 5: Unknown library reference"""
    input_data = {
        "library": {
            "stats": { "defs": { "Normalize": { "steps":[] } } }
        },
        "compose": [ {"ref":"stats:UnknownOp"} ],
        "dataset":[]
    }

    output = run_pipeline(input_data, execute=True)
    print("Test 5 - Unknown library reference:")
    print(json.dumps(output, indent=2))
    assert output["status"] == "error", f"Expected error, got {output}"
    assert output["error_code"] == "UNKNOWN_LIB_REF", f"Expected UNKNOWN_LIB_REF, got {output}"
    print("PASSED!\n")

def test_example6_empty_params():
    """Test 6: Empty params (should default to {})"""
    input_data = {
        "library": {
            "test": { "defs": {
                "Identity": { "steps":[{ "op":"map", "as":"result", "expr":"params.value" }] }
            } }
        },
        "compose": [
            {"ref":"test:Identity"}  # No params provided
        ],
        "dataset":[{"id":1}]
    }

    output = run_pipeline(input_data, execute=True)
    print("Test 6 - Empty params handling:")
    print(json.dumps(output, indent=2))
    # params should be {}, so params.value should resolve to null
    assert output["status"] == "ok", f"Expected ok, got {output}"
    assert output["data"][0]["result"] is None, f"Expected null result, got {output}"
    print("PASSED!\n")

if __name__ == "__main__":
    print("=" * 60)
    print("Running ETL Pipeline Tests")
    print("=" * 60 + "\n")

    test_example1_basic_composition()
    test_example2_mixed_inline()
    test_example3_unknown_namespace()
    test_example4_normalize_mode()
    test_example5_unknown_ref()
    test_example6_empty_params()

    print("=" * 60)
    print("ALL TESTS PASSED!")
    print("=" * 60)
