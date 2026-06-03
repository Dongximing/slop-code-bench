#!/usr/bin/env python3
import sys
sys.path.insert(0, '/workspace')
from query_files import SQLParser

sql = "SELECT * FROM users LIMIT 5"
print(f"Parsing: {sql}")
parser = SQLParser(sql)
result = parser.parse()
print(f"Result: {result}")
