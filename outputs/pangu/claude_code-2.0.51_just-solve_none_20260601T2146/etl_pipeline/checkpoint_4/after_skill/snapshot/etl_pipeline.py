#!/usr/bin/env python3
"""ETL Pipeline Executor - reads JSON spec from STDIN, validates and executes transformations."""

import argparse
import json
import re
import sys


def error(code, message, path):
    print(json.dumps({"status": "error", "error_code": code, "message": f"ETL_ERROR: {message}", "path": path}))
    sys.exit(1)


def _required(step, name, index):
    if name not in step:
        error("SCHEMA_VALIDATION_FAILED", f"Step is missing required '{name}' field", f"pipeline.steps[{index}]")
    return step[name]


def _non_empty_string(value, field_path):
    if not isinstance(value, str):
        error("SCHEMA_VALIDATION_FAILED", f"'{field_path.split('.')[-1]}' must be a string", field_path)
    trimmed = value.strip()
    if not trimmed:
        error("SCHEMA_VALIDATION_FAILED", f"'{field_path.split('.')[-1]}' field is empty after trimming", field_path)
    return trimmed


def validate_expression(expr, index, field):
    if re.search(r'(\+\+|--|\*\*|/\/|<<|>>|\|\||&&|\^%|[<>]\s*[<>])', expr):
        error("BAD_EXPR", "unsupported operator", f"pipeline.steps[{index}].{field}")
    if not expr.strip():
        error("SCHEMA_VALIDATION_FAILED", f"{field} expression is empty", f"pipeline.steps[{index}].{field}")


IDENTIFIER_PATTERN = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')


def validate_definition_name(name, path):
    if not isinstance(name, str):
        error("SCHEMA_VALIDATION_FAILED", "definition name must be a string", path)
    if not IDENTIFIER_PATTERN.match(name):
        error("SCHEMA_VALIDATION_FAILED", f"definition name '{name}' must match ^{IDENTIFIER_PATTERN.pattern}$", path)
    if name.lower() == 'params':
        error("SCHEMA_VALIDATION_FAILED", "definition name 'params' is reserved", path)


def normalize_defs(defs):
    normalized = {}
    for name, def_obj in defs.items():
        validate_definition_name(name, f"defs[{name}]")
        if not isinstance(def_obj, dict):
            error("SCHEMA_VALIDATION_FAILED", f"definition '{name}' must be an object", f"defs[{name}]")
        if "steps" not in def_obj:
            error("SCHEMA_VALIDATION_FAILED", f"definition '{name}' is missing required 'steps' field", f"defs[{name}]")
        steps = def_obj["steps"]
        if not isinstance(steps, list):
            error("SCHEMA_VALIDATION_FAILED", f"definition '{name}'.steps must be an array", f"defs[{name}].steps")
        normalized[name] = {"steps": [normalize_step(step, i) for i, step in enumerate(steps)]}
    return normalized


def parse_input():
    try:
        data = json.load(sys.stdin)
    except json.JSONDecodeError as e:
        error("SCHEMA_VALIDATION_FAILED", f"Invalid JSON: {e}", "root")
    defs = data.get("defs", {})
    pipeline = data.get("pipeline", {})
    dataset = data.get("dataset", [])
    if not isinstance(defs, dict):
        error("SCHEMA_VALIDATION_FAILED", "defs must be an object", "defs")
    if not isinstance(dataset, list):
        error("SCHEMA_VALIDATION_FAILED", "dataset must be an array", "dataset")
    return normalize_defs(defs), pipeline, dataset


# Step Normalizers
def normalize_select(step, index):
    columns = _required(step, "columns", index)
    if not isinstance(columns, list):
        error("SCHEMA_VALIDATION_FAILED", "'columns' must be an array", f"pipeline.steps[{index}].columns")
    for i, col in enumerate(columns):
        if not isinstance(col, str):
            error("SCHEMA_VALIDATION_FAILED", f"columns[{i}] must be a string", f"pipeline.steps[{index}].columns[{i}]")
        if col == "":
            error("SCHEMA_VALIDATION_FAILED", f"columns[{i}] cannot be empty", f"pipeline.steps[{index}].columns[{i}]")
    return {"op": "select", "columns": columns}


def normalize_filter(step, index):
    where = _non_empty_string(_required(step, "where", index), f"pipeline.steps[{index}].where")
    validate_expression(where, index, "where")
    return {"op": "filter", "where": where}


