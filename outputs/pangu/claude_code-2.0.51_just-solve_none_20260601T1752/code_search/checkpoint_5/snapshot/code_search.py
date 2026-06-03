#!/usr/bin/env python3
"""
Command-line code searcher for Python codebases.
Supports exact match, regex, and structure-aware pattern rules.
"""

import argparse
import io
import json
import os
import re
import sys
import tokenize as tokenize_py
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Dict, Any, Tuple

try:
    import esprima
    ESPRIMA_AVAILABLE = True
except ImportError:
    ESPRIMA_AVAILABLE = False

try:
    import libcst as cst
    LIBCST_AVAILABLE = True
except ImportError:
    LIBCST_AVAILABLE = False


@dataclass
class Rule:
    id: str
    kind: str  # "exact" or "regex" or "pattern"
    pattern: str
    languages: List[str] = field(default_factory=list)
    regex_flags: List[str] = field(default_factory=list)

    def __post_init__(self):
        if not self.languages:
            self.languages = ["python", "javascript", "cpp", "rust", "java", "go", "haskell"]


@dataclass
class Position:
    line: int
    col: int


@dataclass
class Match:
    rule_id: str
    file: str
    language: str
    start: Position
    end: Position
    match: str


@dataclass
class CaptureInfo:
    text: str
    ranges: List[Dict[str, Dict[str, int]]]


@dataclass
class PatternMatch:
    rule_id: str
    file: str
    language: str
    start: Position
    end: Position
    match: str
    captures: Dict[str, CaptureInfo]


@dataclass
class Token:
    type: str
    value: str
    start_pos: Position
    end_pos: Position


def parse_args():
    parser = argparse.ArgumentParser(
        description="Search codebase for patterns using rules"
    )
    parser.add_argument("root_dir", help="Path to the codebase to scan")
    parser.add_argument("--rules", required=True, help="Path to JSON rules file")
    parser.add_argument("--encoding", default="utf-8", help="File encoding (default: utf-8)")
    return parser.parse_args()


def load_rules(rules_path: str) -> List[Rule]:
    """Load and validate rules from JSON file."""
    with open(rules_path, "r", encoding="utf-8") as f:
        rules_data = json.load(f)

    rules = []
    rule_ids = set()

    for idx, rule_data in enumerate(rules_data):
        if "id" not in rule_data:
            raise ValueError(f"Rule at index {idx} missing required field 'id'")
        if "kind" not in rule_data:
            raise ValueError(f"Rule at index {idx} missing required field 'kind'")
        if "pattern" not in rule_data:
            raise ValueError(f"Rule at index {idx} missing required field 'pattern'")

        rule_id = rule_data["id"]
        if not rule_id:
            raise ValueError(f"Rule at index {idx} has empty 'id'")
        if rule_id in rule_ids:
            raise ValueError(f"Duplicate rule id: {rule_id}")
        rule_ids.add(rule_id)

        kind = rule_data["kind"]
        if kind not in ("exact", "regex", "pattern"):
            raise ValueError(f"Rule {rule_id}: invalid kind '{kind}', must be 'exact', 'regex', or 'pattern'")

        pattern = rule_data["pattern"]
        if not pattern:
            raise ValueError(f"Rule {rule_id}: pattern cannot be empty")

        languages = rule_data.get("languages", [])
        if languages:
            if not isinstance(languages, list):
                raise ValueError(f"Rule {rule_id}: 'languages' must be an array")
            for lang in languages:
                if lang not in ("python", "javascript", "cpp", "rust", "java", "go", "haskell"):
                    raise ValueError(f"Rule {rule_id}: unsupported language '{lang}', must be one of 'python', 'javascript', 'cpp', 'rust', 'java', 'go', 'haskell'")

        regex_flags = rule_data.get("regex_flags", [])
        if regex_flags:
            if kind != "regex":
                raise ValueError(f"Rule {rule_id}: 'regex_flags' is only valid for kind='regex'")
            if not isinstance(regex_flags, list):
                raise ValueError(f"Rule {rule_id}: 'regex_flags' must be an array")
            valid_flags = {"i", "m", "s"}
            for flag in regex_flags:
                if flag not in valid_flags:
                    raise ValueError(f"Rule {rule_id}: invalid regex flag '{flag}', must be one of 'i', 'm', 's'")

            flags = 0
            if "i" in regex_flags:
                flags |= re.IGNORECASE
            if "m" in regex_flags:
                flags |= re.MULTILINE
            if "s" in regex_flags:
                flags |= re.DOTALL
            try:
                re.compile(pattern, flags)
            except re.error as e:
                raise ValueError(f"Rule {rule_id}: invalid regex pattern: {e}")
        elif kind == "regex":
            try:
                re.compile(pattern)
            except re.error as e:
                raise ValueError(f"Rule {rule_id}: invalid regex pattern: {e}")

        rule = Rule(
            id=rule_id,
            kind=kind,
            pattern=pattern,
            languages=languages,
            regex_flags=regex_flags
        )
        rules.append(rule)

    return rules


