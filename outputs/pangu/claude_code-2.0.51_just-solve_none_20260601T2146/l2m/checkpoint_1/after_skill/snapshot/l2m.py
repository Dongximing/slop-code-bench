#!/usr/bin/env python3
"""LaTeX to KaTeX-compatible Markdown converter."""

import argparse
import re
import sys
from pathlib import Path


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


def convert_latex_to_markdown(input_text: str) -> str:
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
