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


def convert_lists(text: str) -> str:
    """Convert enumerate and itemize environments to Markdown."""

    # Helper to convert a number to lowercase letters (a, b, ..., z, then wraps to a, b, ...)
    def number_to_letters(n: int) -> str:
        """Convert 1-based number to lowercase letter(s), wrapping at 26."""
        return chr(ord('a') + (n - 1) % 26)

    # First check for nesting deeper than 1 level
    # We traverse the text tracking depth of list environments
    depth = 0
    max_depth = 0
    idx = 0
    while idx < len(text):
        begin_match = re.match(r'\\begin\{(enumerate|itemize)}', text[idx:])
        end_match = re.match(r'\\end\{(enumerate|itemize)}', text[idx:])
        if begin_match:
            depth += 1
            max_depth = max(max_depth, depth)
            idx += begin_match.end()
        elif end_match:
            depth -= 1
            idx += end_match.end()
        else:
            idx += 1

    if max_depth > 2:
        print("Error: enumerate nesting deeper than one level", file=sys.stderr)
        sys.exit(1)

    # Main conversion using a stack to track list contexts
    result = []
    i = 0

    while i < len(text):
        # Look for \\begin{enumerate} or \\begin{itemize}
        begin_match = re.match(r'\\begin\{(enumerate|itemize)}', text[i:])
        if not begin_match:
            result.append(text[i])
            i += 1
            continue

        list_type = begin_match.group(1)
        offset = begin_match.end()

        # For enumerate, check for [start=N] option
        start_val = 1
        if list_type == 'enumerate':
            opt_match = re.match(r'\[(.*?)\]', text[offset:])
            if opt_match:
                opt_text = opt_match.group(1).strip()
                if not re.match(r'^start=\d+$', opt_text):
                    print("Error: unsupported enumerate options", file=sys.stderr)
                    sys.exit(1)
                start_val = int(opt_text.split('=')[1])
                offset += opt_match.end()

        # Find matching \end
        end_pat = r'\\end\{' + re.escape(list_type) + r'}'
        end_match = re.search(end_pat, text[offset:])
        if not end_match:
            result.append(text[i:])
            break

        content = text[offset:offset + end_match.start()]
        end_pos = offset + end_match.end()

        # Extract \item commands and their text from content
        item_pat = re.compile(r'\\item(?:\[.*?\])?')
        items = []
        item_matches = list(item_pat.finditer(content))
        for idx, m in enumerate(item_matches):
            start = m.end()
            # Get content from after this \item to the next \item (or end)
            if idx + 1 < len(item_matches):
                item_text = content[start:item_matches[idx + 1].start()]
            else:
                item_text = content[start:]
            items.append(item_text.strip())

        # Now generate markdown based on nesting context
        # Determine nesting: count how many unclosed begin lists exist before this position
        # We'll scan backwards through the original text to find parent
        parent_type = None
        parent_has_itemize_child = False
        j = 0
        nesting_depth = 0
        while j < i:
            b_match = re.match(r'\\begin\{(enumerate|itemize)}', text[j:])
            e_match = re.match(r'\\end\{(enumerate|itemize)}', text[j:])
            if b_match:
                nesting_depth += 1
                if nesting_depth == 1:
                    parent_type = b_match.group(1)
                j += b_match.end()
            elif e_match:
                nesting_depth -= 1
                if nesting_depth == 0:
                    parent_type = None
                j += e_match.end()
            else:
                j += 1

        is_nested = nesting_depth > 0

        # Generate markdown
        if list_type == 'enumerate':
            if not is_nested:
                # Top-level enumerate
                md_items = []
                for idx, item_text in enumerate(items):
                    num = start_val + idx
                    md_items.append(f'**{num}.** {item_text}' if item_text else f'**{num}.**')
                md = '\n\n'.join(md_items)
            else:
                # Nested enumerate
                if parent_type == 'itemize':
                    # enumerate inside itemize: treat as top-level enumerate
                    md_items = []
                    for idx, item_text in enumerate(items):
                        num = start_val + idx
                        md_items.append(f'**{num}.** {item_text}' if item_text else f'**{num}.**')
                    md = '\n\n'.join(md_items)
                else:
                    # enumerate inside enumerate: need parent.x) format
                    # We need the parent's item count. Let's find it by counting
                    # the parent enumerate's items.

                    # First, find the parent enumerate block by scanning forward
                    # from the parent begin
                    # The parent is the most recent unclosed enumerate begin
                    temp_i = 0
                    parent_enum_start = 0
                    parent_enum_end = 0
                    nesting_depth = 0
                    found_parent = False
                    while temp_i < i:
                        b_match = re.match(r'\\begin\{(enumerate|itemize)}', text[temp_i:])
                        e_match = re.match(r'\\end\{(enumerate|itemize)}', text[temp_i:])
                        if b_match:
                            nesting_depth += 1
                            if nesting_depth == 1 and b_match.group(1) == 'enumerate':
                                parent_enum_start = temp_i + b_match.end()
                                # Skip [start=N]
                                if text[parent_enum_start] == '[':
                                    eq = text.find(']', parent_enum_start)
                                    if eq != -1:
                                        parent_enum_start = eq + 1
                                # Find parent end
                                e_match2 = re.search(r'\\end\{enumerate\}', text[parent_enum_start:])
                                if e_match2:
                                    parent_enum_end = parent_enum_start + e_match2.start()
                                    found_parent = True
                            temp_i += b_match.end()
                        elif e_match:
                            nesting_depth -= 1
                            temp_i += e_match.end()
                        else:
                            temp_i += 1

                    parent_item_count = 0
                    if found_parent:
                        # Count items in parent
                        parent_content = text[parent_enum_start:parent_enum_end]
                        parent_item_count = len(re.findall(r'\\item', parent_content))
                    else:
                        parent_item_count = 1  # Fallback

                    # Generate parent.x) items
                    md_items = []
                    for idx, item_text in enumerate(items):
                        letter = number_to_letters(start_val + idx)
                        md_items.append(f'**{parent_item_count}.{letter})** {item_text}' if item_text else f'**{parent_item_count}.{letter})**')
                    md = '\n\n'.join(md_items)
        else:
            # itemize
            md_items = []
            for idx, item_text in enumerate(items):
                md_items.append(f'-   {item_text}' if item_text else '-')
            md = '\n\n'.join(md_items)

        result.append(md)
        i = end_pos

    return ''.join(result)


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