def get_supported_files(root_dir: str) -> List[tuple[Path, str]]:
    """Recursively get all supported source files in root_dir.
    Returns list of (file_path, language) tuples.
    """
    root_path = Path(root_dir)
    files = []

    # Map file extensions to languages
    ext_to_lang = {
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
        # Rust
        ".rs": "rust",
        # Java
        ".java": "java",
        # Go
        ".go": "go",
        # Haskell
        ".hs": "haskell",
        ".lhs": "haskell",
    }

    for file_path in root_path.rglob("*"):
        if file_path.is_file():
            ext = file_path.suffix.lower()
            if ext in ext_to_lang:
                files.append((file_path, ext_to_lang[ext]))

    return files


def calculate_position(text: str, position: int) -> Position:
    """Calculate 1-based line and column from character position."""
    line = text.count("\n", 0, position) + 1
    last_newline = text.rfind("\n", 0, position)
    col = position - last_newline
    return Position(line=line, col=col)


def find_exact_matches(rule: Rule, content: str, file_path: Path, root_dir: Path, language: str) -> List[Match]:
    """Find all exact matches of rule.pattern in content."""
    matches = []
    pattern = rule.pattern

    start_idx = 0
    while True:
        idx = content.find(pattern, start_idx)
        if idx == -1:
            break

        end_idx = idx + len(pattern)
        start_pos = calculate_position(content, idx)
        end_pos = calculate_position(content, end_idx)
        rel_path = file_path.relative_to(root_dir)
        file_str = str(rel_path).replace(os.sep, "/")

        match = Match(
            rule_id=rule.id,
            file=file_str,
            language=language,
            start=start_pos,
            end=end_pos,
            match=pattern
        )
        matches.append(match)

        start_idx = end_idx

    return matches


def find_regex_matches(rule: Rule, content: str, file_path: Path, root_dir: Path, language: str) -> List[Match]:
    """Find all regex matches of rule.pattern in content."""
    matches = []

    flags = 0
    if "i" in rule.regex_flags:
        flags |= re.IGNORECASE
    if "m" in rule.regex_flags:
        flags |= re.MULTILINE
    if "s" in rule.regex_flags:
        flags |= re.DOTALL

    try:
        compiled_pattern = re.compile(rule.pattern, flags)
    except re.error:
        return matches  # Should have been validated already

    for m in compiled_pattern.finditer(content):
        start_idx = m.start()
        end_idx = m.end()
        matched_text = m.group()

        start_pos = calculate_position(content, start_idx)
        end_pos = calculate_position(content, end_idx)
        rel_path = file_path.relative_to(root_dir)
        file_str = str(rel_path).replace(os.sep, "/")

        match = Match(
            rule_id=rule.id,
            file=file_str,
            language=language,
            start=start_pos,
            end=end_pos,
            match=matched_text
        )
        matches.append(match)

    return matches


def tokenize_python(content: str) -> List[Token]:
    """Tokenize Python code."""
    tokens = []
    try:
        readline = io.BytesIO(content.encode('utf-8')).readline
        for tok in tokenize_py.tokenize(readline):
            if tok.type in (tokenize_py.ENCODING, tokenize_py.COMMENT, tokenize_py.NL, tokenize_py.INDENT, tokenize_py.DEDENT, tokenize_py.ENDMARKER):
                continue
            tok_type = tokenize_py.tok_name[tok.type]
            start_pos = Position(line=tok.start[0], col=tok.start[1])
            end_pos = Position(line=tok.end[0], col=tok.end[1])
            value = tok.string
            tokens.append(Token(type=tok_type, value=value, start_pos=start_pos, end_pos=end_pos))
    except Exception:
        tokens = simple_tokenize_python(content)
    return tokens


