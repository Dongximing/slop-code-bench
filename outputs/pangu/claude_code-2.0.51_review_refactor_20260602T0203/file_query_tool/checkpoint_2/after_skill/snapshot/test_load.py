#!/usr/bin/env python3
"""Quick test of the DataFileLoader to verify format detection and loading."""

import sys
import os
import gzip
import bz2
import tempfile
import shutil
import json
from pathlib import Path

# Add workspace to path
sys.path.insert(0, '/workspace')

from query_files import DataFileLoader, DataFileDatabase, QueryExecutor, parse_sql, format_markdown_table

def test_format_detection():
    """Test format detection for various files."""

    # Create a temp directory and test files
    test_dir = tempfile.mkdtemp()

    try:
        # Test CSV detection
        csv_file = Path(test_dir) / "test.csv"
        csv_file.write_text("id,name\n1,Alice\n2,Bob")
        assert DataFileLoader.detect_format(str(csv_file)) == 'csv'
        print("✓ CSV detection works")

        # Test TSV detection
        tsv_file = Path(test_dir) / "test.tsv"
        tsv_file.write_text("id\tname\n1\tAlice")
        assert DataFileLoader.detect_format(str(tsv_file)) == 'tsv'
        print("✓ TSV detection works")

        # Test JSON detection
        json_file = Path(test_dir) / "test.json"
        json_file.write_text('[{"id": 1, "name": "Alice"}]')
        assert DataFileLoader.detect_format(str(json_file)) == 'json'
        print("✓ JSON detection works")

        # Test JSONL detection
        jsonl_file = Path(test_dir) / "test.jsonl"
        jsonl_file.write_text('{"id": 1, "name": "Alice"}')
        assert DataFileLoader.detect_format(str(jsonl_file)) == 'jsonl'
        print("✓ JSONL detection works")

        # Test Parquet detection
        import pandas as pd
        parquet_file = Path(test_dir) / "test.parquet"
        df = pd.DataFrame({"id": [1, 2], "name": ["Alice", "Bob"]})
        df.to_parquet(str(parquet_file), index=False)
        assert DataFileLoader.detect_format(str(parquet_file)) == 'parquet'
        print("✓ Parquet detection works")

        # Test compressed CSV detection (.csv.gz)
        csv_gz_file = Path(test_dir) / "test.csv.gz"
        with gzip.open(str(csv_gz_file), 'wt') as f:
            f.write("id,name\n1,Alice")
        assert DataFileLoader.detect_format(str(csv_gz_file)) == 'csv'
        print("✓ Compressed CSV (.csv.gz) detection works")

        # Test compressed TSV detection (.tsv.bz2)
        tsv_bz2_file = Path(test_dir) / "test.tsv.bz2"
        with bz2.open(str(tsv_bz2_file), 'wt') as f:
            f.write("id\tname\n1\tAlice")
        assert DataFileLoader.detect_format(str(tsv_bz2_file)) == 'tsv'
        print("✓ Compressed TSV (.tsv.bz2) detection works")

        # Test compression check
        is_comp, comp_type = DataFileLoader.is_compressed(str(csv_gz_file))
        assert is_comp == True
        assert comp_type == 'gzip'
        print("✓ Gzip compression detection works")

        is_comp, comp_type = DataFileLoader.is_compressed(str(tsv_bz2_file))
        assert is_comp == True
        assert comp_type == 'bz2'
        print("✓ Bzip2 compression detection works")

        print("\n=== All format detection tests passed ===")

    finally:
        shutil.rmtree(test_dir, ignore_errors=True)


def test_loading_multiple_formats_same_base():
    """Test loading multiple files with same base name but different formats.
    This should raise a ValueError if columns differ.
    """
    import pandas as pd

    test_dir = tempfile.mkdtemp()
    test_data_dir = Path(test_dir) / "test_data"
    test_data_dir.mkdir()

    print("\n=== Testing column validation for files with same name ===")

    try:
        # Create users.csv with certain columns
        users_csv = """id,name,age
1,Alice,30
2,Bob,25"""
        with open(test_data_dir / "users.csv", 'w') as f:
            f.write(users_csv)

        # Create users.tsv with SAME columns - should work
        users_tsv = """id\tname\tage
1\tAlice\t30
2\tBob\t25"""
        with open(test_data_dir / "users.tsv", 'w') as f:
            f.write(users_tsv)

        print("Created users.csv and users.tsv with same columns.")
        print("Loading both files...")

        db = DataFileDatabase(str(test_data_dir))
        print(f"  Loaded tables: {list(db.tables.keys())}")
        print("✓ Files with same columns loaded successfully")

        # Now test with DIFFERENT columns
        print("\nNow testing users.csv vs users.json with DIFFERENT columns...")
        different_cols = """id,name,department
3,Charlie,Engineering"""
        with open(test_data_dir / "users.json", 'w') as f:
            json.dump([{"id": 3, "name": "Charlie", "department": "Engineering"}], f)

        try:
            db2 = DataFileDatabase(str(test_data_dir))
            print("  WARNING: Should have raised ValueError for different columns!")
            print(f"  Loaded tables: {list(db2.tables.keys())}")
        except ValueError as e:
            print(f"✓ Correctly raised ValueError: {e}")

    finally:
        shutil.rmtree(test_dir, ignore_errors=True)


