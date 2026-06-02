#!/usr/bin/env python3
"""Command-line code searcher for Python codebases."""

import argparse
import json
import os
import re
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Rule:
    """Represents a search rule."""
    id: str
    kind: str
    pattern: str
    languages: list[str] = field(default_factory=list)
    regex_flags: list[str] = field(default_factory=list)

    def __post_init__(self):
        if not self.languages:
            self.languages = ["python"]


@dataclass
class Match:
    """Represents a single match."""
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

    def sort_key(self) -> tuple:
        return (self.file, self.line, self.col, self.rule_id)


class RuleCompiler:
    """Compiles regex rules with proper flags."""

    @staticmethod
    def compile_flags(flags: list[str]) -> int:
        """Convert flag names to re module flags."""
        result = 0
        flag_map = {
            "i": re.IGNORECASE,
            "m": re.MULTILINE,
            "s": re.DOTALL
        }
        for flag in flags:
            if flag not in flag_map:
                raise ValueError(f"Invalid regex flag: {flag}. Valid flags: i, m, s")
            result |= flag_map[flag]
        return result


class RuleLoader:
    """Loads and validates rules from JSON file."""

    @staticmethod
    def load(path: str) -> list[Rule]:
        """Load rules from a JSON file."""
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

            languages = item.get("languages", ["python"])
            if not isinstance(languages, list):
                raise ValueError(f"Rule '{rule_id}': languages must be an array")

            # Validate languages - only "python" is supported
            for lang in languages:
                if not isinstance(lang, str) or lang != "python":
                    raise ValueError(f"Rule '{rule_id}': languages may only contain 'python'")

            regex_flags = item.get("regex_flags", [])
            if not isinstance(regex_flags, list):
                raise ValueError(f"Rule '{rule_id}': regex_flags must be an array")

            # Validate regex flags
            valid_flags = {"i", "m", "s"}
            for flag in regex_flags:
                if flag not in valid_flags:
                    raise ValueError(f"Rule '{rule_id}': invalid regex flag '{flag}', must be one of {valid_flags}")

            # Compile regex if kind is regex (validation)
            if kind == "regex":
                flags = RuleCompiler.compile_flags(regex_flags)
                try:
                    re.compile(pattern, flags)
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


class CodeSearcher:
    """Searches Python files for rule matches."""

    def __init__(self, root_dir: str, rules: list[Rule], encoding: str = "utf-8"):
        self.root_dir = root_dir
        self.rules = rules
        self.encoding = encoding

    def search(self) -> list[Match]:
        """Search all .py files in the root directory."""
        matches = []

        for dirpath, _dirnames, filenames in os.walk(self.root_dir):
            for filename in filenames:
                if not filename.endswith(".py"):
                    continue

                filepath = os.path.join(dirpath, filename)
                file_matches = self._search_file(filepath)
                matches.extend(file_matches)

        # Sort matches by file, line, col, rule_id
        matches.sort(key=lambda m: m.sort_key())
        return matches

    def _search_file(self, filepath: str) -> list[Match]:
        """Search a single file for all rule matches."""
        matches = []

        # Read file content
        try:
            with open(filepath, 'r', encoding=self.encoding) as f:
                content = f.read()
        except UnicodeDecodeError:
            # Skip files that fail to decode
            return matches

        # Get relative path with POSIX separators
        rel_path = os.path.relpath(filepath, self.root_dir)
        rel_path = rel_path.replace("\\", "/")

        lines = content.splitlines(keepends=True)

        for rule in self.rules:
            rule_matches = self._apply_rule(content, lines, rel_path, rule)
            matches.extend(rule_matches)

        return matches

    def _apply_rule(self, content: str, lines: list[str], rel_path: str, rule: Rule) -> list[Match]:
        """Apply a single rule to content."""
        matches = []

        if rule.kind == "exact":
            matches = self._match_exact(content, lines, rel_path, rule)
        elif rule.kind == "regex":
            matches = self._match_regex(content, lines, rel_path, rule)

        return matches

    def _match_exact(self, content: str, lines: list[str], rel_path: str, rule: Rule) -> list[Match]:
        """Find exact string matches in content."""
        matches = []
        pattern = rule.pattern
        start = 0

        while True:
            pos = content.find(pattern, start)
            if pos == -1:
                break

            line_num, col_num = self._pos_to_line_col(content[:pos], lines)
            match = Match(
                rule_id=rule.id,
                file=rel_path,
                language="python",
                line=line_num,
                col=col_num,
                end_line=line_num,
                end_col=col_num + len(pattern),
                text=pattern
            )
            matches.append(match)
            start = pos + 1  # Move past this match

        return matches

    def _match_regex(self, content: str, lines: list[str], rel_path: str, rule: Rule) -> list[Match]:
        """Find regex matches in content."""
        matches = []
        flags = RuleCompiler.compile_flags(rule.regex_flags)
        pattern = re.compile(rule.pattern, flags)

        for match in pattern.finditer(content):
            line_num, col_num = self._pos_to_line_col(content[:match.start()], lines)
            end_line_num, end_col_num = self._pos_to_line_col(content[:match.end()], lines)

            m = Match(
                rule_id=rule.id,
                file=rel_path,
                language="python",
                line=line_num,
                col=col_num,
                end_line=end_line_num,
                end_col=end_col_num,
                text=match.group()
            )
            matches.append(m)

        return matches

    def _pos_to_line_col(self, text: str, lines: list[str]) -> tuple[int, int]:
        """Convert a character position to (line, col) 1-based coordinates."""
        current_pos = 0

        for line_idx, line in enumerate(lines):
            line_len = len(line)
            if current_pos + line_len >= len(text):
                # Position is in this line
                col = len(text) - current_pos + 1
                return (line_idx + 1, col)
            current_pos += line_len + 1  # +1 for newline

        # If position is at or past the end
        return (len(lines) + 1, len(lines[-1]) + 1 if lines else 1)


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
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

    # Validate root_dir exists
    if not os.path.isdir(args.root_dir):
        print(f"Error: Directory not found: {args.root_dir}", file=sys.stderr)
        sys.exit(1)

    # Validate rules file exists
    if not os.path.isfile(args.rules):
        print(f"Error: File not found: {args.rules}", file=sys.stderr)
        sys.exit(1)

    # Load rules
    try:
        rules = RuleLoader.load(args.rules)
    except (json.JSONDecodeError, ValueError) as e:
        print(f"Error loading rules: {e}", file=sys.stderr)
        sys.exit(1)

    # Search
    searcher = CodeSearcher(args.root_dir, rules, args.encoding)
    matches = searcher.search()

    # Output JSON Lines
    for match in matches:
        print(json.dumps(match.to_dict()))


if __name__ == "__main__":
    import sys
    main()
