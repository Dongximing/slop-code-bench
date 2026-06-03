#!/usr/bin/env python3
import sys
sys.path.insert(0, '/workspace')
from query_files import SQLParser

sql = "SELECT * FROM users LIMIT 5"

# Test peek_keyword at specific positions
parser = SQLParser(sql)

# Move to right after SELECT
parser.consume("SELECT")
print(f"After 'SELECT', pos={parser.pos}")

# Now test what's at this position
print(f"SQL[pos:] = '{sql[parser.pos:]}'")

# Test peek_keyword
result = parser.peek_keyword(['FROM', 'LIMIT'])
print(f"peek_keyword result: {result}")

# But wait - we have a space at pos 7 and * at pos 8. Let me trace through it
print(f"\nCharacter by character:")
for i, c in enumerate(sql):
    if c == ' ':
        print(f"pos {i}: SPACE")
    else:
        print(f"pos {i}: '{c}'")
