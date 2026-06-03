#!/usr/bin/env python3
"""
Command-line code searcher for Python, JavaScript, and C++ codebases.
Supports exact match and vanilla regex rules.
"""

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, List, Dict, Optional, Tuple
from dataclasses import dataclass, field


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Search Python, JavaScript, and C++ codebases using exact match or regex rules."
    )
    parser.add_argument(
        "root_dir",
        type=str,
        help="Path to the codebase to scan"
    )
    parser.add_argument(
        "--rules",
        type=str,
        required=True,
        help="Path to a JSON array of rules"
    )
    parser.add_argument(
        "--encoding",
        type=str,
        default="utf-8",
        help="File encoding (default: utf-8)"
    )
    return parser.parse_args()


def load_rules(rules_file: str) -> List[Dict[str, Any]]:
    """Load and validate rules from a JSON file."""
    with open(rules_file, 'r', encoding='utf-8') as f:
        rules = json.load(f)

    if not isinstance(rules, list):
        raise ValueError("Rules file must contain a JSON array")

    # Validate each rule
    seen_ids = set()
    for i, rule in enumerate(rules):
        # Check required fields
        if 'id' not in rule or not rule['id']:
            raise ValueError(f"Rule {i}: 'id' must be a non-empty string")
        if rule['id'] in seen_ids:
            raise ValueError(f"Rule {i}: Duplicate rule id: {rule['id']}")
        seen_ids.add(rule['id'])

        if 'kind' not in rule or rule['kind'] not in ('exact', 'regex', 'pattern'):
            raise ValueError(f"Rule {i}: 'kind' must be 'exact', 'regex', or 'pattern'")

        if 'pattern' not in rule or not rule['pattern']:
            raise ValueError(f"Rule {i}: 'pattern' must be a non-empty string")

        # Validate languages if present
        if 'languages' in rule:
            langs = rule['languages']
            if not isinstance(langs, list):
                raise ValueError(f"Rule {i}: 'languages' must be an array")
            for lang in langs:
                if lang not in ("python", "javascript", "cpp"):
                    raise ValueError(f"Rule {i}: Unknown language '{lang}', must be one of 'python', 'javascript', 'cpp'")

        # Validate regex_flags for regex rules
        if rule['kind'] == 'regex' and 'regex_flags' in rule:
            flags = rule['regex_flags']
            if not isinstance(flags, list):
                raise ValueError(f"Rule {i}: 'regex_flags' must be an array")
            valid_flags = {'i', 'm', 's'}
            for flag in flags:
                if flag not in valid_flags:
                    raise ValueError(f"Rule {i}: Invalid regex flag '{flag}', must be one of {valid_flags}")

    return rules


def compile_regex_pattern(pattern: str, flags: List[str]) -> re.Pattern:
    """Compile a regex pattern with the given flags."""
    re_flags = 0
    for flag in flags:
        if flag == 'i':
            re_flags |= re.IGNORECASE
        elif flag == 'm':
            re_flags |= re.MULTILINE
        elif flag == 's':
            re_flags |= re.DOTALL

    try:
        return re.compile(pattern, re_flags)
    except re.error as e:
        raise ValueError(f"Invalid regex pattern: {e}")


def find_matches_in_content(
    content: str,
    rule: Dict[str, Any],
    filename: str,
    language: str
) -> List[Dict[str, Any]]:
    """Find all matches for a rule in the given content."""
    matches = []
    rule_id = rule['id']
    pattern_str = rule['pattern']
    kind = rule['kind']

    if kind == 'exact':
        # For exact match, find all occurrences
        start = 0
        while True:
            idx = content.find(pattern_str, start)
            if idx == -1:
                break
            # Calculate line and column
            line_num, col_num = get_line_col(content, idx)
            matches.append({
                'rule_id': rule_id,
                'file': filename,
                'language': language,
                'start': {'line': line_num, 'col': col_num},
                'end': {'line': get_line_col(content, idx + len(pattern_str))[0],
                        'col': get_line_col(content, idx + len(pattern_str))[1]},
                'match': pattern_str
            })
            start = idx + 1
    elif kind == 'regex':
        compiled = compile_regex_pattern(pattern_str, rule.get('regex_flags', []))

        for m in compiled.finditer(content):
            start_pos = m.start()
            end_pos = m.end()
            start_line, start_col = get_line_col(content, start_pos)
            end_line, end_col = get_line_col(content, end_pos)
            matches.append({
                'rule_id': rule_id,
                'file': filename,
                'language': language,
                'start': {'line': start_line, 'col': start_col},
                'end': {'line': end_line, 'col': end_col},
                'match': m.group(0)
            })
    else:  # kind == 'pattern'
        matches = find_pattern_matches(content, rule, filename, language)

    return matches


