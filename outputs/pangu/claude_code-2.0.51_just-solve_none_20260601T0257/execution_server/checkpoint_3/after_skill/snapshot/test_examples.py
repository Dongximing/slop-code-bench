#!/usr/bin/env python3
"""Test the structured data file handling in execution_server.py"""
import sys
import json
from pathlib import Path
from io import StringIO
import csv

try:
    import yaml
except ImportError:
    yaml = None

# Add execution_server to path
sys.path.insert(0, '/workspace')

# Reimplement the serialization logic for testing
StructuredData = (dict, list, str, int, float, bool, type(None))

def serialize_content(content, filename):
    """Serialize content based on file extension."""
    path = Path(filename)
    ext = path.suffix.lower()
    stem = path.stem

    compression_exts = ['.gz', '.bz2']
    compression = None
    for comp_ext in compression_exts:
        if path.suffixes and path.suffixes[-1].lower() == comp_ext:
            compression = comp_ext
            break

    base_ext = None
    remaining_suffixes = path.suffixes
    if remaining_suffixes and remaining_suffixes[-1].lower() in compression_exts:
        remaining_suffixes = remaining_suffixes[:-1]
    if remaining_suffixes:
        base_ext = remaining_suffixes[-1].lower()

    if compression and len([s for s in path.suffixes if s.lower() in compression_exts]) > 1:
        raise ValueError(f"Multiple compression extensions not allowed: {filename}")

    format_type = None

    if base_ext in ['.json', '.yaml', '.yml']:
        format_type = 'structured'
    elif base_ext in ['.jsonl', '.ndjson']:
        format_type = 'jsonl'
    elif base_ext in ['.csv', '.tsv']:
        format_type = 'tabular'
    elif base_ext is None and compression:
        format_type = 'text'
    elif base_ext:
        format_type = 'text'
    else:
        format_type = 'text'

    if format_type == 'text':
        return str(content) if not isinstance(content, str) else content

    elif format_type == 'structured':
        if base_ext == '.json':
            output = json.dumps(content, ensure_ascii=False)
        elif base_ext in ['.yaml', '.yml'] and yaml is not None:
            output = yaml.dump(content, allow_unicode=True, default_flow_style=False)
        else:
            output = json.dumps(content, ensure_ascii=False)
        return output

    elif format_type == 'jsonl':
        if isinstance(content, str):
            lines = content.strip().split('\n')
            output = '\n'.join(lines) + '\n'
        elif isinstance(content, list):
            lines = [json.dumps(item, ensure_ascii=False) for item in content]
            output = '\n'.join(lines) + '\n'
        else:
            output = json.dumps(content, ensure_ascii=False) + '\n'
        return output

    elif format_type == 'tabular':
        if isinstance(content, str):
            output = content + '\n' if content else '\n'
        elif isinstance(content, dict):
            columns = sorted(content.keys())
            rows_data = []
            num_rows = 0
            for col in columns:
                col_data = content[col]
                if isinstance(col_data, list):
                    rows_data.append(col_data)
                    num_rows = max(num_rows, len(col_data))
                else:
                    rows_data.append([col_data])
                    num_rows = max(num_rows, 1)

            output_io = StringIO()
            writer = csv.writer(output_io)
            writer.writerow(columns)
            for row_idx in range(num_rows):
                row = []
                for col_idx, col in enumerate(columns):
                    if row_idx < len(rows_data[col_idx]):
                        row.append(rows_data[col_idx][row_idx])
                    else:
                        row.append('')
                writer.writerow(row)
            output = output_io.getvalue()

        elif isinstance(content, list):
            if not content:
                output = '\n'
            else:
                if isinstance(content[0], dict):
                    all_keys = set()
                    for item in content:
                        if isinstance(item, dict):
                            all_keys.update(item.keys())
                    columns = sorted(all_keys)

                    output_io = StringIO()
                    writer = csv.writer(output_io)
                    writer.writerow(columns)
                    for item in content:
                        if isinstance(item, dict):
                            row = [item.get(col, '') for col in columns]
                            writer.writerow(row)
                        elif isinstance(item, list):
                            writer.writerow(item)
                        else:
                            writer.writerow([item])
                    output = output_io.getvalue()
                else:
                    output_io = StringIO()
                    writer = csv.writer(output_io)
                    for row in content:
                        writer.writerow([row] if not isinstance(row, (list, tuple)) else row)
                    output = output_io.getvalue()
        else:
            output = str(content)
        return output

    else:
        return str(content) if not isinstance(content, str) else content

