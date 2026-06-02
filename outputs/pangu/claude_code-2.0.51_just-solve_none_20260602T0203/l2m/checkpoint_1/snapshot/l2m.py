#!/usr/bin/env python3
"""LaTeX to Markdown converter (l2m.py)"""

import re
import sys
from pathlib import Path


def remove_comments(text: str) -> str:
    """Remove LaTeX comments, treating \% as literal."""
    # Replace escaped percent signs with a placeholder
    text = text.replace('\\%', '\\x00percent\\x00')
    # Remove everything from first unescaped % to end of line
    lines = text.split('\n')
    cleaned_lines = []
    for line in lines:
        # Find first unescaped %
        match = re.search(r'(?<!\\)%', line)
        if match:
            line = line[:match.start()]
        cleaned_lines.append(line)
    # Restore escaped percent signs
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

    # Find all display math patterns
    pattern = re.compile(r'\\\[(.*?)\\\]', re.DOTALL)

    for match in pattern.finditer(text):
        # Text before this math expression
        before_text = text[last_end:match.start()]
        if before_text:
            result.append(before_text.rstrip())
            result.append('')  # blank line before math block

        # Math content
        math_content = match.group(1)
        result.append('$$')
        result.append(math_content)
        result.append('$$')
        result.append('')  # blank line after math block

        last_end = match.end()

    # Add any remaining text after the last math expression
    remaining_text = text[last_end:]
    if remaining_text:
        result.append(remaining_text)

    output = ''.join(result).rstrip()
    return output


def convert_sections_and_formatting(text: str) -> str:
    """Convert section commands and inline formatting."""
    # Convert sections (must be before inline formatting to avoid partial matches)
    text = re.sub(r'\\section\{([^}]+)\}', r'## \1', text)
    text = re.sub(r'\\subsection\{([^}]+)\}', r'### \1', text)
    text = re.sub(r'\\subsubsection\{([^}]+)\}', r'#### \1', text)

    # Convert inline formatting
    text = re.sub(r'\\emph\{([^}]+)\}', r'_\1_', text)
    text = re.sub(r'\\textbf\{([^}]+)\}', r'**\1**', text)

    return text


def delete_commands(text: str) -> str:
    """Remove \vspace{...}, \medskip, \smallskip, \bigskip commands."""
    # Remove \vspace{...}
    text = re.sub(r'\\vspace\{[^}]*\}', '', text)
    # Remove skip commands
    text = re.sub(r'\\medskip', '', text)
    text = re.sub(r'\\smallskip', '', text)
    text = re.sub(r'\\bigskip', '', text)
    return text


def normalize_blank_lines(text: str) -> str:
    """Collapse runs of 3+ newlines to exactly 2 newlines."""
    return re.sub(r'\n{3,}', '\n\n', text)


def convert_latex_to_markdown(latex_text: str) -> str:
    """Convert LaTeX source to Markdown."""
    # Step 1: Remove comments
    text = remove_comments(latex_text)

    # Step 2: Extract body (between \begin{document} and \end{document})
    text = extract_body(text)

    # Step 3: Strip leading/trailing whitespace from each line
    text = strip_lines(text)

    # Step 4: Convert display math (before section/formatting to handle edge cases)
    text = convert_display_math(text)

    # Step 5: Convert sections and inline formatting
    text = convert_sections_and_formatting(text)

    # Step 6: Delete specific commands
    text = delete_commands(text)

    # Step 7: Normalize blank lines
    text = normalize_blank_lines(text)

    return text.strip()


def main():
    args = sys.argv[1:]

    if not args or args[0] in ['-h', '--help']:
        print(f"Usage: python {Path(__file__).name} INPUT_FILE [-o OUTPUT_FILE]")
        sys.exit(1)

    input_file = args[0]
    output_file = None

    # Parse arguments
    i = 1
    while i < len(args):
        if args[i] == '-o' and i + 1 < len(args):
            output_file = args[i + 1]
            i += 2
        else:
            i += 1

    # Read input file
    try:
        with open(input_file, 'r', encoding='utf-8') as f:
            latex_content = f.read()
    except (FileNotFoundError, PermissionError, OSError):
        print(f"Error: cannot read '{input_file}'", file=sys.stderr)
        sys.exit(1)

    # Convert to Markdown
    markdown_content = convert_latex_to_markdown(latex_content)

    # Determine output path
    if output_file is None:
        input_path = Path(input_file)
        output_file = input_path.with_suffix('.md')
    else:
        output_path = Path(output_file)
        output_path.parent.mkdir(parents=True, exist_ok=True)

    # Write output
    with open(output_file, 'w', encoding='utf-8') as f:
        f.write(markdown_content)

    sys.exit(0)


if __name__ == '__main__':
    main()
