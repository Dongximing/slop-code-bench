#!/usr/bin/env python3
"""LaTeX to KaTeX-compatible Markdown converter."""

import argparse
import re
import sys
from pathlib import Path


def number_to_letters(n: int) -> str:
    """Convert number to lowercase letters: 1->a, 2->b, ..., 26->z, 27->aa, etc."""
    result = ""
    while n > 0:
        n -= 1
        result = chr(ord('a') + n % 26) + result
        n = n // 26
    return result


def remove_comments(text: str) -> str:
    """Remove unescaped % and everything after it on each line."""
    # Replace escaped % (\%) with placeholder to preserve them
    text = text.replace('\\%', '\x01ESCAPED_PERCENT\x01')

    lines = []
    for line in text.split('\n'):
        i = 0
        while i < len(line):
            if line[i] == '%':
                lines.append(line[:i])
                break
            i += 1
        else:
            lines.append(line)

    text = '\n'.join(lines)
    # Restore escaped percents
    return text.replace('\x01ESCAPED_PERCENT\x01', '%')


def extract_body(text: str) -> str:
    r"""Extract text between \begin{document} and \end{document}."""
    m = re.search(r'\\begin\{document\}', text)
    n = re.search(r'\\end\{document\}', text)
    if m and n:
        return text[m.end():n.start()]
    return text


def strip_whitespace(text: str) -> str:
    return '\n'.join(line.strip() for line in text.split('\n'))


def convert_display_math(text: str) -> str:
    """Convert \[...\] to $$...$$ blocks."""
    result = []
    i = 0
    while i < len(text):
        open_match = text.find('\\[', i)
        if open_match == -1:
            result.append(text[i:])
            break
        result.append(text[i:open_match])
        close_match = text.find('\\]', open_match + 2)
        if close_match == -1:
            result.append(text[open_match:])
            break
        result.append('\n$$\n' + text[open_match + 2:close_match] + '\n$$\n')
        i = close_match + 2
    return ''.join(result)


def convert_sections_and_formatting(text: str) -> str:
    """Convert section commands and inline formatting."""
    text = re.sub(r'\\section\{([^}]+)\}', r'## \1', text)
    text = re.sub(r'\\subsection\{([^}]+)\}', r'### \1', text)
    text = re.sub(r'\\subsubsection\{([^}]+)\}', r'#### \1', text)
    text = re.sub(r'\\emph\{([^}]*)\}', r'_\1_', text)
    text = re.sub(r'\\textbf\{([^}]*)\}', r'**\1**', text)
    return text


def delete_commands(text: str) -> str:
    """Remove \vspace{...}, \medskip, \smallskip, \bigskip."""
    text = re.sub(r'\\vspace\{[^}]*\}', '', text)
    text = re.sub(r'\\medskip', '', text)
    text = re.sub(r'\\smallskip', '', text)
    text = re.sub(r'\\bigskip', '', text)
    return text


def normalize_blanks(text: str) -> str:
    return re.sub(r'\n{3,}', '\n\n', text)


def parse_enumerate_opening(line: str) -> tuple[int | None, bool]:
    """Parse enumerate opening line.

    Returns (start_number, has_options)
    - start_number is None or the starting number
    - has_options is True if unsupported options are found

    For unsupported options, return (None, True) to indicate error.
    """
    # Match \begin{enumerate}[start=N] or \begin{enumerate} (with whitespace variations)
    match = re.match(r'\\begin\{enumerate\}(?:\s*)$(.+)$', line)
    if match:
        options = match.group(1).strip()
        # Check for [start=N] format
        start_match = re.match(r'^\[(start=)(\d+)\]$', options)
        if start_match:
            return int(start_match.group(2)), False
        else:
            return None, True  # Unsupported options
    return None, False


