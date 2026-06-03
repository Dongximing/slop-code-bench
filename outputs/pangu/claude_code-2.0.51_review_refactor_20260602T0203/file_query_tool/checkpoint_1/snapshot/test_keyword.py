#!/usr/bin/env python3

# Simulate peek_keyword
sql = "SELECT * FROM users LIMIT 5"
pos = 6  # After "SELECT"

def skip_whitespace():
    global pos
    while pos < len(sql) and sql[pos].isspace():
        pos += 1
    print(f"  After skip: pos={pos}, char='{sql[pos]}'")

skip_whitespace()

keywords = ['FROM', 'WHERE', 'GROUP', 'HAVING', 'ORDER', 'LIMIT', 'OFFSET']

for kw in keywords:
    kw_upper = kw.upper()
    if sql[pos:pos+len(kw_upper)].upper() == kw_upper:
        print(f"Matched '{kw}' at pos {pos}")
        # Check next char
        next_char_pos = pos + len(kw_upper)
        if next_char_pos >= len(sql) or not sql[next_char_pos].isalnum():
            print(f"  Next char at {next_char_pos} is '{sql[next_char_pos]}' - valid word boundary")
        else:
            print(f"  Next char is alnum, not a word boundary")
    else:
        print(f"No match for '{kw}'")