def get_line_col(content: str, pos: int) -> tuple:
    """Get 1-based line and column numbers for a position in content."""
    # Count newlines before this position
    lines_before = content.count('\n', 0, pos)
    # Find the last newline before this position
    last_newline = content.rfind('\n', 0, pos)
    if last_newline == -1:
        col = pos + 1  # 1-based column
    else:
        col = pos - last_newline  # column from start of line
    return (lines_before + 1, col)


# ---------- Pattern Matching Infrastructure ----------

@dataclass
class Token:
    """A token from source code."""
    type: str      # 'name', 'number', 'string', 'operator', 'keyword', 'punctuation', 'whitespace', 'comment', 'unknown'
    text: str
    start: int     # character offset in source
    end: int       # character offset in source (exclusive)


@dataclass
class PatternToken:
    """A token in a pattern rule."""
    type: str      # 'name', 'number', 'string', 'operator', 'keyword', 'punctuation', 'literal', 'meta', 'meta_opt'
    text: str
    var_name: Optional[str] = None  # For meta/meta_opt tokens


@dataclass
class MatchResult:
    """Result of a pattern match."""
    start: int     # start offset in source
    end: int       # end offset in source (exclusive)
    captures: Dict[str, List[Tuple[int, int]]]  # metavariable name -> list of (start, end) ranges in source


def tokenize_python(content: str) -> List[Token]:
    """Tokenize Python source code."""
    tokens = []
    i = 0
    n = len(content)

    # Python keywords
    keywords = {
        'False', 'None', 'True', 'and', 'as', 'assert', 'async', 'await',
        'break', 'class', 'continue', 'def', 'del', 'elif', 'else', 'except',
        'finally', 'for', 'from', 'global', 'if', 'import', 'in', 'is',
        'lambda', 'nonlocal', 'not', 'or', 'pass', 'raise', 'return', 'try',
        'while', 'with', 'yield'
    }

    while i < n:
        ch = content[i]

        # Skip whitespace
        if ch.isspace():
            start = i
            while i < n and content[i].isspace():
                i += 1
            tokens.append(Token('whitespace', content[start:i], start, i))
            continue

        # Comment
        if ch == '#':
            start = i
            while i < n and content[i] != '\n':
                i += 1
            tokens.append(Token('comment', content[start:i], start, i))
            continue

        # String literals
        if ch in ('"', "'", 'r', 'u', 'f', 'b'):
            # Handle prefix + string
            prefix = ''
            if ch in ('r', 'u', 'f', 'b'):
                # Check for combinations like fr, ur, bf, etc.
                prefix_chars = set('rfub')
                while i < n and content[i] in prefix_chars:
                    prefix += content[i]
                    i += 1
            if i < n and content[i] in ('"', "'"):
                quote = content[i]
                start = i
                i += 1
                # Triple quote?
                if i < n and content[i] == quote:
                    i += 1
                    if i < n and content[i] == quote:
                        i += 1
                    # Triple quoted string
                    while i < n and not (content[i-1] == quote and content[i-2] == quote and content[i-3] == quote):
                        if content[i] == '\\' and i + 1 < n:
                            i += 2
                        elif content[i] == '\n':
                            break
                        else:
                            i += 1
                    i += 1  # include closing quote(s)
                else:
                    # Single/double quoted string
                    while i < n and content[i] != quote:
                        if content[i] == '\\' and i + 1 < n:
                            i += 2
                        else:
                            i += 1
                    i += 1  # include closing quote
                tokens.append(Token('string', content[start:i], start, i))
            else:
                # Just a prefix char that wasn't followed by a quote
                tokens.append(Token('unknown', prefix, start - len(prefix), start))
            continue

        # Numbers
        if ch.isdigit() or (ch == '.' and i + 1 < n and content[i+1].isdigit()):
            start = i
            # Decimal integer or float?
            if ch == '.':
                i += 1
                while i < n and content[i].isdigit():
                    i += 1
            else:
                while i < n and content[i].isdigit():
                    i += 1
                if i < n and content[i] == '.':
                    i += 1
                    while i < n and content[i].isdigit():
                        i += 1
                if i < n and content[i].lower() == 'e':
                    i += 1
                    if i < n and content[i] in ('+', '-'):
                        i += 1
                    while i < n and content[i].isdigit():
                        i += 1
                if i < n and content[i].lower() == 'j':
                    i += 1
            tokens.append(Token('number', content[start:i], start, i))
            continue

        # Identifiers and keywords
        if ch.isalpha() or ch == '_':
            start = i
            while i < n and (content[i].isalnum() or content[i] == '_'):
                i += 1
            text = content[start:i]
            if text in keywords:
                tokens.append(Token('keyword', text, start, i))
            else:
                tokens.append(Token('name', text, start, i))
            continue

        # Operators and punctuation
        op_chars = set('+-*/%<=>!&|^~.,:;()[]{}@')
        if ch in op_chars:
            start = i
            # Multi-character operators
            two_char = content[i:i+2]
            three_char = content[i:i+3]
            if three_char in ('...', '**=', '//=', '<<=', '>>=', '**'):
                i += 3
            elif two_char in ('**', '//', '<<', '>>', '//=', '**=', '<<=', '>>=',
                              '==', '!=', '<=', '>=', '+=', '-=', '*=', '/=', '%=',
                              '&=', '|=', '^=', '>>', '<<', '->', '...'):
                i += 2
            else:
                i += 1
            tokens.append(Token('operator', content[start:i], start, i))
            continue

        # Unknown character
        tokens.append(Token('unknown', ch, i, i + 1))
        i += 1

    return tokens


