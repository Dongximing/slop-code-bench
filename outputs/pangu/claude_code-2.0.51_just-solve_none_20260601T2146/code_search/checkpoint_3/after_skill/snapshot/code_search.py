#!/usr/bin/env python3
"""Command-line code searcher with structure-aware pattern matching."""

import argparse
import dataclasses
import json
import os
import re
import sys

import tree_sitter


@dataclasses.dataclass
class Rule:
    id: str
    kind: str
    pattern: str
    languages: list[str] = dataclasses.field(default_factory=list)
    regex_flags: list[str] = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class Match:
    rule_id: str
    file: str
    language: str
    line: int
    col: int
    end_line: int
    end_col: int
    text: str
    captures: dict[str, dict] = dataclasses.field(default_factory=dict)

    def to_dict(self) -> dict:
        result = {
            "rule_id": self.rule_id,
            "file": self.file,
            "language": self.language,
            "start": {"line": self.line, "col": self.col},
            "end": {"line": self.end_line, "col": self.end_col},
            "match": self.text
        }
        if self.captures:
            result["captures"] = dict(sorted(self.captures.items()))
        return result


def load_rules(path: str) -> list[Rule]:
    with open(path, 'r', encoding='utf-8') as f:
        data = json.load(f)

    if not isinstance(data, list):
        raise ValueError("Rules file must be a JSON array")

    rules = []
    seen_ids = set()

    for item in data:
        if not isinstance(item, dict):
            raise ValueError("Each rule must be a JSON object")

        rule_id = item.get("id")
        if not rule_id or not isinstance(rule_id, str):
            raise ValueError("Each rule must have a non-empty string 'id'")

        if rule_id in seen_ids:
            raise ValueError(f"Duplicate rule id: {rule_id}")
        seen_ids.add(rule_id)

        kind = item.get("kind")
        if kind not in ("exact", "regex", "pattern"):
            raise ValueError(f"Rule '{rule_id}': kind must be 'exact', 'regex', or 'pattern'")

        pattern = item.get("pattern")
        if not pattern or not isinstance(pattern, str):
            raise ValueError(f"Rule '{rule_id}': pattern must be a non-empty string")

        languages = item.get("languages")
        if languages is not None:
            if not isinstance(languages, list):
                raise ValueError(f"Rule '{rule_id}': languages must be an array")
            for lang in languages:
                if lang not in ("python", "javascript", "cpp"):
                    raise ValueError(f"Rule '{rule_id}': languages may only contain python, javascript, cpp")

        regex_flags = item.get("regex_flags", [])
        if not isinstance(regex_flags, list):
            raise ValueError(f"Rule '{rule_id}': regex_flags must be an array")

        flag_map = {"i": re.IGNORECASE, "m": re.MULTILINE, "s": re.DOTALL}
        for flag in regex_flags:
            if flag not in flag_map:
                raise ValueError(f"Rule '{rule_id}': invalid regex flag '{flag}'")

        if kind == "regex":
            flag_val = 0
            for flag in regex_flags:
                flag_val |= flag_map[flag]
            try:
                re.compile(pattern, flag_val)
            except re.error as e:
                raise ValueError(f"Rule '{rule_id}': invalid regex pattern: {e}")

        rules.append(Rule(
            id=rule_id,
            kind=kind,
            pattern=pattern,
            languages=languages,
            regex_flags=regex_flags
        ))

    return rules


# Global language parsers (lazy loaded)
_language_parsers: dict[str, tree_sitter.Parser] = {}

LANG_MODULES = {
    "python": "tree_sitter_python",
    "javascript": "tree_sitter_javascript",
    "cpp": "tree_sitter_cpp"
}


def get_parser(language: str) -> tree_sitter.Parser:
    if language not in _language_parsers:
        parser = tree_sitter.Parser()
        module = __import__(LANG_MODULES[language])
        parser.language = module.language
        _language_parsers[language] = parser
    return _language_parsers[language]


# Metavariable pattern: $NAME or $NAME?
METAVAR_RE = re.compile(r'(?<!\\)\$([A-Z_a-z][A-Z_a-z0-9]*)(\?)?')


