#!/usr/bin/env python3
"""LaTeX to Markdown converter with enumerate/itemize support."""

import argparse
import re
import sys
from pathlib import Path


def remove_comments(text: str) -> str:
    """Remove LaTeX comments, handling escaped percent signs."""
    lines = text.split('\n')
    result_lines = []
    for line in lines:
        i = 0
        comment_start = -1
        while i < len(line):
            if line[i] == '%':
                if i > 0 and line[i-1] == '\\':
                    j = i - 2
                    escape_count = 1
                    while j >= 0 and line[j] == '\\':
                        escape_count += 1
                        j -= 1
                    if escape_count % 2 == 1:
                        i += 1
                        continue
                comment_start = i
                break
            i += 1

        if comment_start >= 0:
            line = line[:comment_start]
        result_lines.append(line)

    return '\n'.join(result_lines)


def extract_body(text: str) -> str:
    """Extract text between \\begin{document} and \\end{document}."""
    begin_match = re.search(r'\\begin\{document\}', text)
    end_match = re.search(r'\\end\{document\}', text)

    if begin_match and end_match:
        start = begin_match.end()
        end = end_match.start()
        return text[start:end]

    return text


def strip_lines(text: str) -> str:
    """Strip leading and trailing whitespace from each line."""
    lines = text.split('\n')
    stripped = [line.strip() for line in lines]
    return '\n'.join(stripped)


def convert_display_math(text: str) -> str:
    """Convert LaTeX display math \\[...\\] to Markdown $$...$$ blocks."""
    def replace_display_math(match):
        math_content = match.group(1)
        return '\n$$\n' + math_content + '\n$$\n'

    pattern = r'\\\[(.*?)\\\]'
    text = re.sub(pattern, replace_display_math, text, flags=re.DOTALL)
    return text


def convert_formatting(text: str) -> str:
    """Convert section commands and inline formatting."""
    text = re.sub(r'\\section\{([^}]+)\}', r'## \1', text)
    text = re.sub(r'\\subsection\{([^}]+)\}', r'### \1', text)
    text = re.sub(r'\\subsubsection\{([^}]+)\}', r'#### \1', text)
    text = re.sub(r'\\emph\{([^}]+)\}', r'_\1_', text)
    text = re.sub(r'\\textbf\{([^}]+)\}', r'**\1**', text)
    return text


def delete_commands(text: str) -> str:
    """Remove \\vspace{...}, \\medskip, \\smallskip, \\bigskip commands."""
    text = re.sub(r'\\vspace\{[^}]*\}', '', text)
    text = re.sub(r'\\medskip\s*', '', text)
    text = re.sub(r'\\smallskip\s*', '', text)
    text = re.sub(r'\\bigskip\s*', '', text)
    return text


def normalize_blank_lines(text: str) -> str:
    """Collapse runs of 3 or more newlines to exactly 2 newlines."""
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text


