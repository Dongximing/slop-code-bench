#!/usr/bin/env python3
"""Command-line code searcher with structure-aware pattern matching."""

import argparse
import dataclasses
import json
import os
import re
import sys
from typing import Any

import tree_sitter
from tree_sitter import Language


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
            sorted_captures = dict(sorted(self.captures.items()))
            result["captures"] = sorted_captures
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

        rule = Rule(
            id=rule_id,
            kind=kind,
            pattern=pattern,
            languages=languages,
            regex_flags=regex_flags
        )

        rules.append(rule)

    return rules


# Global language parsers (lazy loaded)
_language_parsers = {}


def get_parser(language: str) -> 'LanguageParser':
    if language not in _language_parsers:
        _language_parsers[language] = LanguageParser(language)
    return _language_parsers[language]


class LanguageParser:
    """Wrapper for tree-sitter language parsers."""

    # Module paths for language parsers
    LANG_MODULES = {
        "python": "tree_sitter_python",
        "javascript": "tree_sitter_javascript",
        "cpp": "tree_sitter_cpp"
    }

    def __init__(self, language: str):
        if language not in self.LANG_MODULES:
            raise ValueError(f"Unsupported language: {language}")

        self.language = language
        self._parser = tree_sitter.Parser()

        try:
            # Import the language-specific module
            module = __import__(self.LANG_MODULES[language])
            # Get the language from the module
            lang = module.language  # This is a tree-sitter Language object
            self._parser.language = lang
        except Exception as e:
            raise RuntimeError(f"Could not load tree-sitter parser for {language}: {e}")

    def parse(self, code: str) -> tree_sitter.Tree:
        return self._parser.parse(bytes(code, "utf-8"))


# Metavariable pattern: $NAME or $NAME?
METAVAR_RE = re.compile(r'(?<!\\)\$([A-Z_a-z][A-Z_a-z0-9]*)(\?)?')