def normalize_map(step, index):
    as_field = _non_empty_string(_required(step, "as", index), f"pipeline.steps[{index}].as")
    expr = _non_empty_string(_required(step, "expr", index), f"pipeline.steps[{index}].expr")
    validate_expression(expr, index, "expr")
    return {"op": "map", "as": as_field, "expr": expr}


def normalize_rename(step, index):
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


def normalize_limit(step, index):
    n = _required(step, "n", index)
    if not isinstance(n, int):
        error("SCHEMA_VALIDATION_FAILED", "'n' must be an integer", f"pipeline.steps[{index}].n")
    if n < 0:
        error("SCHEMA_VALIDATION_FAILED", "'n' must be >= 0", f"pipeline.steps[{index}].n")
    return {"op": "limit", "n": n}


def normalize_branch(step, index):
    branches = _required(step, "branches", index)
    if not isinstance(branches, list):
        error("SCHEMA_VALIDATION_FAILED", "'branches' must be an array", f"pipeline.steps[{index}].branches")
    if len(branches) == 0:
        error("SCHEMA_VALIDATION_FAILED", "'branches' must not be empty", f"pipeline.steps[{index}].branches")

    normalized_branches = []
    otherwise_seen = False

    for i, branch in enumerate(branches):
        if not isinstance(branch, dict):
            error("SCHEMA_VALIDATION_FAILED", f"branch[{i}] must be an object", f"pipeline.steps[{index}].branches[{i}]")

        when = branch.get("when")
        if when is None:
            error("SCHEMA_VALIDATION_FAILED", f"branch[{i}] is missing required 'when' field", f"pipeline.steps[{index}].branches[{i}].when")

        if isinstance(when, str):
            when_stripped = when.strip()
            if when_stripped.lower() == "otherwise":
                if otherwise_seen:
                    error("MALFORMED_STEP", "only one 'otherwise' branch allowed", f"pipeline.steps[{index}].branches")
                if i < len(branches) - 1:
                    error("MALFORMED_STEP", "'otherwise' branch must be last", f"pipeline.steps[{index}].branches")
                otherwise_seen = True
                when = "otherwise"
            else:
                validate_expression(when_stripped, index, f"branches[{i}].when")
                when = when_stripped
        else:
            error("SCHEMA_VALIDATION_FAILED", f"branch[{i}].when must be a string", f"pipeline.steps[{index}].branches[{i}].when")

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


def normalize_call(step, index):
    name = step.get("name")
    if name is None:
        error("SCHEMA_VALIDATION_FAILED", "call step is missing required 'name' field", f"pipeline.steps[{index}]")
    validate_definition_name(name, f"pipeline.steps[{index}].name")

    params = step.get("params")
    if params is None:
        params = {}
    if not isinstance(params, dict):
        error("SCHEMA_VALIDATION_FAILED", "'params' must be an object", f"pipeline.steps[{index}].params")

    def validate_param_value(val, path):
        if val is None:
            return
        if isinstance(val, (str, int, float, bool)):
            return
        if isinstance(val, list):
            for i, item in enumerate(val):
                validate_param_value(item, f"{path}[{i}]")
            return
        error("SCHEMA_VALIDATION_FAILED", f"param value must be a scalar or array, got {type(val).__name__}", path)

    for key, value in params.items():
        if not isinstance(key, str):
            error("SCHEMA_VALIDATION_FAILED", "params keys must be strings", f"pipeline.steps[{index}].params")
        validate_param_value(value, f"pipeline.steps[{index}].params.{key}")

    return {"op": "call", "name": name, "params": params}


NORMALIZE_DISPATCH = {
    "select": normalize_select,
    "filter": normalize_filter,
    "map": normalize_map,
    "rename": normalize_rename,
    "limit": normalize_limit,
    "branch": normalize_branch,
    "call": normalize_call,
}


def normalize_step(step, index):
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

    handler = NORMALIZE_DISPATCH.get(op_normalized)
    if handler is None:
        error("UNKNOWN_OP", f"unsupported op '{op_normalized}'", f"pipeline.steps[{index}].op")
    return handler(step, index)


# Expression Parser/Evaluator
TWO_CHAR_OPS = {'==', '!=', '<=', '>=', '||', '&&'}


