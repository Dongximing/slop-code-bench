#!/usr/bin/env python3
"""Command-line code searcher with structure-aware pattern matching."""

import argparse
import dataclasses
import json
import os
import re
import sys

import tree_sitter

FLAG_MAP = {"i": re.IGNORECASE, "m": re.MULTILINE, "s": re.DOTALL}


@dataclasses.dataclass
class Rule:
    id: str
    kind: str
    pattern: str
    languages: list[str] = dataclasses.field(default_factory=list)
    regex_flags: list[str] = dataclasses.field(default_factory=list)
    fix: dict | None = None


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
    start_byte: int = 0
    end_byte: int = 0

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

    @classmethod
    def create_selector_match(cls, rule_id: str, file: str, language: str,
                               node: tree_sitter.Node, code: str) -> "Match":
        return cls(
            rule_id=rule_id,
            file=file,
            language=language,
            line=node.start_point[0] + 1,
            col=node.start_point[1] + 1,
            end_line=node.end_point[0] + 1,
            end_col=node.end_point[1] + 1,
            text=code[node.start_byte:node.end_byte],
            start_byte=node.start_byte,
            end_byte=node.end_byte
        )


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
        if kind not in ("exact", "regex", "pattern", "selector"):
            raise ValueError(f"Rule '{rule_id}': kind must be 'exact', 'regex', 'pattern', or 'selector'")

        # selector rules use 'selector' field instead of 'pattern'
        if kind == "selector":
            selector = item.get("selector")
            if not selector or not isinstance(selector, str):
                raise ValueError(f"Rule '{rule_id}': selector must be a non-empty string")
            pattern = selector
        else:
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

        for flag in regex_flags:
            if flag not in FLAG_MAP:
                raise ValueError(f"Rule '{rule_id}': invalid regex flag '{flag}'")

        if kind == "regex":
            flag_val = 0
            for flag in regex_flags:
                flag_val |= FLAG_MAP[flag]
            try:
                re.compile(pattern, flag_val)
            except re.error as e:
                raise ValueError(f"Rule '{rule_id}': invalid regex pattern: {e}")

        fix = item.get("fix")
        if fix is not None:
            if not isinstance(fix, dict):
                raise ValueError(f"Rule '{rule_id}': fix must be an object")
            if fix.get("kind") != "replace":
                raise ValueError(f"Rule '{rule_id}': fix.kind must be 'replace'")
            template = fix.get("template")
            if not isinstance(template, str):
                raise ValueError(f"Rule '{rule_id}': fix.template must be a string")

        rules.append(Rule(
            id=rule_id,
            kind=kind,
            pattern=pattern,
            languages=languages,
            regex_flags=regex_flags,
            fix=fix
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


# Pattern to match: $NAME, $NAME?, $MATCH, $$
_EXPAND_RE = re.compile(r'\$(?:[A-Z_a-z][A-Z_a-z0-9]*\??|MATCH|\$)')


def expand_template(template: str, captures: dict[str, dict], full_text: str) -> str:
    def replace(match: re.Match) -> str:
        token = match.group(0)
        if token == '$$':
            return '$'
        if token == '$MATCH':
            return full_text
        return captures.get(token, {"text": token})["text"]

    return _EXPAND_RE.sub(replace, template)


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
            end_col=node.end_point[1] + 1,
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

    @staticmethod
    def _find_nodes_by_type(node: tree_sitter.Node, node_type: str) -> list[tree_sitter.Node]:
        found = []
        if node.type == node_type:
            found.append(node)
        for child in node.children:
            found.extend(CodeSearcher._find_nodes_by_type(child, node_type))
        return found

    def search(self) -> list[Match]:
        matches = []

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
                        self._match_regex(rule, content, rel_path, language, matches)
                    elif rule.kind == "selector":
                        self._match_selector(rule, content, rel_path, language, matches)
                    elif rule.kind == "pattern":
                        self._match_pattern(rule, content, rel_path, language, matches)
                    else:
                        self._match_text(rule, content, rel_path, language, matches)

        matches.sort(key=lambda m: (m.file, m.line, m.col, m.end_line, m.end_col, m.rule_id))
        return matches

    def _match_regex(self, rule: Rule, content: str, file: str,
                     language: str, matches: list[Match]) -> None:
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
                file=file,
                language=language,
                line=before_start.count('\n') + 1,
                col=start_idx - before_start.rfind('\n'),
                end_line=before_end.count('\n') + 1,
                end_col=end_idx - before_end.rfind('\n'),
                text=m.group()
            ))

    def _match_selector(self, rule: Rule, content: str, file: str,
                        language: str, matches: list[Match]) -> None:
        try:
            if language not in self._parsers:
                self._parsers[language] = PatternMatcher(language)
            parser = self._parsers[language]
            tree = parser._parser.parse(bytes(content, "utf-8"))
            match_nodes = list(self._find_nodes_by_type(tree.root_node, rule.pattern))
            for node in match_nodes:
                matches.append(Match.create_selector_match(
                    rule.id, file, language, node, content
                ))
        except Exception as e:
            print(f"Warning: Selector matching failed for {file}: {e}", file=sys.stderr)

    def _match_pattern(self, rule: Rule, content: str, file: str,
                       language: str, matches: list[Match]) -> None:
        try:
            if language not in self._parsers:
                self._parsers[language] = PatternMatcher(language)
            matches.extend(self._parsers[language].match(rule, content, file))
        except Exception as e:
            print(f"Warning: Pattern matching failed for {file}: {e}", file=sys.stderr)

    def _match_text(self, rule: Rule, content: str, file: str,
                    language: str, matches: list[Match]) -> None:
        start = 0
        while True:
            pos = content.find(rule.pattern, start)
            if pos == -1:
                break
            before = content[:pos]
            matches.append(Match(
                rule_id=rule.id,
                file=file,
                language=language,
                line=before.count('\n') + 1,
                col=pos - before.rfind('\n'),
                end_line=before.count('\n') + 1,
                end_col=pos + len(rule.pattern) - before.rfind('\n'),
                text=rule.pattern
            ))
            start = pos + 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Code searcher with structure-aware pattern matching",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Examples:\n  python code_search.py repo --rules rules.json\n  python code_search.py repo --rules rules.json --encoding latin-1"
    )
    parser.add_argument("root_dir", help="Path to the codebase to scan")
    parser.add_argument("--rules", required=True, help="Path to JSON rules file")
    parser.add_argument("--encoding", default="utf-8", help="File encoding (default: utf-8)")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--dry-run", action="store_true",
                       help="Preview changes without writing to disk")
    group.add_argument("--apply-fixes", action="store_true",
                       help="Write changes to disk")
    return parser.parse_args()