def process_items(items_content: str, is_nested: bool = False, start_num: int = 1, parent_num: int | None = None) -> tuple[list[str], int | None]:
    """Process items content and return list of formatted lines.

    For nested enumerate, parent_num is the parent item number.
    Returns (formatted_lines, error_code)
    where error_code is None for success.
    """
    lines = []
    i = 0
    content = items_content
    item_idx = 0

    while i < len(content):
        # Look for \item
        item_match = re.search(r'\\item(?:\[.*?\])?', content[i:])
        if not item_match:
            # No more items, append remaining content
            remaining = content[i:].strip()
            if remaining:
                lines.append(remaining)
            break

        # Text before this item
        before_item = content[i:item_match.start()].strip()
        if before_item:
            lines.append(before_item)

        item_pos = i + item_match.end()
        full_item_match = re.match(r'\\item(?:\[.*?\])\s*', content[item_pos-1:])
        if not full_item_match:
            # Should not happen, but skip
            i += item_match.start() + 1
            continue

        item_header_len = len(full_item_match.group(0))

        # Look for nested environment or end of item
        nested_start = re.search(r'\\begin\{(enumerate|itemize)\}', content[item_pos + item_header_len - 1:])
        nested_end = re.search(r'\\end\{(?:enumerate|itemize)\}', content[item_pos + item_header_len - 1:])

        if nested_start and (not nested_end or nested_start.start() < nested_end.start()):
            # Nested environment found
            nested_type = nested_start.group(1)
            nested_content_start = item_pos + item_header_len - 1 + nested_start.end()

            # Find matching end
            depth = 1
            j = nested_content_start
            while j < len(content) and depth > 0:
                end_match = re.search(r'\\end\{(enumerate|itemize)\}', content[j:])
                start_match = re.search(r'\\begin\{(enumerate|itemize)\}', content[j:])

                if not end_match:
                    break

                if start_match and start_match.start() < end_match.start():
                    depth += 1
                    j += start_match.end()
                else:
                    depth -= 1
                    j += end_match.end()

            nested_content = content[nested_content_start:j - (end_match.end() if end_match else 0)]

            if nested_type == 'enumerate':
                if is_nested:
                    # Deeper than one level - error
                    return [], 1

                # Nested enumerate - use parent-linked labels
                item_idx += 1
                child_lines, error = process_items(nested_content, is_nested=True, start_num=start_num, parent_num=item_idx)
                if error:
                    return [], error
                lines.extend(child_lines)
            else:  # itemize
                # Itemize inside enumerate
                itemize_lines, error = process_items(nested_content, is_nested=False, is_itemize=True)
                if error:
                    return [], error
                lines.extend(itemize_lines)

            i = j
            continue

        # Item content until next \item or end
        next_item = re.search(r'\\item(?:\[.*?\])', content[item_pos + item_header_len - 1:])
        if next_item:
            item_content = content[item_pos + item_header_len - 1:item_pos + item_header_len - 1 + next_item.start()]
        else:
            item_content = content[item_pos + item_header_len - 1:]

        item_content = item_content.strip()

        if is_nested and parent_num is not None:
            # Nested enumerate - use parent-linked labels
            letter = number_to_letters(item_idx + 1)
            lines.append(f"**{parent_num}.{letter})** {item_content}")
        elif is_itemize:
            lines.append(f"-   {item_content}")
        else:
            lines.append(f"**{start_num}.** {item_content}")
            start_num += 1

        item_idx += 1
        i = item_pos + item_header_len - 1 + (next_item.start() if next_item else len(item_content))

    return lines, None
    """Convert LaTeX source to Markdown."""
    text = remove_comments(input_text)
    text = extract_body(text)
    text = strip_whitespace(text)
    text = convert_display_math(text)
    text = convert_sections_and_formatting(text)
    text = delete_commands(text)
    text = normalize_blanks(text)
    return text


def main():
    parser = argparse.ArgumentParser(description='Convert LaTeX to KaTeX-compatible Markdown.')
    parser.add_argument('input_file', help='Input LaTeX file')
    parser.add_argument('-o', '--output', help='Output Markdown file')
    args = parser.parse_args()

    try:
        with open(args.input_file, 'r', encoding='utf-8') as f:
            input_text = f.read()
    except (FileNotFoundError, OSError):
        print(f"Error: cannot read '{args.input_file}'", file=sys.stderr)
        sys.exit(1)

    output_text = convert_latex_to_markdown(input_text)

    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
    else:
        output_path = Path(args.input_file).parent / f"{Path(args.input_file).stem}.md"

    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(output_text)


if __name__ == '__main__':
    main()