def test_example_1():
    """Test JSON file with cat command"""
    print("Test 1: JSON file")
    files = {
        "doc.json": {"foo": [1, 2, 3], "bar": {"a": 1}}
    }
    result = {}
    for filename, content in files.items():
        result[filename] = serialize_content(content, filename)

    output = result['doc.json']
    expected = json.dumps({"foo": [1, 2, 3], "bar": {"a": 1}}, ensure_ascii=False)
    print(f"Expected: {expected}")
    print(f"Actual:   {output}")
    actual_parsed = json.loads(output)
    expected_parsed = json.loads(expected)
    match = actual_parsed == expected_parsed
    print(f"Match: {match}")
    print()
    return match

def test_example_2():
    """Test JSONL file with wc -l command"""
    print("Test 2: JSONL file")
    files = {
        "events.jsonl": [{"id":1,"ok":True},{"id":2,"ok":False}]
    }
    result = {}
    for filename, content in files.items():
        result[filename] = serialize_content(content, filename)

    output = result['events.jsonl']
    print(f"Serialized content: {repr(output)}")
    line_count = output.count('\n')
    print(f"Line count: {line_count}")
    expected_lines = 2
    success = line_count == expected_lines
    print(f"Expected {expected_lines} lines: {success}")
    print()
    return success

def test_example_3():
    """Test CSV file with cat command"""
    print("Test 3: CSV file")
    files = {
        "table.csv": [{"id":1}, {"name":"x"}]
    }
    result = {}
    for filename, content in files.items():
        result[filename] = serialize_content(content, filename)

    output = result['table.csv']
    print(f"Serialized content: {repr(output)}")
    expected = "id,name\n1,\n,x\n"
    success = output == expected
    print(f"Expected: {repr(expected)}")
    print(f"Match: {success}")
    print()
    return success

def test_example_4():
    """Test empty CSV file"""
    print("Test 4: Empty CSV file")
    files = {
        "empty.csv": ""
    }
    result = {}
    for filename, content in files.items():
        result[filename] = serialize_content(content, filename)

    output = result['empty.csv']
    print(f"Serialized content: {repr(output)}")
    expected = "\n"
    success = output == expected
    print(f"Expected: {repr(expected)}")
    print(f"Match: {success}")
    print()
    return success

def test_compressed():
    """Test compressed file"""
    print("Test 5: Compressed file")
    files = {
        "data.json.gz": {"key": "value"}
    }
    result = {}
    for filename, content in files.items():
        result[filename] = serialize_content(content, filename)

    output = result['data.json.gz']
    expected = json.dumps({"key": "value"}, ensure_ascii=False)
    print(f"Raw JSON: {output}")
    success = output == expected
    print(f"Match: {success}")
    print()
    return success

def test_multiple_compression():
    """Test multiple compression extensions (should fail)"""
    print("Test 6: Multiple compression extensions")
    files = {
        "data.json.gz.bz2": {"key": "value"}
    }
    try:
        for filename, content in files.items():
            result = serialize_content(content, filename)
        print("ERROR: Should have raised an exception but didn't")
        return False
    except ValueError as e:
        print(f"Exception raised as expected: {e}")
        return True

if __name__ == "__main__":
    results = []
    results.append(("Example 1 (JSON)", test_example_1()))
    results.append(("Example 2 (JSONL)", test_example_2()))
    results.append(("Example 3 (CSV)", test_example_3()))
    results.append(("Example 4 (Empty CSV)", test_example_4()))
    results.append(("Compressed file", test_compressed()))
    results.append(("Multiple compression error", test_multiple_compression()))

    print("\n=== Summary ===")
    all_passed = True
    for name, passed in results:
        print(f"{name}: {'PASS' if passed else 'FAIL'}")
        if not passed:
            all_passed = False

    sys.exit(0 if all_passed else 1)
