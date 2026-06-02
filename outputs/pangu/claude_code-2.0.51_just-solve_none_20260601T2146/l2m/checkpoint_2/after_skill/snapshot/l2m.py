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


def extract_body(text: str) -> str:
    m = re.search(r'\\begin\{document\}', text)
    n = re.search(r'\\end\{document\}', text)
    if m and n:
        return text[m.end():n.start()]
    return text


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
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
    else:
        out_path = Path(args.input_file).parent / f"{Path(args.input_file).stem}.md"

    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(output_text)


if __name__ == '__main__':
    main()