def tokenize_javascript(content: str) -> List[Token]:
    """Tokenize JavaScript source code."""
    tokens = []
    i = 0
    n = len(content)

    keywords = {
        'break', 'case', 'catch', 'class', 'const', 'continue', 'debugger',
        'default', 'delete', 'do', 'else', 'export', 'extends', 'finally',
        'for', 'function', 'if', 'import', 'instanceof', 'new', 'return',
        'super', 'switch', 'this', 'throw', 'try', 'typeof', 'var', 'void',
        'while', 'with', 'null', 'true', 'false', 'in', 'of', 'let', 'static',
        'get', 'set', 'await', 'async', 'yield'
    }

    while i < n:
        ch = content[i]

        if ch.isspace():
            start = i
            while i < n and content[i].isspace():
                i += 1
            tokens.append(Token('whitespace', content[start:i], start, i))
            continue

        # Single-line comment
        if ch == '/' and i + 1 < n and content[i+1] == '/':
            start = i
            while i < n and content[i] != '\n':
                i += 1
            tokens.append(Token('comment', content[start:i], start, i))
            continue

        # Multi-line comment
        if ch == '/' and i + 1 < n and content[i+1] == '*':
            start = i
            i += 2
            while i < n and not (content[i-1] == '*' and content[i] == '/'):
                i += 1
            i += 1  # include closing '/'
            tokens.append(Token('comment', content[start:i], start, i))
            continue

        # String literals
        if ch in ('"', "'", '`'):
            quote = ch
            start = i
            i += 1
            if quote == '`':
                # Template literal
                while i < n and content[i] != '`':
                    if content[i] == '$' and i + 1 < n and content[i+1] == '{':
                        i += 2
                        # Skip past the expression - find matching }
                        depth = 1
                        i += 1
                        while i < n and depth > 0:
                            if content[i] == '{':
                                depth += 1
                            elif content[i] == '}':
                                depth -= 1
                            i += 1
                    elif content[i] == '\\':
                        i += 2
                    else:
                        i += 1
            else:
                while i < n and content[i] != quote:
                    if content[i] == '\\':
                        i += 2
                    else:
                        i += 1
                i += 1
            tokens.append(Token('string', content[start:i], start, i))
            continue

        # Numbers
        if ch.isdigit() or (ch == '.' and i + 1 < n and content[i+1].isdigit()):
            start = i
            if ch == '.':
                i += 1
                while i < n and content[i].isdigit():
                    i += 1
            else:
                while i < n and content[i].isdigit():
                    i += 1
                if i < n and content[i] == '.':
                    i += 1
                    while i < n and content[i].isdigit():
                        i += 1
                if i < n and content[i].lower() == 'e':
                    i += 1
                    if i < n and content[i] in ('+', '-'):
                        i += 1
                    while i < n and content[i].isdigit():
                        i += 1
            # BigInt suffix
            if i < n and content[i].lower() == 'n':
                i += 1
            tokens.append(Token('number', content[start:i], start, i))
            continue

        # Identifiers and keywords
        if ch.isalpha() or ch == '_' or ch == '$':
            start = i
            while i < n and (content[i].isalnum() or content[i] in '_$'):
                i += 1
            text = content[start:i]
            if text in keywords:
                tokens.append(Token('keyword', text, start, i))
            else:
                tokens.append(Token('name', text, start, i))
            continue

        # Operators and punctuation
        op_chars = set('+-*/%<=>!&|^~.,:;()[]{}?@')
        if ch in op_chars:
            start = i
            two_char = content[i:i+2]
            three_char = content[i:i+3]
            if three_char in ('===', '!==', '>>>', '<<=', '>>='):
                i += 3
            elif two_char in ('==', '!=', '<=', '>=', '&&', '||', '++', '--',
                              '+=', '-=', '*=', '/=', '%=', '<<', '>>', '>>>',
                              '&=', '|=', '^=', '===', '!==', '**', '??', '...'):
                i += 2
            else:
                i += 1
            tokens.append(Token('operator', content[start:i], start, i))
            continue

        tokens.append(Token('unknown', ch, i, i + 1))
        i += 1

    return tokens


