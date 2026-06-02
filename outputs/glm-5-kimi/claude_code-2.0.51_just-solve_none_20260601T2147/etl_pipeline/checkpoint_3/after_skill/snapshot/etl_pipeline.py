#!/usr/bin/env python3
"""ETL Pipeline Parser and Executor - validates, normalizes, and executes pipeline specifications."""

import json
import sys
from dataclasses import dataclass
from typing import Any, Callable, Dict, List


class ETLError(Exception):
    def __init__(self, error_code: str, message: str, path: str):
        self.error_code = error_code
        self.message = message
        self.path = path
        super().__init__(message)


# --- Tokenizer ---

def _tokenize_expression(expr: str, path: str) -> List[str]:
    """Tokenize an expression string into tokens."""
    tokens = []
    i = 0
    expr = expr.strip()

    while i < len(expr):
        if expr[i].isspace():
            i += 1
        elif expr[i] == '"':
            j = i + 1
            while j < len(expr) and expr[j] != '"':
                if expr[j] == '\\' and j + 1 < len(expr):
                    j += 2
                else:
                    j += 1
            if j >= len(expr):
                raise ETLError('BAD_EXPR', 'ETL_ERROR: unterminated string literal', path)
            tokens.append(expr[i:j+1])
            i = j + 1
        elif expr[i] in '()':
            tokens.append(expr[i])
            i += 1
        elif expr[i].isdigit():
            j = i
            while j < len(expr) and (expr[j].isdigit() or expr[j] == '.'):
                j += 1
            tokens.append(expr[i:j])
            i = j
        elif expr[i].isalpha() or expr[i] == '_':
            j = i
            while j < len(expr) and (expr[j].isalnum() or expr[j] == '_'):
                j += 1
            tokens.append(expr[i:j])
            i = j
        elif i + 1 < len(expr) and expr[i:i+2] in ('>=', '<=', '==', '!=', '&&', '||'):
            tokens.append(expr[i:i+2])
            i += 2
        elif expr[i] in '+-*/><!':
            if i + 1 < len(expr) and expr[i] == '*' and expr[i+1] == '*':
                raise ETLError('BAD_EXPR', "ETL_ERROR: unsupported operator '**'", path)
            tokens.append(expr[i])
            i += 1
        elif i + 1 < len(expr) and expr[i] == '^' and expr[i+1] == '^':
            raise ETLError('BAD_EXPR', "ETL_ERROR: unsupported operator '^^'", path)
        else:
            raise ETLError('BAD_EXPR', f"ETL_ERROR: invalid character '{expr[i]}' in expression", path)

    return tokens


# --- AST Nodes ---

@dataclass
class ASTNode:
    """Base class for AST nodes."""
    def evaluate(self, row: Dict[str, Any]) -> Any:
        raise NotImplementedError()


@dataclass
class LiteralNode(ASTNode):
    value: Any

    def evaluate(self, row: Dict[str, Any]) -> Any:
        return self.value


@dataclass
class IdentifierNode(ASTNode):
    name: str

    def evaluate(self, row: Dict[str, Any]) -> Any:
        return row.get(self.name)


@dataclass
class UnaryOpNode(ASTNode):
    op: str
    operand: ASTNode

    def evaluate(self, row: Dict[str, Any]) -> Any:
        val = self.operand.evaluate(row)
        if self.op == '!':
            return True if val is None else not bool(val)
        return None


# --- Binary Operations ---

def _eval_and(left: Any, right: Any) -> Any:
    if left is None or left is False:
        return False
    return right if right is not None else False


def _eval_or(left: Any, right: Any) -> Any:
    if left is True:
        return True
    if left is None or left is False:
        return right if right is not None else False
    return left


def _safe_div(left, right):
    return None if right == 0 else left / right


_ARITH_OPS = {'+': lambda a, b: a + b, '-': lambda a, b: a - b,
              '*': lambda a, b: a * b, '/': _safe_div}

_COMPARE_OPS = {'==': lambda a, b: a == b, '!=': lambda a, b: a != b,
                '<': lambda a, b: a < b, '<=': lambda a, b: a <= b,
                '>': lambda a, b: a > b, '>=': lambda a, b: a >= b}


def _eval_arithmetic(op: str, left: Any, right: Any) -> Any:
    if not isinstance(left, (int, float)) or not isinstance(right, (int, float)):
        return None
    fn = _ARITH_OPS.get(op)
    return fn(left, right) if fn else None


