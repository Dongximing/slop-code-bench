#!/usr/bin/env python3
"""LaTeX to Markdown converter."""

import re
import sys
from pathlib import Path

# Compiled regex pattern for matching \item with optional optional argument
_ITEM_PATTERN = re.compile(r'\\item(?:\[[^]]*\])?(.*?)(?=\\item|\Z)', re.DOTALL)


def _int_to_letters(n):
    """Convert integer 1-26 to a, b, ... z, aa, ab, ..."""
    result = ""
    while n > 0:
        n -= 1
        result = chr(ord('a') + n % 26) + result
        n = n // 26
    return result

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

def convert_lists(text: str) -> str:
    """Convert LaTeX enumerate and itemize environments to Markdown."""
    result = []
    i = 0

    while i < len(text):
        # Check for enumerate or itemize begin (with optional [start=N])
        begin_match = re.search(r'\\begin\{(enumerate|itemize)\}(?:\[([^\]]*)\])?', text[i:])
        if not begin_match:
            # No more list environments, append rest
            result.append(text[i:])
            break

        # Add text before the environment
        result.append(text[i:i + begin_match.start()])
        i += begin_match.end()

        env_name = begin_match.group(1)
        env_opts = begin_match.group(2)  # Can be None or the options string

        # For enumerate, check for invalid options
        if env_name == 'enumerate' and env_opts and env_opts.strip():
            # Only valid if it's exactly start=N where N is digits
            if not re.match(r'^\s*start=\d+\s*$', env_opts):
                print("Error: unsupported enumerate options", file=sys.stderr)
                sys.exit(1)

        # Find matching end
        end_pattern = rf'\\end\{{{env_name}\}}'
        end_match = re.search(end_pattern, text[i:])
        if not end_match:
            # No closing tag, treat as literal
            result.append(rf'\begin{{{env_name}}}')
            if env_opts:
                result.append(f'[{env_opts}]')
            continue

        env_body = text[i:i + end_match.start()]
        i += end_match.end()

        if env_name == 'itemize':
            # Convert itemize to bullet list
            item_matches = _ITEM_PATTERN.finditer(env_body)
            bullet_lines = []
            for item_match in item_matches:
                item_text = item_match.group(1).strip()
                bullet_lines.append(f'-   {item_text}')
            result.append('\n\n'.join(bullet_lines))

        elif env_name == 'enumerate':
            # Start number
            if env_opts:
                start_match = re.search(r'start=(\d+)', env_opts)
                if start_match:
                    current_num = int(start_match.group(1))
                else:
                    current_num = 1
            else:
                current_num = 1

            # Process enumerate items - match non-greedily to next \item or end of env
            item_matches = _ITEM_PATTERN.finditer(env_body)
            item_pattern = ''  # removed
            numbered_lines = []
            num_items_output = 0

            for item_match in item_matches:
                item_text = item_match.group(1).strip()

                # Check for nested enumerate in this item
                nested_enum_start = item_text.find(r'\begin{enumerate}')
                if nested_enum_start >= 0:
                    # Extract outer content before nested enumerate
                    before_nested = item_text[:nested_enum_start].strip()
                    nested_body_start = nested_enum_start + len(r'\begin{enumerate}')
                    nested_enum_end = item_text.find(r'\end{enumerate}', nested_body_start)
                    if nested_enum_end < 0:
                        # Malformed, treat as text
                        nested_enum_end = len(item_text)

                    after_nested = item_text[nested_enum_end + len(r'\end{enumerate}'):].strip()
                    nested_body = item_text[nested_body_start:nested_enum_end].strip()

                    # Output outer item if it has content
                    if before_nested:
                        numbered_lines.append(f'**{current_num}.** {before_nested}')
                        current_num += 1
                        num_items_output += 1

                    # Process nested enumerate
                    child_lines = convert_nested_enumerate(nested_body, current_num)
                    if child_lines:
                        if num_items_output > 0:  # Add blank line before nested if we already output something
                            numbered_lines.append('')
                        numbered_lines.extend(child_lines)

                    # After nested enumerate, continue with after_nested
                    if after_nested:
                        # No blank line needed since nested already handles its own spacing
                        numbered_lines.append(f'**{current_num}.** {after_nested}')
                        current_num += 1
                        num_items_output += 1
                else:
                    # Regular item
                    numbered_lines.append(f'**{current_num}.** {item_text}')
                    current_num += 1
                    num_items_output += 1

            # Now add blank lines between consecutive items
            result_lines = []
            for j, line in enumerate(numbered_lines):
                if j > 0 and numbered_lines[j-1].startswith('**') and line.startswith('**'):
                    # Both are outer items, add blank line between consecutive items
                    result_lines.append('')
                result_lines.append(line)

            result.append('\n\n'.join(result_lines))
        else:
            result.append(rf'\begin{{{env_name}}}')
            if env_opts:
                result.append(f'[{env_opts}]')
            result.append(env_body)
            result.append(rf'\end{{{env_name}}}')

    return ''.join(result)

def convert_nested_enumerate(body: str, parent_num: int) -> list:
    """Convert nested enumerate body to child list items."""
    lines = []
    letter_num = 1

    # Use non-greedy matching for items
    item_matches = _ITEM_PATTERN.finditer(body)

    for item_match in item_matches:
        item_text = item_match.group(1).strip()
        letter = _int_to_letters(letter_num)
        lines.append(f'**{parent_num}.{letter})** {item_text}')
        letter_num += 1

    # Add blank lines between consecutive nested items using join
    # We need to modify: after each item except last, add blank
    if len(lines) > 1:
        result_lines = []
        for j, line in enumerate(lines):
            if j > 0:
                result_lines.append('')
            result_lines.append(line)
        return result_lines
    return lines

def delete_commands(text: str) -> str:
    """Remove \\vspace{...}, \\medskip, \\smallskip, \\bigskip commands."""
    text = re.sub(r'\\vspace{[^}]*}', '', text)
    text = re.sub(r'\\medskip', '', text)
    text = re.sub(r'\\smallskip', '', text)
    text = re.sub(r'\\bigskip', '', text)
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

    # 7. Convert lists
    text = convert_lists(text)

    # 8. Delete commands
    text = delete_commands(text)

    # 9. Normalize blank lines
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