def _parse_enumerate_itemize_with_positions(text: str) -> list:
    """
    Parse all \\begin{environment}...\\end{environment} blocks with positions.

    Returns list of block dicts with:
    - type: 'enumerate' or 'itemize'
    - options: string after [ ] or None
    - content: raw content string (with \item)
    - items: list of dicts with 'text' and 'children'
    - children: list of child blocks
    - begin: start position in original text
    - end: end position in original text
    """
    blocks = []
    pos = 0

    while pos < len(text):
        # Find next \\begin{...}
        m = re.search(r'\\begin\{(enumerate|itemize)\}', text[pos:])
        if not m:
            break

        begin_offset = m.start()
        env_type = m.group(1)
        content_start = pos + m.end()

        # Check for [options]
        options = None
        opt_match = re.match(r'\s*\[(.*?)\]\s*', text[pos + m.end():])
        if opt_match:
            options = opt_match.group(1)
            content_start += opt_match.end()

        # Find matching \\end with stack
        stack = 1
        end_pos = content_start
        while end_pos < len(text):
            next_begin = re.search(r'\\begin\{(enumerate|itemize)\}', text[end_pos:])
            next_end = re.search(r'\\end\{(enumerate|itemize)\}', text[end_pos:])

            if next_begin and (not next_end or next_begin.start() < next_end.start()):
                stack += 1
                end_pos += next_begin.end()
            elif next_end:
                end_env = next_end.group(1)
                stack -= 1
                if stack == 0:
                    content = text[content_start:end_pos + next_end.start()]
                    end_actual = end_pos + next_end.end()

                    # Recursively parse children
                    children = _parse_enumerate_itemize_with_positions(content)

                    # Extract items
                    items = _extract_items_with_children(content, children)

                    block = {
                        'type': env_type,
                        'options': options,
                        'content': content,
                        'items': items,
                        'children': children,
                        'begin': pos + begin_offset,
                        'end': pos + begin_offset + len(m.group(0)) + (len(f'[{options}]') if options else 0) + len(content) + len(f'\\end{{{env_type}}}')
                    }
                    blocks.append(block)

                    pos = pos + begin_offset + end_actual
                    break
                else:
                    end_pos += next_end.end()
            else:
                break
        else:
            # No closing end found
            pos += begin_offset + m.end()

    return blocks


def _extract_items_with_children(content: str, children: list) -> list:
    """
    Extract \\item entries from content, matching children to items by position.

    children are already parsed and have 'begin' positions relative to content.
    """
    items = []

    # Build item positions
    item_positions = []
    for m in re.finditer(r'\\item', content):
        item_positions.append(m.start())

    # For each item, determine what children belong to it
    # Children belong to the item that precedes them

    # Sort children by their position
    sorted_children = sorted(children, key=lambda c: c['begin'])

    item_idx = 0
    child_idx = 0

    for i, item_start in enumerate(item_positions):
        # Determine where this item's text ends
        # It ends at the start of the next item or end of content
        if i < len(item_positions) - 1:
            item_end = item_positions[i + 1]
        else:
            item_end = len(content)

        # Extract text
        item_text_full = content[item_start + 5:item_end]  # +5 to skip '\\item'

        # Remove optional [option]
        m = re.match(r'^\s*\[[^]]*\]\s*', item_text_full)
        if m:
            item_text_full = item_text_full[m.end():]

        # Find what children are within this item's text
        item_children = []
        while child_idx < len(sorted_children):
            child = sorted_children[child_idx]
            # child['begin'] is relative to content
            if child['begin'] >= item_end:
                break
            # Check if child is within this item's text range
            if child['begin'] >= item_start:
                item_children.append(child)
                child_idx += 1
            else:
                child_idx += 1

        items.append({
            'text': ' '.join(item_text_full.split()).strip(),
            'children': item_children
        })

    return items