def _eval_comparison(op: str, left: Any, right: Any) -> Any:
    if left is None or right is None:
        return False
    if not isinstance(left, (int, float, str)):
        return False
    if not (isinstance(left, (int, float)) and isinstance(right, (int, float))):
        if type(left) != type(right):
            return False
    fn = _COMPARE_OPS.get(op)
    return fn(left, right) if fn else False


@dataclass
class BinaryOpNode(ASTNode):
    op: str
    left: ASTNode
    right: ASTNode

    def evaluate(self, row: Dict[str, Any]) -> Any:
        left_val = self.left.evaluate(row)
        right_val = self.right.evaluate(row)
        op = 'and' if self.op == '&&' else 'or' if self.op == '||' else self.op

        if op == 'and':
            return _eval_and(left_val, right_val)
        if op == 'or':
            return _eval_or(left_val, right_val)
        if op in ('+', '-', '*', '/'):
            return _eval_arithmetic(op, left_val, right_val)
        if op in ('==', '!=', '<', '<=', '>', '>='):
            return _eval_comparison(op, left_val, right_val)
        return None


# --- Expression Parser ---

class ExpressionParser:
    """Recursive descent parser for expressions with proper operator precedence."""

    def __init__(self, tokens: List[str], path: str):
        self.tokens = tokens
        self.pos = 0
        self.path = path

    def parse(self) -> ASTNode:
        if not self.tokens:
            raise ETLError('BAD_EXPR', 'ETL_ERROR: empty expression', self.path)
        result = self._parse_or()
        if self.pos < len(self.tokens):
            remaining = ' '.join(self.tokens[self.pos:])
            raise ETLError('BAD_EXPR', f'ETL_ERROR: unexpected token after expression: {remaining}', self.path)
        return result

    def _current(self) -> str | None:
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def _consume(self) -> str:
        token = self.tokens[self.pos]
        self.pos += 1
        return token

    def _parse_or(self) -> ASTNode:
        left = self._parse_and()
        while self._current() in ('||', 'or'):
            op = self._consume()
            right = self._parse_and()
            left = BinaryOpNode(op, left, right)
        return left

    def _parse_and(self) -> ASTNode:
        left = self._parse_equality()
        while self._current() in ('&&', 'and'):
            op = self._consume()
            right = self._parse_equality()
            left = BinaryOpNode(op, left, right)
        return left

    def _parse_equality(self) -> ASTNode:
        left = self._parse_comparison()
        while self._current() in ('==', '!='):
            op = self._consume()
            right = self._parse_comparison()
            left = BinaryOpNode(op, left, right)
        return left

    def _parse_comparison(self) -> ASTNode:
        left = self._parse_additive()
        while self._current() in ('<', '<=', '>', '>='):
            op = self._consume()
            right = self._parse_additive()
            left = BinaryOpNode(op, left, right)
        return left

    def _parse_additive(self) -> ASTNode:
        left = self._parse_multiplicative()
        while self._current() in ('+', '-'):
            op = self._consume()
            right = self._parse_multiplicative()
            left = BinaryOpNode(op, left, right)
        return left

    def _parse_multiplicative(self) -> ASTNode:
        left = self._parse_unary()
        while self._current() in ('*', '/'):
            op = self._consume()
            right = self._parse_unary()
            left = BinaryOpNode(op, left, right)
        return left

    def _parse_unary(self) -> ASTNode:
        if self._current() == '!':
            op = self._consume()
            operand = self._parse_unary()
            return UnaryOpNode(op, operand)
        return self._parse_primary()

    def _parse_primary(self) -> ASTNode:
        token = self._current()

        if token is None:
            raise ETLError('BAD_EXPR', 'ETL_ERROR: unexpected end of expression', self.path)

        if token == '(':
            self._consume()
            node = self._parse_or()
            if self._current() != ')':
                raise ETLError('BAD_EXPR', 'ETL_ERROR: missing closing parenthesis', self.path)
            self._consume()
            return node

        if token == 'true':
            self._consume()
            return LiteralNode(True)

        if token == 'false':
            self._consume()
            return LiteralNode(False)

        if token == 'null':
            self._consume()
            return LiteralNode(None)

        if token.startswith('"'):
            self._consume()
            content = token[1:-1]
            content = content.replace('\\"', '"').replace('\\\\', '\\').replace('\\n', '\n').replace('\\t', '\t')
            return LiteralNode(content)

        if token[0].isdigit():
            self._consume()
            try:
                return LiteralNode(float(token) if '.' in token else int(token))
            except ValueError:
                raise ETLError('BAD_EXPR', f'ETL_ERROR: invalid number format: {token}', self.path)

        if token[0].isalpha() or token[0] == '_':
            self._consume()
            return IdentifierNode(token)

        raise ETLError('BAD_EXPR', f'ETL_ERROR: unexpected token: {token}', self.path)


