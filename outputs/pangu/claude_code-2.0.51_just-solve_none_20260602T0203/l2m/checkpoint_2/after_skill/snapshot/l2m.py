#!/usr/bin/env python3

import re
import sys
from pathlib import Path
from typing import Tuple, List


def remove_comments(text: str) -> str:
    """Remove LaTeX comments, treating \% as literal."""
    text = text.replace('\\%', '\x00percent\x00')
    lines = text.split('\n')
    cleaned_lines = []
    for line in lines:
        match = re.search(r'(?<!\\)%', line)
        if match:
            line = line[:match.start()]
        cleaned_lines.append(line)
    result = '\n'.join(cleaned_lines)
    result = result.replace('\x00percent\x00', '%')
    return result


def extract_body(text: str) -> str:
    """Extract text between \begin{document} and \end{document} if they exist."""
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
    """Convert display math \[...\] to $$...$$ blocks."""
    result = []
    last_end = 0
    pattern = re.compile(r'\\\[(.*?)\\\]', re.DOTALL)

    for match in pattern.finditer(text):
        before_text = text[last_end:match.start()]
        if before_text:
            result.append(before_text.rstrip())
            result.append('')

        math_content = match.group(1)
        result.append('$$')
        result.append(math_content)
        result.append('$$')
        result.append('')
        last_end = match.end()

    remaining_text = text[last_end:]
    if remaining_text:
        result.append(remaining_text)

    output = ''.join(result).rstrip()
    return output


def convert_sections_and_formatting(text: str) -> str:
    """Convert section commands and inline formatting."""
    text = re.sub(r'\\section\{([^}]+)\}', r'## \1', text)
    text = re.sub(r'\\subsection\{([^}]+)\}', r'### \1', text)
    text = re.sub(r'\\subsubsection\{([^}]+)\}', r'#### \1', text)
    text = re.sub(r'\\emph\{([^}]+)\}', r'_\1_', text)
    text = re.sub(r'\\textbf\{([^}]+)\}', r'**\1**', text)
    return text


def delete_commands(text: str) -> str:
    """Remove \vspace{...}, \medskip, \smallskip, \bigskip commands."""
    text = re.sub(r'\\vspace\{[^}]*\}', '', text)
    text = re.sub(r'\\medskip', '', text)
    text = re.sub(r'\\smallskip', '', text)
    text = re.sub(r'\\bigskip', '', text)
    return text


def normalize_blank_lines(text: str) -> str:
    """Collapse runs of 3+ newlines to exactly 2 newlines."""
    return re.sub(r'\n{3,}', '\n\n', text)


def number_to_letter(n: int) -> str:
    """Convert a number (1-based) to a lowercase letter, wrapping from z to a."""
    n -= 1
    return chr(ord('a') + n % 26)


def find_env_blocks(text: str, env_name: str) -> List[Tuple[int, int, str]]:
    """Find all \begin{env_name}...\end{env_name} blocks in text.
    Returns list of (start_pos, end_pos, content) for each block.
    """
    blocks = []
    i = 0
    pattern_str = r'\\begin\{' + re.escape(env_name) + r'\}(?:\[.*?\])?\s*'
    env_begin = re.compile(pattern_str, re.IGNORECASE)
    env_end = re.compile(r'\\end\{' + re.escape(env_name) + r'\}', re.IGNORECASE)

    while i < len(text):
        match = env_begin.search(text[i:])
        if not match:
            break

        block_start = i + match.end()

        # Find matching \end
        depth = 1
        j = block_start
        while depth > 0 and j < len(text):
            next_begin = env_begin.search(text[j:])
            next_end = env_end.search(text[j:])

            if not next_begin and not next_end:
                break

            if next_end and (not next_begin or next_end.start() < next_begin.start()):
                depth -= 1
                if depth == 0:
                    content = text[block_start:j + next_end.start()]
                    blocks.append((i + match.start(), j + next_end.end(), content))
                    break
                j += next_end.end()
            elif next_begin:
                depth += 1
                j += next_begin.end()

        i = j + 1

    return blocks