class PatternMatcher:
    """
    Performs pattern matching using tree-sitter.
    """

    def __init__(self, language: str):
        self.language = language
        self.parser = get_parser(language)

    def match(self, rule: Rule, code: str, filename: str) -> list[Match]:
        """Find all matches of a pattern rule in the given code."""
        if rule.kind != "pattern":
            return []

        pattern = rule.pattern

        # Check if this looks like a function call
        if '(' in pattern and pattern.endswith(')'):
            return self._match_function_call_pattern(rule, pattern, code, filename)
        else:
            # Simple text pattern with optional metavariables
            return self._match_text_pattern(rule, pattern, code, filename)

    def _match_function_call_pattern(self, rule: Rule, pattern: str, code: str,
                                      filename: str) -> list[Match]:
        """Match function call patterns like $X($Y) or console.log($TAG, $TAG)."""
        matches = []
        tree = self.parser.parse(code)

        # Walk the AST to find function calls
        def walk_node(node: tree_sitter.Node):
            if node.type in ("call_expression", "call"):
                match = self._match_call_against_pattern(node, pattern, code)
                if match:
                    matches.append(match)
            for child in node.children:
                walk_node(child)

        walk_node(tree.root_node)

        # Sort by position
        matches.sort(key=lambda m: (m.line, m.col, m.end_line, m.end_col))
        return matches

    def _match_call_against_pattern(self, call_node: tree_sitter.Node, pattern: str,
                                     code: str) -> Match | None:
        """Match a single call expression against the pattern."""
        # Parse the pattern
        # Pattern is like "console.log($TAG, $TAG)" or "$X($Y)"
        if '(' not in pattern or not pattern.endswith(')'):
            return None

        # Split into function name and arguments
        paren_idx = pattern.index('(')
        func_pattern = pattern[:paren_idx].strip()
        args_pattern_str = pattern[paren_idx+1:-1]  # Remove outer parens

        # Get the function name node
        func_node = call_node.child_by_field_name("function")
        if func_node is None and call_node.children:
            func_node = call_node.children[0]

        if func_node is None:
            return None

        func_text = code[func_node.start_byte:func_node.end_byte]

        # Check if function name matches
        if func_pattern.startswith('$'):
            # Metavariable - any function name matches
            pass
        elif func_text != func_pattern:
            return None

        # Get arguments
        args_node = None
        for child in call_node.children:
            if child.type in ("argument_list", "arguments"):
                args_node = child
                break

        if args_node is None:
            if args_pattern_str.strip():
                return None  # Pattern expects arguments but none present
            # No arguments in pattern and source
            return self._create_match(call_node, pattern, code, {})

        actual_args = []
        for child in args_node.children:
            if child.type not in ("(", ")", ",", "argument_list", "arguments"):
                actual_args.append(child)

        # Parse argument pattern
        arg_pattern_names, arg_pattern_parts = self._parse_arg_pattern(args_pattern_str)

        if len(actual_args) != len(arg_pattern_names):
            return None

        # Extract captures
        captures = {}
        arg_mappings = {}  # For duplicate metavariable checking

        for i, name in enumerate(arg_pattern_names):
            if i >= len(actual_args):
                return None

            arg_node = actual_args[i]
            arg_text = code[arg_node.start_byte:arg_node.end_byte]

            if name:  # Non-empty if it's a real metavariable
                if name not in captures:
                    captures[name] = {
                        "text": arg_text,
                        "ranges": []
                    }
                captures[name]["ranges"].append({
                    "start": {"line": arg_node.start_point[0] + 1, "col": arg_node.start_point[1] + 1},
                    "end": {"line": arg_node.end_point[0] + 1, "col": arg_node.end_point[1]}
                })

                # Track for duplicate checking
                if name not in arg_mappings:
                    arg_mappings[name] = []
                arg_mappings[name].append(arg_text)

        # Check duplicate metavariable consistency
        for name, texts in arg_mappings.items():
            if len(set(texts)) > 1:
                return None

        return self._create_match(call_node, pattern, code, captures)

    def _parse_arg_pattern(self, args_str: str) -> tuple[list[str], list[str]]:
        """Parse argument pattern, returning list of metavariable names (empty for literals)."""
        args_str = args_str.strip()
        if not args_str:
            return [], []

        # Split by comma
        parts = self._split_by_comma(args_str)
        names = []
        for part in parts:
            part = part.strip()
            # Check if this is a quoted string literal (literal, not metavariable)
            if (part.startswith('"') and part.endswith('"')) or \
               (part.startswith("'") and part.endswith("'")) or \
               (part.startswith('`') and part.endswith('`')):
                names.append('')  # Literal
            elif part.startswith('$'):
                # Metavariable
                name = part[1:]  # Remove $
                if name.endswith('?'):
                    name = name[:-1]
                names.append(name)
            else:
                names.append('')  # Literal

        return names, parts

    def _split_by_comma(self, s: str) -> list[str]:
        """Split by comma, respecting nested parentheses/square brackets."""
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
        """Create a Match object from a tree-sitter node."""
        start_point = node.start_point
        end_point = node.end_point
        node_text = code[node.start_byte:node.end_byte]

        # Format captures with $ prefix
        formatted_captures = {}
        for k, v in captures.items():
            formatted_captures[f"${k}"] = v

        return Match(
            rule_id="",
            file="",
            language=self.language,
            line=start_point[0] + 1,
            col=start_point[1] + 1,
            end_line=end_point[0] + 1,
            end_col=end_point[1],
            text=node_text,
            captures=formatted_captures
        )

    def _match_text_pattern(self, rule: Rule, pattern: str, code: str,
                            filename: str) -> list[Match]:
        """
        Match text patterns with metavariables using tree-sitter.
        This handles patterns like "print($GREETING)" where the pattern itself
        contains the exact syntax we're searching for.
        """
        matches = []

        # For patterns with metavariables, we need to:
        # 1. Find the pattern text in the code
        # 2. Extract the metavariable content
        # 3. Verify with tree-sitter that it's valid code (not in comment/string)

        # Extract metavariable positions
        metavars = list(METAVAR_RE.finditer(pattern))

        if not metavars:
            # No metavariables - simple text search (handled elsewhere)
            return []

        # Find the first metavariable to establish base position
        # The pattern is a function call or other expression
        if '(' in pattern and pattern.endswith(')'):
            # Use function call AST matching
            return self._match_function_call_pattern(rule, pattern, code, filename)

        # For non-function-call patterns, use text search + AST validation
        tree = self.parser.parse(code)

        start = 0
        while True:
            # Find next occurrence of the literal text parts
            pos = self._find_next_match(code, pattern, metavars, start, tree)
            if pos == -1:
                break

            # Check it's not in a comment or string using tree-sitter
            if not self._is_in_code(code, pos, pos + len(pattern), tree):
                start = pos + 1
                continue

            line = code[:pos].count('\n')
            col = len(code[:pos]) - code[:pos].rfind('\n')
            end_line = code[:pos + len(pattern)].count('\n')
            end_col = len(code[:pos + len(pattern)]) - code[:pos + len(pattern)].rfind('\n')

            # Extract captures
            captures = self._extract_captures(pattern, code, pos, metavars)

            matches.append(Match(
                rule_id=rule.id,
                file=filename,
                language=self.language,
                line=line + 1,
                col=col,
                end_line=end_line + 1,
                end_col=end_col,
                text=code[pos:pos + len(pattern)],
                captures=captures
            ))

            start = pos + 1

        return matches

    def _find_next_match(self, code: str, pattern: str,
                         metavars: list[re.Match], start: int,
                         tree: tree_sitter.Tree) -> int:
        """Find next match of pattern in code, handling metavariables."""
        if not metavars:
            return code.find(pattern, start)

        # Find the first non-metavariable segment
        pos = 0
        while pos < len(pattern):
            # Find next metavariable or end
            remaining = pattern[pos:]
            metavar_match = METAVAR_RE.search(remaining)

            if metavar_match is None:
                # Rest of pattern is literal
                literal = remaining
                idx = code.find(literal, start)
                if idx == -1:
                    return -1

                # Verify preceding part matches
                if pos == 0:
                    return idx

                # Check preceding literal
                prev_literal = pattern[:pos]
                check_pos = code.find(prev_literal, start)
                if check_pos == -1 or check_pos + len(prev_literal) > idx:
                    return -1

                return check_pos

            else:
                # There's a metavariable
                if metavar_match.start() > 0:
                    # There's literal text before this metavariable
                    literal = remaining[:metavar_match.start()]
                    idx = code.find(literal, start)
                    if idx == -1:
                        return -1

                    # Calculate position after this literal
                    pos = metavar_match.end()
                    start = idx + len(literal)
                else:
                    # Metavariable at start
                    pos = metavar_match.end()
                    # Skip - we can't search for metavariable content directly

                    # Continue to next literal
                    continue

        return -1

    def _is_in_code(self, code: str, start: int, end: int,
                    tree: tree_sitter.Tree) -> bool:
        """Check if the range is in actual code (not comment or string)."""
        # Use tree-sitter to find nodes covering this range
        root_node = tree.root_node

        # Walk from root to find the node at the start position
        node = root_node
        while node:
            if node.start_byte <= start < node.end_byte:
                # This node contains our range
                # Check if it's a comment or string
                if node.type in ("comment", "string", "string_literal"):
                    return False

                # Check parent
                parent = self._find_parent(root_node, node)
                if parent and parent.type in ("comment", "string", "string_literal"):
                    return False

                return True

            # Navigate to child containing start
            found = False
            for child in node.children:
                if child.start_byte <= start < child.end_byte:
                    node = child
                    found = True
                    break
            if not found:
                break

        return True

    def _find_parent(self, root: tree_sitter.Node, target: tree_sitter.Node) -> tree_sitter.Node | None:
        """Find parent of target node."""
        for child in root.children:
            if child == target:
                return root
            if child.start_byte <= target.start_byte < child.end_byte:
                result = self._find_parent(child, target)
                if result:
                    return result
        return None

    def _extract_captures(self, pattern: str, code: str,
                          match_start: int,
                          metavars: list[re.Match]) -> dict:
        """Extract capture groups from a pattern match."""
        captures = {}

        for m in metavars:
            metavar_name = m.group(1)
            metavar_start = m.start()
            metavar_end = m.end()

            # Calculate position in code
            code_start = match_start + metavar_start
            code_end = code_start + (metavar_end - metavar_start)

            # Skip $ prefix for the capture text
            actual_text_start = code_start + 1

            # For optional metavariables (?), skip the ? too
            if m.group(2) == '?':
                actual_text_end = code_end - 1  # Skip ?
            else:
                actual_text_end = code_end

            text = code[actual_text_start:actual_text_end]

            key = f"${metavar_name}"
            if key not in captures:
                captures[key] = {
                    "text": text,
                    "ranges": []
                }

            # Find all occurrences of this metavariable in the pattern
            all_matches = list(METAVAR_RE.finditer(pattern))
            all_names = [mm.group(1) for mm in all_matches]

            if metavar_name in all_names:
                # Find all positions of this metavariable in the matched code
                # The matched code is at match_start to match_start + len(pattern)
                matched_code = code[match_start:match_start + len(pattern)]

                for idx, mm in enumerate(all_matches):
                    if mm.group(1) == metavar_name:
                        # This occurrence
                        occ_start = match_start + mm.start() + 1  # Skip $
                        occ_end = occ_start + (mm.end() - mm.start())
                        if m.group(2) == '?':
                            occ_end -= 1  # Skip ?

                        occ_text = code[occ_start:occ_end]
                        line = code[:occ_start].count('\n')
                        col = len(code[:occ_start]) - code[:occ_start].rfind('\n')
                        end_line = code[:occ_end].count('\n')
                        end_col = len(code[:occ_end]) - code[:occ_end].rfind('\n')

                        captures[key]["ranges"].append({
                            "start": {"line": line + 1, "col": col},
                            "end": {"line": end_line + 1, "col": end_col}
                        })

        return captures


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
        self.parsers: dict[str, PatternMatcher] = {}

    def _get_parser(self, language: str) -> PatternMatcher:
        if language not in self.parsers:
            self.parsers[language] = PatternMatcher(language)
        return self.parsers[language]

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
                    elif rule.kind == "pattern":
                        try:
                            parser = self._get_parser(language)
                            pattern_matches = parser.match(rule, content, rel_path)
                            matches.extend(pattern_matches)
                        except Exception as e:
                            print(f"Warning: Pattern matching failed for {rel_path}: {e}", file=sys.stderr)
                    else:
                        # exact match
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

        # Sort matches deterministically
        matches.sort(key=lambda m: (m.file, m.line, m.col, m.end_line, m.end_col, m.rule_id))
        return matches


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Code searcher with structure-aware pattern matching",
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