class PatternMatcher:
    """Performs structure-aware pattern matching using tree-sitter."""

    def __init__(self, language: str):
        self.language = language
        self._parser = get_parser(language)

    def match(self, rule: Rule, code: str, filename: str) -> list[Match]:
        if rule.kind != "pattern":
            return []

        pattern = rule.pattern
        if '(' in pattern and pattern.endswith(')'):
            return self._match_function_call_pattern(rule, pattern, code, filename)
        return self._match_text_pattern(rule, pattern, code, filename)

    def _match_function_call_pattern(self, rule: Rule, pattern: str, code: str,
                                      filename: str) -> list[Match]:
        matches = []
        tree = self._parser.parse(bytes(code, "utf-8"))

        def walk_node(node: tree_sitter.Node):
            if node.type in ("call_expression", "call"):
                match = self._match_call_against_pattern(node, pattern, code)
                if match:
                    matches.append(match)
            for child in node.children:
                walk_node(child)

        walk_node(tree.root_node)
        matches.sort(key=lambda m: (m.line, m.col, m.end_line, m.end_col))
        return matches

    def _match_call_against_pattern(self, call_node: tree_sitter.Node, pattern: str,
                                     code: str) -> Match | None:
        if '(' not in pattern or not pattern.endswith(')'):
            return None

        paren_idx = pattern.index('(')
        func_pattern = pattern[:paren_idx].strip()
        args_pattern_str = pattern[paren_idx+1:-1]

        func_node = call_node.child_by_field_name("function")
        if func_node is None and call_node.children:
            func_node = call_node.children[0]
        if func_node is None:
            return None

        func_text = code[func_node.start_byte:func_node.end_byte]
        if func_pattern.startswith('$'):
            pass  # Metavariable matches any function name
        elif func_text != func_pattern:
            return None

        args_node = None
        for child in call_node.children:
            if child.type in ("argument_list", "arguments"):
                args_node = child
                break

        if args_node is None:
            return self._create_match(call_node, pattern, code, {}) if not args_pattern_str.strip() else None

        actual_args = [c for c in args_node.children
                       if c.type not in ("(", ")", ",", "argument_list", "arguments")]

        arg_pattern_names, arg_pattern_parts = self._parse_arg_pattern(args_pattern_str)
        if len(actual_args) != len(arg_pattern_names):
            return None

        captures = {}
        arg_mappings = {}

        for i, name in enumerate(arg_pattern_names):
            if i >= len(actual_args):
                return None

            arg_node = actual_args[i]
            arg_text = code[arg_node.start_byte:arg_node.end_byte]

            if name:
                if name not in captures:
                    captures[name] = {"text": arg_text, "ranges": []}
                captures[name]["ranges"].append({
                    "start": {"line": arg_node.start_point[0] + 1, "col": arg_node.start_point[1] + 1},
                    "end": {"line": arg_node.end_point[0] + 1, "col": arg_node.end_point[1]}
                })
                arg_mappings.setdefault(name, []).append(arg_text)

        for name, texts in arg_mappings.items():
            if len(set(texts)) > 1:
                return None

        return self._create_match(call_node, pattern, code, captures)

    def _parse_arg_pattern(self, args_str: str) -> tuple[list[str], list[str]]:
        args_str = args_str.strip()
        if not args_str:
            return [], []

        parts = self._split_by_comma(args_str)
        names = []
        for part in parts:
            part = part.strip()
            if part.startswith(('"', "'", '`')) and part.endswith(('"', "'", '`')):
                names.append('')
            elif part.startswith('$'):
                name = part[1:].rstrip('?')
                names.append(name)
            else:
                names.append('')

        return names, parts

    def _split_by_comma(self, s: str) -> list[str]:
        parts = []
        current = ""
        depth = 0

        for char in s:
            if char in '([':
                depth += 1
                current += char
            elif char in ')]':
                depth -= 1
                current += char
            elif char == ',' and depth == 0:
                parts.append(current.strip())
                current = ""
            else:
                current += char

        if current:
            parts.append(current.strip())
        return parts

    def _create_match(self, node: tree_sitter.Node, text: str, code: str,
                      captures: dict) -> Match:
        formatted_captures = {f"${k}": v for k, v in captures.items()}
        return Match(
            rule_id="",
            file="",
            language=self.language,
            line=node.start_point[0] + 1,
            col=node.start_point[1] + 1,
            end_line=node.end_point[0] + 1,
            end_col=node.end_point[1],
            text=code[node.start_byte:node.end_byte],
            captures=formatted_captures
        )

    def _match_text_pattern(self, rule: Rule, pattern: str, code: str,
                            filename: str) -> list[Match]:
        matches = []
        metavars = list(METAVAR_RE.finditer(pattern))

        if not metavars or '(' in pattern and pattern.endswith(')'):
            return matches

        tree = self._parser.parse(bytes(code, "utf-8"))
        start = 0

        while True:
            pos = self._find_next_match(code, pattern, metavars, start, tree)
            if pos == -1:
                break

            end = pos + len(pattern)
            if not self._is_in_code(code, pos, end, tree):
                start = pos + 1
                continue

            captures = self._extract_captures(pattern, code, pos, metavars)
            if not captures:
                start = pos + 1
                continue

            before = code[:pos]
            after = code[:end]
            matches.append(Match(
                rule_id=rule.id,
                file=filename,
                language=self.language,
                line=before.count('\n') + 1,
                col=pos - before.rfind('\n'),
                end_line=after.count('\n') + 1,
                end_col=end - after.rfind('\n'),
                text=code[pos:end],
                captures=captures
            ))
            start = pos + 1

        return matches

    def _find_next_match(self, code: str, pattern: str,
                         metavars: list[re.Match], start: int,
                         tree: tree_sitter.Tree) -> int:
        if not metavars:
            return code.find(pattern, start)

        pos = 0
        while pos < len(pattern):
            remaining = pattern[pos:]
            m = METAVAR_RE.search(remaining)

            if m is None:
                idx = code.find(remaining, start)
                if idx == -1:
                    return -1
                if pos == 0:
                    return idx
                prev = pattern[:pos]
                check = code.find(prev, start)
                if check == -1 or check + len(prev) > idx:
                    return -1
                return check

            if m.start() > 0:
                literal = remaining[:m.start()]
                idx = code.find(literal, start)
                if idx == -1:
                    return -1
                pos = m.end()
                start = idx + len(literal)
            else:
                pos = m.end()

        return -1

    def _is_in_code(self, code: str, start: int, end: int,
                    tree: tree_sitter.Tree) -> bool:
        node = tree.root_node
        while True:
            found = False
            for child in node.children:
                if child.start_byte <= start < child.end_byte:
                    node = child
                    found = True
                    break
            if not found:
                break

        if node is None or node.type in ("comment", "string", "string_literal"):
            return False

        parent = node.parent
        while parent:
            if parent.type in ("comment", "string", "string_literal"):
                return False
            parent = parent.parent
        return True

    def _extract_captures(self, pattern: str, code: str,
                          match_start: int,
                          metavars: list[re.Match]) -> dict:
        captures = {}

        for m in metavars:
            metavar_name = m.group(1)
            key = f"${metavar_name}"
            if key in captures:
                continue

            code_start = match_start + m.start() + 1
            code_end = match_start + m.end()
            if m.group(2) == '?':
                code_end -= 1

            text = code[code_start:code_end]
            ranges = []

            for idx, mm in enumerate(metavars):
                if mm.group(1) == metavar_name:
                    r_start = match_start + mm.start() + 1
                    r_end = match_start + mm.end()
                    if mm.group(2) == '?':
                        r_end -= 1

                    line = code[:r_start].count('\n')
                    col = r_start - code[:r_start].rfind('\n') - 1
                    end_line = code[:r_end].count('\n')
                    end_col = r_end - code[:r_end].rfind('\n') - 1

                    ranges.append({
                        "start": {"line": line + 1, "col": col},
                        "end": {"line": end_line + 1, "col": end_col}
                    })

            captures[key] = {"text": text, "ranges": ranges}

        return captures