def tokenize_javascript(content: str) -> List[Token]:
    """Tokenize JavaScript code using esprima if available."""
    if not ESPRIMA_AVAILABLE:
        return simple_tokenize_javascript(content)
    try:
        ast = esprima.parseScript(content, tokens=True, loc=True, comment=True)
        tokens = []
        for tok in ast.tokens:
            start_pos = Position(line=tok.loc.start.line, col=tok.loc.start.column + 1)
            end_pos = Position(line=tok.loc.end.line, col=tok.loc.end.column + 1)
            tokens.append(Token(type=tok.type, value=tok.value, start_pos=start_pos, end_pos=end_pos))
        return tokens
    except Exception:
        return simple_tokenize_javascript(content)


def tokenize_cpp(content: str) -> List[Token]:
    """Tokenize C++ code."""
    return simple_tokenize_cpp(content)


def simple_tokenize_python(content: str) -> List[Token]:
    """Simple fallback tokenizer for Python."""
    tokens = []
    try:
        readline = io.BytesIO(content.encode('utf-8')).readline
        for tok in tokenize_py.tokenize(readline):
            if tok.type in (tokenize_py.ENCODING, tokenize_py.COMMENT, tokenize_py.NL, tokenize_py.INDENT, tokenize_py.DEDENT, tokenize_py.ENDMARKER):
                continue
            tok_type = tokenize_py.tok_name[tok.type]
            start_pos = Position(line=tok.start[0], col=tok.start[1])
            end_pos = Position(line=tok.end[0], col=tok.end[1])
            value = tok.string
            tokens.append(Token(type=tok_type, value=value, start_pos=start_pos, end_pos=end_pos))
    except Exception:
        pass
    return tokens


def simple_tokenize_javascript(content: str) -> List[Token]:
    """Simple fallback tokenizer for JavaScript."""
    tokens = []
    i = 0
    length = len(content)
    while i < length:
        char = content[i]

        if char.isspace():
            i += 1
            continue

        if char == '/' and i + 1 < length:
            if content[i+1] == '/':
                i += 2
                while i < length and content[i] != '\n':
                    i += 1
                continue
            elif content[i+1] == '*':
                i += 2
                while i < length and not (content[i-1] == '*' and content[i] == '/'):
                    i += 1
                i += 1
                continue

        if char in ('"', "'", '`'):
            quote = char
            value = char
            start_pos = calculate_position(content, i)
            i += 1
            while i < length and content[i] != quote:
                if content[i] == '\\':
                    value += content[i]
                    i += 1
                value += content[i]
                i += 1
            if i < length:
                value += content[i]
                i += 1
            end_pos = calculate_position(content, i)
            tokens.append(Token(type='string', value=value, start_pos=start_pos, end_pos=end_pos))
            continue

        if char.isdigit():
            value = char
            start_pos = calculate_position(content, i)
            i += 1
            while i < length and (content[i].isdigit() or content[i] in ('.', 'e', 'E', '-', '+')):
                value += content[i]
                i += 1
            end_pos = calculate_position(content, i)
            tokens.append(Token(type='number', value=value, start_pos=start_pos, end_pos=end_pos))
            continue

        if char.isalpha() or char == '_':
            value = char
            start_pos = calculate_position(content, i)
            i += 1
            while i < length and (content[i].isalnum() or content[i] == '_'):
                value += content[i]
                i += 1
            end_pos = calculate_position(content, i)
            tokens.append(Token(type='identifier', value=value, start_pos=start_pos, end_pos=end_pos))
            continue

        if len(char) > 0:
            value = char
            start_pos = calculate_position(content, i)
            if i + 1 < length and content[i:i+2] in ('//', '/*', '==', '!=', '<=', '>=', '&&', '||', '++', '--', '+=', '-=', '*=', '/=', '=>'):
                value = content[i:i+2]
                i += 2
            else:
                i += 1
            end_pos = calculate_position(content, i)
            tokens.append(Token(type='operator', value=value, start_pos=start_pos, end_pos=end_pos))
            continue

        i += 1

    return tokens


