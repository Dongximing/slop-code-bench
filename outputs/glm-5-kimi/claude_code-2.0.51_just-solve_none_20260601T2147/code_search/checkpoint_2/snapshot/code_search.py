#!/usr/bin/env python3
"""Command-line code searcher for Python, JavaScript, and C++ codebases."""

import argparse
import json
import os
import re
from pathlib import Path
from typing import List, Dict, Any, Optional

_REGEX_FLAGS = {"i": re.IGNORECASE, "m": re.MULTILINE, "s": re.DOTALL}

# Map file extensions to language names
_EXTENSION_TO_LANGUAGE = {
    # Python
    ".py": "python",
    # JavaScript
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    # C++
    ".cc": "cpp",
    ".cpp": "cpp",
    ".cxx": "cpp",
    ".hh": "cpp",
    ".hpp": "cpp",
    ".hxx": "cpp",
}

_ALL_LANGUAGES = ["python", "javascript", "cpp"]


def _make_match(rule_id: str, file_path: str, language: str, content: str,
                start: int, end: int, matched_text: str) -> Dict[str, Any]:
    start_line, start_col = _line_col(content, start)
    end_line, end_col = _line_col(content, end)
    return {
        "rule_id": rule_id,
        "file": file_path,
        "language": language,
        "start": {"line": start_line, "col": start_col},
        "end": {"line": end_line, "col": end_col},
        "match": matched_text
    }


def _line_col(content: str, pos: int) -> tuple:
    """Convert a byte-offset position to 1-based (line, col)."""
    lines = content[:pos].split('\n')
    return len(lines), len(lines[-1]) + 1


def _compile_regex(rule: Dict[str, Any]) -> re.Pattern:
    flags = 0
    for flag in rule.get("regex_flags", []):
        flags |= _REGEX_FLAGS.get(flag, 0)
    return re.compile(rule["pattern"], flags)


def _find_exact(content: str, pattern: str, rule_id: str, file_path: str, language: str) -> List[Dict[str, Any]]:
    matches = []
    start = 0
    while True:
        pos = content.find(pattern, start)
        if pos == -1:
            break
        matches.append(_make_match(rule_id, file_path, language, content, pos, pos + len(pattern), pattern))
        start = pos + 1
    return matches


def _find_regex(content: str, compiled: re.Pattern, rule_id: str, file_path: str, language: str) -> List[Dict[str, Any]]:
    return [
        _make_match(rule_id, file_path, language, content, m.start(), m.end(), m.group())
        for m in compiled.finditer(content)
    ]


_FINDERS = {
    "exact": lambda content, rule, rid, fpath, lang: _find_exact(content, rule["pattern"], rid, fpath, lang),
    "regex": lambda content, rule, rid, fpath, lang: _find_regex(content, _compile_regex(rule), rid, fpath, lang),
}


def scan_file(file_path: str, full_path: Path, language: str, rules: List[Dict[str, Any]], encoding: str) -> List[Dict[str, Any]]:
    try:
        with open(full_path, "r", encoding=encoding) as f:
            content = f.read()
    except (UnicodeDecodeError, OSError):
        return []

    all_matches = []
    for rule in rules:
        rule_languages = rule.get("languages", _ALL_LANGUAGES)
        if language not in rule_languages:
            continue
        finder = _FINDERS.get(rule["kind"])
        if finder:
            all_matches.extend(finder(content, rule, rule["id"], file_path, language))
    return all_matches


def main():
    parser = argparse.ArgumentParser(description="Code searcher for Python, JavaScript, and C++ codebases")
    parser.add_argument("root_dir", help="Path to the codebase to scan")
    parser.add_argument("--rules", required=True, help="Path to a JSON array of rules")
    parser.add_argument("--encoding", default="utf-8", help="File encoding (default: utf-8)")
    args = parser.parse_args()

    root_dir = Path(args.root_dir).resolve()
    with open(args.rules, "r", encoding="utf-8") as f:
        rules = json.load(f)

    all_matches = []
    for dirpath, _, filenames in os.walk(root_dir):
        for filename in filenames:
            full_path = Path(dirpath) / filename
            ext = full_path.suffix.lower()
            language = _EXTENSION_TO_LANGUAGE.get(ext)
            if language is None:
                continue
            rel_path = full_path.relative_to(root_dir).as_posix()
            all_matches.extend(scan_file(rel_path, full_path, language, rules, args.encoding))

    all_matches.sort(key=lambda m: (m["file"], m["start"]["line"], m["start"]["col"], m["rule_id"]))
    for match in all_matches:
        print(json.dumps(match, separators=(',', ':')))


if __name__ == "__main__":
    main()
