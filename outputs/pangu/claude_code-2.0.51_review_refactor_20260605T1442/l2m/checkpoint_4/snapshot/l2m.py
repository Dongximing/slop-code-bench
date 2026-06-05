#!/usr/bin/env python3
"""LaTeX to Markdown converter."""

import re
import sys
from pathlib import Path

# Match \\item with optional bracket argument
_ITEM_PATTERN = re.compile(r'\\item(?:\[[^]]*\])?(.*?)(?=\\item|\Z)', re.DOTALL)

# Recognized theorem-like environments mapping
_ADMONITION_ENVIRONMENTS = {
    'definition': ('tip', 'Definition'),
    'definitions': ('tip', 'Definitions'),
    'propriete': ('tip', 'Propriete'),
    'proprietes': ('tip', 'Proprietes'),
    'theoreme': ('tip', 'Theoreme'),
    'theorem': ('tip', 'Theorem'),
    'exemple': ('note', 'Exemple'),
    'exemples': ('note', 'Exemples'),
    'remarque': ('note', 'Remarque'),
    'remarques': ('note', 'Remarques'),
    'preuve': ('note', 'Preuve'),
    'methode': ('warning', 'Methode'),
}

# Output mode: None (plain), 'docusaurus', or 'mkdocs'
_OUTPUT_MODE = None

# Patterns for macro definitions in preamble
_DEF_PATTERN = re.compile(r'\\def\\([a-zA-Z]+)\{([^}]*)\}')
_NEWCMD_PATTERN = re.compile(r'\\newcommand\\([a-zA-Z]+)\{([^}]*)\}')

# Patterns for environment unwrapping
_MULTICOLS_PATTERN = re.compile(r'\\begin\{multicols\}\{([^}]+)\}(.*?)\\end\{multicols\}', re.DOTALL)
_MINIPAGE_PATTERN = re.compile(r'\\begin\{minipage\}\{[^}]*\}(.*?)\\end\{minipage\}', re.DOTALL)
_PARBOX_PATTERN = re.compile(r'\\parbox\{[^}]*\}\{([^}]*)\}(.*)', re.DOTALL)

# Pattern for includegraphics
_INCLUDEGRAPHICS_PATTERN = re.compile(r'\\includegraphics(?:\[[^\]]*\])?\{([^}]*)\}')

# Pattern for \np macro
_NP_PATTERN = re.compile(r'\\np\{([^}]*)\}')


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
        return text[begin_match.end():end_match.start()]
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

                before = text[line_start:i]
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
            numbered_lines = []
            num_items_output = 0

            for item_match in item_matches:
                item_text = item_match.group(1).strip()

                nested_enum_start = item_text.find(r'\begin{enumerate}')
                if nested_enum_start >= 0:
                    before_nested = item_text[:nested_enum_start].strip()
                    nested_body_start = nested_enum_start + len(r'\begin{enumerate}')
                    nested_enum_end = item_text.find(r'\end{enumerate}', nested_body_start)
                    if nested_enum_end < 0:
                        nested_enum_end = len(item_text)

                    after_nested = item_text[nested_enum_end + len(r'\end{enumerate}'):].strip()
                    nested_body = item_text[nested_body_start:nested_enum_end].strip()

                    if before_nested:
                        numbered_lines.append(f'**{current_num}.** {before_nested}')
                        current_num += 1
                        num_items_output += 1

                    child_lines = convert_nested_enumerate(nested_body, current_num)
                    if child_lines:
                        if num_items_output > 0:
                            numbered_lines.append('')
                        numbered_lines.extend(child_lines)

                    if after_nested:
                        numbered_lines.append(f'**{current_num}.** {after_nested}')
                        current_num += 1
                        num_items_output += 1
                else:
                    # Regular item
                    numbered_lines.append(f'**{current_num}.** {item_text}')
                    current_num += 1
                    num_items_output += 1

            result_lines = []
            for j, line in enumerate(numbered_lines):
                if j > 0 and numbered_lines[j-1].startswith('**') and line.startswith('**'):
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

    if len(lines) > 1:
        result_lines = []
        for j, line in enumerate(lines):
            if j > 0:
                result_lines.append('')
            result_lines.append(line)
        return result_lines
    return lines

def _render_admonition(env_name: str, content: str, mode: str) -> str:
    """Render an admonition block based on the output mode.

    Args:
        env_name: The environment name ('solution' or a recognized theorem-like environment)
        content: The inner content of the environment
        mode: Output mode ('plain', 'docusaurus', or 'mkdocs')

    Returns:
        The rendered admonition block
    """
    if env_name == 'solution':
        admo_type = 'tip'
        label = 'Solution:'
    elif env_name in _ADMONITION_ENVIRONMENTS:
        admo_type, label = _ADMONITION_ENVIRONMENTS[env_name]
    else:
        # Unknown environment - return as-is (pass through unchanged)
        return r'\begin{' + env_name + '}' + content + r'\end{' + env_name + '}'

    # Process nested admonitions recursively
    processed_content = _process_nested_admonitions(content, mode)

    if mode == 'docusaurus':
        # Label for solution already contains ':', so don't add another
        if label.endswith(':'):
            return f':::{admo_type} {label}\n\n{processed_content}\n\n:::'
        else:
            return f':::{admo_type} {label}:\n\n{processed_content}\n\n:::'
    elif mode == 'mkdocs':
        indented = '\n'.join('    ' + line for line in processed_content.split('\n'))
        return f'!!!{admo_type} "{label}"\n\n{indented}'
    else:  # plain mode
        if env_name == 'solution':
            return f'**{label}**\n\n{processed_content}'
        else:
            return processed_content


