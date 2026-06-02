#!/usr/bin/env python3
"""LaTeX to KaTeX-compatible Markdown converter."""

import argparse
import re
import sys
from pathlib import Path


def remove_comments(text: str) -> str:
    """Remove LaTeX comments (first unescaped % on each line)."""
    # First, temporarily replace escaped % with a placeholder (doesn't contain %)
    text = text.replace('\\%', '\x01ESCAPED_PERCENT\x01')

    lines = text.split('\n')
    result = []
    for line in lines:
        # Find the first unescaped %
        i = 0
        while i < len(line):
            if line[i] == '%' and (i == 0 or line[i-1] != '\\'):
                # This is an unescaped % - it starts a comment
                result.append(line[:i])
                break
            i += 1
        else:
            # No unescaped % found
            result.append(line)

    text = '\n'.join(result)
    # Restore escaped percents
    text = text.replace('\x01ESCAPED_PERCENT\x01', '%')
    return text


def extract_body(text: str) -> str:
    r"""Extract text between \begin{document} and \end{document} if present."""
    begin_match = re.search(r'\\begin\{document\}', text)
    end_match = re.search(r'\\end\{document\}', text)

    if begin_match and end_match:
        start = begin_match.end()
        end = end_match.start()
        return text[start:end]

    return text


def strip_whitespace(text: str) -> str:
    """Strip leading and trailing whitespace from each line."""
    lines = text.split('\n')
    stripped = [line.strip() for line in lines]
    return '\n'.join(stripped)


def convert_display_math(text: str) -> str:
    r"""Convert display math from \[...\] to $$...$$ blocks."""
    result = []
    i = 0
    while i < len(text):
        # Find next \[
        open_match = text.find('\\[', i)
        if open_match == -1:
            # No more display math
            result.append(text[i:])
            break

        # Add text before \[
        before = text[i:open_match]
        if before:
            result.append(before)

        # Find closing \]
        close_match = text.find('\\]', open_match + 2)
        if close_match == -1:
            # Unclosed - treat as literal
            result.append(text[open_match:])
            break

        # Extract math content (between \[ and \])
        math_content = text[open_match + 2:close_match]

        # Add $$ block with math content on separate lines
        result.append('\n$$\n')
        result.append(math_content)
        result.append('\n$$\n')

        i = close_match + 2

    return ''.join(result)


def convert_sections_and_formatting(text: str) -> str:
    """Convert section commands and inline formatting."""
    # Section commands
    text = re.sub(r'\\section\{([^}]+)\}', r'## \1', text)
    text = re.sub(r'\\subsection\{([^}]+)\}', r'### \1', text)
    text = re.sub(r'\\subsubsection\{([^}]+)\}', r'#### \1', text)

    # Inline formatting - need to handle nested/braced content
    # \emph{text} -> _text_
    # \textbf{text} -> **text**

    def replace_emph(match):
        return '_' + match.group(1) + '_'

    def replace_bf(match):
        return '**' + match.group(1) + '**'

    # Simple pattern to match \emph{...} and \textbf{...}
    # Handle balanced braces
    text = re.sub(r'\\emph\{([^}]*)\}', replace_emph, text)
    text = re.sub(r'\\textbf\{([^}]*)\}', replace_bf, text)

    return text


def delete_commands(text: str) -> str:
    r"""Remove \vspace{...}, \medskip, \smallskip, \bigskip commands."""
    # Remove \vspace{...}
    text = re.sub(r'\\vspace\{[^}]*\}', '', text)
    # Remove skip commands
    text = re.sub(r'\\medskip', '', text)
    text = re.sub(r'\\smallskip', '', text)
    text = re.sub(r'\\bigskip', '', text)
    return text


def normalize_blanks(text: str) -> str:
    """Collapse 3+ consecutive newlines to exactly 2."""
    return re.sub(r'\n{3,}', '\n\n', text)


def convert_latex_to_markdown(input_text: str) -> str:
    """Convert LaTeX source to Markdown."""
    # Step 1: Remove comments
    text = remove_comments(input_text)

    # Step 2: Extract body (between \begin{document} and \end{document})
    text = extract_body(text)

    # Step 3: Strip whitespace from each line
    text = strip_whitespace(text)

    # Step 4: Convert display math
    text = convert_display_math(text)

    # Step 5: Convert sections and inline formatting
    text = convert_sections_and_formatting(text)

    # Step 6: Delete commands
    text = delete_commands(text)

    # Step 7: Normalize blank lines
    text = normalize_blanks(text)

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
    except (FileNotFoundError, OSError):
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
        # Replace final extension with .md
        stem = input_path.stem
        output_path = input_path.parent / f"{stem}.md"

    # Write output
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(output_text)

    sys.exit(0)


if __name__ == '__main__':
    main()
