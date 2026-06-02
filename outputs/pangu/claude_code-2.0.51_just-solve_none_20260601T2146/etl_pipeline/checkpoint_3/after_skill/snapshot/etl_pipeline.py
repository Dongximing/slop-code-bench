#!/usr/bin/env python3
"""ETL Pipeline Executor - reads JSON spec from STDIN, validates and executes transformations."""

import argparse
import json
import re
import sys
from typing import Any, Callable


def error(code: str, message: str, path: str) -> None:
    """Output error JSON and exit."""
    print(json.dumps({"status": "error", "error_code": code, "message": f"ETL_ERROR: {message}", "path": path}))
    sys.exit(1)


def _required(step: dict, name: str, index: int):
    """Get required field or error."""
    if name not in step:
        error("SCHEMA_VALIDATION_FAILED", f"Step is missing required '{name}' field", f"pipeline.steps[{index}]")
    return step[name]


def _non_empty_string(value, field_path: str) -> str:
    """Validate and return non-empty trimmed string."""
    if not isinstance(value, str):
        error("SCHEMA_VALIDATION_FAILED", f"'{field_path.split('.')[-1]}' must be a string", field_path)
    trimmed = value.strip()
    if not trimmed:
        error("SCHEMA_VALIDATION_FAILED", f"'{field_path.split('.')[-1]}' field is empty after trimming", field_path)
    return trimmed


def validate_expression(expr: str, index: int, field: str) -> None:
    """Validate expression syntax."""
    if re.search(r'(\+\+|--|\*\*|/\/|<<|>>|\|\||&&|\^%|[<>]\s*[<>])', expr):
        error("BAD_EXPR", f"unsupported operator", f"pipeline.steps[{index}].{field}")
    if not expr.strip():
        error("SCHEMA_VALIDATION_FAILED", f"{field} expression is empty", f"pipeline.steps[{index}].{field}")


def parse_input():
    """Parse JSON from STDIN, return (pipeline, dataset)."""
    try:
        data = json.load(sys.stdin)
    except json.JSONDecodeError as e:
        error("SCHEMA_VALIDATION_FAILED", f"Invalid JSON: {e}", "root")
    pipeline = data.get("pipeline", {})
    dataset = data.get("dataset", [])
    if not isinstance(dataset, list):
        error("SCHEMA_VALIDATION_FAILED", "dataset must be an array", "dataset")
    return pipeline, dataset


# Step Normalizers
def normalize_select(step: dict, index: int) -> dict:
    columns = _required(step, "columns", index)
    if not isinstance(columns, list):
        error("SCHEMA_VALIDATION_FAILED", "'columns' must be an array", f"pipeline.steps[{index}].columns")
    for i, col in enumerate(columns):
        if not isinstance(col, str):
            error("SCHEMA_VALIDATION_FAILED", f"columns[{i}] must be a string", f"pipeline.steps[{index}].columns[{i}]")
        if col == "":
            error("SCHEMA_VALIDATION_FAILED", f"columns[{i}] cannot be empty", f"pipeline.steps[{index}].columns[{i}]")
    return {"op": "select", "columns": columns}


def normalize_filter(step: dict, index: int) -> dict:
    where = _non_empty_string(_required(step, "where", index), f"pipeline.steps[{index}].where")
    validate_expression(where, index, "where")
    return {"op": "filter", "where": where}


def normalize_map(step: dict, index: int) -> dict:
    as_field = _non_empty_string(_required(step, "as", index), f"pipeline.steps[{index}].as")
    expr = _non_empty_string(_required(step, "expr", index), f"pipeline.steps[{index}].expr")
    validate_expression(expr, index, "expr")
    return {"op": "map", "as": as_field, "expr": expr}