def parse_expression(expr: str, path: str) -> ASTNode:
    tokens = _tokenize_expression(expr, path)
    return ExpressionParser(tokens, path).parse()


def evaluate_expression(expr: str, row: Dict[str, Any], path: str) -> Any:
    return parse_expression(expr, path).evaluate(row)


def validate_expression(expr: str, path: str) -> None:
    if not expr or not expr.strip():
        raise ETLError('BAD_EXPR', 'ETL_ERROR: empty expression', path)
    try:
        parse_expression(expr, path)
    except ETLError:
        raise
    except Exception as e:
        raise ETLError('BAD_EXPR', f'ETL_ERROR: {e}', path) from e


# --- Normalization ---

def _require_string_field(step: Dict[str, Any], field: str, path: str) -> str:
    if field not in step:
        raise ETLError('MISSING_COLUMN',
                       f'ETL_ERROR: missing required field "{field}"', f'{path}.{field}')
    value = step[field]
    if not isinstance(value, str):
        raise ETLError('SCHEMA_VALIDATION_FAILED',
                       f'ETL_ERROR: {field} must be a string', f'{path}.{field}')
    trimmed = value.strip()
    if not trimmed:
        raise ETLError('SCHEMA_VALIDATION_FAILED',
                       f'ETL_ERROR: {field} cannot be empty', f'{path}.{field}')
    return trimmed


def _normalize_select(step: Dict[str, Any], path: str) -> Dict[str, Any]:
    if 'columns' not in step:
        raise ETLError('MISSING_COLUMN',
                       'ETL_ERROR: missing required field "columns"', f'{path}.columns')
    columns = step['columns']
    if not isinstance(columns, list):
        raise ETLError('SCHEMA_VALIDATION_FAILED',
                       'ETL_ERROR: columns must be an array', f'{path}.columns')
    for i, col in enumerate(columns):
        if not isinstance(col, str):
            raise ETLError('SCHEMA_VALIDATION_FAILED',
                           'ETL_ERROR: column names must be strings', f'{path}.columns[{i}]')
    return {'columns': columns}


def _normalize_filter(step: Dict[str, Any], path: str) -> Dict[str, Any]:
    where = _require_string_field(step, 'where', path)
    validate_expression(where, f'{path}.where')
    return {'where': where}


def _normalize_map(step: Dict[str, Any], path: str) -> Dict[str, Any]:
    as_val = _require_string_field(step, 'as', path)
    expr = _require_string_field(step, 'expr', path)
    validate_expression(expr, f'{path}.expr')
    return {'as': as_val, 'expr': expr}


def _normalize_rename(step: Dict[str, Any], path: str) -> Dict[str, Any]:
    if 'mapping' in step:
        mapping = step['mapping']
        if not isinstance(mapping, dict):
            raise ETLError('SCHEMA_VALIDATION_FAILED',
                           'ETL_ERROR: mapping must be an object', f'{path}.mapping')
        for k, v in mapping.items():
            if not isinstance(k, str) or not isinstance(v, str):
                raise ETLError('SCHEMA_VALIDATION_FAILED',
                               'ETL_ERROR: mapping keys and values must be strings',
                               f'{path}.mapping')
        return {'mapping': mapping}

    if 'from' not in step or 'to' not in step:
        raise ETLError('MISSING_COLUMN',
                       'ETL_ERROR: rename requires either from/to or mapping', path)

    from_val = _require_string_field(step, 'from', path)
    to_val = _require_string_field(step, 'to', path)
    return {'mapping': {from_val: to_val}}


def _normalize_limit(step: Dict[str, Any], path: str) -> Dict[str, Any]:
    if 'n' not in step:
        raise ETLError('MISSING_COLUMN',
                       'ETL_ERROR: missing required field "n"', f'{path}.n')
    n = step['n']
    if not isinstance(n, int) or isinstance(n, bool):
        raise ETLError('SCHEMA_VALIDATION_FAILED',
                       'ETL_ERROR: n must be an integer', f'{path}.n')
    if n < 0:
        raise ETLError('SCHEMA_VALIDATION_FAILED',
                       'ETL_ERROR: n must be >= 0', f'{path}.n')
    return {'n': n}