def tokenize(expr):
    pos = 0
    length = len(expr)
    while pos < length:
        ch = expr[pos]
        if ch in ' \t\n\r':
            pos += 1
            continue
        if ch == '.' and pos + 1 < length and expr[pos + 1].isalpha():
            yield ('OPERATOR', '.')
            pos += 1
            continue
        if pos + 1 < length:
            two_char = expr[pos:pos + 2]
            if two_char in TWO_CHAR_OPS:
                yield ('OPERATOR', two_char)
                pos += 2
                continue
        if ch == '(':
            yield ('LPAREN', '(')
            pos += 1
            continue
        if ch == ')':
            yield ('RPAREN', ')')
            pos += 1
            continue
        if ch == '!':
            yield ('OPERATOR', '!')
            pos += 1
            continue
        if ch in '*/+-<>|&':
            yield ('OPERATOR', ch)
            pos += 1
            continue
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
        if ch.isdigit() or (ch == '-' and pos + 1 < length and expr[pos + 1].isdigit()):
            start = pos
            if ch == '-':
                pos += 1
            while pos < length and (expr[pos].isdigit() or expr[pos] == '.'):
                pos += 1
            num_str = expr[start:pos]
            yield ('LITERAL', float(num_str) if '.' in num_str else int(num_str))
            continue
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
            start = pos
            while pos < length and (expr[pos].isalnum() or expr[pos] == '_'):
                pos += 1
            ident = expr[start:pos]
            if ident:
                yield ('IDENTIFIER', ident)
                continue
            raise ValueError(f"Unexpected character: {ch}")


def parse_expression(tokens):
    tokens_iter = iter(tokens)
    current = None

    def advance():
        nonlocal current
        try:
            return next(tokens_iter)
        except StopIteration:
            return ('EOF', None)

    def peek():
        return current

    def expect(expected_type, error_msg):
        if peek()[0] != expected_type:
            raise ValueError(error_msg)
        t = peek()
        advance()
        return t

    def parse_primary():
        t = peek()
        if t[0] == 'LPAREN':
            advance()
            node = parse_or()
            expect('RPAREN', "Expected ')'")
            return node
        if t[0] == 'IDENTIFIER':
            advance()
            base = t[1]
            parts = [base]
            while peek()[0] == 'OPERATOR' and peek()[1] == '.':
                advance()
                expect('IDENTIFIER', "Expected identifier after '.'")
                parts.append(t[1])
            return ('ident', parts[0]) if len(parts) == 1 else ('member_access', tuple(parts))
        if t[0] == 'LITERAL':
            advance()
            return ('literal', t[1])
        raise ValueError(f"Unexpected token: {t}")

    current = advance()

    def parse_or():
        node = parse_and()
        while peek()[0] == 'OPERATOR' and peek()[1] == '||':
            advance()
            node = ('or', node, parse_and())
        return node

    def parse_and():
        node = parse_equality()
        while peek()[0] == 'OPERATOR' and peek()[1] == '&&':
            advance()
            node = ('and', node, parse_equality())
        return node

    def parse_equality():
        node = parse_comparison()
        while peek()[0] == 'OPERATOR' and peek()[1] in ('==', '!='):
            op = peek()[1]
            advance()
            node = (op, node, parse_comparison())
        return node

    def parse_comparison():
        node = parse_additive()
        while peek()[0] == 'OPERATOR' and peek()[1] in ('<', '<=', '>', '>='):
            op = peek()[1]
            advance()
            node = (op, node, parse_additive())
        return node

    def parse_additive():
        node = parse_multiplicative()
        while peek()[0] == 'OPERATOR' and peek()[1] in ('+', '-'):
            op = peek()[1]
            advance()
            node = (op, node, parse_multiplicative())
        return node

    def parse_multiplicative():
        node = parse_unary()
        while peek()[0] == 'OPERATOR' and peek()[1] in ('*', '/'):
            op = peek()[1]
            advance()
            node = (op, node, parse_unary())
        return node

    def parse_unary():
        if peek()[0] == 'OPERATOR' and peek()[1] == '!':
            advance()
            return ('!', parse_unary())
        if peek()[0] == 'OPERATOR' and peek()[1] == '-':
            advance()
            return ('-', parse_unary())
        return parse_primary()

    return parse_or()


