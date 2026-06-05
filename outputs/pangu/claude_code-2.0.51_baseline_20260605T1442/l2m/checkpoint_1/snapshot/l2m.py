#!/usr/bin/env python3
"""LaTeX to Markdown converter."""

import re
import sys
from pathlib import Path


def remove_comments(text: str) -> str:
    """Remove LaTeX comments (unescaped % to end of line)."""
    lines = text.split('\n')
    result = []
    for line in lines:
        i = 0
        comment_start = -1
        while i < len(line):
            if line[i] == '%':
                # Check if escaped (preceded by odd number of backslashes)
                backslash_count = 0
                j = i - 1
                while j >= 0 and line[j] == '\\':
                    backslash_count += 1
                    j -= 1
                # If odd backslashes, % is escaped
                if backslash_count % 2 == 1:
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
    r"""Extract text between \begin{document} and \end{document}."""
    begin_match = re.search(r'\\begin\{document\}', text)
    end_match = re.search(r'\\end\{document\}', text)
    if begin_match and end_match:
        return text[begin_match.end():end_match.start()]
    return text


def strip_lines(text: str) -> str:
    """Strip leading and trailing whitespace from each line."""
    lines = text.split('\n')
    return '\n'.join(line.strip() for line in lines)


def convert_display_math(text: str) -> str:
    r"""Convert \[...\] to $$...$$ blocks."""
    result = []
    i = 0
    while i < len(text):
        # Check for \[
        if i + 1 < len(text) and text[i:i+2] == '\\[':
            # Find matching \]
            j = i + 2
            depth = 1
            while j < len(text) and depth > 0:
                if j + 1 < len(text) and text[j:j+2] == '\\[':
                    depth += 1
                    j += 2
                elif j + 1 < len(text) and text[j:j+2] == '\\]':
                    depth -= 1
                    if depth > 0:
                        j += 2
                    else:
                        break
                else:
                    j += 1
            if depth == 0:
                math_content = text[i+2:j]
                # Output $$ block
                result.append('$$')
                result.append(math_content.strip())
                result.append('$$')
                i = j + 2
            else:
                # No closing bracket, treat as literal
                result.append(text[i])
                i += 1
        else:
            result.append(text[i])
            i += 1

    output = ''.join(result)
    # Clean up adjacent lines: if $$ block has text on same line, separate them
    # This handles cases like "text$$math$$text"
    lines = output.split('\n')
    cleaned_lines = []
    for line in lines:
        # Check if line has text mixed with $$ markers
        if '$$' in line:
            parts = line.split('$$')
            # parts[0] - text before first $$
            # parts[1] - math content
            # parts[2] - text after second $$ (if any)
            # parts[3+] - should not happen with valid input
            if len(parts) >= 1 and parts[0]:
                cleaned_lines.append(parts[0].rstrip())
            if len(parts) >= 2:
                cleaned_lines.append('$$')
                cleaned_lines.append(parts[1].strip())
                cleaned_lines.append('$$')
            if len(parts) >= 3 and parts[2]:
                cleaned_lines.append(parts[2].lstrip())
            if len(parts) > 3:
                # Shouldn't happen, but append remaining
                for p in parts[3:]:
                    cleaned_lines.append(f'$$ {p}' if p else '$$')
        else:
            cleaned_lines.append(line)

    return '\n'.join(cleaned_lines)


def convert_formatting(text: str) -> str:
    """Convert section commands and inline formatting."""
    # Section commands
    text = re.sub(r'\\section\{([^}]+)\}', r'## \1', text)
    text = re.sub(r'\\subsection\{([^}]+)\}', r'### \1', text)
    text = re.sub(r'\\subsubsection\{([^}]+)\}', r'#### \1', text)

    # Inline formatting with nested brace handling
    def replace_emph(match):
        return '_' + match.group(1) + '_'

    def replace_bf(match):
        return '**' + match.group(1) + '**'

    # Match \emph{...} where ... may contain balanced braces
    def replace_balanced(match):
        prefix = match.group(0)[:-1]  # everything before opening brace
        content = match.group(1)
        cmd = match.group(2)
        if cmd == 'emph':
            return '_' + content + '_'
        elif cmd == 'textbf':
            return '**' + content + '**'
        return match.group(0)

    # Use a more robust pattern that handles nested braces
    # Pattern: \\cmd{content}
    # We need to parse balanced braces
    i = 0
    result = []
    while i < len(text):
        # Look for backslash followed by word and brace
        match = re.match(r'\\(emph|textbf)\{', text[i:])
        if match:
            # Found a formatting command, find matching brace
            cmd = match.group(1)
            start = i + match.end() - 1
            brace_count = 1
            j = start + 1
            while j < len(text) and brace_count > 0:
                if text[j] == '{':
                    brace_count += 1
                elif text[j] == '}':
                    brace_count -= 1
                j += 1
            if brace_count == 0:
                content = text[start + 1:j - 1]
                if cmd == 'emph':
                    result.append('_' + content + '_')
                else:
                    result.append('**' + content + '**')
                i = j
            else:
                result.append(text[i])
                i += 1
        else:
            result.append(text[i])
            i += 1

    return ''.join(result)


def delete_commands(text: str) -> str:
    """Remove \\vspace{...} and skip commands."""
    # Remove \vspace{...}
    text = re.sub(r'\\vspace\{[^}]*\}', '', text)
    text = re.sub(r'\\medskip', '', text)
    text = re.sub(r'\\smallskip', '', text)
    text = re.sub(r'\\bigskip', '', text)
    return text


def normalize_blank_lines(text: str) -> str:
    """Collapse 3+ consecutive newlines to exactly 2 newlines."""
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
    # 5. Convert formatting and sections
    text = convert_formatting(text)
    # 6. Delete commands
    text = delete_commands(text)
    # 7. Strip lines again
    text = strip_lines(text)
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
        if args[1] == '-o':
            if len(args) != 3:
                print("Usage: python l2m.py INPUT_FILE [-o OUTPUT_FILE]", file=sys.stderr)
                sys.exit(1)
            output_file = args[2]
        else:
            print("Usage: python l2m.py INPUT_FILE [-o OUTPUT_FILE]", file=sys.stderr)
            sys.exit(1)

    # Read input file
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
        input_path = Path(input_file)
        output_file = str(input_path.with_suffix('.md'))

    # Create parent directories if needed
    output_path = Path(output_file)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Write output
    with open(output_file, 'w', encoding='utf-8') as f:
        f.write(result)

    sys.exit(0)


if __name__ == '__main__':
    main()
