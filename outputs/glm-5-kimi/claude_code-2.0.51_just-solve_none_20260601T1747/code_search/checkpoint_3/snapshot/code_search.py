#!/usr/bin/env python3

import argparse
import json
import os
import re
from pathlib import Path
from typing import List, Dict, Any, Tuple, Optional, Set
import tree_sitter_python
import tree_sitter_javascript
import tree_sitter_cpp
from tree_sitter import Language, Parser, Node, Tree


EXTENSION_TO_LANGUAGE = {
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

VALID_LANGUAGES = {'python', 'javascript', 'cpp'}

LANGUAGE_MODULES = {
    'python': tree_sitter_python,
    'javascript': tree_sitter_javascript,
    'cpp': tree_sitter_cpp,
}


def parse_args():
    parser = argparse.ArgumentParser(description='Code searcher for Python, JavaScript, and C++ codebases')
    parser.add_argument('root_dir', help='Path to the codebase to scan')
    parser.add_argument('--rules', required=True, help='Path to JSON rules file')
    parser.add_argument('--encoding', default='utf-8', help='File encoding (default: utf-8)')
    return parser.parse_args()


def load_rules(rules_path: str) -> List[Dict[str, Any]]:
    with open(rules_path, 'r', encoding='utf-8') as f:
        return json.load(f)


def validate_rules(rules: List[Dict[str, Any]]) -> None:
    seen_ids = set()
    valid_flags = {'i', 'm', 's'}

    for rule in rules:
        if 'id' not in rule or not rule['id']:
            raise ValueError("Rule missing 'id' field or 'id' is empty")
        if 'kind' not in rule:
            raise ValueError(f"Rule '{rule.get('id', 'unknown')}' missing 'kind' field")
        if 'pattern' not in rule or not rule['pattern']:
            raise ValueError(f"Rule '{rule['id']}' missing 'pattern' field or 'pattern' is empty")

        if rule['id'] in seen_ids:
            raise ValueError(f"Duplicate rule id: '{rule['id']}'")
        seen_ids.add(rule['id'])

        if rule['kind'] not in ('exact', 'regex', 'pattern'):
            raise ValueError(f"Rule '{rule['id']}' has invalid kind: '{rule['kind']}'")

        languages = rule.get('languages', list(VALID_LANGUAGES))
        if not isinstance(languages, list):
            raise ValueError(f"Rule '{rule['id']}' languages must be a list")
        for lang in languages:
            if lang not in VALID_LANGUAGES:
                raise ValueError(f"Rule '{rule['id']}' has unsupported language: '{lang}'")

        if rule['kind'] == 'regex':
            regex_flags = rule.get('regex_flags', [])
            if not isinstance(regex_flags, list):
                raise ValueError(f"Rule '{rule['id']}' regex_flags must be a list")
            for flag in regex_flags:
                if flag not in valid_flags:
                    raise ValueError(f"Rule '{rule['id']}' has invalid regex flag: '{flag}'")
            flags = 0
            for flag in regex_flags:
                if flag == 'i':
                    flags |= re.IGNORECASE
                elif flag == 'm':
                    flags |= re.MULTILINE
                elif flag == 's':
                    flags |= re.DOTALL
            try:
                re.compile(rule['pattern'], flags)
            except re.error as e:
                raise ValueError(f"Rule '{rule['id']}' has invalid regex pattern: {e}")


def get_files(root_dir: str) -> List[Tuple[Path, str]]:
    files = []
    root = Path(root_dir)
    for path in root.rglob('*'):
        if path.is_file():
            ext = path.suffix.lower()
            if ext in EXTENSION_TO_LANGUAGE:
                files.append((path, EXTENSION_TO_LANGUAGE[ext]))
    return sorted(files, key=lambda x: x[0])


def calculate_position(content: str, idx: int) -> Tuple[int, int]:
    line_num = content.count('\n', 0, idx) + 1
    if idx == 0:
        line_start = 0
    else:
        last_newline = content.rfind('\n', 0, idx)
        line_start = last_newline + 1 if last_newline >= 0 else 0
    col = idx - line_start + 1
    return line_num, col


def find_exact_matches(content: str, pattern: str, rule_id: str, file_path: str, language: str) -> List[Dict[str, Any]]:
    matches = []
    pos = 0

    while True:
        idx = content.find(pattern, pos)
        if idx == -1:
            break

        start_line, start_col = calculate_position(content, idx)
        end_idx = idx + len(pattern)
        # End position should be the position AFTER the last character
        # calculate_position gives the column of the character AT that index
        # So we calculate for end_idx - 1 to get the last character's column,
        # then add 1 to get the exclusive end column
        if len(pattern) > 0:
            last_char_idx = end_idx - 1
            end_line, last_col = calculate_position(content, last_char_idx)
            end_col = last_col + 1
        else:
            end_line, end_col = start_line, start_col

        matches.append({
            'rule_id': rule_id,
            'file': file_path,
            'language': language,
            'start': {'line': start_line, 'col': start_col},
            'end': {'line': end_line, 'col': end_col},
            'match': pattern
        })

        pos = idx + 1

    return matches


def find_regex_matches(content: str, pattern: str, flags: int, rule_id: str, file_path: str, language: str) -> List[Dict[str, Any]]:
    matches = []
    regex = re.compile(pattern, flags)

    for match in regex.finditer(content):
        start_idx = match.start()
        end_idx = match.end()
        match_text = match.group()

        start_line, start_col = calculate_position(content, start_idx)
        end_line, end_col = calculate_position(content, end_idx)

        matches.append({
            'rule_id': rule_id,
            'file': file_path,
            'language': language,
            'start': {'line': start_line, 'col': start_col},
            'end': {'line': end_line, 'col': end_col},
            'match': match_text
        })

    return matches


# ============================================================
# Pattern matching with tree-sitter
# ============================================================

METAVAR_RE = re.compile(r'\$([A-Za-z_][A-Za-z0-9_]*)(\?)?')


def _get_parser(lang: str) -> Parser:
    """Create a tree-sitter Parser for the given language."""
    mod = LANGUAGE_MODULES[lang]
    return Parser(Language(mod.language()))


def _node_text(node: Node, src: bytes) -> str:
    return src[node.start_byte:node.end_byte].decode('utf-8')


def _node_text_str(node: Node, content: str) -> str:
    return content[node.start_byte:node.end_byte]


def _collect_all_nodes(root: Node) -> List[Node]:
    """Return every node in the tree (depth-first, pre-order)."""
    result = []
    stack = [root]
    while stack:
        n = stack.pop()
        result.append(n)
        for child in reversed(n.children):
            stack.append(child)
    return result


def _pattern_to_placeholder(pattern: str) -> Tuple[str, List[Tuple[str, bool]]]:
    """
    Convert a pattern with metavariables into valid code for parsing.
    Returns (placeholder_code, ordered_var_info) where each var_info is (name, is_optional).
    """
    ordered_vars = []

    # Replace $$ with a sentinel that won't be touched
    working = pattern.replace('$$', '\x00DOLLAR\x00')

    def _replace(m):
        name = '$' + m.group(1)
        optional = m.group(2) == '?'
        ordered_vars.append((name, optional))
        return '_MV_' + m.group(1)

    working = METAVAR_RE.sub(_replace, working)
    # Restore literal $
    working = working.replace('\x00DOLLAR\x00', '$')
    return working, ordered_vars


def _map_metavar_positions_in_pattern(pattern_code: str) -> List[Tuple[int, int, str]]:
    """
    In the *placeholder* pattern code, find the byte ranges that correspond
    to each metavariable placeholder (e.g. _MV_X).  Returns list of
    (start_byte, end_byte, var_name).
    """
    results = []
    for m in re.finditer(r'_MV_([A-Za-z_][A-Za-z0-9_]*)', pattern_code):
        results.append((m.start(), m.end(), '$' + m.group(1)))
    return results


def _match_trees(p_node: Node, s_node: Node,
                 p_src: bytes, s_src: bytes,
                 var_map: Dict[str, List[int]]) -> bool:
    """
    Recursively match pattern tree node `p_node` against source tree node `s_node`.

    `var_map` maps metavar name -> list of (source_node_ids that have been captured).
    We record the start_byte/end_byte of captured source nodes in a separate dict
    returned alongside the boolean.

    Actually, let's return (bool, captures_dict) where captures_dict maps
    var_name -> list of (start_byte, end_byte, text).
    """
    pass  # implemented below


def _match_trees_impl(pn: Node, sn: Node,
                      p_bytes: bytes, s_bytes: bytes,
                      captures: Dict[str, List[Tuple[int, int, str]]],
                      var_name_for_node: Optional[str]) -> bool:
    """
    Recursive structural match.
    captures is mutated in-place: var_name -> [(start, end, text), ...]
    var_name_for_node: if not None, this pattern node is a metavar placeholder.
    """
    if var_name_for_node is not None:
        # Metavariable — capture the entire source subtree
        text = _node_text(sn, s_bytes)
        if var_name_for_node not in captures:
            captures[var_name_for_node] = [(sn.start_byte, sn.end_byte, text)]
        else:
            # Must match previous captures of same var
            prev_text = captures[var_name_for_node][0][2]
            if text != prev_text:
                return False
            captures[var_name_for_node].append((sn.start_byte, sn.end_byte, text))
        return True

    # Non-metavar: node types must match
    if pn.type != sn.type:
        return False

    # If both are leaf nodes (no children), text must match exactly
    if not pn.children and not sn.children:
        return _node_text(pn, p_bytes) == _node_text(sn, s_bytes)

    # If pattern has no children but source does, check text equality
    if not pn.children:
        return _node_text(pn, p_bytes) == _node_text(sn, s_bytes)

    # Both have children — structural match
    if len(pn.children) != len(sn.children):
        return False

    for pc, sc in zip(pn.children, sn.children):
        if not _match_trees_impl(pc, sc, p_bytes, s_bytes, captures, None):
            return False
    return True


def _find_metavar_leaves(root: Node, p_bytes: bytes) -> Dict[str, List[Node]]:
    """
    Find all leaf nodes in the pattern tree that correspond to metavar placeholders.
    Returns dict of var_name -> [list of pattern nodes].
    """
    result: Dict[str, List[Node]] = {}
    stack = [root]
    while stack:
        n = stack.pop()
        text = _node_text(n, p_bytes)
        m = re.fullmatch(r'_MV_([A-Za-z_][A-Za-z0-9_]*)', text)
        if m:
            var_name = '$' + m.group(1)
            result.setdefault(var_name, []).append(n)
        else:
            for child in n.children:
                stack.append(child)
    return result


def _map_pattern_nodes_to_vars(root: Node, p_bytes: bytes) -> Dict[int, str]:
    """
    Map node id -> var_name for all metavar placeholder nodes in the pattern tree.
    """
    result = {}
    stack = [root]
    while stack:
        n = stack.pop()
        text = _node_text(n, p_bytes)
        m = re.fullmatch(r'_MV_([A-Za-z_][A-Za-z0-9_]*)', text)
        if m:
            result[id(n)] = '$' + m.group(1)
        for child in n.children:
            stack.append(child)
    return result


def _structural_match(pn: Node, sn: Node,
                      p_bytes: bytes, s_bytes: bytes,
                      var_node_map: Dict[int, str],
                      captures: Dict[str, List[Tuple[int, int, str]]]) -> bool:
    """
    Match pattern AST against source AST with metavariable capture.
    """
    # Check if this pattern node is a metavar
    var_name = var_node_map.get(id(pn))
    if var_name is not None:
        text = _node_text(sn, s_bytes)
        if var_name in captures:
            prev = captures[var_name][0][2]
            if text != prev:
                return False
            captures[var_name].append((sn.start_byte, sn.end_byte, text))
        else:
            captures[var_name] = [(sn.start_byte, sn.end_byte, text)]
        return True

    # Types must match
    if pn.type != sn.type:
        return False

    # Leaf comparison
    if not pn.children:
        return _node_text(pn, p_bytes) == _node_text(sn, s_bytes)

    # Children must match one-to-one
    if len(pn.children) != len(sn.children):
        return False

    for pc, sc in zip(pn.children, sn.children):
        if not _structural_match(pc, sc, p_bytes, s_bytes, var_node_map, captures):
            return False
    return True


def find_pattern_matches(content: str, pattern: str, rule_id: str, file_path: str,
                         language: str) -> List[Dict[str, Any]]:
    """
    Find all matches of a pattern rule in source content.
    """
    matches = []

    # Step 1: Build placeholder pattern
    placeholder, ordered_vars = _pattern_to_placeholder(pattern)

    # Step 2: Parse pattern
    try:
        p_parser = _get_parser(language)
        p_tree = p_parser.parse(bytes(placeholder, 'utf-8'))
    except Exception:
        return matches

    p_bytes = bytes(placeholder, 'utf-8')

    # Check pattern parsed correctly
    if p_tree.root_node.has_error:
        # Try to find the first non-error child
        pass  # We'll still try matching what we can

    # Find the meaningful root node of the pattern (skip module/statement wrappers)
    p_root = _get_pattern_root(p_tree.root_node, language, p_bytes)
    if p_root is None:
        return matches

    # Map pattern nodes to metavar names
    var_node_map = _map_pattern_nodes_to_vars(p_root, p_bytes)

    # Step 3: Parse source
    try:
        s_parser = _get_parser(language)
        s_tree = s_parser.parse(bytes(content, 'utf-8'))
    except Exception:
        return matches

    s_bytes = bytes(content, 'utf-8')

    # Step 4: Try matching at every source node
    seen = set()  # avoid duplicate matches at same (start, end)
    for sn in _collect_all_nodes(s_tree.root_node):
        captures: Dict[str, List[Tuple[int, int, str]]] = {}
        if _structural_match(p_root, sn, p_bytes, s_bytes, var_node_map, captures):
            key = (sn.start_byte, sn.end_byte)
            if key in seen:
                continue
            seen.add(key)

            # Match end position is inclusive (column of the last character)
            # tree-sitter end_byte is exclusive, so use end_byte - 1 for inclusive match end
            start_line, start_col = calculate_position(content, sn.start_byte)
            if sn.end_byte > sn.start_byte:
                end_line, end_col = calculate_position(content, sn.end_byte - 1)
            else:
                end_line, end_col = start_line, start_col
            match_text = content[sn.start_byte:sn.end_byte]

            formatted_captures = {}
            for var_name, positions in captures.items():
                ranges = []
                for (sb, eb, _) in positions:
                    sl, sc = calculate_position(content, sb)
                    el, ec = calculate_position(content, eb)
                    ranges.append({
                        'start': {'line': sl, 'col': sc},
                        'end': {'line': el, 'col': ec}
                    })
                formatted_captures[var_name] = {
                    'text': positions[0][2],
                    'ranges': ranges
                }

            matches.append({
                'rule_id': rule_id,
                'file': file_path,
                'language': language,
                'start': {'line': start_line, 'col': start_col},
                'end': {'line': end_line, 'col': end_col},
                'match': match_text,
                'captures': dict(sorted(formatted_captures.items()))
            })

    return matches


def _get_pattern_root(root: Node, language: str, p_bytes: bytes) -> Optional[Node]:
    """
    Get the meaningful root node of a pattern tree, stripping language-specific wrappers.
    """
    # For most languages, the root is 'module' or 'program' which wraps an
    # 'expression_statement' or similar. We want the inner content.
    if language == 'python':
        # Python: module -> expression_statement -> (actual node)
        # or module -> simple_statement -> (actual node if multiple)
        if root.type == 'module' and root.children:
            child = root.children[0]
            if child.type == 'expression_statement' and child.children:
                return child.children[0]
            # For simple statements like assignments, returns
            return child
    elif language == 'javascript':
        # JS: program -> expression_statement -> (actual node)
        if root.type == 'program' and root.children:
            child = root.children[0]
            if child.type == 'expression_statement' and child.children:
                return child.children[0]
            return child
    elif language == 'cpp':
        # C++: translation_unit -> ...
        if root.type == 'translation_unit' and root.children:
            child = root.children[0]
            if child.type == 'expression_statement' and child.children:
                return child.children[0]
            return child

    return root


def main():
    args = parse_args()

    rules = load_rules(args.rules)
    validate_rules(rules)

    files = get_files(args.root_dir)
    all_matches = []

    for file_path, file_language in files:
        try:
            with open(file_path, 'r', encoding=args.encoding) as f:
                content = f.read()
        except (UnicodeDecodeError, IOError):
            continue

        rel_path = file_path.relative_to(args.root_dir)
        rel_path_str = str(rel_path).replace(os.sep, '/')

        for rule in rules:
            rule_id = rule['id']
            kind = rule['kind']
            pattern = rule['pattern']
            languages = rule.get('languages', list(VALID_LANGUAGES))

            if file_language not in languages:
                continue

            if kind == 'exact':
                matches = find_exact_matches(content, pattern, rule_id, rel_path_str, file_language)
            elif kind == 'regex':
                flags = 0
                for flag in rule.get('regex_flags', []):
                    if flag == 'i':
                        flags |= re.IGNORECASE
                    elif flag == 'm':
                        flags |= re.MULTILINE
                    elif flag == 's':
                        flags |= re.DOTALL
                matches = find_regex_matches(content, pattern, flags, rule_id, rel_path_str, file_language)
            else:  # kind == 'pattern'
                matches = find_pattern_matches(content, pattern, rule_id, rel_path_str, file_language)

            all_matches.extend(matches)

    # Sort by file, start position, end position (earlier end first), then rule_id
    all_matches.sort(key=lambda m: (
        m['file'],
        m['start']['line'],
        m['start']['col'],
        m['end']['line'],
        m['end']['col'],
        m['rule_id']
    ))

    for match in all_matches:
        print(json.dumps(match, separators=(',', ':')))


if __name__ == '__main__':
    main()