def evaluate_ast(ast, context, params=None):
    if params is None:
        params = {}

    if ast[0] == 'literal':
        return ast[1]
    if ast[0] == 'ident':
        return context.get(ast[1])
    if ast[0] == 'member_access':
        base, member = ast[1]
        return params.get(member) if base == 'params' else None
    if ast[0] == '!':
        return not bool(evaluate_ast(ast[1], context, params))
    if ast[0] == '-':
        val = evaluate_ast(ast[1], context, params)
        return None if val is None else (-val if isinstance(val, (int, float)) else None)

    left = evaluate_ast(ast[1], context, params)
    right = evaluate_ast(ast[2], context, params)

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


def evaluate_expression(expr, context, params=None):
    ast = parse_expression(tokenize(expr))
    return evaluate_ast(ast, context, params)


# Pipeline Execution
def execute_pipeline(steps, dataset, defs=None, calling_def=None, params=None):
    if defs is None:
        defs = {}
    if params is None:
        params = {}

    def execute_branch(rows_list, step, step_index):
        branches = step["branches"]
        strategy = step["merge"]["strategy"]
        all_results = []
        matched_indices = set()

        for branch_index, branch in enumerate(branches):
            when = branch["when"]
            branch_steps = branch["steps"]

            if when != "otherwise":
                matching_rows = []
                matching_indices_br = []
                for row_idx, row in enumerate(rows_list):
                    if row_idx in matched_indices:
                        continue
                    try:
                        if evaluate_expression(when, row, params):
                            matching_rows.append(row)
                            matching_indices_br.append(row_idx)
                    except ValueError:
                        error("BAD_EXPR", "expression error in branch condition", f"pipeline.steps[{step_index}].branches[{branch_index}].when")
                matched_indices.update(matching_indices_br)
            else:
                matching_indices_br = [i for i in range(len(rows_list)) if i not in matched_indices]
                matching_rows = [rows_list[i] for i in matching_indices_br]
                matched_indices.update(matching_indices_br)

            branch_result = execute_pipeline(branch_steps, matching_rows, defs, calling_def, params) if branch_steps else matching_rows
            all_results.append(branch_result)

        return [row for branch_res in all_results for row in branch_res]

    rows_in = len(dataset)
    current_rows = dataset

    for i, step in enumerate(steps):
        op = step["op"]
        if op == "select":
            columns = step["columns"]
            current_rows = [{col: row[col] for col in columns} for row in current_rows]
        elif op == "map":
            mapped = []
            for row in current_rows:
                new_row = row.copy()
                try:
                    new_row[step["as"]] = evaluate_expression(step["expr"], row, params)
                except ValueError:
                    error("BAD_EXPR", "expression error", f"pipeline.steps[{i}].expr")
                mapped.append(new_row)
            current_rows = mapped
        elif op == "rename":
            result_map = step["mapping"]
            current_rows = [{**row, **{result_map.get(k, k): row[k] for k in row if k not in result_map}} for row in current_rows]
        elif op == "filter":
            filtered = []
            for row in current_rows:
                try:
                    if evaluate_expression(step["where"], row, params):
                        filtered.append(row)
                except ValueError:
                    error("BAD_EXPR", "expression error", f"pipeline.steps[{i}].where")
            current_rows = filtered
        elif op == "limit":
            current_rows = current_rows[:step["n"]]
        elif op == "branch":
            current_rows = execute_branch(current_rows, step, i)
        elif op == "call":
            name = step["name"]
            call_params = step.get("params", {})
            if calling_def is not None and name == calling_def:
                error("RECURSION_FORBIDDEN", "recursive call detected", f"defs[{name}].steps[0].name")
            if name not in defs:
                error("UNKNOWN_DEF", f"definition '{name}' not found", f"pipeline.steps[{i}].name")
            def_steps = defs[name]["steps"]
            if def_steps:
                merged_params = {**params, **call_params}
                execute_pipeline(def_steps, current_rows, defs, calling_def=name, params=merged_params)

    return current_rows, rows_in, len(current_rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="ETL Pipeline Executor")
    parser.add_argument("--execute", action="store_true", default=False, help="Execute the pipeline and return data/metrics")
    args = parser.parse_args()

    defs, pipeline, dataset = parse_input()

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
        result_data, rows_in, rows_out = execute_pipeline(normalized_steps, dataset, defs)
        print(json.dumps({"status": "ok", "data": result_data, "metrics": {"rows_in": rows_in, "rows_out": rows_out}}))


if __name__ == "__main__":
    main()
