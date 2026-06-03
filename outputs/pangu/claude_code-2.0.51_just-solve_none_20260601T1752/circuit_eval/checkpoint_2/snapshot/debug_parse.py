#!/usr/bin/env python3
import sys
sys.path.insert(0, '.')

from circopt import Parser

with open('adder.circ', 'r') as f:
    text = f.read()

print("File contents:")
print(repr(text))
print()

parser = Parser(text, 'adder.circ')
try:
    result = parser.parse()
    print("Parse result:")
    print(result)
except Exception as e:
    print(f"Error: {e}")
    print(f"Error type: {type(e)}")
    if hasattr(parser, 'debug_state'):
        print(f"Debug state: {parser.debug_state()}")