def simple_tokenize_cpp(content: str) -> List[Token]:
    """Simple tokenizer for C++."""
    tokens = []
    i = 0
    length = len(content)
    while i < length:
        char = content[i]

        if char.isspace():
            i += 1
            continue

        if char == '/' and i + 1 < length:
            if content[i+1] == '/':
                i += 2
                while i < length and content[i] != '\n':
                    i += 1
                continue
            elif content[i+1] == '*':
                i += 2
                while i < length and not (content[i-1] == '*' and content[i] == '/'):
                    i += 1
                i += 1
                continue

        if char in ('"', "'"):
            quote = char
            value = char
            start_pos = calculate_position(content, i)
            i += 1
            while i < length and content[i] != quote:
                if content[i] == '\\':
                    value += content[i]
                    i += 1
                value += content[i]
                i += 1
            if i < length:
                value += content[i]
                i += 1
            end_pos = calculate_position(content, i)
            tokens.append(Token(type='string', value=value, start_pos=start_pos, end_pos=end_pos))
            continue

        if char == "'":
            value = char
            start_pos = calculate_position(content, i)
            i += 1
            while i < length and content[i] != "'":
                if content[i] == '\\':
                    value += content[i]
                    i += 1
                value += content[i]
                i += 1
            if i < length:
                value += content[i]
                i += 1
            end_pos = calculate_position(content, i)
            tokens.append(Token(type='char', value=value, start_pos=start_pos, end_pos=end_pos))
            continue

        if char.isdigit() or (char == '.' and i + 1 < length and content[i+1].isdigit()):
            value = char
            start_pos = calculate_position(content, i)
            i += 1
            while i < length and (content[i].isdigit() or content[i] in ('.', 'e', 'E', 'x', 'X', '-', '+')):
                value += content[i]
                i += 1
            end_pos = calculate_position(content, i)
            tokens.append(Token(type='number', value=value, start_pos=start_pos, end_pos=end_pos))
            continue

        if char.isalpha() or char == '_':
            value = char
            start_pos = calculate_position(content, i)
            i += 1
            while i < length and (content[i].isalnum() or content[i] == '_'):
                value += content[i]
                i += 1
            end_pos = calculate_position(content, i)
            tokens.append(Token(type='identifier', value=value, start_pos=start_pos, end_pos=end_pos))
            continue

        if len(char) > 0:
            value = char
            start_pos = calculate_position(content, i)
            two_chars = content[i:i+2]
            if two_chars in ('==', '!=', '<=', '>=', '&&', '||', '++', '--', '->', '+=', '-=', '*=', '/=', '%=', '&=', '|=', '^=', '<<', '>>', '<<=', '>>='):
                value = two_chars
                i += 2
            else:
                i += 1
            end_pos = calculate_position(content, i)
            tokens.append(Token(type='operator', value=value, start_pos=start_pos, end_pos=end_pos))
            continue

        i += 1

    return tokens


