#!/usr/bin/env python3
"""LaTeX to Markdown converter."""

import re
import sys
from pathlib import Path


def remove_comments(text: str) -> str:
    """Remove LaTeX comments from text."""
    lines = text.split('\n')
    result = []
    for line in lines:
        # Find first unescaped %
        i = 0
        comment_start = -1
        while i < len(line):
            if line[i] == '%':
                # Check if escaped
                if i > 0 and line[i-1] == '\\':
                    # Skip this % - it's escaped
                    i += 1
                    continue
                comment_start = i
                break
            i += 1
        if comment_start >= 0:
            line = line[:comment_start]
        result.append(line)
    return '\n'.join(result)


def extract_body(text: str) -> str:
    r"""Extract content between \begin{document} and \end{document}."""
    begin_match = re.search(r'\\begin\{document\}', text)
    end_match = re.search(r'\\end\{document\}', text)

    if begin_match and end_match:
        start = begin_match.end()
        end = end_match.start()
        return text[start:end]

    return text


def strip_lines(text: str) -> str:
    """Strip leading and trailing whitespace from each line."""
    return '\n'.join(line.strip() for line in text.split('\n'))


def convert_display_math(text: str) -> str:
    r"""Convert \[...\] display math to $$ blocks."""
    result = []
    i = 0

    while i < len(text):
        # Look for \[
        if i + 1 < len(text) and text[i:i+2] == '\\[':
            # Find matching \]
            j = i + 2
            while j + 1 < len(text) and text[j:j+2] != '\\]':
                j += 1
            if j + 1 < len(text) and text[j:j+2] == '\\]':
                math_content = text[i+2:j]

                # Find the line boundaries for this expression
                line_start = text.rfind('\n', 0, i) + 1
                line_end = text.find('\n', j + 2)
                if line_end == -1:
                    line_end = len(text)

                # Text before this math block on the same line
                before = text[line_start:i]
                # Text after this math block on the same line
                after = text[j+2:line_end]

                # Build output for this line segment
                if before:
                    result.append(before)
                result.append('$$')
                result.append(math_content)
                result.append('$$')
                if after:
                    result.append(after)

                # Add newline if there was one after the math block
                if line_end < len(text) and text[line_end] == '\n':
                    result.append('\n')

                i = line_end + 1
            else:
                # No closing \], treat as literal
                result.append(text[i])
                i += 1
        else:
            result.append(text[i])
            i += 1

    return ''.join(result)


def convert_sections(text: str) -> str:
    """Convert section commands to Markdown headers."""
    # Order matters: do subsubsection before subsection before section
    # to avoid nested replacements
    text = re.sub(r'\\subsubsection\{([^}]+)\}', r'#### \1', text)
    text = re.sub(r'\\subsection\{([^}]+)\}', r'### \1', text)
    text = re.sub(r'\\section\{([^}]+)\}', r'## \1', text)
    return text


def convert_formatting(text: str) -> str:
    """Convert inline formatting commands."""
    text = re.sub(r'\\emph\{([^}]+)\}', r'_\1_', text)
    text = re.sub(r'\\textbf\{([^}]+)\}', r'**\1**', text)
    return text


def delete_commands(text: str) -> str:
    r"""Remove \vspace{...}, \medskip, \smallskip, \bigskip commands."""
    for cmd in (r'\vspace{[^}]*}', r'\medskip', r'\smallskip', r'\bigskip'):
        text = re.sub(r'\\' + cmd, '', text)
    return text


def normalize_blank_lines(text: str) -> str:
    """Collapse 3+ consecutive newlines to exactly 2."""
    return re.sub(r'\n{3,}', '\n\n', text)


def convert_latex_to_markdown(text: str) -> str:
    """Convert LaTeX source to Markdown."""
    # 1. Remove comments
    text = remove_comments(text)

    # 2. Extract body
    text = extract_body(text)

    # 3. Strip lines
    text = strip_lines(text)

    # 4. Convert display math
    text = convert_display_math(text)

    # 5. Convert sections
    text = convert_sections(text)

    # 6. Convert formatting
    text = convert_formatting(text)

    # 7. Delete commands
    text = delete_commands(text)

    # 8. Normalize blank lines
    text = normalize_blank_lines(text)

    return text


def main():
    args = sys.argv[1:]

    if len(args) < 1 or len(args) > 3:
        print("Usage: python l2m.py INPUT_FILE [-o OUTPUT_FILE]", file=sys.stderr)
        sys.exit(1)

    input_file = args[0]
    output_file = None

    if len(args) >= 2:
        if args[1] != '-o':
            print("Usage: python l2m.py INPUT_FILE [-o OUTPUT_FILE]", file=sys.stderr)
            sys.exit(1)
        if len(args) != 3:
            print("Usage: python l2m.py INPUT_FILE [-o OUTPUT_FILE]", file=sys.stderr)
            sys.exit(1)
        output_file = args[2]

    # Read input
    try:
        with open(input_file, 'r', encoding='utf-8') as f:
            content = f.read()
    except (FileNotFoundError, PermissionError, OSError):
        print(f"Error: cannot read '{input_file}'", file=sys.stderr)
        sys.exit(1)

    # Convert
    result = convert_latex_to_markdown(content)

    # Determine output path
    if output_file is None:
        path = Path(input_file)
        output_file = str(path.with_suffix('.md'))

    # Create parent directories if needed
    out_path = Path(output_file)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # Write output
    with open(output_file, 'w', encoding='utf-8') as f:
        f.write(result)

    sys.exit(0)


if __name__ == '__main__':
    main()