def normalize_rename(step: dict, index: int) -> dict:
    has_from_to = "from" in step and "to" in step
    has_mapping = "mapping" in step
    if not (has_from_to or has_mapping):
        error("SCHEMA_VALIDATION_FAILED", "rename step requires either ('from' and 'to') or 'mapping'", f"pipeline.steps[{index}]")
    if has_from_to and has_mapping:
        error("SCHEMA_VALIDATION_FAILED", "rename step cannot have both ('from' and 'to') and 'mapping'", f"pipeline.steps[{index}]")

    if has_from_to:
        mapping = {_non_empty_string(step["from"], f"pipeline.steps[{index}].from"): _non_empty_string(step["to"], f"pipeline.steps[{index}].to")}
    else:
        mapping_obj = _required(step, "mapping", index)
        if not isinstance(mapping_obj, dict):
            error("SCHEMA_VALIDATION_FAILED", "'mapping' must be an object", f"pipeline.steps[{index}].mapping")
        mapping = {}
        for key, value in mapping_obj.items():
            if not isinstance(key, str):
                error("SCHEMA_VALIDATION_FAILED", "mapping keys must be strings", f"pipeline.steps[{index}].mapping")
            if not isinstance(value, str):
                error("SCHEMA_VALIDATION_FAILED", "mapping values must be strings", f"pipeline.steps[{index}].mapping[{json.dumps(key)}]")
            mapping[key] = value
    return {"op": "rename", "mapping": mapping}


def normalize_limit(step: dict, index: int) -> dict:
    n = _required(step, "n", index)
    if not isinstance(n, int):
        error("SCHEMA_VALIDATION_FAILED", "'n' must be an integer", f"pipeline.steps[{index}].n")
    if n < 0:
        error("SCHEMA_VALIDATION_FAILED", "'n' must be >= 0", f"pipeline.steps[{index}].n")
    return {"op": "limit", "n": n}


def normalize_branch(step: dict, index: int) -> dict:
    branches = _required(step, "branches", index)
    if not isinstance(branches, list):
        error("SCHEMA_VALIDATION_FAILED", "'branches' must be an array", f"pipeline.steps[{index}].branches")
    if len(branches) == 0:
        error("SCHEMA_VALIDATION_FAILED", "'branches' must not be empty", f"pipeline.steps[{index}].branches")

    normalized_branches = []
    otherwise_count = 0

    for i, branch in enumerate(branches):
        if not isinstance(branch, dict):
            error("SCHEMA_VALIDATION_FAILED", f"branch[{i}] must be an object", f"pipeline.steps[{index}].branches[{i}]")

        when = branch.get("when")
        if when is None:
            error("SCHEMA_VALIDATION_FAILED", f"branch[{i}] is missing required 'when' field", f"pipeline.steps[{index}].branches[{i}].when")

        is_otherwise = False
        if isinstance(when, str):
            when_stripped = when.strip()
            if when_stripped.lower() == "otherwise":
                is_otherwise = True
                when = "otherwise"
            else:
                validate_expression(when_stripped, index, f"branches[{i}].when")
                when = when_stripped
        else:
            error("SCHEMA_VALIDATION_FAILED", f"branch[{i}].when must be a string", f"pipeline.steps[{index}].branches[{i}].when")

        if is_otherwise:
            otherwise_count += 1
            if otherwise_count > 1:
                error("MALFORMED_STEP", "only one 'otherwise' branch allowed", f"pipeline.steps[{index}].branches")
            if i < len(branches) - 1:
                error("MALFORMED_STEP", "'otherwise' branch must be last", f"pipeline.steps[{index}].branches")

        branch_steps = branch.get("steps", [])
        if not isinstance(branch_steps, list):
            error("SCHEMA_VALIDATION_FAILED", f"branch[{i}].steps must be an array", f"pipeline.steps[{index}].branches[{i}].steps")

        normalized_branch = {"when": when, "steps": branch_steps}
        if "id" in branch:
            id_val = branch["id"]
            if not isinstance(id_val, str):
                error("SCHEMA_VALIDATION_FAILED", f"branch[{i}].id must be a string", f"pipeline.steps[{index}].branches[{i}].id")
            normalized_branch["id"] = id_val.strip()

        normalized_branches.append(normalized_branch)

    merge = step.get("merge", {})
    if not isinstance(merge, dict):
        error("SCHEMA_VALIDATION_FAILED", "'merge' must be an object", f"pipeline.steps[{index}].merge")

    strategy = merge.get("strategy", "concat")
    if not isinstance(strategy, str):
        error("SCHEMA_VALIDATION_FAILED", "'merge.strategy' must be a string", f"pipeline.steps[{index}].merge.strategy")

    strategy_stripped = strategy.strip().lower()
    if strategy_stripped != "concat":
        error("SCHEMA_VALIDATION_FAILED", "'merge.strategy' must be 'concat'", f"pipeline.steps[{index}].merge.strategy")

    return {"op": "branch", "branches": normalized_branches, "merge": {"strategy": strategy_stripped}}