def process_enumerate_block(content: str, start_num: int, depth: int, parent_num: int) -> List[str]:
    """Process an enumerate block and return markdown lines.
    If depth > 0, this is a nested enumerate within another enumerate.
    """
    lines = []

    if depth > 0:
        # Error: deeper than one level of nesting
        sys.stderr.write("Error: enumerate nesting deeper than one level")
        sys.exit(1)

    item_pattern = re.compile(r'^\\item(?:\[.*?\])?\s*', re.MULTILINE)

    # Find all item positions
    items = []
    for match in item_pattern.finditer(content):
        item_start = match.start()
        item_end = match.end()

        # Get content until next item or end of content
        next_match = item_pattern.search(content[item_end:])
        if next_match:
            item_text = content[item_end:item_end + next_match.start()].strip()
        else:
            item_text = content[item_end:].strip()

        items.append((item_start, item_text, item_end))

    # Now process each item, looking for nested enumerate after it
    current_num = start_num

    for idx, (item_start, item_text, item_end) in enumerate(items):
        # Determine the range of content after this item
        next_item_start = items[idx + 1][0] if idx + 1 < len(items) else len(content)

        # Look for nested enumerate between this item and the next
        search_region = content[item_end:next_item_start]
        nested_match = re.search(r'\\begin\{enumerate\}(?:\[.*?\])?\s*', search_region, re.IGNORECASE)

        if nested_match:
            # Found nested enumerate
            # Find its content and end
            nested_start_in_region = nested_match.end()
            # Find matching \end{enumerate} for this nested one
            depth_nested = 1
            search_pos = nested_start_in_region
            nested_end_in_region = None

            while depth_nested > 0 and search_pos < len(search_region):
                n_begin = re.search(r'\\begin\{enumerate\}(?:\[.*?\])?\s*', search_region[search_pos:], re.IGNORECASE)
                n_end = re.search(r'\\end\{enumerate\}', search_region[search_pos:], re.IGNORECASE)

                if n_end and (not n_begin or n_end.start() < n_begin.start()):
                    depth_nested -= 1
                    if depth_nested == 0:
                        nested_end_in_region = search_pos + n_end.start()
                        break
                    search_pos += n_end.end()
                elif n_begin:
                    depth_nested += 1
                    search_pos += n_begin.end()

            if nested_end_in_region is not None:
                nested_content = search_region[nested_start_in_region:nested_end_in_region]
                # Process nested enumerate with depth + 1
                for nested_line in process_enumerate_block(nested_content, 1, depth + 1, current_num):
                    lines.append(nested_line)
            else:
                # No proper end found, treat as regular item
                if depth == 0:
                    lines.append(f"**{current_num}.** {item_text}")
                else:
                    lines.append(f"**{parent_num}.{number_to_letter(current_num)})** {item_text}")
                current_num += 1
        else:
            # Regular item
            if depth == 0:
                lines.append(f"**{current_num}.** {item_text}")
            else:
                lines.append(f"**{parent_num}.{number_to_letter(current_num)})** {item_text}")
            current_num += 1

        # Add blank line between items
        if idx < len(items) - 1:
            lines.append('')

    return lines


def process_enumerate_blocks(text: str) -> str:
    """Replace all enumerate blocks with markdown."""
    result = []
    last_end_pos = 0

    blocks = find_env_blocks(text, 'enumerate')

    for block_start, block_end, content in blocks:
        # Add text before this block
        if block_start > last_end_pos:
            before_text = text[last_end_pos:block_start].rstrip()
            if before_text:
                result.append(before_text)
                result.append('')
        else:
            result.append('')

        # Process this enumerate block
        lines = process_enumerate_block(content, 1, 0, 0)
        result.extend(lines)
        result.append('')

        last_end_pos = block_end

    # Add any remaining text
    if last_end_pos < len(text):
        result.append(text[last_end_pos:])

    output = '\n'.join(result)
    return output.strip()