def tokenize_cpp(content: str) -> List[Token]:
    """Tokenize C++ source code."""
    tokens = []
    i = 0
    n = len(content)

    keywords = {
        'alignas', 'alignof', 'and', 'and_eq', 'asm', 'auto', 'bitand', 'bitor',
        'bool', 'break', 'case', 'catch', 'char', 'char8_t', 'char16_t', 'char32_t',
        'class', 'compl', 'const', 'constexpr', 'const_cast', 'continue', 'co_await',
        'co_return', 'co_yield', 'decltype', 'default', 'delete', 'do', 'double',
        'dynamic_cast', 'else', 'enum', 'explicit', 'export', 'extern', 'false',
        'float', 'for', 'friend', 'goto', 'if', 'inline', 'int', 'long', 'mutable',
        'namespace', 'new', 'not', 'not_eq', 'noexcept', 'nullptr', 'operator',
        'or', 'or_eq', 'private', 'protected', 'public', 'register', 'reinterpret_cast',
        'return', 'short', 'signed', 'sizeof', 'static', 'static_assert', 'static_cast',
        'struct', 'switch', 'template', 'this', 'thread_local', 'throw', 'true',
        'try', 'typedef', 'typeid', 'typename', 'union', 'unsigned', 'using',
        'virtual', 'void', 'volatile', 'wchar_t', 'while', 'xor', 'xor_eq'
    }

    while i < n:
        ch = content[i]

        if ch.isspace():
            start = i
            while i < n and content[i].isspace():
                i += 1
            tokens.append(Token('whitespace', content[start:i], start, i))
            continue

        # Single-line comment
        if ch == '/' and i + 1 < n and content[i+1] == '/':
            start = i
            while i < n and content[i] != '\n':
                i += 1
            tokens.append(Token('comment', content[start:i], start, i))
            continue

        # Multi-line comment
        if ch == '/' and i + 1 < n and content[i+1] == '*':
            start = i
            i += 2
            while i < n and not (content[i-1] == '*' and content[i] == '/'):
                i += 1
            i += 1
            tokens.append(Token('comment', content[start:i], start, i))
            continue

        # String literals
        if ch in ('"', "'"):
            quote = ch
            start = i
            i += 1
            while i < n and content[i] != quote:
                if content[i] == '\\':
                    i += 2
                else:
                    # Raw string literal (R"...")
                    if quote == '"' and i < n and content[i] == 'R':
                        # Check for ("..."
                        j = i + 1
                        while j < n and content[j] in 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_()./ * :!^~%-=+?<>;&{{}}':
                            j += 1
                        if j < n and content[j] == '("':
                            # Find matching ")
                            inner = content[j+2:]
                            # This is complex, just scan until matching ")
                            depth = 1
                            k = j + 2
                            while k < n and depth > 0:
                                if inner[k-(j+2)] == ')':
                                    if k+1 < n and content[k+1] == '"':
                                        depth -= 1
                                        if depth == 0:
                                            k += 1
                                            break
                                k += 1
                            i = k
                            continue
                        else:
                            i += 1
                    else:
                        i += 1
                pass
            i += 1
            tokens.append(Token('string', content[start:i], start, i))
            continue

        # Character literals
        if ch == "'":
            start = i
            i += 1
            while i < n and content[i] != "'":
                if content[i] == '\\':
                    i += 2
                else:
                    i += 1
            i += 1
            tokens.append(Token('string', content[start:i], start, i))
            continue

        # Numbers
        if ch.isdigit() or (ch == '.' and i + 1 < n and content[i+1].isdigit()):
            start = i
            if ch == '.':
                i += 1
                while i < n and content[i].isdigit():
                    i += 1
            else:
                while i < n and content[i].isdigit():
                    i += 1
                if i < n and content[i] == '.':
                    i += 1
                    if i < n and content[i].isdigit():
                        while i < n and content[i].isdigit():
                            i += 1
                if i < n and content[i].lower() == 'e':
                    i += 1
                    if i < n and content[i] in ('+', '-'):
                        i += 1
                    while i < n and content[i].isdigit():
                        i += 1
                if i < n and content[i].lower() == 'f':
                    i += 1
                elif i < n and content[i].lower() == 'l':
                    i += 1
            # Integer suffixes
            if i < n and content[i].lower() == 'u':
                i += 1
                if i < n and content[i].lower() == 'l':
                    i += 1
            elif i < n and content[i].lower() == 'l':
                i += 1
                if i < n and content[i].lower() == 'u':
                    i += 1
            tokens.append(Token('number', content[start:i], start, i))
            continue

        # Identifiers and keywords
        if ch.isalpha() or ch == '_':
            start = i
            while i < n and (content[i].isalnum() or content[i] == '_'):
                i += 1
            text = content[start:i]
            if text in keywords:
                tokens.append(Token('keyword', text, start, i))
            else:
                tokens.append(Token('name', text, start, i))
            continue

        # Operators and punctuation
        op_chars = set('+-*/%<=>!&|^~.,:;()[]{}?@~')
        if ch in op_chars:
            start = i
            two_char = content[i:i+2]
            three_char = content[i:i+3]
            if three_char in ('...', '<<=', '>>=', '***='):
                i += 3
            elif two_char in ('++', '--', '==', '!=', '<=', '>=', '&&', '||',
                              '+=', '-=', '*=', '/=', '%=', '<<', '>>', '->*', '->',
                              '&=', '|=', '^=', '<<=', '>>=', '##', '==='):
                i += 2
            else:
                i += 1
            tokens.append(Token('operator', content[start:i], start, i))
            continue

        tokens.append(Token('unknown', ch, i, i + 1))
        i += 1

    return tokens