def _convert_enumerate_itemize(blocks: list, parent_item_num: int = None, depth: int = 0) -> str:
    """
    Convert parsed blocks to Markdown.

    Args:
        blocks: List of block dicts
        parent_item_num: Number of parent enumerate item (for **P.x)** format)
        depth: Current nesting depth (0 = top, 1 = nested)

    Returns:
        Markdown string
    """
    if depth > 1:
        print("Error: enumerate nesting deeper than one level", file=sys.stderr)
        sys.exit(1)

    result_parts = []

    for block in blocks:
        env_type = block['type']
        options = block['options']
        items = block['items']
        children = block['children']

        # Validate enumerate options
        if env_type == 'enumerate' and options:
            if not re.match(r'^start=\d+$', options):
                print("Error: unsupported enumerate options", file=sys.stderr)
                sys.exit(1)

        # Recursively process children
        # For items, pass parent_item_num
        # For children blocks, they're at same depth as items

        if env_type == 'enumerate':
            start = 1
            if options:
                m = re.match(r'start=(\d+)', options)
                if m:
                    start = int(m.group(1))

            if depth == 0:
                # Top-level: **N.** content
                converted = []
                for i, item in enumerate(items):
                    num = start + i
                    text = item['text']

                    # Process child blocks within this item
                    # These are nested enumerate/itemize
                    child_md = _convert_enumerate_itemize(item['children'], num, depth + 1)

                    if child_md:
                        converted.append(f"**{num}.** {text} {child_md}")
                    else:
                        converted.append(f"**{num}.** {text}")
                result_parts.append('\n\n'.join(converted))

            elif depth == 1:
                if parent_item_num is not None:
                    # Nested inside enumerate: **P.x)**
                    converted = []
                    for i, item in enumerate(items):
                        letter = chr(ord('a') + i)
                        text = item['text']

                        # Check if item has children (deeper nesting)
                        if item['children']:
                            print("Error: enumerate nesting deeper than one level", file=sys.stderr)
                            sys.exit(1)

                        converted.append(f"**{parent_item_num}.{letter})** {text}")
                    result_parts.append('\n\n'.join(converted))
                else:
                    # Inside itemize: standalone enumerate
                    converted = []
                    for i, item in enumerate(items):
                        num = start + i
                        text = item['text']

                        child_md = _convert_enumerate_itemize(item['children'], None, depth + 1)

                        if child_md:
                            converted.append(f"**{num}.** {text} {child_md}")
                        else:
                            converted.append(f"**{num}.** {text}")
                    result_parts.append('\n\n'.join(converted))

        elif env_type == 'itemize':
            converted = []
            for i, item in enumerate(items):
                text = item['text']

                child_md = _convert_enumerate_itemize(item['children'], parent_item_num, depth + 1)

                if child_md:
                    converted.append(f"-   {text} {child_md}")
                else:
                    converted.append(f"-   {text}")

            if depth == 0:
                # Top-level itemize
                result_parts.append('\n'.join(converted))
            elif depth == 1:
                # Nested itemize
                # If inside enumerate, just bullets
                # If inside itemize, just bullets
                result_parts.append('\n'.join(converted))

    return '\n\n'.join(result_parts) if result_parts else ''


def convert_latex_to_markdown(input_text: str) -> str:
    """Convert LaTeX source to Markdown."""
    # Step 1: Remove comments
    text = remove_comments(input_text)

    # Step 2: Extract body
    text = extract_body(text)

    # Step 3: Strip lines
    text = strip_lines(text)

    # Step 4: Convert display math
    text = convert_display_math(text)

    # Step 5: Convert formatting and section commands
    text = convert_formatting(text)

    # Step 6: Delete commands
    text = delete_commands(text)

    # Step 7: Process enumerate/itemize
    blocks = _parse_enumerate_itemize_with_positions(text)

    if blocks:
        # Build final output by replacing blocks with converted markdown
        # Sort blocks by position
        sorted_blocks = sorted(blocks, key=lambda b: b['begin'])

        final_parts = []
        last_end = 0

        for block in sorted_blocks:
            # Add text before this block (from original text)
            final_parts.append(text[last_end:block['begin']])

            # Convert this block
            block_md = _convert_enumerate_itemize([block])
            final_parts.append(block_md)

            last_end = block['end']

        # Add remaining text after last block
        final_parts.append(text[last_end:])

        text = ''.join(final_parts)

    # Step 8: Normalize blank lines
    text = normalize_blank_lines(text)

    return text


def main():
    parser = argparse.ArgumentParser(
        description='Convert LaTeX to KaTeX-compatible Markdown.'
    )
    parser.add_argument('input_file', help='Input LaTeX file')
    parser.add_argument('-o', '--output', help='Output Markdown file')

    args = parser.parse_args()
    input_path = Path(args.input_file)

    try:
        with open(input_path, 'r', encoding='utf-8') as f:
            input_text = f.read()
    except (FileNotFoundError, PermissionError, OSError):
        print(f"Error: cannot read '{args.input_file}'", file=sys.stderr)
        sys.exit(1)

    output_text = convert_latex_to_markdown(input_text)

    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
    else:
        output_path = input_path.with_suffix('.md')

    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(output_text)

    sys.exit(0)


if __name__ == '__main__':
    main()