class CodeSearcher:
    EXTENSION_MAP = {
        ".py": "python",
        ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript",
        ".cc": "cpp", ".cpp": "cpp", ".cxx": "cpp",
        ".hh": "cpp", ".hpp": "cpp", ".hxx": "cpp"
    }

    def __init__(self, root_dir: str, rules: list[Rule], encoding: str = "utf-8"):
        self.root_dir = root_dir
        self.rules = rules
        self.encoding = encoding
        self._parsers: dict[str, PatternMatcher] = {}

    def _get_parser(self, language: str) -> PatternMatcher:
        if language not in self._parsers:
            self._parsers[language] = PatternMatcher(language)
        return self._parsers[language]

    def search(self) -> list[Match]:
        matches = []
        FLAG_MAP = {"i": re.IGNORECASE, "m": re.MULTILINE, "s": re.DOTALL}

        for dirpath, _dirnames, filenames in os.walk(self.root_dir):
            for filename in filenames:
                ext = os.path.splitext(filename)[1]
                language = self.EXTENSION_MAP.get(ext)
                if language is None:
                    continue

                filepath = os.path.join(dirpath, filename)
                rel_path = os.path.relpath(filepath, self.root_dir).replace("\\", "/")

                try:
                    with open(filepath, 'r', encoding=self.encoding) as f:
                        content = f.read()
                except UnicodeDecodeError:
                    continue

                for rule in self.rules:
                    if rule.languages and language not in rule.languages:
                        continue

                    if rule.kind == "regex":
                        flag_val = 0
                        for flag in rule.regex_flags:
                            flag_val |= FLAG_MAP[flag]
                        pattern = re.compile(rule.pattern, flag_val)
                        for m in pattern.finditer(content):
                            start_idx = m.start()
                            end_idx = m.end()
                            before_start = content[:start_idx]
                            before_end = content[:end_idx]
                            matches.append(Match(
                                rule_id=rule.id,
                                file=rel_path,
                                language=language,
                                line=before_start.count('\n') + 1,
                                col=start_idx - before_start.rfind('\n'),
                                end_line=before_end.count('\n') + 1,
                                end_col=end_idx - before_end.rfind('\n'),
                                text=m.group()
                            ))
                    elif rule.kind == "pattern":
                        try:
                            parser = self._get_parser(language)
                            matches.extend(parser.match(rule, content, rel_path))
                        except Exception as e:
                            print(f"Warning: Pattern matching failed for {rel_path}: {e}", file=sys.stderr)
                    else:
                        pattern = rule.pattern
                        start = 0
                        while True:
                            pos = content.find(pattern, start)
                            if pos == -1:
                                break
                            before = content[:pos]
                            matches.append(Match(
                                rule_id=rule.id,
                                file=rel_path,
                                language=language,
                                line=before.count('\n') + 1,
                                col=pos - before.rfind('\n'),
                                end_line=before.count('\n') + 1,
                                end_col=pos + len(pattern) - before.rfind('\n'),
                                text=pattern
                            ))
                            start = pos + 1

        matches.sort(key=lambda m: (m.file, m.line, m.col, m.end_line, m.end_col, m.rule_id))
        return matches


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Code searcher with structure-aware pattern matching",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Examples:\n  python code_search.py repo --rules rules.json\n  python code_search.py repo --rules rules.json --encoding latin-1"
    )
    parser.add_argument("root_dir", help="Path to the codebase to scan")
    parser.add_argument("--rules", required=True, help="Path to JSON rules file")
    parser.add_argument("--encoding", default="utf-8", help="File encoding (default: utf-8)")
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    if not os.path.isdir(args.root_dir):
        print(f"Error: Directory not found: {args.root_dir}", file=sys.stderr)
        sys.exit(1)

    if not os.path.isfile(args.rules):
        print(f"Error: File not found: {args.rules}", file=sys.stderr)
        sys.exit(1)

    try:
        rules = load_rules(args.rules)
    except (json.JSONDecodeError, ValueError) as e:
        print(f"Error loading rules: {e}", file=sys.stderr)
        sys.exit(1)

    searcher = CodeSearcher(args.root_dir, rules, args.encoding)
    for match in searcher.search():
        print(json.dumps(match.to_dict()))


if __name__ == "__main__":
    main()
