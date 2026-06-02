#!/usr/bin/env python3
"""LaTeX to KaTeX-compatible Markdown converter."""

import argparse
import re
import sys
from pathlib import Path


def number_to_letters(n: int) -> str:
    """Convert 1->a, 2->b, ..., 26->z, 27->aa, etc."""
    result = ""
    while n > 0:
        n -= 1
        result = chr(ord('a') + n % 26) + result
        n //= 26
    return result


def remove_comments(text: str) -> str:
    """Remove unescaped % and everything after it on each line."""
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
    return '\n'.join(lines).replace('\x01ESCAPED_PERCENT\x01', '%')


def extract_preamble_and_body(text: str) -> tuple[str, str]:
    """Extract preamble (before \\begin{document}) and body (after)."""
    m = re.search(r'\\begin\{document\}', text)
    n = re.search(r'\\end\{document\}', text)
    if m and n:
        return text[:m.start()], text[m.end():n.start()]
    return '', text


def extract_body(text: str) -> str:
    """Extract the body text (between \\begin{document} and \\end{document})."""
    _, body = extract_preamble_and_body(text)
    return body


def extract_macros(text: str) -> dict[str, str]:
    """Extract parameter-free macro definitions from preamble.
    Supports: \\def\\CMD{replacement} and \\newcommand{\\CMD}{replacement}
    """
    macros = {}

    # Pattern for \def\CMD{replacement}
    def_pattern = r'\\def\\(\w+)\{([^}]+)\}'

    # Pattern for \newcommand{\CMD}{replacement}
    newcommand_pattern = r'\\newcommand{\\(\w+)}\{([^}]+)\}'

    for match in re.finditer(def_pattern, text):
        cmd_name = match.group(1)
        replacement = match.group(2)
        macros[cmd_name] = replacement

    for match in re.finditer(newcommand_pattern, text):
        cmd_name = match.group(1)
        replacement = match.group(2)
        macros[cmd_name] = replacement

    return macros


def detect_macro_cycles(macros: dict[str, str]) -> bool:
    """Detect if there are cycles in macro definitions using DFS."""
    visited = set()
    recursion_stack = set()

    def dfs(cmd):
        if cmd in recursion_stack:
            return True
        if cmd in visited:
            return False

        visited.add(cmd)
        recursion_stack.add(cmd)

        # Check if replacement contains references to other macros
        replacement = macros.get(cmd, '')
        # Find all macro names referenced in replacement
        for match in re.finditer(r'\\(\w+)', replacement):
            referenced_cmd = match.group(1)
            if referenced_cmd in macros:
                if dfs(referenced_cmd):
                    return True

        recursion_stack.remove(cmd)
        return False

    for cmd in macros:
        if cmd not in visited:
            if dfs(cmd):
                return True
    return False


def expand_macros(text: str, macros: dict[str, str], max_iter: int = 100) -> str:
    """Expand macros in text until no more changes."""
    prev_text = text
    iteration = 0
    while iteration < max_iter:
        iteration += 1
        new_text = prev_text
        for cmd_name, replacement in macros.items():
            # Match exact command name with word boundary
            pattern = r'\\' + re.escape(cmd_name) + r'(?![a-zA-Z])'
            # Use a lambda to do literal replacement
            new_text = re.sub(pattern, lambda m: replacement, new_text)

        if new_text == prev_text:
            break
        prev_text = new_text

    return prev_text


def strip_whitespace(text: str) -> str:
    return '\n'.join(line.strip() for line in text.split('\n'))


def convert_display_math(text: str) -> str:
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
    text = re.sub(r'\\section\{([^}]+)\}', r'## \1', text)
    text = re.sub(r'\\subsection\{([^}]+)\}', r'### \1', text)
    text = re.sub(r'\\subsubsection\{([^}]+)\}', r'#### \1', text)
    text = re.sub(r'\\emph\{([^}]*)\}', r'_\1_', text)
    text = re.sub(r'\\textbf\{([^}]*)\}', r'**\1**', text)
    return text


def delete_commands(text: str) -> str:
    text = re.sub(r'\\vspace\{[^}]*\}', '', text)
    text = re.sub(r'\\medskip', '', text)
    text = re.sub(r'\\smallskip', '', text)
    text = re.sub(r'\\bigskip', '', text)
    return text


def normalize_blanks(text: str) -> str:
    return re.sub(r'\n{3,}', '\n\n', text)


def convert_includegraphics(text: str) -> str:
    """Convert \\includegraphics{path} and \\includegraphics[options]{path} to Markdown."""
    # Check for malformed commands (no path)
    malformed = re.search(r'\\includegraphics(?:\[[^\]]*\])?\{\s*\}', text)
    if malformed:
        print("Error: malformed includegraphics command", file=sys.stderr)
        sys.exit(1)

    # Pattern with optional brackets
    pattern = r'\\includegraphics(?:\[[^\]]*\])?\{([^}]+)\}'
    return re.sub(pattern, r'![image](\1)', text)