def normalize_step(step: dict, index: int) -> dict:
    """Normalize a single step object."""
    if not isinstance(step, dict):
        error("SCHEMA_VALIDATION_FAILED", "Each step must be an object", f"pipeline.steps[{index}]")
    if "op" not in step:
        error("SCHEMA_VALIDATION_FAILED", "Step is missing required 'op' field", f"pipeline.steps[{index}]")

    op = step["op"]
    if not isinstance(op, str):
        error("SCHEMA_VALIDATION_FAILED", "'op' must be a string", f"pipeline.steps[{index}].op")

    op_normalized = op.strip().lower()
    if not op_normalized:
        error("SCHEMA_VALIDATION_FAILED", "'op' field is empty after trimming", f"pipeline.steps[{index}].op")

    dispatch = {
        "select": normalize_select,
        "filter": normalize_filter,
        "map": normalize_map,
        "rename": normalize_rename,
        "limit": normalize_limit,
        "branch": normalize_branch,
    }
    handler = dispatch.get(op_normalized)
    if handler is None:
        error("UNKNOWN_OP", f"unsupported op '{op_normalized}'", f"pipeline.steps[{index}].op")
    return handler(step, index)


# Expression Parser/Evaluator - Simplified with direct evaluation
TWO_CHAR_OPS = {'==', '!=', '<=', '>=', '||', '&&'}

def tokenize(expr: str):
    """Tokenize expression string into (type, value) tuples."""
    pos = 0
    length = len(expr)
    while pos < length:
        ch = expr[pos]
        if ch in ' \t\n\r':
            pos += 1
            continue

        # Two-character operators
        if pos + 1 < length:
            two_char = expr[pos:pos + 2]
            if two_char in TWO_CHAR_OPS:
                yield ('OPERATOR', two_char)
                pos += 2
                continue

        # Parentheses
        if ch == '(':
            yield ('LPAREN', '(')
            pos += 1
            continue
        if ch == ')':
            yield ('RPAREN', ')')
            pos += 1
            continue

        # Single char operators
        if ch == '!':
            yield ('OPERATOR', '!')
            pos += 1
            continue
        if ch in '*/+-<>|&':
            yield ('OPERATOR', ch)
            pos += 1
            continue

        # String literal
        if ch == '"':
            pos += 1
            result = []
            while pos < length and expr[pos] != '"':
                result.append(expr[pos])
                pos += 1
            if pos >= length:
                raise ValueError("Unterminated string literal")
            pos += 1
            yield ('LITERAL', ''.join(result))
            continue

        # Number literal
        if ch.isdigit() or (ch == '-' and pos + 1 < length and expr[pos + 1].isdigit()):
            start = pos
            if ch == '-':
                pos += 1
            while pos < length and (expr[pos].isdigit() or expr[pos] == '.'):
                pos += 1
            num_str = expr[start:pos]
            yield ('LITERAL', float(num_str) if '.' in num_str else int(num_str))
            continue

        # Keywords
        for keyword in ['true', 'false', 'null']:
            if expr.startswith(keyword, pos):
                pos += len(keyword)
                if keyword == 'true':
                    yield ('LITERAL', True)
                elif keyword == 'false':
                    yield ('LITERAL', False)
                else:
                    yield ('LITERAL', None)
                break
        else:
            # Identifier
            start = pos
            while pos < length and (expr[pos].isalnum() or expr[pos] == '_'):
                pos += 1
            ident = expr[start:pos]
            if ident:
                yield ('IDENTIFIER', ident)
                continue
            raise ValueError(f"Unexpected character: {ch}")


