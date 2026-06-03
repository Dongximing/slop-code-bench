#!/usr/bin/env python3
import sys
sys.path.insert(0, '/workspace')
from query_files import SQLParser

sql = "SELECT * FROM users LIMIT 5"

# Test parse_expression step by step
parser = SQLParser(sql)
parser.consume("SELECT")
print(f"After 'SELECT', pos={parser.pos}")

# Now parse_expression
result = parser.parse_expression()
print(f"parse_expression returned: '{result}'")
print(f"Parser pos after parse_expression: {parser.pos}")
