#!/usr/bin/env python3
"""Command-line code searcher for Python, JavaScript, and C++ codebases."""

import argparse
import json
import os
import re
from pathlib import Path
from typing import List, Dict, Any

_REGEX_FLAGS = {"i": re.IGNORECASE, "m": re.MULTILINE, "s": re.DOTALL}

_EXTENSION_TO_LANGUAGE = {
    ".py": "python",
    ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".cc": "cpp", ".cpp": "cpp", ".cxx": "cpp",
    ".hh": "cpp", ".hpp": "cpp", ".hxx": "cpp",
}

_ALL_LANGUAGES = ["python", "javascript", "cpp"]


def _line_col(content: str, pos: int) -> tuple[int, int]:
    before = content[:pos]
    line = before.count('\n') + 1
    col = len(before) - before.rfind('\n')
    return line, col


def _prepare_rules(rules: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    prepared = []
    for rule in rules:
        entry = {
            "id": rule["id"],
            "languages": rule.get("languages", _ALL_LANGUAGES),
        }
        if rule["kind"] == "regex":
            flags = 0
            for f in rule.get("regex_flags", []):
                flags |= _REGEX_FLAGS.get(f, 0)
            entry["compiled"] = re.compile(rule["pattern"], flags)
        else:
            entry["pattern"] = rule["pattern"]
        prepared.append(entry)
    return prepared


def _find_matches(content: str, prepared: Dict[str, Any], file_path: str, language: str) -> List[Dict[str, Any]]:
    rule_id = prepared["id"]
    if "compiled" in prepared:
        hits = [(m.start(), m.end(), m.group()) for m in prepared["compiled"].finditer(content)]
    else:
        pattern = prepared["pattern"]
        hits = []
        start = 0
        while True:
            pos = content.find(pattern, start)
            if pos == -1:
                break
            hits.append((pos, pos + len(pattern), pattern))
            start = pos + 1

    matches = []
    for start, end, text in hits:
        sl, sc = _line_col(content, start)
        el, ec = _line_col(content, end)
        matches.append({
            "rule_id": rule_id, "file": file_path, "language": language,
            "start": {"line": sl, "col": sc}, "end": {"line": el, "col": ec},
            "match": text,
        })
    return matches


def scan_file(file_path: str, full_path: Path, language: str,
              rules: List[Dict[str, Any]], encoding: str) -> List[Dict[str, Any]]:
    try:
        with open(full_path, "r", encoding=encoding) as f:
            content = f.read()
    except (UnicodeDecodeError, OSError):
        return []

    all_matches = []
    for rule in rules:
        if language in rule["languages"]:
            all_matches.extend(_find_matches(content, rule, file_path, language))
    return all_matches


def main():
    parser = argparse.ArgumentParser(description="Code searcher for Python, JavaScript, and C++ codebases")
    parser.add_argument("root_dir", help="Path to the codebase to scan")
    parser.add_argument("--rules", required=True, help="Path to a JSON array of rules")
    parser.add_argument("--encoding", default="utf-8", help="File encoding (default: utf-8)")
    args = parser.parse_args()

    root_dir = Path(args.root_dir).resolve()
    with open(args.rules, "r", encoding="utf-8") as f:
        raw_rules = json.load(f)
    rules = _prepare_rules(raw_rules)

    all_matches = []
    for dirpath, _, filenames in os.walk(root_dir):
        for filename in filenames:
            full_path = Path(dirpath) / filename
            language = _EXTENSION_TO_LANGUAGE.get(full_path.suffix.lower())
            if language is None:
                continue
            rel_path = full_path.relative_to(root_dir).as_posix()
            all_matches.extend(scan_file(rel_path, full_path, language, rules, args.encoding))

    all_matches.sort(key=lambda m: (m["file"], m["start"]["line"], m["start"]["col"], m["rule_id"]))
    for match in all_matches:
        print(json.dumps(match, separators=(',', ':')))


if __name__ == "__main__":
    main()
