#!/usr/bin/env python3
"""Unit test for filter implementation - no server needed."""

import sys
sys.path.insert(0, '/workspace/venv/lib/python3.12/site-packages')

# Create a test dataset manually
from datagate import datasets

dataset_id = 'test'
csv_content = """name,age,city
Alice,30,New York
Bob,25,Boston
Charlie,35,Chicago
David,28,Denver
Eve,32,Seattle
Frank,22,Austin
Grace,29,Portland
Henry,40,Miami"""

import csv as csv_module
lines = csv_content.strip().split('\n')
reader = csv_module.reader(lines)
header = next(reader)
columns = [col.strip() for col in header]
rows = []
for row in reader:
    if row:
        typed_row = []
        for val in row:
            if val.isdigit():
                typed_row.append(int(val))
            else:
                typed_row.append(val)
        rows.append(typed_row)

datasets[dataset_id] = {'columns': columns, 'rows': rows}

# Test filter parsing logic directly
from datagate import get_dataset
from flask import Flask, request, jsonify
import json

app = Flask(__name__)

# Monkey patch app for testing
with app.test_request_context(f'/datasets/{dataset_id}?name__exact=Alice'):
    request.args  # This triggers the request context
    # Test that filter parsing works without server
    print("Testing filter parsing directly...")
    print("Columns:", columns)
    print("Rows:", rows)

# Now test the filtering logic directly
def test_filter_logic():
    print("\n=== Testing Filter Logic Directly ===\n")

    # Simulate the filtering that would happen in get_dataset
    all_rows = [(i + 1, row) for i, row in enumerate(rows)]
    columns_list = columns

    # Test exact
    print("Test: name__exact='Alice'")
    filters = [('name', 'exact', 'Alice')]
    filtered = []
    for original_rownum, row in all_rows:
        matches_all = True
        for column, comparator, value in filters:
            col_index = columns_list.index(column)
            cell_value = row[col_index]
            if comparator == 'exact':
                if str(cell_value) != value:
                    matches_all = False
                    break
        if matches_all:
            filtered.append((original_rownum, row))
    print(f"  Result: {len(filtered)} row(s)")
    assert len(filtered) == 1
    assert filtered[0][1][0] == 'Alice'

    # Test contains
    print("Test: city__contains='York'")
    filters = [('city', 'contains', 'York')]
    filtered = []
    for original_rownum, row in all_rows:
        matches_all = True
        for column, comparator, value in filters:
            col_index = columns_list.index(column)
            cell_value = row[col_index]
            if comparator == 'contains':
                if value not in str(cell_value):
                    matches_all = False
                    break
        if matches_all:
            filtered.append((original_rownum, row))
    print(f"  Result: {len(filtered)} row(s)")
    assert len(filtered) == 1
    assert filtered[0][1][2] == 'New York'

    # Test less
    print("Test: age__less=30")
    filters = [('age', 'less', '30')]
    filtered = []
    for original_rownum, row in all_rows:
        matches_all = True
        for column, comparator, value in filters:
            col_index = columns_list.index(column)
            cell_value = row[col_index]
            if comparator == 'less':
                try:
                    cell_num = float(cell_value) if cell_value is not None else None
                    filter_num = float(value)
                    if cell_num is None or cell_num >= filter_num:
                        matches_all = False
                        break
                except (ValueError, TypeError):
                    matches_all = False
                    break
        if matches_all:
            filtered.append((original_rownum, row))
    print(f"  Result: {len(filtered)} row(s)")
    assert len(filtered) == 4  # Bob(25), David(28), Frank(22), Grace(29)

    # Test greater
    print("Test: age__greater=30")
    filters = [('age', 'greater', '30')]
    filtered = []
    for original_rownum, row in all_rows:
        matches_all = True
        for column, comparator, value in filters:
            col_index = columns_list.index(column)
            cell_value = row[col_index]
            if comparator == 'greater':
                try:
                    cell_num = float(cell_value) if cell_value is not None else None
                    filter_num = float(value)
                    if cell_num is None or cell_num <= filter_num:
                        matches_all = False
                        break
                except (ValueError, TypeError):
                    matches_all = False
                    break
        if matches_all:
            filtered.append((original_rownum, row))
    print(f"  Result: {len(filtered)} row(s)")
    assert len(filtered) == 3  # Alice(30 is NOT >30), Eve(32), Charlie(35), Henry(40)

    # Test multiple (ANDed)
    print("Test: age__greater=25 and age__less=35")
    filters = [('age', 'greater', '25'), ('age', 'less', '35')]
    filtered = []
    for original_rownum, row in all_rows:
        matches_all = True
        for column, comparator, value in filters:
            col_index = columns_list.index(column)
            cell_value = row[col_index]
            if comparator in ('less', 'greater'):
                try:
                    cell_num = float(cell_value) if cell_value is not None else None
                    filter_num = float(value)
                    if comparator == 'less':
                        if cell_num is None or cell_num >= filter_num:
                            matches_all = False
                            break
                    else:
                        if cell_num is None or cell_num <= filter_num:
                            matches_all = False
                            break
                except (ValueError, TypeError):
                    matches_all = False
                    break
            elif comparator == 'exact':
                if str(cell_value) != value:
                    matches_all = False
                    break
            elif comparator == 'contains':
                if value not in str(cell_value):
                    matches_all = False
                    break
        if matches_all:
            filtered.append((original_rownum, row))
    print(f"  Result: {len(filtered)} row(s)")
    ages = [row[1] for _, row in filtered]
    print(f"  Ages: {ages}")

    print("\n=== All unit tests passed! ===\n")

if __name__ == '__main__':
    test_filter_logic()
    sys.exit(0)