def parse_pattern(pattern_str: str, language: str) -> List[PatternToken]:
    """Parse a pattern string into tokens, handling metavariables."""
    tokens = []
    i = 0
    n = len(pattern_str)

    # Get keyword set for the language
    if language == 'python':
        keywords = {
            'False', 'None', 'True', 'and', 'as', 'assert', 'async', 'await',
            'break', 'class', 'continue', 'def', 'del', 'elif', 'else', 'except',
            'finally', 'for', 'from', 'global', 'if', 'import', 'in', 'is',
            'lambda', 'nonlocal', 'not', 'or', 'pass', 'raise', 'return', 'try',
            'while', 'with', 'yield'
        }
    elif language == 'javascript':
        keywords = {
            'break', 'case', 'catch', 'class', 'const', 'continue', 'debugger',
            'default', 'delete', 'do', 'else', 'export', 'extends', 'finally',
            'for', 'function', 'if', 'import', 'instanceof', 'new', 'return',
            'super', 'switch', 'this', 'throw', 'try', 'typeof', 'var', 'void',
            'while', 'with', 'null', 'true', 'false', 'in', 'of', 'let', 'static',
            'get', 'set', 'await', 'async', 'yield'
        }
    else:  # cpp
        keywords = {
            'alignas', 'alignof', 'and', 'and_eq', 'asm', 'auto', 'bitand', 'bitor',
            'bool', 'break', 'case', 'catch', 'char', 'char8_t', 'char16_t', 'char32_t',
            'class', 'compl', 'const', 'constexpr', 'const_cast', 'continue', 'co_await',
            'co_return', 'co_yield', 'decltype', 'default', 'delete', 'do', 'double',
            'dynamic_cast', 'else', 'enum', 'explicit', 'export', 'extern', 'false',
            'float', 'for', 'friend', 'goto', 'if', 'inline', 'int', 'long', 'mutable',
            'namespace', 'new', 'not', 'not_eq', 'noexcept', 'nullptr', 'operator',
            'or', 'or_eq', 'private', 'protected', 'public', 'register', 'reinterpret_cast',
            'return', 'short', 'signed', 'sizeof', 'static', 'static_assert', 'static_cast',
            'struct', 'switch', 'template', 'this', 'thread_local', 'throw', 'true',
            'try', 'typedef', 'typeid', 'typename', 'union', 'unsigned', 'using',
            'virtual', 'void', 'volatile', 'wchar_t', 'while', 'xor', 'xor_eq'
        }

    while i < n:
        ch = pattern_str[i]

        # Handle escaped $
        if ch == '$' and i + 1 < n and pattern_str[i+1] == '$':
            tokens.append(PatternToken('literal', '$'))
            i += 2
            continue

        # Handle metavariables
        if ch == '$':
            start = i
            i += 1

            # Check for optional metavariable
            is_optional = False
            if i < n and pattern_str[i] == '?':
                is_optional = True
                i += 1

            # Read the name
            if i < n and (pattern_str[i].isalpha() or pattern_str[i] == '_'):
                name_start = i
                while i < n and (pattern_str[i].isalnum() or pattern_str[i] == '_'):
                    i += 1
                name = pattern_str[name_start:i]
                tokens.append(PatternToken('meta_opt' if is_optional else 'meta', name, var_name=name))
            else:
                # Just a $ followed by something else - treat as literal $
                tokens.append(PatternToken('literal', '$'))
            continue

        # Skip whitespace in pattern (not matched against)
        if ch.isspace():
            i += 1
            continue

        # String literals in pattern
        if ch in ('"', "'"):
            quote = ch
            start = i
            i += 1
            while i < n and pattern_str[i] != quote:
                if pattern_str[i] == '\\':
                    i += 2
                else:
                    i += 1
            i += 1
            tokens.append(PatternToken('string', pattern_str[start:i]))
            continue

        # Numbers
        if ch.isdigit() or (ch == '.' and i + 1 < n and pattern_str[i+1].isdigit()):
            start = i
            if ch == '.':
                i += 1
                while i < n and pattern_str[i].isdigit():
                    i += 1
            else:
                while i < n and pattern_str[i].isdigit():
                    i += 1
                if i < n and pattern_str[i] == '.':
                    i += 1
                    while i < n and pattern_str[i].isdigit():
                        i += 1
            tokens.append(PatternToken('number', pattern_str[start:i]))
            continue

        # Operators and punctuation (skip whitespace, so we match these)
        op_chars = set('+-*/%<=>!&|^~.,:;()[]{}?@')
        if ch in op_chars:
            start = i
            two_char = pattern_str[i:i+2]
            if len(two_char) == 2 and two_char in ('==', '!=', '<=', '>=', '&&', '||',
                                                     '++', '--', '+=', '-=', '*=', '/=',
                                                     '%=', '<<', '>>', '&=', '|=', '^='):
                i += 2
            else:
                i += 1
            tokens.append(PatternToken('operator', pattern_str[start:i]))
            continue

        # Identifiers and keywords
        if ch.isalpha() or ch == '_':
            start = i
            while i < n and (pattern_str[i].isalnum() or pattern_str[i] == '_'):
                i += 1
            text = pattern_str[start:i]
            if text in keywords:
                tokens.append(PatternToken('keyword', text))
            else:
                tokens.append(PatternToken('name', text))
            continue

        # Unknown character - skip or treat as literal
        i += 1

    return tokens


