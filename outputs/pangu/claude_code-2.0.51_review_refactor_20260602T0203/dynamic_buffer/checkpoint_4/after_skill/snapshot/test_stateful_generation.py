#!/usr/bin/env python3
"""
Test dynamic_buffer.py with sample data for stateful transforms.
"""

import sys
import os
import tempfile
import json

sys.path.insert(0, '/workspace/dynamic_buffer_gen')

from dynamic_buffer import (
    infer_transformation,
    generate_python_module,
    detect_format,
    read_sample_data
)

def test_cumulative_sum():
    """Test prefix sum/cumulative transform inference."""

    # Create sample data
    input_csv = """t,value
1,10
2,20
3,5
4,15
"""
    output_csv = """t,value,cumulative
1,10,10
2,20,30
3,5,35
4,15,50
"""

    with tempfile.TemporaryDirectory() as tmpdir:
        # Write input
        input_path = os.path.join(tmpdir, 'input.csv')
        with open(input_path, 'w') as f:
            f.write(input_csv)

        # Write output
        output_path = os.path.join(tmpdir, 'output.csv')
        with open(output_path, 'w') as f:
            f.write(output_csv)

        # Read and infer
        fmt = detect_format(input_path)
        input_rows = read_sample_data(input_path, fmt)
        output_rows = read_sample_data(output_path, fmt)

        transform = infer_transformation(input_rows, output_rows, fmt)

        print("=== Cumulative Sum Test ===")
        print(f"Stateful transforms: {len(transform.stateful_transforms)}")
        for st in transform.stateful_transforms:
            print(f"  - {st.output_column}: {st.transform_subtype}")
            if hasattr(st, 'input_column'):
                print(f"    from: {st.input_column}")

        # Generate module
        output_dir = os.path.join(tmpdir, 'gen')
        generate_python_module(transform, output_dir, 'test_mod')

        # Try importing and using the generated module
        sys.path.insert(0, output_dir)
        from test_mod.dynamic_preprocessor import DynamicPreprocessor

        # Create a test input with more rows
        test_input = os.path.join(tmpdir, 'test_input.csv')
        with open(test_input, 'w') as f:
            f.write("""t,value
5,25
6,30
7,10
""")

        # Process with small buffer to test streaming
        preprocessor = DynamicPreprocessor(buffer=1, cache_dir=None)
        results = list(preprocessor(test_input))

        print(f"\nGenerated results:")
        for r in results:
            print(f"  t={r.get('t')}, value={r.get('value')}, cumulative={r.get('cumulative')}")

        expected_cumulative = [60, 85, 95]  # 50 + 25, + 30, + 10
        actual_cumulative = [r.get('cumulative') for r in results]

        if actual_cumulative == expected_cumulative:
            print("✓ Cumulative sum test PASSED")
            return True
        else:
            print(f"✗ Cumulative sum test FAILED: expected {expected_cumulative}, got {actual_cumulative}")
            return False


def test_sliding_window():
    """Test sliding window average inference."""

    # Create sample data
    input_jsonl = """{"id": 1, "score": 10}
{"id": 2, "score": 20}
{"id": 3, "score": 30}
{"id": 4, "score": 40}
"""
    output_jsonl = """{"id": 1, "score": 10, "avg3": 10}
{"id": 2, "score": 20, "avg3": 15}
{"id": 3, "score": 30, "avg3": 20}
{"id": 4, "score": 40, "avg3": 30}
"""

    with tempfile.TemporaryDirectory() as tmpdir:
        input_path = os.path.join(tmpdir, 'input.jsonl')
        with open(input_path, 'w') as f:
            f.write(input_jsonl)

        output_path = os.path.join(tmpdir, 'output.jsonl')
        with open(output_path, 'w') as f:
            f.write(output_jsonl)

        fmt = detect_format(input_path)
        input_rows = read_sample_data(input_path, fmt)
        output_rows = read_sample_data(output_path, fmt)

        transform = infer_transformation(input_rows, output_rows, fmt)

        print("\n=== Sliding Window Test ===")
        print(f"Stateful transforms: {len(transform.stateful_transforms)}")
        for st in transform.stateful_transforms:
            print(f"  - {st.output_column}: {st.transform_subtype}")

        # Generate module
        output_dir = os.path.join(tmpdir, 'gen')
        generate_python_module(transform, output_dir, 'test_mod')

        sys.path.insert(0, output_dir)
        from test_mod.dynamic_preprocessor import DynamicPreprocessor

        # Test with more rows
        test_input = os.path.join(tmpdir, 'test_input.jsonl')
        with open(test_input, 'w') as f:
            f.write("""{"id": 5, "score": 50}
{"id": 6, "score": 60}
""")

        preprocessor = DynamicPreprocessor(buffer=1, cache_dir=None)
        results = list(preprocessor(test_input))

        print(f"\nGenerated results:")
        for r in results:
            print(f"  id={r.get('id')}, score={r.get('score')}, avg3={r.get('avg3')}")

        expected_avg3 = [50, 60]  # Actually should be (40+50)/2=45 for row 5, (50+60)/2=55 for row 6
        # Wait, window=3: last 3 values: 20+30+40=30, etc. Let's compute properly:
        # Row 5 (score=50): last 3 = 30, 40, 50 -> avg=40
        # Row 6 (score=60): last 3 = 40, 50, 60 -> avg=50
        expected_avg3 = [40, 50]

        actual_avg3 = [r.get('avg3') for r in results]

        if actual_avg3 == expected_avg3:
            print("✓ Sliding window test PASSED")
            return True
        else:
            print(f"✗ Sliding window test FAILED: expected {expected_avg3}, got {actual_avg3}")
            return False


def test_neighbor_filter():
    """Test neighbor-based filtering."""

    input_json = """[
  {"i": 1, "status": "start"},
  {"i": 2, "status": "ok"},
  {"i": 3, "status": "duplicate"},
  {"i": 4, "status": "ok"},
  {"i": 5, "status": "duplicate"}
]
"""
    output_json = """[
  {"i": 1, "status": "start"},
  {"i": 2, "status": "ok"},
  {"i": 4, "status": "ok"}
]
"""

    with tempfile.TemporaryDirectory() as tmpdir:
        input_path = os.path.join(tmpdir, 'input.json')
        with open(input_path, 'w') as f:
            f.write(input_json)

        output_path = os.path.join(tmpdir, 'output.json')
        with open(output_path, 'w') as f:
            f.write(output_json)

        fmt = detect_format(input_path)
        input_rows = read_sample_data(input_path, fmt)
        output_rows = read_sample_data(output_path, fmt)

        transform = infer_transformation(input_rows, output_rows, fmt)

        print("\n=== Neighbor Filter Test ===")
        print(f"Simple filters: {len(transform.filter_conditions)}")
        for f in transform.filter_conditions:
            print(f"  - {f.column} {f.operator} {f.value}")
        print(f"Neighbor filters: {len(transform.neighbor_filters)}")
        for nf in transform.neighbor_filters:
            print(f"  - filter_type: {nf.filter_type}, column: {nf.column}, value: {nf.value}")

        return True


if __name__ == '__main__':
    test_cumulative_sum()
    print()
    test_sliding_window()
    print()
    test_neighbor_filter()