def unwrap_environments(text: str) -> str:
    """Unwrap multicols, minipage, and parbox environments."""
    # Unwrap multicols - handle {N} argument
    text = re.sub(r'\\begin\{multicols\}\{[^}]*\}', '', text)
    text = re.sub(r'\\end\{multicols\}', '', text)

    # Unwrap minipage - handle {width} argument
    text = re.sub(r'\\begin\{minipage\}\{[^}]*\}', '', text)
    text = re.sub(r'\\end\{minipage\}', '', text)

    return text


def convert_parbox(text: str) -> str:
    """Convert \\parbox{width}{content} by removing wrapper and keeping content."""
    pattern = r'\\parbox\{([^}]+)\}\{([^}]+)\}'

    def replace(match):
        width = match.group(1)
        content = match.group(2)
        # Check if match was successful (malformed)
        if content is None or width is None:
            print("Error: malformed parbox command", file=sys.stderr)
            sys.exit(1)
        return content

    return re.sub(pattern, replace, text)


def convert_np_macro(text: str) -> str:
    """Convert \\np{...} by removing wrapper and keeping inner text."""
    # Match inside inline math as well: $...\np{...}...$
    pattern = r'\\np\{([^}]+)\}'
    return re.sub(pattern, r'\1', text)


def parse_enumerate_options(line: str) -> tuple[int | None, bool]:
    """Return (start_number, has_error) from enumerate opening line."""
    m = re.match(r'\\begin\{enumerate\}(.*)$', line)
    if not m:
        return None, False
    opts = m.group(1).strip()
    if not opts:
        return 1, False
    sm = re.match(r'^\[(start=)(\d+)\]$', opts)
    if sm:
        return int(sm.group(2)), False
    return None, True


def process_items(content: str, list_type: str = 'enumerate', is_nested: bool = False,
                  start_num: int = 1, parent_num: int | None = None) -> tuple[list[str], int | None]:
    """Process itemize/enumerate content. Returns (lines, error_code)."""
    lines = []
    i = 0
    item_idx = 0

    while i < len(content):
        ms = re.search(r'\\item(?:\[.*?\])?', content[i:])
        if not ms:
            rem = content[i:].strip()
            if rem:
                lines.append(rem)
            break

        before = content[i:i + ms.start()].strip()
        if before:
            lines.append(before)
            i += ms.start()
            continue

        pos = i + ms.end()
        inner = content[pos - 1:]

        ns = re.search(r'\\begin\{(enumerate|itemize)\}', inner)
        ne = re.search(r'\\end\{(?:enumerate|itemize)\}', inner)
        has_nested = ns and (not ne or ns.start() < ne.start())

        if has_nested:
            ntype = ns.group(1)
            cstart = pos - 1 + ns.end()
            depth = 1
            j = cstart
            while j < len(content) and depth > 0:
                er = re.search(r'\\end\{(enumerate|itemize)\}', content[j:])
                sr = re.search(r'\\begin\{(enumerate|itemize)\}', content[j:])
                if not er:
                    break
                if sr and sr.start() < er.start():
                    depth += 1
                    j += sr.end()
                else:
                    depth -= 1
                    j += er.end()
            nested = content[cstart:j - (er.end() if er else 0)]

            if ntype == 'enumerate':
                if is_nested:
                    return [], 1
                item_idx += 1
                child, err = process_items(nested, True, start_num, item_idx)
                if err:
                    return [], err
                lines.extend(child)
            else:
                child, err = process_items(nested, 'itemize', False)
                if err:
                    return [], err
                lines.extend(child)

            i = j
            continue

        ni = re.search(r'\\item(?:\[.*?\])', inner)
        item_content = inner[ni.start():] if ni else inner
        item_content = item_content.strip()

        if list_type == 'itemize':
            lines.append(f'-   {item_content}')
        elif is_nested and parent_num is not None:
            lines.append(f'**{parent_num}.{number_to_letters(item_idx + 1)})** {item_content}')
        else:
            lines.append(f'**{start_num}.** {item_content}')
            start_num += 1

        item_idx += 1
        i += pos - 1 + (ni.start() if ni else len(item_content))

    return lines, None


def convert_latex_to_markdown(input_text: str) -> str:
    """Convert LaTeX source to Markdown."""
    # Step 1: Remove comments (escaped % must be handled first)
    text = remove_comments(input_text)

    # Step 2: Extract preamble and body
    preamble, body = extract_preamble_and_body(text)

    # Step 3: Extract and validate macros from preamble
    macros = extract_macros(preamble)
    if detect_macro_cycles(macros):
        print("Error: cyclic macro definition", file=sys.stderr)
        sys.exit(1)

    # Step 4: Apply environment unwrapping to body (before macro expansion)
    body = unwrap_environments(body)

    # Step 5: Convert parbox commands
    body = convert_parbox(body)

    # Step 6: Convert includegraphics
    body = convert_includegraphics(body)

    # Step 7: Convert np macro
    body = convert_np_macro(body)

    # Step 8: Expand macros in the body
    body = expand_macros(body, macros)

    # Step 9: Process body with remaining conversions
    body = strip_whitespace(body)
    body = convert_display_math(body)
    body = convert_sections_and_formatting(body)
    body = delete_commands(body)
    body = normalize_blanks(body)

    return body


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
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
    else:
        out_path = Path(args.input_file).parent / f"{Path(args.input_file).stem}.md"

    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(output_text)


if __name__ == '__main__':
    main()
