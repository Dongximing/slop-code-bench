#!/usr/bin/env python3
"""Test the generated DynamicPreprocessor module."""

import sys
sys.path.insert(0, '/workspace/gen')

from dyna import DynamicPreprocessor

pre = DynamicPreprocessor(buffer=2)
test_csv = "/workspace/sample1/input.csv"

print("Testing generated preprocessor...")
print("-" * 50)

rows = list(pre(test_csv))
print(f"\nProcessed {len(rows)} rows:")
for i, row in enumerate(rows):
    print(f"  Row {i}: {row}")

expected_rows = [
    {"id": "1", "name": "Alice"},
    {"id": "3", "name": "Carol"}
]

print(f"\nVerification:")
if len(rows) == len(expected_rows):
    print(f"  ✓ Row count matches: {len(rows)} rows")

    all_match = True
    for i, (actual, expected) in enumerate(zip(rows, expected_rows)):
        if actual == expected:
            print(f"  ✓ Row {i} matches: {actual}")
        else:
            print(f"  ✗ Row {i} mismatch:")
            print(f"      Actual:   {actual}")
            print(f"      Expected: {expected}")
            all_match = False

    if all_match:
        print("\n✅ Test PASSED!")
    else:
        print("\n❌ Test FAILED!")
        sys.exit(1)
else:
    print(f"\n❌ Test FAILED! Expected {len(expected_rows)} rows, got {len(rows)}")
    sys.exit(1)