def parse_pattern(pattern_str: str) -> List[Token]:
    """Parse a pattern string into tokens and identify metavariables.
    Metavariable format: $NAME or $NAME? (optional)
    Literal $: $$
    """
    pattern_tokens = []
    n = len(pattern_str)
    i = 0

    while i < n:
        char = pattern_str[i]

        if char == '$':
            if i + 1 < n and pattern_str[i+1] == '$':
                # Literal $
                token_type = 'dollar'
                value = '$'
                start_pos = Position(line=1, col=i + 1)
                end_pos = Position(line=1, col=i + 2)
                pattern_tokens.append(Token(type=token_type, value=value, start_pos=start_pos, end_pos=end_pos))
                i += 2
                continue
            elif i + 1 < n and pattern_str[i+1].isalpha():
                # Metavariable - $NAME or $NAME?
                j = i + 1
                while j < n and pattern_str[j].isalpha():
                    j += 1
                optional = False
                if j < n and pattern_str[j] == '?':
                    optional = True
                    j += 1
                name = pattern_str[i:j]
                token_type = 'metavariable_optional' if optional else 'metavariable'
                value = name
                start_pos = Position(line=1, col=i + 1)
                end_pos = Position(line=1, col=j + 1)
                pattern_tokens.append(Token(type=token_type, value=name, start_pos=start_pos, end_pos=end_pos))
                i = j
                continue
            else:
                # Single $ at end or before space
                start_pos = Position(line=1, col=i + 1)
                end_pos = Position(line=1, col=i + 2)
                pattern_tokens.append(Token(type='dollar', value=char, start_pos=start_pos, end_pos=end_pos))
                i += 1
                continue

        if char.isspace():
            i += 1
            continue
        elif char.isalpha() or char == '_':
            value = char
            start_pos = Position(line=1, col=i + 1)
            j = i + 1
            while j < n and (pattern_str[j].isalnum() or pattern_str[j] == '_'):
                value += pattern_str[j]
                j += 1
            end_pos = Position(line=1, col=j + 1)
            pattern_tokens.append(Token(type='identifier', value=value, start_pos=start_pos, end_pos=end_pos))
            i = j
        elif char.isdigit():
            value = char
            start_pos = Position(line=1, col=i + 1)
            j = i + 1
            while j < n and pattern_str[j].isdigit():
                value += pattern_str[j]
                j += 1
            end_pos = Position(line=1, col=j + 1)
            pattern_tokens.append(Token(type='number', value=value, start_pos=start_pos, end_pos=end_pos))
            i = j
        else:
            value = char
            start_pos = Position(line=1, col=i + 1)
            two_chars = pattern_str[i:i+2]
            if two_chars in ('==', '!=', '<=', '>=', '&&', '||', '++', '--', '+=', '-=', '*=', '/=', '->', '<<', '>>'):
                end_pos = Position(line=1, col=i + 3)
                pattern_tokens.append(Token(type='operator', value=two_chars, start_pos=start_pos, end_pos=end_pos))
                i += 2
            else:
                end_pos = Position(line=1, col=i + 2)
                pattern_tokens.append(Token(type='operator', value=char, start_pos=start_pos, end_pos=end_pos))
                i += 1

    return pattern_tokens


def match_pattern_token(pattern_tok: Token, source_tok: Token) -> bool:
    """Check if a source token matches a pattern token."""
    if pattern_tok.type == 'metavariable':
        return source_tok.type not in ('whitespace', '')
    if pattern_tok.type == 'metavariable_optional':
        return True
    return pattern_tok.value == source_tok.value


def find_pattern_matches(rule: Rule, content: str, file_path: Path, root_dir: Path,
                         language: str) -> List[PatternMatch]:
    """Find all pattern matches in content."""
    if language == "python":
        source_tokens = tokenize_python(content)
    elif language == "javascript":
        source_tokens = tokenize_javascript(content)
    else:
        source_tokens = tokenize_cpp(content)

    pattern_tokens = parse_pattern(rule.pattern)
    matches = []

    n = len(source_tokens)
    p = len(pattern_tokens)

    if p == 0:
        return matches

    i = 0
    while i < n:
        match_pos = i
        pattern_pos = 0
        captured = {}  # metavariable_name -> list of (start_idx, end_idx)
        failed = False

        while pattern_pos < p:
            pattern_tok = pattern_tokens[pattern_pos]

            if pattern_tok.type == 'metavariable_optional':
                if match_pos < n:
                    source_tok = source_tokens[match_pos]
                    if source_tok.type == 'comment':
                        match_pos += 1
                        continue
                    if match_pattern_token(pattern_tok, source_tok):
                        if pattern_tok.value not in captured:
                            captured[pattern_tok.value] = []
                        captured[pattern_tok.value].append((match_pos, match_pos + 1))
                        match_pos += 1
                pattern_pos += 1
                continue

            if match_pos < n:
                source_tok = source_tokens[match_pos]

                if source_tok.type == 'comment':
                    match_pos += 1
                    continue

                if match_pattern_token(pattern_tok, source_tok):
                    if pattern_tok.type == 'metavariable':
                        if pattern_tok.value not in captured:
                            captured[pattern_tok.value] = []
                        captured[pattern_tok.value].append((match_pos, match_pos + 1))
                    match_pos += 1
                    pattern_pos += 1
                else:
                    failed = True
                    break
            else:
                failed = True
                break

        if not failed and pattern_pos == p:
            start_idx = i
            end_idx = match_pos

            if start_idx < end_idx:
                actual_start = source_tokens[start_idx]
                actual_end = source_tokens[end_idx - 1]

                start_pos = actual_start.start_pos
                end_pos = actual_end.end_pos

                def find_char_offset(token: Token) -> int:
                    lines_before = content.split('\n')[:token.start_pos.line - 1]
                    prefix = '\n'.join(lines_before)
                    search_start = len(prefix)
                    found = content.find(token.value, search_start)
                    return found if found != -1 else search_start

                matched_start = find_char_offset(actual_start)
                matched_end = matched_start
                for j in range(start_idx, end_idx):
                    t = source_tokens[j]
                    found = content.find(t.value, matched_end)
                    if found != -1:
                        matched_end = found + len(t.value)

                matched_text = content[matched_start:matched_end]

                captures = {}
                for meta_name, ranges in captured.items():
                    meta_ranges = []
                    meta_text_parts = []
                    for start_tk_idx, end_tk_idx in ranges:
                        start_tk = source_tokens[start_tk_idx]
                        last_tk = source_tokens[end_tk_idx - 1]

                        actual_range_start = find_char_offset(start_tk)
                        actual_range_end = find_char_offset(last_tk) + len(last_tk.value)

                        meta_ranges.append({
                            "start": {"line": calculate_position(content, actual_range_start).line, "col": calculate_position(content, actual_range_start).col},
                            "end": {"line": calculate_position(content, actual_range_end).line, "col": calculate_position(content, actual_range_end).col}
                        })
                        meta_text_parts.append(content[actual_range_start:actual_range_end])

                    captures[f"${meta_name}"] = CaptureInfo(
                        text="".join(meta_text_parts),
                        ranges=meta_ranges
                    )

                pm = PatternMatch(
                    rule_id=rule.id,
                    file=str(file_path.relative_to(root_dir)).replace(os.sep, "/"),
                    language=language,
                    start=start_pos,
                    end=end_pos,
                    match=matched_text,
                    captures=captures
                )
                matches.append(pm)

        i += 1

    # Sort by start position, then end position
    matches.sort(key=lambda m: (m.start.line, m.start.col, m.end.line, m.end.col))

    return matches