def parse_expression(tokens):
    """Parse expression from tokens and return AST."""
    tokens_iter = iter(tokens)
    current = None

    def _advance():
        nonlocal current
        try:
            current = next(tokens_iter)
        except StopIteration:
            current = ('EOF', None)

    def peek():
        return current

    def expect(expected_type, error_msg):
        if peek()[0] != expected_type:
            raise ValueError(error_msg)
        t = peek()
        _advance()
        return t

    _advance()

    def parse_or():
        node = parse_and()
        while peek()[0] == 'OPERATOR' and peek()[1] == '||':
            _advance()
            node = ('or', node, parse_and())
        return node

    def parse_and():
        node = parse_equality()
        while peek()[0] == 'OPERATOR' and peek()[1] == '&&':
            _advance()
            node = ('and', node, parse_equality())
        return node

    def parse_equality():
        node = parse_comparison()
        while peek()[0] == 'OPERATOR' and peek()[1] in ('==', '!='):
            op = peek()[1]
            _advance()
            node = (op, node, parse_comparison())
        return node

    def parse_comparison():
        node = parse_additive()
        while peek()[0] == 'OPERATOR' and peek()[1] in ('<', '<=', '>', '>='):
            op = peek()[1]
            _advance()
            node = (op, node, parse_additive())
        return node

    def parse_additive():
        node = parse_multiplicative()
        while peek()[0] == 'OPERATOR' and peek()[1] in ('+', '-'):
            op = peek()[1]
            _advance()
            node = (op, node, parse_multiplicative())
        return node

    def parse_multiplicative():
        node = parse_unary()
        while peek()[0] == 'OPERATOR' and peek()[1] in ('*', '/'):
            op = peek()[1]
            _advance()
            node = (op, node, parse_unary())
        return node

    def parse_unary():
        if peek()[0] == 'OPERATOR' and peek()[1] == '!':
            _advance()
            return ('!', parse_unary())
        if peek()[0] == 'OPERATOR' and peek()[1] == '-':
            _advance()
            return ('-', parse_unary())
        return parse_primary()

    def parse_primary():
        t = peek()
        if t[0] == 'LPAREN':
            _advance()
            node = parse_or()
            expect('RPAREN', "Expected ')'")
            return node
        if t[0] == 'IDENTIFIER':
            _advance()
            return ('ident', t[1])
        if t[0] == 'LITERAL':
            _advance()
            return ('literal', t[1])
        raise ValueError(f"Unexpected token: {t}")

    return parse_or()


def evaluate_ast(ast, context):
    """Evaluate AST against context."""
    if ast[0] == 'literal':
        return ast[1]
    if ast[0] == 'ident':
        return context.get(ast[1])
    if ast[0] == '!':
        val = evaluate_ast(ast[1], context)
        return not bool(val)
    if ast[0] == '-':
        val = evaluate_ast(ast[1], context)
        return None if val is None else (-val if isinstance(val, (int, float)) else None)

    left = evaluate_ast(ast[1], context)
    right = evaluate_ast(ast[2], context)

    op = ast[0]
    if op in ('+', '-', '*', '/') and (left is None or right is None):
        return None

    if op == '+':
        return left + right if isinstance(left, (int, float)) and isinstance(right, (int, float)) else None
    if op == '-':
        return left - right if isinstance(left, (int, float)) and isinstance(right, (int, float)) else None
    if op == '*':
        return left * right if isinstance(left, (int, float)) and isinstance(right, (int, float)) else None
    if op == '/':
        return left / right if isinstance(left, (int, float)) and isinstance(right, (int, float)) and right != 0 else None
    if op == '==':
        return left == right if type(left) == type(right) else (False if left is None or right is None else (left == right if isinstance(left, (int, float)) and isinstance(right, (int, float)) else False))
    if op == '!=':
        return left != right if type(left) == type(right) else (True if left is None or right is None else (left != right if isinstance(left, (int, float)) and isinstance(right, (int, float)) else True))
    if op == '<':
        return left < right if isinstance(left, (int, float)) and isinstance(right, (int, float)) else False
    if op == '<=':
        return left <= right if isinstance(left, (int, float)) and isinstance(right, (int, float)) else False
    if op == '>':
        return left > right if isinstance(left, (int, float)) and isinstance(right, (int, float)) else False
    if op == '>=':
        return left >= right if isinstance(left, (int, float)) and isinstance(right, (int, float)) else False
    if op == 'or':
        return bool(left) or bool(right)
    if op == 'and':
        return bool(left) and bool(right)
    return None


def evaluate_expression(expr: str, context: dict) -> Any:
    """Parse and evaluate expression string against context."""
    ast = parse_expression(tokenize(expr))
    return evaluate_ast(ast, context)


# Pipeline Execution
def execute_select(row: dict, step: dict, step_index: int) -> dict:
    columns = step["columns"]
    result = {}
    for col in columns:
        if col not in row:
            error("MISSING_COLUMN", f"column '{col}' not found in row", f"pipeline.steps[{step_index}].columns[{columns.index(col)}]")
        result[col] = row[col]
    return result


