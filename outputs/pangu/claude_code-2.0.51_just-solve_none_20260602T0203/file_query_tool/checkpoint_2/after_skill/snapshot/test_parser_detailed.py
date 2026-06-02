#!/usr/bin/env python3
import sys
sys.path.insert(0, '/workspace')
from query_files import SQLParser

sql = "SELECT * FROM users LIMIT 5"
print(f"SQL: '{sql}'")
print(f"String length: {len(sql)}")

# Character positions
for i, c in enumerate(sql):
    print(f"pos={i}: '{c}'")

parser = SQLParser(sql)
print(f"\nInitial parser pos: {parser.pos}")

# Manually step through
parser.consume("SELECT")
print(f"After consuming SELECT, pos={parser.pos}")

# Now check what's at pos
print(f"Char at pos={parser.pos}: '{parser.sql[parser.pos]}'")
parser.skip_whitespace()
print(f"After skip_whitespace, pos={parser.pos}, char='{parser.sql[parser.pos]}'")

# Now check peek_keyword
result = parser.peek_keyword(['FROM', 'WHERE', 'GROUP', 'HAVING', 'ORDER', 'LIMIT', 'OFFSET'])
print(f"peek_keyword result: {result}")

# Now what does peek return?
print(f"peek(None) returns: '{parser.peek(None)}'")