def _process_nested_admonitions(text: str, mode: str) -> str:
    """Process nested admonition environments within text recursively.

    This handles recognized environments nested inside other recognized environments.
    """
    result = []
    i = 0

    while i < len(text):
        # Look for \begin{solution} or \begin{recognized-env}
        begin_match = re.search(r'\\begin\{([a-zA-Z]+)\}', text[i:])
        if not begin_match:
            # No more environments, append rest
            result.append(text[i:])
            break

        # Add text before the environment
        result.append(text[i:i + begin_match.start()])
        i += begin_match.end()

        env_name = begin_match.group(1)

        # Find matching end
        end_pattern = rf'\\end\{{{env_name}\}}'
        end_match = re.search(end_pattern, text[i:])
        if not end_match:
            # No closing tag - raise error for recognized environments/solutions
            if env_name in _ADMONITION_ENVIRONMENTS or env_name == 'solution':
                raise ValueError(f"unterminated environment '{env_name}'")
            # Unknown environments pass through
            result.append(rf'\begin{{{env_name}}}')
            result.append(text[i:])
            break

        env_body = text[i:i + end_match.start()]
        i += end_match.end()

        # Check if this is a recognized environment or solution
        if env_name in _ADMONITION_ENVIRONMENTS or env_name == 'solution':
            # Recursively render this admonition
            rendered = _render_admonition(env_name, env_body, mode)
            result.append(rendered)
        else:
            # Unknown environment - pass through unchanged
            result.append(rf'\begin{{{env_name}}}')
            result.append(env_body)
            result.append(rf'\end{{{env_name}}}')

    return ''.join(result)


def convert_admonitions(text: str) -> str:
    """Convert recognized theorem-like environments and solution blocks to admonitions.

    This processes top-level admonition environments and handles nested ones recursively.
    """
    # Determine mode based on global setting
    if _OUTPUT_MODE == 'docusaurus':
        mode = 'docusaurus'
    elif _OUTPUT_MODE == 'mkdocs':
        mode = 'mkdocs'
    else:
        mode = 'plain'

    return _process_nested_admonitions(text, mode)

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

    # 8. Convert admonitions
    text = convert_admonitions(text)

    # 9. Delete commands
    text = delete_commands(text)

    # 10. Normalize blank lines
    text = normalize_blank_lines(text)

    return text

def main():
    args = sys.argv[1:]

    # Parse arguments
    input_file = None
    output_file = None
    mode_flag = None  # 'docusaurus' or 'mkdocs'

    i = 0
    while i < len(args):
        arg = args[i]

        if arg == '-o':
            if i + 1 >= len(args):
                print("Usage: python l2m.py INPUT_FILE [-o OUTPUT_FILE] [--docusaurus | --mkdocs]", file=sys.stderr)
                sys.exit(1)
            if output_file is not None:
                print("Usage: python l2m.py INPUT_FILE [-o OUTPUT_FILE] [--docusaurus | --mkdocs]", file=sys.stderr)
                sys.exit(1)
            output_file = args[i + 1]
            i += 2
        elif arg == '--docusaurus':
            if mode_flag is not None:
                print("Error: choose at most one output mode", file=sys.stderr)
                sys.exit(1)
            mode_flag = 'docusaurus'
            i += 1
        elif arg == '--mkdocs':
            if mode_flag is not None:
                print("Error: choose at most one output mode", file=sys.stderr)
                sys.exit(1)
            mode_flag = 'mkdocs'
            i += 1
        elif input_file is None:
            input_file = arg
            i += 1
        else:
            print("Usage: python l2m.py INPUT_FILE [-o OUTPUT_FILE] [--docusaurus | --mkdocs]", file=sys.stderr)
            sys.exit(1)

    if input_file is None:
        print("Usage: python l2m.py INPUT_FILE [-o OUTPUT_FILE] [--docusaurus | --mkdocs]", file=sys.stderr)
        sys.exit(1)

    # Read input
    try:
        with open(input_file, 'r', encoding='utf-8') as f:
            content = f.read()
    except (FileNotFoundError, PermissionError, OSError):
        print(f"Error: cannot read '{input_file}'", file=sys.stderr)
        sys.exit(1)

    # Set global output mode
    global _OUTPUT_MODE
    _OUTPUT_MODE = mode_flag

    # Convert (with error handling for unterminated environments)
    try:
        result = convert_latex_to_markdown(content)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

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