def _normalize_branch(step: Dict[str, Any], path: str) -> Dict[str, Any]:
    if 'branches' not in step:
        raise ETLError('MISSING_COLUMN',
                       'ETL_ERROR: missing required field "branches"', f'{path}.branches')
    branches = step['branches']
    if not isinstance(branches, list):
        raise ETLError('SCHEMA_VALIDATION_FAILED',
                       'ETL_ERROR: branches must be an array', f'{path}.branches')
    if len(branches) == 0:
        raise ETLError('MALFORMED_STEP',
                       'ETL_ERROR: branches cannot be empty', f'{path}.branches')

    normalized_branches = []
    otherwise_count = 0
    otherwise_index = -1

    for i, branch in enumerate(branches):
        branch_path = f'{path}.branches[{i}]'

        if not isinstance(branch, dict):
            raise ETLError('SCHEMA_VALIDATION_FAILED',
                           'ETL_ERROR: branch must be an object', branch_path)

        normalized_branch = {}

        if 'id' in branch:
            id_val = branch['id']
            if not isinstance(id_val, str):
                raise ETLError('SCHEMA_VALIDATION_FAILED',
                               'ETL_ERROR: id must be a string', f'{branch_path}.id')
            normalized_branch['id'] = id_val

        # Validate when
        if 'when' not in branch:
            raise ETLError('MISSING_COLUMN',
                           'ETL_ERROR: missing required field "when"', f'{branch_path}.when')
        when = branch['when']
        if not isinstance(when, str):
            raise ETLError('SCHEMA_VALIDATION_FAILED',
                           'ETL_ERROR: when must be a string', f'{branch_path}.when')

        if when == 'otherwise':
            otherwise_count += 1
            otherwise_index = i
            normalized_branch['when'] = 'otherwise'
        else:
            trimmed_when = when.strip()
            if not trimmed_when:
                raise ETLError('SCHEMA_VALIDATION_FAILED',
                               'ETL_ERROR: when cannot be empty', f'{branch_path}.when')
            validate_expression(trimmed_when, f'{branch_path}.when')
            normalized_branch['when'] = trimmed_when

        if 'steps' not in branch:
            raise ETLError('MISSING_COLUMN',
                           'ETL_ERROR: missing required field "steps"', f'{branch_path}.steps')
        steps = branch['steps']
        if not isinstance(steps, list):
            raise ETLError('SCHEMA_VALIDATION_FAILED',
                           'ETL_ERROR: steps must be an array', f'{branch_path}.steps')

        normalized_branch['steps'] = [normalize_step(sub_step, j, branch_path) for j, sub_step in enumerate(steps)]

        normalized_branches.append(normalized_branch)

    # Validate otherwise constraints
    if otherwise_count > 1:
        raise ETLError('MALFORMED_STEP',
                       'ETL_ERROR: at most one otherwise branch allowed', f'{path}.branches')
    if otherwise_count == 1 and otherwise_index != len(branches) - 1:
        raise ETLError('MALFORMED_STEP',
                       'ETL_ERROR: otherwise branch must be last', f'{path}.branches')

    # Validate optional merge (default: concat)
    if 'merge' in step:
        merge = step['merge']
        if not isinstance(merge, dict):
            raise ETLError('SCHEMA_VALIDATION_FAILED',
                           'ETL_ERROR: merge must be an object', f'{path}.merge')
        strategy = merge.get('strategy', 'concat')
        if not isinstance(strategy, str):
            raise ETLError('SCHEMA_VALIDATION_FAILED',
                           'ETL_ERROR: strategy must be a string', f'{path}.merge.strategy')
        if strategy != 'concat':
            raise ETLError('SCHEMA_VALIDATION_FAILED',
                           f'ETL_ERROR: unsupported merge strategy "{strategy}"', f'{path}.merge.strategy')
    else:
        strategy = 'concat'

    return {'branches': normalized_branches, 'merge': {'strategy': strategy}}


