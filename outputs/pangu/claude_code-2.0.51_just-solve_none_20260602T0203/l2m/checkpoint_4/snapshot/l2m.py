#!/usr/bin/env python3

import re
import sys
from pathlib import Path
from typing import Tuple, List


def remove_comments(text: str) -> str:
    """Remove LaTeX comments, treating \% as literal."""
    text = text.replace('\\%', '\x00percent\x00')
    lines = text.split('\n')
    cleaned_lines = []
    for line in lines:
        match = re.search(r'(?<!\\)%', line)
        if match:
            line = line[:match.start()]
        cleaned_lines.append(line)
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


def extract_preamble_macros(text: str) -> dict:
    """Extract parameter-free macro definitions from the preamble (text before \begin{document}).
    Only parameter-free definitions are eligible.
    Returns a dictionary of macro_name -> replacement text.
    If \begin{document} is absent, returns empty dict.
    """
    begin_match = re.search(r'\\begin\{document\}', text)

    if not begin_match:
        return {}

    preamble = text[:begin_match.start()]

    macros = {}

    # Match \def\CMD{replacement} - parameter-free
    for match in re.finditer(r'\\def\s*\\([a-zA-Z]+)\s*{([^}]*)}', preamble):
        name = match.group(1)  # Preserve original case
        replacement = match.group(2)
        macros[name] = replacement

    # Match \newcommand{\CMD}{replacement} - parameter-free
    for match in re.finditer(r'\\newcommand\s*\{\\([a-zA-Z]+)\}\s*{([^}]*)}', preamble):
        name = match.group(1)  # Preserve original case
        replacement = match.group(2)
        macros[name] = replacement

    return macros


def expand_macros(text: str, macros: dict) -> str:
    """Expand macro definitions in text.
    Apply macro substitutions only in body text.
    For eligible acyclic macro chains, keep expanding until body text stops changing.
    If eligible parameter-free macro definitions form a reference cycle, exit with status 1.
    Macro matching must be exact by command name; shared prefixes must not cause partial matches.
    """
    if not macros:
        return text

    # Detect cycles using depth-first search on macro references
    def find_cycles():
        visited = set()
        rec_stack = set()

        def dfs(macro_name):
            if macro_name in rec_stack:
                return True  # Cycle detected
            if macro_name in visited:
                return False  # Already processed, no cycle
            visited.add(macro_name)
            rec_stack.add(macro_name)

            # Get the replacement for this macro
            replacement = macros.get(macro_name, '')
            # Find any macro names referenced in the replacement
            for match in re.finditer(r'\\([a-zA-Z]+)\b', replacement):
                ref_name = match.group(1).lower()
                if ref_name in macros:
                    if dfs(ref_name):
                        return True

            rec_stack.remove(macro_name)
            return False

        for macro_name in macros:
            if macro_name not in visited:
                if dfs(macro_name):
                    return True
        return False

    if find_cycles():
        sys.stderr.write("Error: cyclic macro definition")
        sys.exit(1)

    # Expand macros by repeatedly applying substitutions until no change
    prev_text = None
    while prev_text != text:
        prev_text = text
        # Process macros in a deterministic order (sorted by name)
        # This ensures consistent expansion
        for name, replacement in sorted(macros.items()):
            # Match the exact command name with word boundary to avoid partial matches
            # Use lookahead/lookbehind to ensure it's a complete command
            pattern = re.compile(r'(?<![a-zA-Z0-9\\])\\' + re.escape(name) + r'(?![a-zA-Z])')
            # Use a function as replacement to handle backslashes in replacement text
            text = pattern.sub(lambda m: replacement, text)

    return text


def convert_includegraphics(text: str) -> str:
    """Convert \\includegraphics{path} and \\includegraphics[options]{path} to Markdown image syntax.
    Discard optional bracketed options.
    Use literal alt text 'image'.
    If encountered without a well-formed required {path} argument, exit with status 1.
    """
    # Match both forms: \includegraphics{path} and \includegraphics[options]{path}
    # Process in order to avoid double-processing

    def replace_match(match):
        # There may be optional [options] before the required {path}
        path_match = re.search(r'\{([^}]+)\}', match.group(0))
        if not path_match:
            sys.stderr.write("Error: malformed includegraphics command")
            sys.exit(1)
        path = path_match.group(1)
        return '![image](' + path + ')'

    # Match \includegraphics optionally with [...] and then with {path}
    text = re.sub(r'\\includegraphics(?:\[[^\]]*\])?\{[^}]*\}', replace_match, text)
    return text