def apply_fixes_to_content(content: str, fixes: list[dict]) -> tuple[str, list[dict]]:
    """Apply fixes to content, return (updated_content, applied_fixes).
    fixes should be sorted by position. Each fix dict should have:
        start_byte, end_byte, replacement
    """
    if not fixes:
        return content, []

    result = []
    last_end = 0
    applied = []

    for fix in fixes:
        if fix.get("skipped_reason"):
            applied.append(fix)
            continue

        start = fix["start_byte"]
        end = fix["end_byte"]
        replacement = fix["replacement"]

        if start < last_end:
            fix["skipped_reason"] = "overlap"
            applied.append(fix)
            continue

        result.append(content[last_end:start])
        result.append(replacement)
        last_end = end
        applied.append(fix)

    result.append(content[last_end:])
    return ''.join(result), applied


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
    matches = searcher.search()

    # Collect fix candidates
    fix_candidates: dict[str, list[dict]] = {}

    for m in matches:
        rule = next((r for r in rules if r.id == m.rule_id), None)
        if rule and rule.fix:
            fix = rule.fix
            template = fix["template"]
            replacement = expand_template(template, m.captures, m.text)

            file_key = m.file
            fix_entry = {
                "rule_id": m.rule_id,
                "file": m.file,
                "language": m.language,
                "start": m.start,
                "end": m.end,
                "replacement": replacement,
                "applied": args.apply_fixes,
                "skipped_reason": None,
                "start_byte": m.line * 1000 + m.col,  # rough byte offset approximation
                "end_byte": m.end_line * 1000 + m.end_col,
            }
            fix_candidates.setdefault(file_key, []).append(fix_entry)

    # Sort fix candidates by position
    for file_key in fix_candidates:
        fix_candidates[file_key].sort(key=lambda f: (
            f["start"]["line"], f["start"]["col"],
            f["end"]["line"], f["end"]["col"], f["rule_id"]
        ))

    # Apply conflict resolution for write mode
    if args.apply_fixes:
        file_contents: dict[str, str] = {}

        for file_key, fixes in fix_candidates.items():
            filepath = os.path.join(searcher.root_dir, file_key)
            try:
                with open(filepath, 'r', encoding=args.encoding) as f:
                    content = f.read()
            except FileNotFoundError:
                continue

            updated, applied = apply_fixes_to_content(content, fixes)
            file_contents[file_key] = updated

            for fix in applied:
                fix["applied"] = True

            if updated != content:
                with open(filepath, 'w', encoding=args.encoding) as f:
                    f.write(updated)

    # Prepare output lines
    output_lines = []

    # Add match lines (for all matches, including those with fixes)
    for m in matches:
        output_lines.append(("match", m.to_dict()))

    # Add fix lines
    for file_key, fixes in fix_candidates.items():
        for fix in fixes:
            if args.dry_run:
                fix["applied"] = False
            output_lines.append(("fix", {
                "event": "fix",
                "rule_id": fix["rule_id"],
                "file": fix["file"],
                "language": fix["language"],
                "start": fix["start"],
                "end": fix["end"],
                "replacement": fix["replacement"],
                "applied": fix["applied"],
                "skipped_reason": fix["skipped_reason"]
            }))

    # Sort all output lines: file, line, col, rule_id, then match before fix
    output_lines.sort(key=lambda x: (
        x[1]["file"],
        x[1]["start"]["line"],
        x[1]["start"]["col"],
        x[1]["rule_id"],
        0 if x[0] == "match" else 1
    ))

    for _, line in output_lines:
        print(json.dumps(line))


if __name__ == "__main__":
    main()
