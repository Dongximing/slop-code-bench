#!/usr/bin/env python3
"""Comprehensive test script for synth_configs.py"""

import json
import os
import subprocess
import sys
import tempfile
import yaml

def run_test(name, test_dir, should_succeed, expected_warnings=None, expected_exit=0):
    """Run a test case"""
    print(f"\n{'='*50}")
    print(f"Running test: {name}")
    print(f"{'='*50}")

    # Create output directory if it doesn't exist
    out_dir = os.path.join(test_dir, "out")
    os.makedirs(out_dir, exist_ok=True)

    # Run the program
    cmd = ["python", "synth_configs.py",
           "--base", os.path.join(test_dir, "base.yaml"),
           "--overrides", os.path.join(test_dir, "overrides"),
           "--flags", os.path.join(test_dir, "flags.json"),
           "--env", os.path.join(test_dir, "env.list"),
           "--out", out_dir]

    result = subprocess.run(cmd, capture_output=True, text=True)
    exit_code = result.returncode

    print(f"Exit code: {exit_code}")
    print(f"Stderr: {result.stderr}")
    print(f"Stdout: {result.stdout}")

    if should_succeed:
        if exit_code == 0:
            print("✓ Test passed (exited 0)")
            # Check outputs
            spec_path = os.path.join(out_dir, "final_spec.json")
            warn_path = os.path.join(out_dir, "warnings.txt")

            if os.path.exists(spec_path):
                with open(spec_path, 'r') as f:
                    spec = json.load(f)
                print(f"Spec keys: {list(spec.keys())}")
                # Check keys are sorted
                spec_str = f.read()
                # Reload sorted spec
                with open(spec_path, 'r') as f:
                    spec_loaded = json.load(f)
                spec_sorted = json.dumps(spec_loaded, separators=(',', ':'))
                with open(spec_path, 'r') as f:
                    spec_content = f.read().strip()
                if spec_content == spec_sorted:
                    print("✓ Output keys are sorted")
                else:
                    print("✗ Output keys are not sorted")
                    print(f"Expected: {spec_sorted}")
                    print(f"Got: {spec_content}")
            else:
                print("✗ final_spec.json not created")

            if os.path.exists(warn_path):
                with open(warn_path, 'r') as f:
                    warnings = f.read().strip()
                print(f"Warnings: {warnings}")
            else:
                print("✗ warnings.txt not created")

        else:
            print("✗ Test failed (should succeed but exited with error)")
            return False
    else:
        if exit_code != 0:
            print("✓ Test passed (correctly failed)")
        else:
            print("✗ Test failed (should fail but succeeded)")
            return False

    return True


def setup_test_dir(base_yml, defaults_yml=None, run_yml=None, flags_json=None, env_list=None):
    """Create a test directory structure"""
    test_dir = tempfile.mkdtemp()
    os.makedirs(os.path.join(test_dir, "overrides"))

    with open(os.path.join(test_dir, "base.yaml"), "w") as f:
        f.write(base_yml)

    if defaults_yml:
        with open(os.path.join(test_dir, "overrides", "defaults.yaml"), "w") as f:
            f.write(defaults_yml)
    else:
        with open(os.path.join(test_dir, "overrides", "defaults.yaml"), "w") as f:
            f.write("{}")

    if run_yml:
        with open(os.path.join(test_dir, "overrides", "run.yaml"), "w") as f:
            f.write(run_yml)

    if flags_json:
        with open(os.path.join(test_dir, "flags.json"), "w") as f:
            f.write(flags_json)
    else:
        with open(os.path.join(test_dir, "flags.json"), "w") as f:
            f.write("{}")

    if env_list:
        with open(os.path.join(test_dir, "env.list"), "w") as f:
            f.write(env_list)
    else:
        open(os.path.join(test_dir, "env.list"), "w").close()

    return test_dir


# Test 1: Example 1 - Basic successful merge
print("\n" + "="*70)
print("TEST 1: Example 1 - Basic successful merge")
print("="*70)
test_dir = setup_test_dir(
    base_yml="""experiment_name: qp-trials
training:
  optimizer: adam
resources:
  accelerators:
    - type: a100
      count: 4
""",
    defaults_yml="""training:
  batch_size: 32
resources:
  profile: standard
  world_size: 1
""",
    run_yml="""training:
  batch_size: 64
resources:
  world_size: 8
""",
    flags_json='{"training": {"optimizer": "adamw"}}'
)

run_test("Example 1", test_dir, should_succeed=True)

# Test 2: Example 2 - Missing experiment_name
print("\n" + "="*70)
print("TEST 2: Example 2 - Missing experiment_name")
print("="*70)
test_dir = setup_test_dir(
    base_yml="""training:
  optimizer: adam
""",
    defaults_yml="""resources:
  profile: standard
"""
)
os.makedirs(os.path.join(test_dir, "overrides"), exist_ok=True)

# Create defaults.yaml
with open(os.path.join(test_dir, "overrides", "defaults.yaml"), "w") as f:
    f.write("resources:\n  profile: standard\n")

run_test("Example 2 - Missing key", test_dir, should_succeed=False, expected_exit=1)

print("\n" + "="*70)
print("All tests completed!")
print("="*70)