def find_and_unwrap_environment_blocks(text: str, env_name: str, env_pattern: str = None) -> str:
    """Find and unwrap specific environment blocks while preserving content.
    Handles nested environments properly using stack-based approach.
    """
    if env_pattern is None:
        env_pattern = env_name

    result = []
    last_end_pos = 0

    # Compile patterns for begin and end
    begin_re = re.compile(r'\\begin\{' + re.escape(env_pattern) + r'\}(?:\[.*?\])?\s*', re.IGNORECASE)
    end_re = re.compile(r'\\end\{' + re.escape(env_pattern) + r'\}', re.IGNORECASE)

    i = 0
    while i < len(text):
        match = begin_re.search(text[i:])
        if not match:
            break

        block_start = i + match.start()
        env_start = i + match.end()

        # Find matching end with proper nesting
        depth = 1
        j = env_start
        while depth > 0 and j < len(text):
            next_begin = begin_re.search(text[j:])
            next_end = end_re.search(text[j:])

            if not next_begin and not next_end:
                break

            if next_end and (not next_begin or next_end.start() < next_begin.start()):
                depth -= 1
                if depth == 0:
                    # Found the matching end
                    content = text[env_start:j + next_end.start()]
                    result.append(text[last_end_pos:block_start])
                    result.append(content)
                    last_end_pos = j + next_end.end()
                    break
                j += next_end.end()
            elif next_begin:
                depth += 1
                j += next_begin.end()

        i = j + 1

    result.append(text[last_end_pos:])
    return ''.join(result)


def unwrap_environments(text: str) -> str:
    """Unwrap layout containers (multicols, minipage, parbox) while preserving enclosed content.
    """
    # Unwrap multicols environment: \begin{multicols}{N}...\end{multicols}
    # Need to skip over the optional {N} argument in the begin tag
    result = []
    last_end_pos = 0

    i = 0
    while i < len(text):
        # Match \begin{multicols} with optional {N}
        match = re.search(r'\\begin\{multicols\}(?:\{[^}]*\})?\s*', text[i:], re.IGNORECASE)
        if not match:
            break

        block_start = i + match.start()
        env_start = i + match.end()

        # Find matching end
        end_re = re.compile(r'\\end\{multicols\}', re.IGNORECASE)
        depth = 1
        j = env_start
        while depth > 0 and j < len(text):
            next_begin = re.search(r'\\begin\{multicols\}(?:\{[^}]*\})?\s*', text[j:], re.IGNORECASE)
            next_end = end_re.search(text[j:])

            if not next_begin and not next_end:
                break

            if next_end and (not next_begin or next_end.start() < next_begin.start()):
                depth -= 1
                if depth == 0:
                    content = text[env_start:j + next_end.start()]
                    result.append(text[last_end_pos:block_start])
                    result.append(content)
                    last_end_pos = j + next_end.end()
                    break
                j += next_end.end()
            elif next_begin:
                depth += 1
                j += next_begin.end()

        i = j + 1

    result.append(text[last_end_pos:])
    text = ''.join(result)

    # Unwrap minipage environment: \begin{minipage}{width}...\end{minipage}
    result = []
    last_end_pos = 0

    i = 0
    while i < len(text):
        match = re.search(r'\\begin\{minipage\}\{[^}]*\}\s*', text[i:], re.IGNORECASE)
        if not match:
            break

        block_start = i + match.start()
        env_start = i + match.end()

        end_re = re.compile(r'\\end\{minipage\}', re.IGNORECASE)
        depth = 1
        j = env_start
        while depth > 0 and j < len(text):
            next_begin = re.search(r'\\begin\{minipage\}\{[^}]*\}\s*', text[j:], re.IGNORECASE)
            next_end = end_re.search(text[j:])

            if not next_begin and not next_end:
                break

            if next_end and (not next_begin or next_end.start() < next_begin.start()):
                depth -= 1
                if depth == 0:
                    content = text[env_start:j + next_end.start()]
                    result.append(text[last_end_pos:block_start])
                    result.append(content)
                    last_end_pos = j + next_end.end()
                    break
                j += next_end.end()
            elif next_begin:
                depth += 1
                j += next_begin.end()

        i = j + 1

    result.append(text[last_end_pos:])
    text = ''.join(result)

    # Handle \parbox{width}{content} - must have both required braced arguments
    text = re.sub(r'\\parbox\{[^}]*\}\{[^}]*\}', replace_parbox, text)

    return text