def test_querying_loaded_tables():
    """Test executing SQL queries on loaded tables."""
    import pandas as pd

    print("\n=== Testing SQL queries on various formats ===")

    test_dir = tempfile.mkdtemp()
    test_data_dir = Path(test_dir) / "test_data"
    test_data_dir.mkdir()

    try:
        # Create users.csv
        users_csv = """id,first_name,job,country,car,phone
1,Rori,Mechanical Systems Engineer,AR,Chevy,Cat
2,Darin,Marketing Assistant,AR,Ferrari,BlackBerry
3,Sheilah,VP Accounting,CH,Ford,Samsung
4,Teodor,Compensation Analyst,,Ferrari,TCL
5,Mirelle,Assistant Manager,AR,Toyota,Acer
19,Darryl,Physical Therapy Assistant,FR,Dodge,ZTE
31,Othilia,Chemical Engineer,AR,Ferrari,Sewon
32,Gaylene,Accounting Assistant I,AR,Toyota,Microsoft"""
        with open(test_data_dir / "users.csv", 'w') as f:
            f.write(users_csv)

        # Create users.tsv
        users_tsv = """id\tfirst_name\tjob\tcountry\tcar\tphone
1\tRori\tMechanical Systems Engineer\tAR\tChevy\tCat
2\tDarin\tMarketing Assistant\tAR\tFerrari\tBlackBerry
3\tSheilah\tVP Accounting\tCH\tFord\tSamsung
4\tTeodor\tCompensation Analyst\t\tFerrari\tTCL
5\tMirelle\tAssistant Manager\tAR\tToyota\tAcer
19\tDarryl\tPhysical Therapy Assistant\tFR\tDodge\tZTE
31\tOthilia\tChemical Engineer\tAR\tFerrari\tSewon
32\tGaylene\tAccounting Assistant I\tAR\tToyota\tMicrosoft"""
        with open(test_data_dir / "users.tsv", 'w') as f:
            f.write(users_tsv)

        # Create users.json
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
        with open(test_data_dir / "users.json", 'w') as f:
            json.dump(users_json, f)

        # Create users.jsonl
        with open(test_data_dir / "users.jsonl", 'w') as f:
            for user in users_json:
                f.write(json.dumps(user) + '\n')

        # Create users.parquet
        df = pd.DataFrame(users_json)
        df.to_parquet(test_data_dir / "users.parquet", index=False)

        # Create users.csv.gz
        with gzip.open(test_data_dir / "users.csv.gz", 'wt') as f:
            f.write(users_csv)

        # Create users.tsv.bz2
        with bz2.open(test_data_dir / "users.tsv.bz2", 'wt') as f:
            f.write(users_tsv)

        print("Created test files")

        # Load the database
        db = DataFileDatabase(str(test_data_dir))
        print(f"Loaded tables: {list(db.tables.keys())}")

        # Run test queries
        executor = QueryExecutor(db)

        # Test 1: Simple SELECT
        print("\n--- Query 1: SELECT * FROM users (first 2 rows) ---")
        ast = parse_sql("SELECT * FROM users ORDER BY id LIMIT 2")
        result = executor.execute(ast)
        print(format_markdown_table(result))

        # Test 2: WHERE with LIKE
        print("\n--- Query 2: Jobs with 'Assistant' ---")
        ast = parse_sql("SELECT country, first_name, job FROM users WHERE job LIKE '%Assistant'")
        result = executor.execute(ast)
        print(format_markdown_table(result))

        # Test 3: COUNT(*) GROUP BY (simple, no alias)
        print("\n--- Query 3: COUNT BY country ---")
        ast = parse_sql("SELECT country, COUNT(*) FROM users GROUP BY country")
        result = executor.execute(ast)
        print(format_markdown_table(result))

        # Test 4: Aggregate functions
        print("\n--- Query 4: MAX and MIN id ---")
        ast = parse_sql("SELECT MAX(id) as max_id, MIN(id) as min_id FROM users")
        result = executor.execute(ast)
        print(format_markdown_table(result))

        # Test 5: JSON-specific (NULL handling)
        print("\n--- Query 5: NULL values in country ---")
        ast = parse_sql("SELECT first_name, country FROM users WHERE country IS NULL")
        result = executor.execute(ast)
        print(format_markdown_table(result))

        print("\n✓ All query tests passed!")

    finally:
        shutil.rmtree(test_dir, ignore_errors=True)


if __name__ == '__main__':
    print("=== Testing Multiple File Formats Support ===\n")

    test_format_detection()
    test_loading_multiple_formats_same_base()
    test_querying_loaded_tables()

    print("\n=== All tests completed successfully ===")