def pattern_matches(tokens: List[Token], pattern: List[PatternToken]) -> List[MatchResult]:
    """Find all matches of a pattern in tokenized source code."""
    if not pattern:
        return []

    results = []

    # Filter out irrelevant tokens
    def is_relevant(t: Token) -> bool:
        return t.type not in ('whitespace', 'comment')

    relevant_tokens = [t for t in tokens if is_relevant(t)]

    # Helper to match a single token against a pattern token
    def tokens_match(source_tok: Token, pat_tok: PatternToken) -> bool:
        if pat_tok.type == 'literal':
            return source_tok.text == pat_tok.text
        elif pat_tok.type == 'string':
            return source_tok.type == 'string'
        elif pat_tok.type == 'number':
            return source_tok.type == 'number'
        elif pat_tok.type == 'name':
            return source_tok.type == 'name'
        elif pat_tok.type == 'keyword':
            return source_tok.type == 'keyword'
        elif pat_tok.type == 'operator':
            return source_tok.type == 'operator'
        elif pat_tok.type == 'punctuation':
            return source_tok.type in ('punctuation', 'operator')
        elif pat_tok.type == 'meta':
            # Meta variable matches any relevant single token
            return True
        elif pat_tok.type == 'meta_opt':
            # Optional meta - matches any relevant single token or nothing
            return True
        return False

    # Try to match pattern starting at each position
    n = len(relevant_tokens)
    p = len(pattern)

    for start_idx in range(n):
        # Track captures for this potential match
        captures: Dict[str, List[Tuple[int, int]]] = {}
        matched = True
        pattern_idx = 0
        src_idx = start_idx

        # We need to handle optional metavariables specially - they can be skipped
        while pattern_idx < p and src_idx < n:
            pat_tok = pattern[pattern_idx]

            if pat_tok.type == 'meta_opt':
                # Try to match if we have tokens
                if src_idx < n and tokens_match(relevant_tokens[src_idx], pat_tok):
                    # Match this token
                    if pat_tok.var_name:
                        if pat_tok.var_name not in captures:
                            captures[pat_tok.var_name] = []
                        captures[pat_tok.var_name].append((relevant_tokens[src_idx].start,
                                                           relevant_tokens[src_idx].end))
                    src_idx += 1
                # If no match, just skip (optional means it can be absent)
                pattern_idx += 1
            else:
                if src_idx < n and tokens_match(relevant_tokens[src_idx], pat_tok):
                    if pat_tok.type == 'meta' and pat_tok.var_name:
                        # Record the capture
                        if pat_tok.var_name not in captures:
                            captures[pat_tok.var_name] = []
                        captures[pat_tok.var_name].append((relevant_tokens[src_idx].start,
                                                           relevant_tokens[src_idx].end))
                    src_idx += 1
                    pattern_idx += 1
                else:
                    matched = False
                    break

        if matched and pattern_idx >= p:
            # Check consistency of all captures (same text for same var name)
            var_texts: Dict[str, str] = {}
            consistent = True
            for var_name, ranges in list(captures.items()):
                for start, end in ranges:
                    text = content[start:end]
                    if var_name in var_texts:
                        if var_texts[var_name] != text:
                            consistent = False
                            break
                    else:
                        var_texts[var_name] = text
                if not consistent:
                    break

            if consistent:
                # Full match found from start_idx to src_idx-1
                match_start = relevant_tokens[start_idx].start
                match_end = relevant_tokens[src_idx - 1].end
                # Convert to simple captures (just the first occurrence for the match level)
                # The full ranges will be computed in find_pattern_matches
                simple_captures = {k: v[0] for k, v in captures.items()}
                results.append(MatchResult(match_start, match_end, simple_captures))
                # But we need to store the full captures too - modify MatchResult or store separately
                # Actually, let's create a new dataclass that stores full captures
                # For now, let's return full captures

    return results