def replace_parbox(match):
    # Check if we have well-formed braces: \parbox{width}{content}
    full_match = match.group(0)
    # Count braces to make sure they're properly formed
    open_braces = [i for i, c in enumerate(full_match) if c == '{']
    close_braces = [i for i, c in enumerate(full_match) if c == '}']

    if len(open_braces) < 2 or len(close_braces) < 2:
        sys.stderr.write("Error: malformed parbox command")
        sys.exit(1)

    # Find the content between the two sets of braces
    first_brace = open_braces[0]
    second_brace = open_braces[1]
    first_close = close_braces[0]
    second_close = close_braces[1]

    # Second opening must come before first closing, and second closing after second opening
    if not (first_brace < second_brace < first_close < second_close):
        sys.stderr.write("Error: malformed parbox command")
        sys.exit(1)

    content = full_match[second_brace + 1:first_close]
    return content


def process_np_macro(text: str) -> str:
    """Process \\np{...} formatting macro - remove wrapper and keep only inner text.
    Applied in plain text and inside inline math.
    """
    # Process in plain text first
    def replace_np(match):
        return match.group(1)

    text = re.sub(r'\\np\{([^}]*)\}', replace_np, text)

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
    pattern = re.compile(r'\\\[(.*?)\\\]', re.DOTALL)

    for match in pattern.finditer(text):
        before_text = text[last_end:match.start()]
        if before_text:
            result.append(before_text.rstrip())
            result.append('')

        math_content = match.group(1)
        result.append('$$')
        result.append(math_content)
        result.append('$$')
        result.append('')
        last_end = match.end()

    remaining_text = text[last_end:]
    if remaining_text:
        result.append(remaining_text)

    output = ''.join(result).rstrip()
    return output


def convert_sections_and_formatting(text: str) -> str:
    """Convert section commands and inline formatting."""
    text = re.sub(r'\\section\{([^}]+)\}', r'## \1', text)
    text = re.sub(r'\\subsection\{([^}]+)\}', r'### \1', text)
    text = re.sub(r'\\subsubsection\{([^}]+)\}', r'#### \1', text)
    text = re.sub(r'\\emph\{([^}]+)\}', r'_\1_', text)
    text = re.sub(r'\\textbf\{([^}]+)\}', r'**\1**', text)
    return text


def delete_commands(text: str) -> str:
    """Remove \vspace{...}, \medskip, \smallskip, \bigskip commands."""
    text = re.sub(r'\\vspace\{[^}]*\}', '', text)
    text = re.sub(r'\\medskip', '', text)
    text = re.sub(r'\\smallskip', '', text)
    text = re.sub(r'\\bigskip', '', text)
    return text


def normalize_blank_lines(text: str) -> str:
    """Collapse runs of 3+ newlines to exactly 2 newlines."""
    return re.sub(r'\n{3,}', '\n\n', text)


def number_to_letter(n: int) -> str:
    """Convert a number (1-based) to a lowercase letter, wrapping from z to a."""
    n -= 1
    return chr(ord('a') + n % 26)


def find_env_blocks(text: str, env_name: str) -> List[Tuple[int, int, str]]:
    """Find all \begin{env_name}...\end{env_name} blocks in text.
    Returns list of (start_pos, end_pos, content) for each block.
    """
    blocks = []
    i = 0
    pattern_str = r'\\begin\{' + re.escape(env_name) + r'\}(?:\[.*?\])?\s*'
    env_begin = re.compile(pattern_str, re.IGNORECASE)
    env_end = re.compile(r'\\end\{' + re.escape(env_name) + r'\}', re.IGNORECASE)

    while i < len(text):
        match = env_begin.search(text[i:])
        if not match:
            break

        block_start = i + match.end()

        # Find matching \end
        depth = 1
        j = block_start
        while depth > 0 and j < len(text):
            next_begin = env_begin.search(text[j:])
            next_end = env_end.search(text[j:])

            if not next_begin and not next_end:
                break

            if next_end and (not next_begin or next_end.start() < next_begin.start()):
                depth -= 1
                if depth == 0:
                    content = text[block_start:j + next_end.start()]
                    blocks.append((i + match.start(), j + next_end.end(), content))
                    break
                j += next_end.end()
            elif next_begin:
                depth += 1
                j += next_begin.end()

        i = j + 1

    return blocks


