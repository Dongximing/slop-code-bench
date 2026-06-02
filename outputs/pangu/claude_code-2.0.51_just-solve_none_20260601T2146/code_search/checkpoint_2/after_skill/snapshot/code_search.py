#!/usr/bin/env python3
"""Command-line code searcher for Python codebases."""

import argparse
import dataclasses
import json
import os
import re
import sys


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

    def to_dict(self) -> dict:
        return {
            "rule_id": self.rule_id,
            "file": self.file,
            "language": self.language,
            "start": {"line": self.line, "col": self.col},
            "end": {"line": self.end_line, "col": self.end_col},
            "match": self.text
        }


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
        if kind not in ("exact", "regex"):
            raise ValueError(f"Rule '{rule_id}': kind must be 'exact' or 'regex'")

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
            if flag not in ("i", "m", "s"):
                raise ValueError(f"Rule '{rule_id}': invalid regex flag '{flag}'")

        if kind == "regex":
            flag_val = 0
            flag_map = {"i": re.IGNORECASE, "m": re.MULTILINE, "s": re.DOTALL}
            for flag in regex_flags:
                flag_val |= flag_map[flag]
            try:
                re.compile(pattern, flag_val)
            except re.error as e:
                raise ValueError(f"Rule '{rule_id}': invalid regex pattern: {e}")

        rules.append(Rule(id=rule_id, kind=kind, pattern=pattern,
                         languages=languages, regex_flags=regex_flags))

    return rules


class CodeSearcher:
    EXTENSION_MAP = {
        ".py": "python",
        ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript",
        ".cc": "cpp", ".cpp": "cpp", ".cxx": "cpp",
        ".hh": "cpp", ".hpp": "cpp", ".hxx": "cpp",
    }

    def __init__(self, root_dir: str, rules: list[Rule], encoding: str = "utf-8"):
        self.root_dir = root_dir
        self.rules = rules
        self.encoding = encoding

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

                lines = content.splitlines(keepends=True)

                for rule in self.rules:
                    if rule.languages and language not in rule.languages:
                        continue

                    if rule.kind == "regex":
                        flag_val = 0
                        flag_map = {"i": re.IGNORECASE, "m": re.MULTILINE, "s": re.DOTALL}
                        for flag in rule.regex_flags:
                            flag_val |= flag_map[flag]
                        pattern = re.compile(rule.pattern, flag_val)
                        for m in pattern.finditer(content):
                            line = content[:m.start()].count('\n')
                            col = len(content[:m.start()]) - content[:m.start()].rfind('\n')
                            end_line = content[:m.end()].count('\n')
                            end_col = len(content[:m.end()]) - content[:m.end()].rfind('\n')
                            matches.append(Match(
                                rule_id=rule.id, file=rel_path, language=language,
                                line=line + 1, col=col, end_line=end_line + 1,
                                end_col=end_col, text=m.group()
                            ))
                    else:
                        pattern = rule.pattern
                        start = 0
                        while True:
                            pos = content.find(pattern, start)
                            if pos == -1:
                                break
                            line = content[:pos].count('\n')
                            col = len(content[:pos]) - content[:pos].rfind('\n')
                            matches.append(Match(
                                rule_id=rule.id, file=rel_path, language=language,
                                line=line + 1, col=col, end_line=line + 1,
                                end_col=col + len(pattern), text=pattern
                            ))
                            start = pos + 1

        matches.sort(key=lambda m: (m.file, m.line, m.col, m.rule_id))
        return matches


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Code searcher for Python codebases",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python code_search.py repo --rules rules.json
  python code_search.py repo --rules rules.json --encoding latin-1
        """
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
    matches = searcher.search()

    for match in matches:
        print(json.dumps(match.to_dict()))


if __name__ == "__main__":
    main()
