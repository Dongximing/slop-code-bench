#!/usr/bin/env python3
"""LaTeX to Markdown converter."""

import argparse
import os
import re
import sys
from collections import OrderedDict


def remove_comments(text: str) -> str:
    r"""Remove LaTeX comments (first unescaped % on each line)."""
    lines = text.splitlines(keepends=True)
    result = []

    for line in lines:
        i = 0
        while i < len(line):
            if line[i] == '%':
                # Check if this is an escaped percent (preceded by \)
                backslash_count = 0
                j = i - 1
                while j >= 0 and line[j] == '\\':
                    backslash_count += 1
                    j -= 1
                if backslash_count % 2 == 1:
                    result.append(line[i])
                    i += 1
                else:
                    break
            else:
                result.append(line[i])
                i += 1
        if line.endswith('\n'):
            result.append('\n')

    return ''.join(result)


def extract_macros(text: str) -> dict:
    r"""Extract parameter-free macro definitions from preamble.

    Returns a dict mapping command names to their replacement text.
    Only processes \def\CMD{replacement} and \newcommand{\CMD}{replacement}
    without optional parameter arguments.
    """
    # Find preamble (text before \begin{document})
    begin_match = re.search(r'\\begin\{document\}', text)
    if not begin_match:
        return {}

    preamble = text[:begin_match.start()]

    macros = {}

    # Match \def\CMD{replacement}
    # Must not have optional parameter spec like \def\CMD[...]{...}
    for match in re.finditer(r'\\def\s*(\\[a-zA-Z]+)\s*\{([^}]*)\}', preamble):
        cmd_name = match.group(1)
        replacement = match.group(2)
        macros[cmd_name] = replacement

    # Match \newcommand{\CMD}{replacement}
    # Must not have optional parameter spec like \newcommand{\CMD}[...]{...}
    for match in re.finditer(r'\\newcommand\s*\{(\\[a-zA-Z]+)\}\s*\{([^}]*)\}', preamble):
        cmd_name = match.group(1)
        replacement = match.group(2)
        macros[cmd_name] = replacement

    return macros


def expand_macros(text: str, macros: dict) -> str:
    r"""Expand macros in text, detecting cycles.

    Applies macro substitutions repeatedly until body text stops changing.
    For cyclic macro definitions, exits with status 1 and writes error to stderr.
    """
    if not macros:
        return text

    # Build dependency graph to detect cycles in macro definitions
    graph = {}

    # For each macro, find what macros it references in its replacement
    for cmd, replacement in macros.items():
        deps = set()
        # Find any macro command names in the replacement
        for other_cmd in macros:
            if other_cmd == cmd:
                continue
            pattern = re.compile(r'(?<!\\)' + re.escape(other_cmd) + r'(?![a-zA-Z])')
            if pattern.search(replacement):
                deps.add(other_cmd)
        graph[cmd] = deps

    # Check for cycles using depth-first search
    visited = set()
    in_stack = set()

    def has_cycle(cmd):
        """Return True if cmd has a cycle in its dependency graph."""
        visited.add(cmd)
        in_stack.add(cmd)

        for dep in graph.get(cmd, []):
            if dep not in visited:
                if has_cycle(dep):
                    return True
            elif dep in in_stack:
                return True

        in_stack.remove(cmd)
        return False

    for cmd in macros:
        if cmd not in visited:
            if has_cycle(cmd):
                print("Error: cyclic macro definition", file=sys.stderr)
                sys.exit(1)

    result = text
    visited_states = set()

    while True:
        if result in visited_states:
            print("Error: cyclic macro definition", file=sys.stderr)
            sys.exit(1)
        visited_states.add(result)

        new_result = result
        sorted_macros = sorted(macros.items(), key=lambda x: len(x[0]), reverse=True)

        for cmd, replacement in sorted_macros:
            escaped_cmd = re.escape(cmd)
            pattern = re.compile(r'(?<!\\)' + escaped_cmd + r'(?![a-zA-Z])')
            new_result = pattern.sub(lambda m: replacement, new_result)

        if new_result == result:
            break
        result = new_result

    return result


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
                if text[j:j+2] == r'\\]':
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


def convert_include_graphics(text: str) -> str:
    r"""Convert \includegraphics{path} and \includegraphics[options]{path} to Markdown image syntax."""
    # Process all instances with while loops to handle multiple

    while True:
        # First try with options: \includegraphics[options]{path}
        match = re.search(r'\\includegraphics\s*\[([^\]]*)\]\s*\{([^}]*)\}', text)
        if match:
            path = match.group(2)
            text = text[:match.start()] + f'![image]({path})' + text[match.end():]
            continue

        # Then try without options: \includegraphics{path}
        match = re.search(r'\\includegraphics\s*\{([^}]*)\}', text)
        if match:
            path = match.group(1)
            text = text[:match.start()] + f'![image]({path})' + text[match.end():]
            continue

        break

    # Check for malformed includegraphics (any \includegraphics not followed by {path})
    # Look for \includegraphics at the end of string or without braces
    pos = 0
    while pos < len(text):
        if text[pos:pos+13] == '\\includegraphics':
            check_pos = pos + 13
            # Skip whitespace
            while check_pos < len(text) and text[check_pos] in ' \t':
                check_pos += 1
            # Must be followed by { after optional [...]
            if check_pos < len(text) and text[check_pos] == '[':
                # Find closing ]
                close_bracket = text.find(']', check_pos)
                if close_bracket == -1:
                    print("Error: malformed includegraphics command", file=sys.stderr)
                    sys.exit(1)
                check_pos = close_bracket + 1
            # Should now have {
            if check_pos >= len(text) or text[check_pos] != '{':
                print("Error: malformed includegraphics command", file=sys.stderr)
                sys.exit(1)
            # Skip past {} content
            brace_count = 1
            i = check_pos + 1
            while i < len(text) and brace_count > 0:
                if text[i] == '{':
                    brace_count += 1
                elif text[i] == '}':
                    brace_count -= 1
                i += 1
            if brace_count != 0:
                print("Error: malformed includegraphics command", file=sys.stderr)
                sys.exit(1)
            pos = i
        else:
            pos += 1

    return text