def process_enumerate_block(content: str, start_num: int, depth: int, parent_num: int) -> List[str]:
    """Process an enumerate block and return markdown lines.
    If depth > 0, this is a nested enumerate within another enumerate.
    """
    lines = []

    if depth > 0:
        # Error: deeper than one level of nesting
        sys.stderr.write("Error: enumerate nesting deeper than one level")
        sys.exit(1)

    item_pattern = re.compile(r'^\\item(?:\[.*?\])?\s*', re.MULTILINE)

    # Find all item positions
    items = []
    for match in item_pattern.finditer(content):
        item_start = match.start()
        item_end = match.end()

        # Get content until next item or end of content
        next_match = item_pattern.search(content[item_end:])
        if next_match:
            item_text = content[item_end:item_end + next_match.start()].strip()
        else:
            item_text = content[item_end:].strip()

        items.append((item_start, item_text, item_end))

    # Now process each item, looking for nested enumerate after it
    current_num = start_num

    for idx, (item_start, item_text, item_end) in enumerate(items):
        # Determine the range of content after this item
        next_item_start = items[idx + 1][0] if idx + 1 < len(items) else len(content)

        # Look for nested enumerate between this item and the next
        search_region = content[item_end:next_item_start]
        nested_match = re.search(r'\\begin\{enumerate\}(?:\[.*?\])?\s*', search_region, re.IGNORECASE)

        if nested_match:
            # Found nested enumerate
            # Find its content and end
            nested_start_in_region = nested_match.end()
            # Find matching \end{enumerate} for this nested one
            depth_nested = 1
            search_pos = nested_start_in_region
            nested_end_in_region = None

            while depth_nested > 0 and search_pos < len(search_region):
                n_begin = re.search(r'\\begin\{enumerate\}(?:\[.*?\])?\s*', search_region[search_pos:], re.IGNORECASE)
                n_end = re.search(r'\\end\{enumerate\}', search_region[search_pos:], re.IGNORECASE)

                if n_end and (not n_begin or n_end.start() < n_begin.start()):
                    depth_nested -= 1
                    if depth_nested == 0:
                        nested_end_in_region = search_pos + n_end.start()
                        break
                    search_pos += n_end.end()
                elif n_begin:
                    depth_nested += 1
                    search_pos += n_begin.end()

            if nested_end_in_region is not None:
                nested_content = search_region[nested_start_in_region:nested_end_in_region]
                # Process nested enumerate with depth + 1
                for nested_line in process_enumerate_block(nested_content, 1, depth + 1, current_num):
                    lines.append(nested_line)
            else:
                # No proper end found, treat as regular item
                if depth == 0:
                    lines.append(f"**{current_num}.** {item_text}")
                else:
                    lines.append(f"**{parent_num}.{number_to_letter(current_num)})** {item_text}")
                current_num += 1
        else:
            # Regular item
            if depth == 0:
                lines.append(f"**{current_num}.** {item_text}")
            else:
                lines.append(f"**{parent_num}.{number_to_letter(current_num)})** {item_text}")
            current_num += 1

        # Add blank line between items
        if idx < len(items) - 1:
            lines.append('')

    return lines


def process_enumerate_blocks(text: str) -> str:
    """Replace all enumerate blocks with markdown."""
    result = []
    last_end_pos = 0

    blocks = find_env_blocks(text, 'enumerate')

    for block_start, block_end, content in blocks:
        # Add text before this block
        if block_start > last_end_pos:
            before_text = text[last_end_pos:block_start].rstrip()
            if before_text:
                result.append(before_text)
                result.append('')
        else:
            result.append('')

        # Process this enumerate block
        lines = process_enumerate_block(content, 1, 0, 0)
        result.extend(lines)
        result.append('')

        last_end_pos = block_end

    # Add any remaining text
    if last_end_pos < len(text):
        result.append(text[last_end_pos:])

    output = '\n'.join(result)
    return output.strip()


