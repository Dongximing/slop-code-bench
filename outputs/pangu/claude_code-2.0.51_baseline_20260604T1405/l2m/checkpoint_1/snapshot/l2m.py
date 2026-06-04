#!/usr/bin/env python3
"""LaTeX to Markdown converter."""

import argparse
import re
import sys
from pathlib import Path


def remove_comments(text: str) -> str:
    """Remove LaTeX comments, handling escaped percent signs."""
    lines = text.split('\n')
    result_lines = []
    for line in lines:
        # Find first unescaped %
        i = 0
        comment_start = -1
        while i < len(line):
            if line[i] == '%':
                # Check if it's escaped (preceded by backslash)
                if i > 0 and line[i-1] == '\\':
                    # Check if the backslash is escaped itself
                    j = i - 2
                    escape_count = 1
                    while j >= 0 and line[j] == '\\':
                        escape_count += 1
                        j -= 1
                    # If odd number of backslashes, it's escaped
                    if escape_count % 2 == 1:
                        i += 1
                        continue
                # Unescaped % found - this starts a comment
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
    # Use regex to replace all \\[...\\] patterns
    def replace_display_math(match):
        math_content = match.group(1)
        return '\n$$\n' + math_content + '\n$$\n'

    # Pattern to match \\[...\\] with content
    pattern = r'\\\[(.*?)\\\]'

    # We need to be careful about newlines in math content
    # Use DOTALL flag to match across lines
    text = re.sub(pattern, replace_display_math, text, flags=re.DOTALL)

    return text


def convert_formatting(text: str) -> str:
    """Convert section commands and inline formatting."""
    # Section commands
    text = re.sub(r'\\section\{([^}]+)\}', r'## \1', text)
    text = re.sub(r'\\subsection\{([^}]+)\}', r'### \1', text)
    text = re.sub(r'\\subsubsection\{([^}]+)\}', r'#### \1', text)

    # Inline formatting
    text = re.sub(r'\\emph\{([^}]+)\}', r'_\1_', text)
    text = re.sub(r'\\textbf\{([^}]+)\}', r'**\1**', text)

    return text


def delete_commands(text: str) -> str:
    """Remove \\vspace{...}, \\medskip, \\smallskip, \\bigskip commands."""
    # Remove \vspace{...}
    text = re.sub(r'\\vspace\{[^}]*\}', '', text)
    # Remove skip commands (they may or may not have braces)
    text = re.sub(r'\\medskip\s*', '', text)
    text = re.sub(r'\\smallskip\s*', '', text)
    text = re.sub(r'\\bigskip\s*', '', text)

    return text


def normalize_blank_lines(text: str) -> str:
    """Collapse runs of 3 or more newlines to exactly 2 newlines."""
    # Replace 3+ newlines with 2 newlines
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text


def convert_latex_to_markdown(input_text: str) -> str:
    """Convert LaTeX source to Markdown."""
    # Step 1: Remove comments
    text = remove_comments(input_text)

    # Step 2: Extract body (between \begin{document} and \end{document})
    text = extract_body(text)

    # Step 3: Strip lines
    text = strip_lines(text)

    # Step 4: Convert display math
    text = convert_display_math(text)

    # Step 5: Convert formatting and section commands
    text = convert_formatting(text)

    # Step 6: Delete commands
    text = delete_commands(text)

    # Step 7: Normalize blank lines
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

    # Read input file
    try:
        with open(input_path, 'r', encoding='utf-8') as f:
            input_text = f.read()
    except (FileNotFoundError, PermissionError, OSError):
        print(f"Error: cannot read '{args.input_file}'", file=sys.stderr)
        sys.exit(1)

    # Convert
    output_text = convert_latex_to_markdown(input_text)

    # Determine output path
    if args.output:
        output_path = Path(args.output)
        # Create parent directories if needed
        output_path.parent.mkdir(parents=True, exist_ok=True)
    else:
        # Replace extension with .md
        output_path = input_path.with_suffix('.md')

    # Write output
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(output_text)

    sys.exit(0)


if __name__ == '__main__':
    main()