def find_pattern_matches(
    content: str,
    rule: Dict[str, Any],
    filename: str,
    language: str
) -> List[Dict[str, Any]]:
    """Find all pattern matches for a rule in the given content."""
    matches = []
    rule_id = rule['id']
    pattern_str = rule['pattern']

    # Tokenize source code
    if language == 'python':
        tokens = tokenize_python(content)
    elif language == 'javascript':
        tokens = tokenize_javascript(content)
    else:  # cpp
        tokens = tokenize_cpp(content)

    # Parse pattern
    pattern = parse_pattern(pattern_str, language)

    if not pattern:
        return matches

    # Find matches
    match_results = pattern_matches(tokens, pattern)

    # Convert to output format
    for mr in match_results:
        match_text = content[mr.start:mr.end]
        start_line, start_col = get_line_col(content, mr.start)
        end_line, end_col = get_line_col(content, mr.end)

        # Build captures with ranges
        captures = {}
        for var_name in sorted(mr.captures.keys()):
            ranges = mr.captures[var_name]
            # Build ranges for each occurrence
            range_list = []
            for cap_start, cap_end in ranges:
                cap_text = content[cap_start:cap_end]
                cap_start_line, cap_start_col = get_line_col(content, cap_start)
                cap_end_line, cap_end_col = get_line_col(content, cap_end)
                range_list.append({
                    'start': {'line': cap_start_line, 'col': cap_start_col},
                    'end': {'line': cap_end_line, 'col': cap_end_col}
                })
            # Use the text from the first range
            first_start, first_end = ranges[0]
            cap_text = content[first_start:first_end]
            captures[var_name] = {
                'text': cap_text,
                'ranges': range_list
            }

        matches.append({
            'rule_id': rule_id,
            'file': filename,
            'language': language,
            'start': {'line': start_line, 'col': start_col},
            'end': {'line': end_line, 'col': end_col},
            'match': match_text,
            'captures': captures
        })

    return matches