def process_itemize_items(content: str) -> List[str]:
    """Process itemize items and return markdown lines."""
    lines = []

    item_pattern = re.compile(r'^\\item(?:\[.*?\])?\s*', re.MULTILINE)
    items = []

    for match in item_pattern.finditer(content):
        item_end = match.end()
        next_match = item_pattern.search(content[item_end:])
        if next_match:
            item_text = content[item_end:item_end + next_match.start()].strip()
        else:
            item_text = content[item_end:].strip()
        items.append(item_text)

    for idx, item_text in enumerate(items):
        lines.append(f"-   {item_text}")
        if idx < len(items) - 1:
            lines.append('')

    return lines


def find_itemize_blocks(text: str) -> List[Tuple[int, int, str]]:
    """Find all itemize blocks in text and return (start_pos, end_pos, content)."""
    blocks = []
    i = 0

    while i < len(text):
        match = re.search(r'\\begin\{itemize\}(?:\[.*?\])?\s*', text[i:], re.IGNORECASE)
        if not match:
            break

        start_pos = i + match.end()
        block_start = i + match.start()

        depth = 1
        j = start_pos
        while depth > 0 and j < len(text):
            next_begin = re.search(r'\\begin\{itemize\}(?:\[.*?\])?\s*', text[j:], re.IGNORECASE)
            next_end = re.search(r'\\end\{itemize\}', text[j:], re.IGNORECASE)

            if not next_begin and not next_end:
                break

            if next_end and (not next_begin or next_end.start() < next_begin.start()):
                depth -= 1
                if depth == 0:
                    content = text[start_pos:j + next_end.start()]
                    blocks.append((block_start, j + next_end.end(), content))
                    break
                j += next_end.end()
            elif next_begin:
                depth += 1
                j += next_begin.end()

        i = j + 1

    return blocks


def process_itemize_blocks(text: str) -> str:
    """Replace all itemize blocks with markdown."""
    result = []
    last_end_pos = 0

    blocks = find_itemize_blocks(text)

    for block_start, block_end, content in blocks:
        if block_start > last_end_pos:
            before_text = text[last_end_pos:block_start].rstrip()
            if before_text:
                result.append(before_text)
                result.append('')

        lines = process_itemize_items(content)
        result.extend(lines)
        result.append('')

        last_end_pos = block_end

    if last_end_pos < len(text):
        result.append(text[last_end_pos:])

    output = '\n'.join(result)
    return output.strip()


def convert_latex_to_markdown(latex_text: str) -> str:
    """Convert LaTeX source to Markdown."""
    text = remove_comments(latex_text)
    text = extract_body(text)
    text = strip_lines(text)
    text = convert_display_math(text)
    text = convert_sections_and_formatting(text)
    text = delete_commands(text)
    text = process_enumerate_blocks(text)
    text = process_itemize_blocks(text)
    text = normalize_blank_lines(text)
    return text.strip()


def main():
    args = sys.argv[1:]

    if not args or args[0] in ['-h', '--help']:
        print(f"Usage: python {Path(__file__).name} INPUT_FILE [-o OUTPUT_FILE]")
        sys.exit(1)

    input_file = args[0]
    output_file = None

    i = 1
    while i < len(args):
        if args[i] == '-o' and i + 1 < len(args):
            output_file = args[i + 1]
            i += 2
        else:
            i += 1

    try:
        with open(input_file, 'r', encoding='utf-8') as f:
            latex_content = f.read()
    except (FileNotFoundError, PermissionError, OSError):
        print(f"Error: cannot read '{input_file}'", file=sys.stderr)
        sys.exit(1)

    markdown_content = convert_latex_to_markdown(latex_content)

    if output_file is None:
        input_path = Path(input_file)
        output_file = input_path.with_suffix('.md')
    else:
        output_path = Path(output_file)
        output_path.parent.mkdir(parents=True, exist_ok=True)

    with open(output_file, 'w', encoding='utf-8') as f:
        f.write(markdown_content)

    sys.exit(0)


if __name__ == '__main__':
    main()