def scan_file(file_path: Path, rules: List[Rule], root_dir: Path, encoding: str, language: str) -> List[Any]:
    """Scan a single file for all rule matches."""
    matches = []

    try:
        with open(file_path, "r", encoding=encoding) as f:
            content = f.read()
    except UnicodeDecodeError:
        return matches

    for rule in rules:
        if rule.kind == "exact":
            file_matches = find_exact_matches(rule, content, file_path, root_dir, language)
            matches.extend(file_matches)
        elif rule.kind == "regex":
            file_matches = find_regex_matches(rule, content, file_path, root_dir, language)
            matches.extend(file_matches)
        elif rule.kind == "pattern":
            file_matches = find_pattern_matches(rule, content, file_path, root_dir, language)
            matches.extend(file_matches)

    return matches


def match_to_json(match: Any) -> dict:
    """Convert a Match (or PatternMatch) to JSON-serializable dict."""
    base = {
        "rule_id": match.rule_id,
        "file": match.file,
        "language": match.language,
        "start": {"line": match.start.line, "col": match.start.col},
        "end": {"line": match.end.line, "col": match.end.col},
        "match": match.match
    }

    if isinstance(match, PatternMatch):
        # Serialize captures with sorted keys
        captures = {}
        for key in sorted(match.captures.keys()):
            cap = match.captures[key]
            captures[key] = {
                "text": cap.text,
                "ranges": cap.ranges
            }
        base["captures"] = captures

    return base


def main():
    args = parse_args()

    if not os.path.isdir(args.root_dir):
        print(f"Error: '{args.root_dir}' is not a directory", file=sys.stderr)
        sys.exit(1)

    try:
        rules = load_rules(args.rules)
    except (json.JSONDecodeError, ValueError) as e:
        print(f"Error loading rules: {e}", file=sys.stderr)
        sys.exit(1)

    root_path = Path(args.root_dir)
    files = get_supported_files(args.root_dir)

    all_matches = []
    for file_path, language in files:
        applicable_rules = [r for r in rules if language in r.languages]
        if not applicable_rules:
            continue
        file_matches = scan_file(file_path, applicable_rules, root_path, args.encoding, language)
        all_matches.extend(file_matches)

    # Sort by file, start position, rule_id
    all_matches.sort(key=lambda m: (m.file, m.start.line, m.start.col, m.rule_id))

    for match in all_matches:
        print(json.dumps(match_to_json(match)))

    sys.exit(0)


if __name__ == "__main__":
    main()