def execute_map(row: dict, step: dict, step_index: int) -> dict:
    result = row.copy()
    try:
        result[step["as"]] = evaluate_expression(step["expr"], row)
    except ValueError as e:
        error("BAD_EXPR", f"expression error: {e}", f"pipeline.steps[{step_index}].expr")
    return result


def execute_rename(row: dict, step: dict, step_index: int) -> dict:
    result = row.copy()
    for source, target in step["mapping"].items():
        if source not in result:
            error("MISSING_COLUMN", f"column '{source}' not found in row", f"pipeline.steps[{step_index}].mapping")
        result[target] = result[source]
        del result[source]
    return result


def execute_filter(row: dict, step: dict, step_index: int) -> dict | None:
    try:
        return row if evaluate_expression(step["where"], row) else None
    except ValueError as e:
        error("BAD_EXPR", f"expression error: {e}", f"pipeline.steps[{step_index}].where")


def execute_limit(rows: list, step: dict, step_index: int) -> list:
    return rows[:step["n"]]


def execute_branch(rows: list, step: dict, step_index: int) -> list:
    branches = step["branches"]
    strategy = step["merge"]["strategy"]
    all_results = []
    matched_indices = set()

    for branch_index, branch in enumerate(branches):
        when = branch["when"]
        branch_steps = branch["steps"]

        if when != "otherwise":
            matching_rows = []
            matching_indices = []
            for row_idx, row in enumerate(rows):
                if row_idx in matched_indices:
                    continue
                try:
                    if evaluate_expression(when, row):
                        matching_rows.append(row)
                        matching_indices.append(row_idx)
                except ValueError:
                    error("BAD_EXPR", "expression error in branch condition", f"pipeline.steps[{step_index}].branches[{branch_index}].when")
            matched_indices.update(matching_indices)
        else:
            matching_indices = [i for i in range(len(rows)) if i not in matched_indices]
            matching_rows = [rows[i] for i in matching_indices]
            matched_indices.update(matching_indices)

        branch_result = execute_pipeline(branch_steps, matching_rows)[0] if branch_steps else matching_rows
        all_results.append(branch_result)

    return [row for branch_result in all_results for row in branch_result] if strategy == "concat" else []


def execute_pipeline(steps: list, dataset: list):
    rows_in = len(dataset)
    current_rows = dataset

    handlers = {
        "select": lambda s, i, r: [execute_select(row, s, i) for row in r],
        "map": lambda s, i, r: [execute_map(row, s, i) for row in r],
        "rename": lambda s, i, r: [execute_rename(row, s, i) for row in r],
        "filter": lambda s, i, r: [result for row in r if (result := execute_filter(row, s, i)) is not None],
        "limit": lambda s, i, r: execute_limit(r, s, i),
        "branch": lambda s, i, r: execute_branch(r, s, i),
    }

    for i, step in enumerate(steps):
        op = step["op"]
        handler = handlers.get(op)
        if handler is None:
            error("UNKNOWN_OP", f"unsupported op '{op}'", f"pipeline.steps[{i}].op")
        current_rows = handler(step, i, current_rows)

    return current_rows, rows_in, len(current_rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="ETL Pipeline Executor")
    parser.add_argument("--execute", action="store_true", default=False, help="Execute the pipeline and return data/metrics")
    args = parser.parse_args()

    pipeline, dataset = parse_input()

    if not isinstance(pipeline, dict):
        error("SCHEMA_VALIDATION_FAILED", "pipeline must be an object", "pipeline")

    steps = pipeline.get("steps")
    if steps is None:
        error("SCHEMA_VALIDATION_FAILED", "pipeline is missing required 'steps' field", "pipeline")
    if not isinstance(steps, list):
        error("SCHEMA_VALIDATION_FAILED", "'steps' must be an array", "pipeline.steps")

    if not args.execute:
        print(json.dumps({"status": "ok", "normalized": {"steps": [normalize_step(step, i) for i, step in enumerate(steps)]}}))
    else:
        normalized_steps = [normalize_step(step, i) for i, step in enumerate(steps)]
        result_data, rows_in, rows_out = execute_pipeline(normalized_steps, dataset)
        print(json.dumps({"status": "ok", "data": result_data, "metrics": {"rows_in": rows_in, "rows_out": rows_out}}))


if __name__ == "__main__":
    main()
