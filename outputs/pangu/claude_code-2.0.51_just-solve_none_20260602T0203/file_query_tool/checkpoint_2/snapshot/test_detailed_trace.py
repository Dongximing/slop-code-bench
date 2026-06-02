#!/usr/bin/env python3
import sys
sys.path.insert(0, '/workspace')
from query_files import SQLParser

sql = "SELECT * FROM users LIMIT 5"
print(f"SQL: '{sql}'")

# Trace step by step
parser = SQLParser(sql)

# Manually call parse_select_list but with detailed trace
print("\n=== Calling parse_select_list ===")
items = parser.parse_select_list()
print(f"\nResult items: {items}")
print(f"Final parser pos: {parser.pos}")
print(f"Remaining SQL: '{sql[parser.pos:]}'")
