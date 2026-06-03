#!/usr/bin/env python3
import sys
sys.path.insert(0, '/workspace')
from query_files import parse_sql

sql = "SELECT * FROM users LIMIT 5"
print(f"Parsing: {sql}")
try:
    result = parse_sql(sql)
    print(f"Result: {result}")
except Exception as e:
    print(f"Error: {e}")
    import traceback
    traceback.print_exc()