def scan_file(
    filepath: Path,
    root_dir: Path,
    rules: List[Dict[str, Any]],
    encoding: str
) -> List[Dict[str, Any]]:
    """Scan a single source file and return all matches."""
    all_matches = []

    # Detect language from file extension
    language = get_language_from_extension(filepath)
    if language is None:
        return all_matches  # Skip unsupported file types

    # Read file content
    try:
        with open(filepath, 'r', encoding=encoding) as f:
            content = f.read()
    except UnicodeDecodeError:
        # Skip files that fail to decode
        return all_matches

    # Get relative path with forward slashes
    rel_path = filepath.relative_to(root_dir)
    filename = rel_path.as_posix()

    # Check language support
    for rule in rules:
        languages = rule.get('languages', ['python', 'javascript', 'cpp'])
        if language not in languages:
            continue

        matches = find_matches_in_content(content, rule, filename, language)
        all_matches.extend(matches)

    return all_matches


def get_language_from_extension(filepath: Path) -> Optional[str]:
    """Detect language from file extension."""
    ext = filepath.suffix.lower()
    language_map = {
        '.py': 'python',
        '.js': 'javascript',
        '.mjs': 'javascript',
        '.cjs': 'javascript',
        '.cc': 'cpp',
        '.cpp': 'cpp',
        '.cxx': 'cpp',
        '.hh': 'cpp',
        '.hpp': 'cpp',
        '.hxx': 'cpp',
    }
    return language_map.get(ext)


def scan_directory(
    root_dir: Path,
    rules: List[Dict[str, Any]],
    encoding: str
) -> List[Dict[str, Any]]:
    """Scan all Python, JavaScript, and C++ files in a directory and return all matches."""
    all_matches = []

    # Supported extensions for each language
    extensions = ['*.py', '*.js', '*.mjs', '*.cjs', '*.cc', '*.cpp', '*.cxx', '*.hh', '*.hpp', '*.hxx']

    for pattern in extensions:
        for filepath in root_dir.rglob(pattern):
            matches = scan_file(filepath, root_dir, rules, encoding)
            all_matches.extend(matches)

    # Sort matches: by file (lexicographically), then start.line, then start.col, then rule_id
    all_matches.sort(key=lambda m: (m['file'], m['start']['line'], m['start']['col'], m['rule_id']))

    return all_matches


def main() -> int:
    """Main entry point."""
    args = parse_arguments()

    # Validate root directory
    root_dir = Path(args.root_dir)
    if not root_dir.exists():
        print(f"Error: Directory does not exist: {args.root_dir}", file=sys.stderr)
        return 1
    if not root_dir.is_dir():
        print(f"Error: Not a directory: {args.root_dir}", file=sys.stderr)
        return 1

    # Load rules
    try:
        rules = load_rules(args.rules)
    except (json.JSONDecodeError, ValueError) as e:
        print(f"Error loading rules: {e}", file=sys.stderr)
        return 1

    # Scan directory
    matches = scan_directory(root_dir, rules, args.encoding)

    # Output JSON Lines
    for match in matches:
        print(json.dumps(match))

    return 0


if __name__ == '__main__':
    sys.exit(main())