_OP_NORMALIZERS: Dict[str, Callable[[Dict[str, Any], str], Dict[str, Any]]] = {
    'branch': _normalize_branch,
    'filter': _normalize_filter,
    'limit': _normalize_limit,
    'map': _normalize_map,
    'rename': _normalize_rename,
    'select': _normalize_select,
}


def normalize_step(step: Dict[str, Any], index: int, parent_path: str = None) -> Dict[str, Any]:
    if parent_path is not None:
        path = f'{parent_path}.steps[{index}]'
    else:
        path = f'pipeline.steps[{index}]'

    if not isinstance(step, dict):
        raise ETLError('SCHEMA_VALIDATION_FAILED',
                       'ETL_ERROR: step must be an object', path)

    op_raw = step.get('op')
    if op_raw is None:
        raise ETLError('SCHEMA_VALIDATION_FAILED',
                       'ETL_ERROR: missing required field "op"', path)
    if not isinstance(op_raw, str):
        raise ETLError('SCHEMA_VALIDATION_FAILED',
                       'ETL_ERROR: op must be a string', f'{path}.op')

    op = op_raw.strip().lower()
    if not op:
        raise ETLError('SCHEMA_VALIDATION_FAILED',
                       'ETL_ERROR: op cannot be empty', f'{path}.op')

    if op not in _OP_NORMALIZERS:
        raise ETLError('UNKNOWN_OP',
                       f'ETL_ERROR: unsupported op \'{op}\'', f'{path}.op')

    normalized = _OP_NORMALIZERS[op](step, path)
    return {'op': op, **{k: normalized[k] for k in sorted(normalized)}}


# --- Execution ---

def _exec_select(rows: List[Dict[str, Any]], step: Dict[str, Any], path: str, step_index: int) -> List[Dict[str, Any]]:
    columns = step['columns']
    result = []
    for row in rows:
        new_row = {}
        for i, col in enumerate(columns):
            if col not in row:
                raise ETLError('MISSING_COLUMN',
                               f"ETL_ERROR: column '{col}' not found in row",
                               f'{path}.columns[{i}]')
            new_row[col] = row[col]
        result.append(new_row)
    return result


def _exec_filter(rows: List[Dict[str, Any]], step: Dict[str, Any], path: str, step_index: int) -> List[Dict[str, Any]]:
    where = step['where']
    result = []
    for row in rows:
        val = evaluate_expression(where, row, f'{path}.where')
        if val is not None and val is not False:
            result.append(row)
    return result


def _exec_map(rows: List[Dict[str, Any]], step: Dict[str, Any], path: str, step_index: int) -> List[Dict[str, Any]]:
    as_field = step['as']
    expr = step['expr']
    result = []
    for row in rows:
        new_row = row.copy()
        new_row[as_field] = evaluate_expression(expr, row, f'{path}.expr')
        result.append(new_row)
    return result


def _exec_rename(rows: List[Dict[str, Any]], step: Dict[str, Any], path: str, step_index: int) -> List[Dict[str, Any]]:
    mapping = step['mapping']
    result = []
    for row in rows:
        new_row = row.copy()
        for src, dst in mapping.items():
            if src not in new_row:
                raise ETLError('MISSING_COLUMN',
                               f"ETL_ERROR: column '{src}' not found in row",
                               f'{path}.mapping.{src}')
            new_row[dst] = new_row[src]
            if dst != src:
                del new_row[src]
        result.append(new_row)
    return result


def _exec_limit(rows: List[Dict[str, Any]], step: Dict[str, Any], path: str, step_index: int) -> List[Dict[str, Any]]:
    return rows[:step['n']]


def _exec_branch(rows: List[Dict[str, Any]], step: Dict[str, Any], path: str, step_index: int) -> List[Dict[str, Any]]:
    branches = step['branches']
    branch_buckets: Dict[int, List[Dict[str, Any]]] = {i: [] for i in range(len(branches))}

    for row in rows:
        for i, branch in enumerate(branches):
            when = branch['when']
            if when == 'otherwise' or (val := evaluate_expression(when, row, f'{path}.branches[{i}].when')) is not None and val is not False:
                branch_buckets[i].append(row)
                break

    result = []
    for i, branch in enumerate(branches):
        branch_path = f'{path}.branches[{i}]'
        branch_rows = branch_buckets[i]
        if branch_rows:
            sub_steps = branch['steps']
            for j, sub_step in enumerate(sub_steps):
                sub_path = f'{branch_path}.steps[{j}]'
                sub_op = sub_step['op']
                try:
                    branch_rows = _OP_EXECUTORS[sub_op](branch_rows, sub_step, sub_path, j)
                except ETLError:
                    raise
                except Exception as e:
                    raise ETLError('EXECUTION_FAILED', f'ETL_ERROR: {e}', sub_path) from e
            result.extend(branch_rows)

    return result


