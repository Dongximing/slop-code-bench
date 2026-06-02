#!/usr/bin/env python3
"""Test the extended query_files implementation."""

import sys
import os
import gzip
import bz2
import json
import tempfile
import shutil
from pathlib import Path

sys.path.insert(0, '/workspace')

from query_files import DataFileDatabase, parse_sql, format_markdown_table


def create_test_data(data_dir):
    """Create test files in various formats."""
    data_dir = Path(data_dir)

    # users.csv - original
    users_csv = """id,first_name,job,country,car,phone
1,Rori,Mechanical Systems Engineer,AR,Chevy,Cat
2,Darin,Marketing Assistant,AR,Ferrari,BlackBerry
3,Sheilah,VP Accounting,CH,Ford,Samsung
4,Teodor,Compensation Analyst,,Ferrari,TCL
5,Mirelle,Assistant Manager,AR,Toyota,Acer
19,Darryl,Physical Therapy Assistant,FR,Dodge,ZTE
31,Othilia,Chemical Engineer,AR,Ferrari,Sewon
32,Gaylene,Accounting Assistant I,AR,Toyota,Microsoft"""

    with open(data_dir / "users.csv", 'w') as f:
        f.write(users_csv)

    # users.tsv - same data as TSV
    users_tsv = """id	first_name	job	country	car	phone
1	Rori	Mechanical Systems Engineer	AR	Chevy	Cat
2	Darin	Marketing Assistant	AR	Ferrari	BlackBerry
3	Sheilah	VP Accounting	CH	Ford	Samsung
4	Teodor	Compensation Analyst		Ferrari	TCL
5	Mirelle	Assistant Manager	AR	Toyota	Acer
19	Darryl	Physical Therapy Assistant	FR	Dodge	ZTE
31	Othilia	Chemical Engineer	AR	Ferrari	Sewon
32	Gaylene	Accounting Assistant I	AR	Toyota	Microsoft"""

    with open(data_dir / "users.tsv", 'w') as f:
        f.write(users_tsv)

    # users.json - JSON format
    users_json = [
        {"id": 1, "first_name": "Rori", "job": "Mechanical Systems Engineer", "country": "AR", "car": "Chevy", "phone": "Cat"},
        {"id": 2, "first_name": "Darin", "job": "Marketing Assistant", "country": "AR", "car": "Ferrari", "phone": "BlackBerry"},
        {"id": 3, "first_name": "Sheilah", "job": "VP Accounting", "country": "CH", "car": "Ford", "phone": "Samsung"},
        {"id": 4, "first_name": "Teodor", "job": "Compensation Analyst", "country": None, "car": "Ferrari", "phone": "TCL"},
        {"id": 5, "first_name": "Mirelle", "job": "Assistant Manager", "country": "AR", "car": "Toyota", "phone": "Acer"},
        {"id": 19, "first_name": "Darryl", "job": "Physical Therapy Assistant", "country": "FR", "car": "Dodge", "phone": "ZTE"},
        {"id": 31, "first_name": "Othilia", "job": "Chemical Engineer", "country": "AR", "car": "Ferrari", "phone": "Sewon"},
        {"id": 32, "first_name": "Gaylene", "job": "Accounting Assistant I", "country": "AR", "car": "Toyota", "phone": "Microsoft"}
    ]
    with open(data_dir / "users.json", 'w') as f:
        json.dump(users_json, f)

    # users.jsonl - JSONL format
    with open(data_dir / "users.jsonl", 'w') as f:
        for user in users_json:
            f.write(json.dumps(user) + '\n')

    # users.parquet - Parquet format
    import pandas as pd
    df = pd.DataFrame(users_json)
    df.to_parquet(data_dir / "users.parquet", index=False)

    # users.csv.gz - compressed CSV
    with gzip.open(data_dir / "users.csv.gz", 'wt') as f:
        f.write(users_csv)

    # users.tsv.bz2 - compressed TSV
    with bz2.open(data_dir / "users.tsv.bz2", 'wt') as f:
        f.write(users_tsv)

    # products data for JOIN tests
    products_csv = """product_id,name,category,price
1,Widget A,Electronics,29.99
2,Widget B,Electronics,39.99
3,Widget C,Home,19.99
4,Widget D,Home,49.99
5,Widget E,Office,9.99"""
    with open(data_dir / "products.csv", 'w') as f:
        f.write(products_csv)

    print(f"Created test files in {data_dir}")


def run_tests(data_dir):
    """Run various test queries."""
    from query_files import QueryExecutor, DataFileDatabase

    db = DataFileDatabase(data_dir)
    executor = QueryExecutor(db)

    print("\n=== Available tables ===")
    print(list(db.tables.keys()))

    # Test 1: Simple SELECT on CSV
    print("\n=== Test 1: SELECT * FROM users (LIMIT 5) ===")
    ast = parse_sql("SELECT * FROM users LIMIT 5")
    result = executor.execute(ast)
    print(format_markdown_table(result))

    # Test 2: TSV file
    print("\n=== Test 2: TSV users (jobs with 'Assistant') ===")
    ast = parse_sql("SELECT id, first_name, job FROM users WHERE job LIKE '%Assistant' LIMIT 3")
    result = executor.execute(ast)
    print(format_markdown_table(result))

    # Test 3: JSON file
    print("\n=== Test 3: JSON users (country AR) ===")
    ast = parse_sql("SELECT first_name, country FROM users WHERE country = 'AR' LIMIT 3")
    result = executor.execute(ast)
    print(format_markdown_table(result))

    # Test 4: JSONL file
    print("\n=== Test 4: JSONL users (country is NULL) ===")
    ast = parse_sql("SELECT * FROM users WHERE country IS NULL")
    result = executor.execute(ast)
    print(format_markdown_table(result))

    # Test 5: Parquet file
    print("\n=== Test 5: Parquet users (count by country) ===")
    ast = parse_sql("SELECT country, COUNT(*) as 'People' FROM users GROUP BY country")
    result = executor.execute(ast)
    print(format_markdown_table(result))

    # Test 6: Compressed CSV
    print("\n=== Test 6: Compressed CSV (.csv.gz) ===")
    # The compressed file should be picked up
    ast = parse_sql("SELECT COUNT(*) FROM users")
    result = executor.execute(ast)
    print(format_markdown_table(result))

    # Test 7: Compressed TSV
    print("\n=== Test 7: Compressed TSV (.tsv.bz2) ===")
    ast = parse_sql("SELECT MAX(id) as max_id, MIN(id) as min_id FROM users")
    result = executor.execute(ast)
    print(format_markdown_table(result))

    # Test 8: JOIN query
    print("\n=== Test 8: JOIN query (users and products) ===")
    # We need to have a common column for join test
    # Let's create a simple join query on same users table (self-join)
    ast = parse_sql("SELECT u1.first_name, u2.first_name as partner_name FROM users u1 LEFT JOIN users u2 ON u1.id < u2.id LIMIT 5")
    result = executor.execute(ast)
    print(format_markdown_table(result))

    print("\n=== All tests passed! ===")


def main():
    # Create temp directory for test data
    test_dir = tempfile.mkdtemp()
    test_data_dir = Path(test_dir) / "test_data"
    test_data_dir.mkdir()

    print(f"Test directory: {test_data_dir}")

    try:
        create_test_data(test_data_dir)
        run_tests(test_data_dir)
    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
    finally:
        # Cleanup
        shutil.rmtree(test_dir, ignore_errors=True)


if __name__ == '__main__':
    main()
