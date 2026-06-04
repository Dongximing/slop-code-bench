#!/usr/bin/env python3
"""LaTeX to Markdown converter."""

import re
import sys


def remove_comments(text: str) -> str:
    r"""Remove LaTeX comments (first unescaped % on each line)."""
    lines = text.splitlines(keepends=True)
    result = []

    for line in lines:
        i = 0
        while i < len(line):
            if line[i] == '%':
                # Check if this is an escaped percent (preceded by \)
                # Count backslashes before the %
                backslash_count = 0
                j = i - 1
                while j >= 0 and line[j] == '\\':
                    backslash_count += 1
                    j -= 1
                # If there's an odd number of backslashes, the % is escaped
                if backslash_count % 2 == 1:
                    # Escaped %, keep it
                    result.append(line[i])
                    i += 1
                else:
                    # Unescaped %, start of comment, discard rest of line
                    break
            else:
                result.append(line[i])
                i += 1
        # Keep the newline if there was one
        if line.endswith('\n'):
            result.append('\n')

    return ''.join(result)


def extract_body(text: str) -> str:
    r"""Extract text between \begin{document} and \end{document} if present."""
    begin_match = re.search(r'\\begin\{document\}', text)
    if begin_match:
        start_pos = begin_match.end()
        end_match = re.search(r'\\end\{document\}', text[start_pos:])
        if end_match:
            return text[start_pos:start_pos + end_match.start()]
        return text[start_pos:]
    return text


def strip_lines(text: str) -> str:
    """Strip leading and trailing whitespace from each line."""
    lines = text.splitlines(keepends=True)
    stripped = []
    for line in lines:
        if line.endswith('\n'):
            stripped.append(line.rstrip('\n').strip() + '\n')
        elif line == '':
            # Empty line at end of file without newline
            stripped.append('')
        else:
            stripped.append(line.strip())
    return ''.join(stripped)


def convert_display_math(text: str) -> str:
    r"""Convert display math delimiters \[ ... \] to $$ ... $$."""
    result = []
    i = 0

    while i < len(text):
        # Look for \[
        if text[i:i+2] == '\\[':
            # Find matching \]
            j = i + 2
            content = []
            while j < len(text):
                if text[j:j+2] == '\\\]':
                    # Found matching \]
                    result.append('$$\n')
                    if content:
                        result.append(''.join(content))
                        if content[-1] != '\n':
                            result.append('\n')
                    result.append('$$\n')
                    i = j + 2
                    break
                elif text[j:j+2] == '\\[':
                    # Nested \[ - treat as literal
                    content.append('\\[')
                    j += 2
                else:
                    content.append(text[j])
                    j += 1
            else:
                # No closing \] found, treat as literal
                result.append('\\[')
                result.extend(content)
                i = j
        else:
            result.append(text[i])
            i += 1

    return ''.join(result)


def convert_sections_and_formatting(text: str) -> str:
    r"""Convert section commands and inline formatting."""
    # Section commands
    text = re.sub(r'\\section\{([^}]*)\}', r'## \1', text)
    text = re.sub(r'\\subsection\{([^}]*)\}', r'### \1', text)
    text = re.sub(r'\\subsubsection\{([^}]*)\}', r'#### \1', text)

    # Inline formatting
    text = re.sub(r'\\emph\{([^}]*)\}', r'_\1_', text)
    text = re.sub(r'\\textbf\{([^}]*)\}', r'**\1**', text)

    return text


def remove_commands(text: str) -> str:
    """Remove specific LaTeX commands."""
    # Remove \vspace{...}
    text = re.sub(r'\\vspace\{[^}]*\}', '', text)
    # Remove \medskip, \smallskip, \bigskip
    text = re.sub(r'\\medskip\b', '', text)
    text = re.sub(r'\\smallskip\b', '', text)
    text = re.sub(r'\\bigskip\b', '', text)
    return text


def normalize_blank_lines(text: str) -> str:
    """Collapse 3+ consecutive newlines to exactly 2 newlines."""
    # Replace 3 or more newlines with exactly 2
    return re.sub(r'\n{3,}', '\n\n', text)


def process_latex_to_markdown(text: str) -> str:
    """Process LaTeX text and convert to Markdown."""
    # Step 1: Remove comments
    text = remove_comments(text)

    # Step 2: Extract body (between \begin{document} and \end{document})
    text = extract_body(text)

    # Step 3: Strip lines
    text = strip_lines(text)

    # Step 4: Convert sections and inline formatting
    text = convert_sections_and_formatting(text)

    # Step 5: Convert display math (needs to be done after other conversions)
    text = convert_display_math(text)

    # Step 6: Remove commands
    text = remove_commands(text)

    # Step 7: Normalize blank lines
    text = normalize_blank_lines(text)

    return text


def main():
    """Main entry point."""
    import argparse

    parser = argparse.ArgumentParser(
        description='Convert LaTeX to KaTeX-compatible Markdown.'
    )
    parser.add_argument('input_file', help='Input LaTeX file')
    parser.add_argument('-o', '--output', help='Output Markdown file')

    args = parser.parse_args()

    # Read input file
    try:
        with open(args.input_file, 'r', encoding='utf-8') as f:
            latex_content = f.read()
    except (IOError, OSError):
        print(f"Error: cannot read '{args.input_file}'", file=sys.stderr)
        sys.exit(1)

    # Process the content
    markdown_content = process_latex_to_markdown(latex_content)

    # Determine output file
    if args.output:
        output_path = args.output
        # Create parent directories if needed
        import os
        os.makedirs(os.path.dirname(output_path) or '.', exist_ok=True)
    else:
        # Replace extension with .md
        root, _ = os.path.splitext(args.input_file)
        output_path = f"{root}.md"

    # Write output
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(markdown_content)

    sys.exit(0)


if __name__ == '__main__':
    main()
