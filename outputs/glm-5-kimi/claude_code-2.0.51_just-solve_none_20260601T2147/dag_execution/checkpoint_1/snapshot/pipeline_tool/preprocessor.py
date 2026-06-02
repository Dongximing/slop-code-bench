"""
Preprocessor for pipeline files - extracts run blocks before tokenization.
"""

import re
from typing import Dict, Tuple


def preprocess_pipeline(source: str) -> Tuple[str, Dict[str, str]]:
    """
    Extract run block contents from pipeline source and replace with placeholders.
    Returns (modified_source, run_blocks_map).
    """
    run_blocks = {}
    counter = [0]

    def extract_run_block(match):
        # Find the matching closing brace
        start = match.end()
        depth = 1
        i = start
        while i < len(source) and depth > 0:
            if source[i] == '{':
                depth += 1
            elif source[i] == '}':
                depth -= 1
            elif source[i] in ('"', "'"):
                # Skip string literals
                quote = source[i]
                i += 1
                while i < len(source) and source[i] != quote:
                    if source[i] == '\\':
                        i += 1
                    i += 1
            i += 1

        content = source[start:i-1].strip()
        placeholder = f'__RUN_BLOCK_{counter[0]}__'
        run_blocks[placeholder] = content
        counter[0] += 1

        # Return the match with placeholder
        return match.group(0).rstrip() + ' ' + placeholder + ' }'

    # Match run: { ... } and extract content
    # We need a custom approach since we need balanced braces
    result = _extract_blocks(source, run_blocks, counter)

    return result, run_blocks


def _extract_blocks(source: str, run_blocks: Dict[str, str], counter: list) -> str:
    """Extract run blocks from source."""
    result = []
    i = 0

    while i < len(source):
        # Look for run:
        if source[i:i+4] == 'run:' or (source[i:i+5] == 'run :'):
            # Find the opening brace
            j = i + 3
            while j < len(source) and source[j] != '{':
                j += 1

            if j >= len(source):
                result.append(source[i:])
                break

            result.append(source[i:j+1])  # Include 'run: {'

            # Find matching closing brace
            depth = 1
            k = j + 1
            while k < len(source) and depth > 0:
                if source[k] == '{':
                    depth += 1
                elif source[k] == '}':
                    depth -= 1
                elif source[k] in ('"', "'"):
                    quote = source[k]
                    k += 1
                    while k < len(source) and source[k] != quote:
                        if source[k] == '\\':
                            k += 1
                        k += 1
                elif source[k:k+2] == '//':
                    # Skip comments
                    while k < len(source) and source[k] != '\n':
                        k += 1
                k += 1

            content = source[j+1:k-1].strip()
            placeholder = f'__RUN_BLOCK_{counter[0]}__'
            run_blocks[placeholder] = content
            counter[0] += 1

            result.append(placeholder + ' }')
            i = k
        else:
            result.append(source[i])
            i += 1

    return ''.join(result)