_OP_EXECUTORS: Dict[str, Callable[[List[Dict[str, Any]], Dict[str, Any], str, int], List[Dict[str, Any]]]] = {
    'branch': _exec_branch,
    'filter': _exec_filter,
    'limit': _exec_limit,
    'map': _exec_map,
    'rename': _exec_rename,
    'select': _exec_select,
}


def execute_pipeline(steps: List[Dict[str, Any]], dataset: List[Dict[str, Any]]) -> tuple[list[Dict[str, Any]], Dict[str, int]]:
    rows = [row.copy() for row in dataset]
    rows_in = len(rows)

    for step_index, step in enumerate(steps):
        op = step['op']
        path = f'pipeline.steps[{step_index}]'
        try:
            rows = _OP_EXECUTORS[op](rows, step, path, step_index)
        except ETLError:
            raise
        except Exception as e:
            raise ETLError('EXECUTION_FAILED', f'ETL_ERROR: {e}', path) from e

    return rows, {'rows_in': rows_in, 'rows_out': len(rows)}


# --- Main Processing ---

def process_pipeline(data: Dict[str, Any], execute: bool = False) -> Dict[str, Any]:
    if not isinstance(data, dict):
        raise ETLError('SCHEMA_VALIDATION_FAILED',
                       'ETL_ERROR: input must be a JSON object', '')

    if 'pipeline' not in data:
        raise ETLError('SCHEMA_VALIDATION_FAILED',
                       'ETL_ERROR: missing required field "pipeline"', 'pipeline')
    if 'dataset' not in data:
        raise ETLError('SCHEMA_VALIDATION_FAILED',
                       'ETL_ERROR: missing required field "dataset"', 'dataset')

    pipeline = data['pipeline']
    if not isinstance(pipeline, dict):
        raise ETLError('SCHEMA_VALIDATION_FAILED',
                       'ETL_ERROR: pipeline must be an object', 'pipeline')

    if 'steps' not in pipeline:
        raise ETLError('SCHEMA_VALIDATION_FAILED',
                       'ETL_ERROR: missing required field "steps"', 'pipeline.steps')
    steps = pipeline['steps']
    if not isinstance(steps, list):
        raise ETLError('SCHEMA_VALIDATION_FAILED',
                       'ETL_ERROR: steps must be an array', 'pipeline.steps')

    dataset = data['dataset']
    if not isinstance(dataset, list):
        raise ETLError('SCHEMA_VALIDATION_FAILED',
                       'ETL_ERROR: dataset must be an array', 'dataset')
    for i, item in enumerate(dataset):
        if not isinstance(item, dict):
            raise ETLError('SCHEMA_VALIDATION_FAILED',
                           'ETL_ERROR: dataset items must be objects', f'dataset[{i}]')

    normalized_steps = [normalize_step(s, i) for i, s in enumerate(steps)]

    if execute:
        result_rows, metrics = execute_pipeline(normalized_steps, dataset)
        return {'status': 'ok', 'data': result_rows, 'metrics': metrics}
    return {'status': 'ok', 'normalized': {'steps': normalized_steps}}


def main():
    args = sys.argv[1:]
    execute = False

    i = 0
    while i < len(args):
        arg = args[i]
        if arg == '--execute':
            if i + 1 < len(args) and args[i + 1].lower() in ('true', 'false'):
                execute = args[i + 1].lower() == 'true'
                i += 2
            else:
                execute = True
                i += 1
        else:
            i += 1

    try:
        data = json.loads(sys.stdin.read())
    except json.JSONDecodeError as e:
        print(json.dumps({
            'status': 'error',
            'error_code': 'SCHEMA_VALIDATION_FAILED',
            'message': f'ETL_ERROR: invalid JSON: {e}',
            'path': ''
        }))
        sys.exit(1)

    try:
        result = process_pipeline(data, execute=execute)
        print(json.dumps(result))
    except ETLError as e:
        print(json.dumps({
            'status': 'error',
            'error_code': e.error_code,
            'message': e.message,
            'path': e.path
        }))
        sys.exit(1)


if __name__ == '__main__':
    main()
