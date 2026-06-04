#!/usr/bin/env python3
import re
import sys
sys.path.insert(0, '/workspace')

from l2m import convert_latex_to_markdown_legacy, _parse_admonition_blocks, strip_lines

# Read the test file
with open('test_admonitions.tex', 'r') as f:
    input_text = f.read()

print("Step 1: Legacy conversion")
text = convert_latex_to_markdown_legacy(input_text)
print(f"Text after legacy (length {len(text)}):")
print(repr(text[:500]))
print("...")

print("\n\nStep 2: Admonition parsing")
blocks = _parse_admonition_blocks(text)
print(f"Found {len(blocks)} blocks")
for i, b in enumerate(blocks):
    print(f"Block {i}: env_name={b['env_name']}, begin={b['begin']}, end={b['end']}")
    print(f"  content preview: {repr(b['content'][:100])}")
    for j, c in enumerate(b.get('children', [])):
        print(f"    Child {j}: env_name={c['env_name']}")
