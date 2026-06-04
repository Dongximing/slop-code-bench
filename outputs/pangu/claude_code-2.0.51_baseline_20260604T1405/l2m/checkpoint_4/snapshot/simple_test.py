#!/usr/bin/env python3
import re
import sys
sys.path.insert(0, '/workspace')

from l2m import convert_latex_to_markdown_legacy, _parse_admonition_blocks

# Simple test
input_text = r'''\begin{definition}
This is a definition.
\end{definition}'''

text = convert_latex_to_markdown_legacy(input_text)
print(f"After legacy: {repr(text)}")

blocks = _parse_admonition_blocks(text)
print(f"Blocks: {len(blocks)}")
for b in blocks:
    print(f"  {b['env_name']}: begin={b['begin']}, end={b['end']}")