def unwrap_environments(text: str) -> str:
    r"""Unwrap multicols, minipage, and parbox environments."""
    # Use while loops for all unwrapping to handle multiple instances

    # Unwrap \begin{multicols}{N}...
    while True:
        match = re.search(r'\\begin\{multicols\}[^}]*\}', text)
        if not match:
            break
        start = match.start()
        content_start = match.end()
        end_match = re.search(r'\\end\{multicols\}', text[content_start:])
        if not end_match:
            print("Error: malformed multicols environment", file=sys.stderr)
            sys.exit(1)
        content = text[content_start:content_start + end_match.start()]
        text = text[:start] + content + text[content_start + end_match.end():]

    # Unwrap \begin{minipage}{width}...
    while True:
        match = re.search(r'\\begin\{minipage\}[^}]*\}', text)
        if not match:
            break
        start = match.start()
        content_start = match.end()
        end_match = re.search(r'\\end\{minipage\}', text[content_start:])
        if not end_match:
            print("Error: malformed minipage environment", file=sys.stderr)
            sys.exit(1)
        content = text[content_start:content_start + end_match.start()]
        text = text[:start] + content + text[content_start + end_match.end():]

    # Handle \parbox{width}{content}
    while True:
        match = re.search(r'\\parbox\s*\{([^}]*)\}\s*\{([^}]*)\}', text)
        if not match:
            break
        content = match.group(2)
        text = text[:match.start()] + content + text[match.end():]

    # Check for malformed parbox (any \parbox not properly terminated)
    # Look for \parbox commands that don't have the correct form \parbox{...}{...}
    pos = 0
    while pos < len(text):
        if text[pos:pos+7] == '\\parbox':
            # Try to parse the next part
            check_pos = pos + 7  # After \parbox
            # Skip whitespace
            while check_pos < len(text) and text[check_pos] in ' \t':
                check_pos += 1
            # Must have {
            if check_pos >= len(text) or text[check_pos] != '{':
                print("Error: malformed parbox command", file=sys.stderr)
                sys.exit(1)
            brace_count = 1
            i = check_pos + 1
            while i < len(text) and brace_count > 0:
                if text[i] == '{':
                    brace_count += 1
                elif text[i] == '}':
                    brace_count -= 1
                i += 1
            if brace_count != 0:
                print("Error: malformed parbox command", file=sys.stderr)
                sys.exit(1)
            # Must have {
            if i >= len(text) or text[i] != '{':
                print("Error: malformed parbox command", file=sys.stderr)
                sys.exit(1)
            brace_count = 1
            i += 1
            while i < len(text) and brace_count > 0:
                if text[i] == '{':
                    brace_count += 1
                elif text[i] == '}':
                    brace_count -= 1
                i += 1
            if brace_count != 0:
                print("Error: malformed parbox command", file=sys.stderr)
                sys.exit(1)
            pos = i
        else:
            pos += 1

    return text


def convert_np_macro(text: str) -> str:
    r"""Convert \np{...} formatting macro by removing the wrapper."""
    for match in re.finditer(r'\\np\s*\{([^}]*)\}', text):
        content = match.group(1)
        text = text[:match.start()] + content + text[match.end():]
        break  # Restart after modification

    return text


def convert_sections_and_formatting(text: str) -> str:
    r"""Convert section commands and inline formatting."""
    text = re.sub(r'\\section\{([^}]*)\}', r'## \1', text)
    text = re.sub(r'\\subsection\{([^}]*)\}', r'### \1', text)
    text = re.sub(r'\\subsubsection\{([^}]*)\}', r'#### \1', text)
    text = re.sub(r'\\emph\{([^}]*)\}', r'_\1_', text)
    text = re.sub(r'\\textbf\{([^}]*)\}', r'**\1**', text)

    return text


def remove_commands(text: str) -> str:
    """Remove specific LaTeX commands."""
    text = re.sub(r'\\vspace\{[^}]*\}|\\medskip\b|\\smallskip\b|\\bigskip\b', '', text)
    return text


def normalize_blank_lines(text: str) -> str:
    """Collapse 3+ consecutive newlines to exactly 2 newlines."""
    return re.sub(r'\n{3,}', '\n\n', text)


def process_latex_to_markdown(text: str) -> str:
    """Process LaTeX text and convert to Markdown."""
    text = remove_comments(text)

    # Extract macros from preamble before extracting body
    macros = extract_macros(text)

    text = extract_body(text)
    text = expand_macros(text, macros)
    text = strip_lines(text)
    text = convert_sections_and_formatting(text)
    text = convert_display_math(text)
    text = convert_include_graphics(text)
    text = unwrap_environments(text)
    text = convert_np_macro(text)
    text = remove_commands(text)
    text = normalize_blank_lines(text)
    return text


def main():
    """Main entry point."""
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