def process_itemize_items(content: str) -> List[str]:
    """Process itemize items and return markdown lines."""
    lines = []

    item_pattern = re.compile(r'^\\item(?:\[.*?\])?\s*', re.MULTILINE)
    items = []

    for match in item_pattern.finditer(content):
        item_end = match.end()
        next_match = item_pattern.search(content[item_end:])
        if next_match:
            item_text = content[item_end:item_end + next_match.start()].strip()
        else:
            item_text = content[item_end:].strip()
        items.append(item_text)

    for idx, item_text in enumerate(items):
        lines.append(f"-   {item_text}")
        if idx < len(items) - 1:
            lines.append('')

    return lines


def find_itemize_blocks(text: str) -> List[Tuple[int, int, str]]:
    """Find all itemize blocks in text and return (start_pos, end_pos, content)."""
    blocks = []
    i = 0

    while i < len(text):
        match = re.search(r'\\begin\{itemize\}(?:\[.*?\])?\s*', text[i:], re.IGNORECASE)
        if not match:
            break

        start_pos = i + match.end()
        block_start = i + match.start()

        depth = 1
        j = start_pos
        while depth > 0 and j < len(text):
            next_begin = re.search(r'\\begin\{itemize\}(?:\[.*?\])?\s*', text[j:], re.IGNORECASE)
            next_end = re.search(r'\\end\{itemize\}', text[j:], re.IGNORECASE)

            if not next_begin and not next_end:
                break

            if next_end and (not next_begin or next_end.start() < next_begin.start()):
                depth -= 1
                if depth == 0:
                    content = text[start_pos:j + next_end.start()]
                    blocks.append((block_start, j + next_end.end(), content))
                    break
                j += next_end.end()
            elif next_begin:
                depth += 1
                j += next_begin.end()

        i = j + 1

    return blocks


def process_itemize_blocks(text: str) -> str:
    """Replace all itemize blocks with markdown."""
    result = []
    last_end_pos = 0

    blocks = find_itemize_blocks(text)

    for block_start, block_end, content in blocks:
        if block_start > last_end_pos:
            before_text = text[last_end_pos:block_start].rstrip()
            if before_text:
                result.append(before_text)
                result.append('')

        lines = process_itemize_items(content)
        result.extend(lines)
        result.append('')

        last_end_pos = block_end

    if last_end_pos < len(text):
        result.append(text[last_end_pos:])

    output = '\n'.join(result)
    return output.strip()


def convert_latex_to_markdown(latex_text: str) -> str:
    """Convert LaTeX source to Markdown."""
    text = remove_comments(latex_text)

    # First extract body to get preamble
    full_body = extract_body(text)

    # Extract preamble macros from full text (before \begin{document})
    macros = extract_preamble_macros(text)

    # Get the actual body text - either extracted or full if no document tags
    text = full_body

    # Apply macro expansion first before other conversions
    text = expand_macros(text, macros)

    text = strip_lines(text)
    text = convert_display_math(text)
    text = convert_sections_and_formatting(text)
    text = delete_commands(text)

    # Process np macro in the content first
    text = process_np_macro(text)

    # Unwrap environment containers
    text = unwrap_environments(text)

    # Convert includegraphics commands
    text = convert_includegraphics(text)

    text = process_enumerate_blocks(text)
    text = process_itemize_blocks(text)
    text = normalize_blank_lines(text)
    return text.strip()


def main():
    args = sys.argv[1:]

    if not args or args[0] in ['-h', '--help']:
        print(f"Usage: python {Path(__file__).name} INPUT_FILE [-o OUTPUT_FILE]")
        sys.exit(1)

    input_file = args[0]
    output_file = None

    i = 1
    while i < len(args):
        if args[i] == '-o' and i + 1 < len(args):
            output_file = args[i + 1]
            i += 2
        else:
            i += 1

    try:
        with open(input_file, 'r', encoding='utf-8') as f:
            latex_content = f.read()
    except (FileNotFoundError, PermissionError, OSError):
        print(f"Error: cannot read '{input_file}'", file=sys.stderr)
        sys.exit(1)

    markdown_content = convert_latex_to_markdown(latex_content)

    if output_file is None:
        input_path = Path(input_file)
        output_file = input_path.with_suffix('.md')
    else:
        output_path = Path(output_file)
        output_path.parent.mkdir(parents=True, exist_ok=True)

    with open(output_file, 'w', encoding='utf-8') as f:
        f.write(markdown_content)

    sys.exit(0)


if __name__ == '__main__':
    main()
