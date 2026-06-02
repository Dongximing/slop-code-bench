#!/usr/bin/env python3
"""ETL Pipeline Parser and Executor - validates, normalizes, and executes pipeline specifications."""

import json
import sys
from typing import Any, Dict, List, Optional, Tuple

# Binary operators for tokenizer validation
BINARY_OPS = frozenset({'+', '-', '*', '/', '>', '>=', '<', '<=', '==', '!=', 'and', 'or'})


class ETLError(Exception):
    def __init__(self, error_code: str, message: str, path: str):
        self.error_code = error_code
        self.message = message
        self.path = path
        super().__init__(message)


# =============================================================================
# TOKENIZER
# =============================================================================

def _tokenize_expression(expr: str, path: str) -> List[str]:
    """Tokenize an expression string into tokens."""
    tokens = []
    i = 0
    expr = expr.strip()

    while i < len(expr):
        if expr[i].isspace():
            i += 1
        elif expr[i] == '"':
            # String literal
            j = i + 1
            while j < len(expr) and expr[j] != '"':
                if expr[j] == '\\' and j + 1 < len(expr):
                    j += 2
                else:
                    j += 1
            if j >= len(expr):
                raise ETLError('BAD_EXPR', 'ETL_ERROR: unterminated string literal', path)
            tokens.append(expr[i:j+1])  # Include quotes
            i = j + 1
        elif expr[i] in '()':
            tokens.append(expr[i])
            i += 1
        elif expr[i].isdigit():
            # Number literal
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
            # Check for unsupported operators like **, ^^
            if i + 1 < len(expr) and expr[i] == '*' and expr[i+1] == '*':
                raise ETLError('BAD_EXPR', "ETL_ERROR: unsupported operator '**'", path)
            tokens.append(expr[i])
            i += 1
        elif i + 1 < len(expr) and expr[i] == '^' and expr[i+1] == '^':
            raise ETLError('BAD_EXPR', "ETL_ERROR: unsupported operator '^^'", path)
        else:
            # Unknown character
            raise ETLError('BAD_EXPR', f"ETL_ERROR: invalid character '{expr[i]}' in expression", path)

    return tokens


# =============================================================================
# EXPRESSION PARSER AND EVALUATOR
# =============================================================================

class ExpressionParser:
    """Recursive descent parser for expressions with proper operator precedence."""

    def __init__(self, tokens: List[str], path: str):
        self.tokens = tokens
        self.pos = 0
        self.path = path

    def parse(self) -> 'ASTNode':
        """Parse the expression and return an AST node."""
        if not self.tokens:
            raise ETLError('BAD_EXPR', 'ETL_ERROR: empty expression', self.path)
        result = self._parse_or()
        if self.pos < len(self.tokens):
            remaining = ' '.join(self.tokens[self.pos:])
            raise ETLError('BAD_EXPR', f'ETL_ERROR: unexpected token after expression: {remaining}', self.path)
        return result

    def _current(self) -> Optional[str]:
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def _consume(self) -> str:
        token = self.tokens[self.pos]
        self.pos += 1
        return token

    def _parse_or(self) -> 'ASTNode':
        """Parse || operator (lowest precedence)."""
        left = self._parse_and()
        while self._current() in ('||', 'or'):
            op = self._consume()
            right = self._parse_and()
            left = BinaryOpNode(op, left, right)
        return left

    def _parse_and(self) -> 'ASTNode':
        """Parse && operator."""
        left = self._parse_equality()
        while self._current() in ('&&', 'and'):
            op = self._consume()
            right = self._parse_equality()
            left = BinaryOpNode(op, left, right)
        return left

    def _parse_equality(self) -> 'ASTNode':
        """Parse == and != operators."""
        left = self._parse_comparison()
        while self._current() in ('==', '!='):
            op = self._consume()
            right = self._parse_comparison()
            left = BinaryOpNode(op, left, right)
        return left

    def _parse_comparison(self) -> 'ASTNode':
        """Parse <, <=, >, >= operators."""
        left = self._parse_additive()
        while self._current() in ('<', '<=', '>', '>='):
            op = self._consume()
            right = self._parse_additive()
            left = BinaryOpNode(op, left, right)
        return left

    def _parse_additive(self) -> 'ASTNode':
        """Parse + and - operators."""
        left = self._parse_multiplicative()
        while self._current() in ('+', '-'):
            op = self._consume()
            right = self._parse_multiplicative()
            left = BinaryOpNode(op, left, right)
        return left

    def _parse_multiplicative(self) -> 'ASTNode':
        """Parse * and / operators."""
        left = self._parse_unary()
        while self._current() in ('*', '/'):
            op = self._consume()
            right = self._parse_unary()
            left = BinaryOpNode(op, left, right)
        return left

    def _parse_unary(self) -> 'ASTNode':
        """Parse unary ! operator."""
        if self._current() == '!':
            op = self._consume()
            operand = self._parse_unary()
            return UnaryOpNode(op, operand)
        return self._parse_primary()

    def _parse_primary(self) -> 'ASTNode':
        """Parse primary expressions: literals, identifiers, parentheses."""
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
            # String literal
            self._consume()
            # Parse the string content (handle escape sequences)
            content = token[1:-1]
            content = content.replace('\\"', '"').replace('\\\\', '\\').replace('\\n', '\n').replace('\\t', '\t')
            return LiteralNode(content)

        if token[0].isdigit():
            # Number literal
            self._consume()
            try:
                if '.' in token:
                    return LiteralNode(float(token))
                else:
                    return LiteralNode(int(token))
            except ValueError:
                raise ETLError('BAD_EXPR', f'ETL_ERROR: invalid number format: {token}', self.path)

        if token[0].isalpha() or token[0] == '_':
            # Identifier
            self._consume()
            return IdentifierNode(token)

        raise ETLError('BAD_EXPR', f'ETL_ERROR: unexpected token: {token}', self.path)


class ASTNode:
    """Base class for AST nodes."""
    def evaluate(self, row: Dict[str, Any]) -> Any:
        raise NotImplementedError()


class LiteralNode(ASTNode):
    def __init__(self, value: Any):
        self.value = value

    def evaluate(self, row: Dict[str, Any]) -> Any:
        return self.value


class IdentifierNode(ASTNode):
    def __init__(self, name: str):
        self.name = name

    def evaluate(self, row: Dict[str, Any]) -> Any:
        return row.get(self.name)


class BinaryOpNode(ASTNode):
    def __init__(self, op: str, left: ASTNode, right: ASTNode):
        self.op = op
        self.left = left
        self.right = right

    def evaluate(self, row: Dict[str, Any]) -> Any:
        left_val = self.left.evaluate(row)
        right_val = self.right.evaluate(row)

        # Handle && and || aliases
        op = self.op
        if op == '&&':
            op = 'and'
        elif op == '||':
            op = 'or'

        # Boolean operators
        if op == 'and':
            # Null is falsy
            if left_val is None or left_val is False:
                return left_val if left_val is not None else False
            if left_val is True:
                return right_val if right_val is not None else False
            # Non-boolean left value - truthy
            return right_val if right_val is not None else False

        if op == 'or':
            if left_val is True:
                return True
            if left_val is None or left_val is False:
                return right_val if right_val is not None else False
            # Non-boolean left value - truthy
            return left_val

        # Arithmetic operators - null handling
        if op in ('+', '-', '*', '/'):
            if left_val is None or right_val is None:
                return None
            if not isinstance(left_val, (int, float)) or not isinstance(right_val, (int, float)):
                return None

            if op == '+':
                return left_val + right_val
            if op == '-':
                return left_val - right_val
            if op == '*':
                return left_val * right_val
            if op == '/':
                if right_val == 0:
                    return None
                return left_val / right_val

        # Comparison operators - null handling
        if op in ('==', '!=', '<', '<=', '>', '>='):
            if left_val is None or right_val is None:
                return False

            # Type mismatch returns false
            if type(left_val) != type(right_val):
                # Special case: int and float are compatible
                if isinstance(left_val, (int, float)) and isinstance(right_val, (int, float)):
                    pass  # Continue with comparison
                else:
                    return False

            if op == '==':
                return left_val == right_val
            if op == '!=':
                return left_val != right_val
            if op == '<':
                if not isinstance(left_val, (int, float, str)):
                    return False
                return left_val < right_val
            if op == '<=':
                if not isinstance(left_val, (int, float, str)):
                    return False
                return left_val <= right_val
            if op == '>':
                if not isinstance(left_val, (int, float, str)):
                    return False
                return left_val > right_val
            if op == '>=':
                if not isinstance(left_val, (int, float, str)):
                    return False
                return left_val >= right_val

        return None


class UnaryOpNode(ASTNode):
    def __init__(self, op: str, operand: ASTNode):
        self.op = op
        self.operand = operand

    def evaluate(self, row: Dict[str, Any]) -> Any:
        val = self.operand.evaluate(row)

        if self.op == '!':
            if val is None:
                return True
            if isinstance(val, bool):
                return not val
            # Non-boolean values: treat truthy/falsy
            return not bool(val)

        return None


def parse_expression(expr: str, path: str) -> ASTNode:
    """Parse an expression string and return an AST."""
    tokens = _tokenize_expression(expr, path)
    parser = ExpressionParser(tokens, path)
    return parser.parse()


def evaluate_expression(expr: str, row: Dict[str, Any], path: str) -> Any:
    """Parse and evaluate an expression against a row."""
    ast = parse_expression(expr, path)
    return ast.evaluate(row)


# =============================================================================
# VALIDATION FUNCTIONS (for normalization phase)
# =============================================================================

def validate_expression(expr: str, path: str) -> None:
    """Validate an expression without executing it."""
    if not expr or not expr.strip():
        raise ETLError('BAD_EXPR', 'ETL_ERROR: empty expression', path)
    try:
        parse_expression(expr, path)
    except ETLError:
        raise
    except Exception as e:
        raise ETLError('BAD_EXPR', f'ETL_ERROR: {e}', path)


def _require_string_field(step: Dict[str, Any], field: str, path: str) -> str:
    """Require a non-empty trimmed string field from step."""
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


# =============================================================================
# NORMALIZATION FUNCTIONS
# =============================================================================

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


_OP_NORMALIZERS = {
    'select': _normalize_select,
    'filter': _normalize_filter,
    'map': _normalize_map,
    'rename': _normalize_rename,
    'limit': _normalize_limit,
}


def normalize_step(step: Dict[str, Any], index: int) -> Dict[str, Any]:
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

    normalized = {'op': op}
    normalized.update(_OP_NORMALIZERS[op](step, path))

    # Reorder keys: op first, then alphabetically
    result = {'op': normalized['op']}
    result.update((k, normalized[k]) for k in sorted(k for k in normalized if k != 'op'))
    return result


# =============================================================================
# EXECUTION FUNCTIONS
# =============================================================================

def _execute_select(row: Dict[str, Any], columns: List[str], path: str, step_index: int) -> Dict[str, Any]:
    """Execute select operation on a single row."""
    result = {}
    for i, col in enumerate(columns):
        if col not in row:
            raise ETLError('MISSING_COLUMN',
                           f"ETL_ERROR: column '{col}' not found in row",
                           f'pipeline.steps[{step_index}].columns[{i}]')
        result[col] = row[col]
    return result


def _execute_filter(row: Dict[str, Any], where: str, path: str) -> bool:
    """Execute filter operation on a single row. Returns True if row passes."""
    result = evaluate_expression(where, row, path)
    # Convert result to boolean
    if result is None:
        return False
    if isinstance(result, bool):
        return result
    # Non-boolean values: truthy/falsy
    return bool(result)


def _execute_map(row: Dict[str, Any], as_field: str, expr: str, path: str) -> Dict[str, Any]:
    """Execute map operation on a single row."""
    result = row.copy()
    value = evaluate_expression(expr, row, path)
    result[as_field] = value
    return result


def _execute_rename(row: Dict[str, Any], mapping: Dict[str, str], path: str, step_index: int) -> Dict[str, Any]:
    """Execute rename operation on a single row."""
    result = row.copy()
    for src, dst in mapping.items():
        if src not in result:
            raise ETLError('MISSING_COLUMN',
                           f"ETL_ERROR: column '{src}' not found in row",
                           f'pipeline.steps[{step_index}].mapping.{src}')
        result[dst] = result[src]
        if dst != src:
            del result[src]
    return result


def _execute_limit(rows: List[Dict[str, Any]], n: int) -> List[Dict[str, Any]]:
    """Execute limit operation."""
    return rows[:n]


def execute_pipeline(steps: List[Dict[str, Any]], dataset: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """Execute the full pipeline on the dataset."""
    rows = [row.copy() for row in dataset]  # Deep copy to avoid modifying original
    rows_in = len(rows)

    for step_index, step in enumerate(steps):
        op = step['op']
        path = f'pipeline.steps[{step_index}]'

        try:
            if op == 'select':
                columns = step['columns']
                new_rows = []
                for row in rows:
                    new_rows.append(_execute_select(row, columns, path, step_index))
                rows = new_rows

            elif op == 'filter':
                where = step['where']
                rows = [row for row in rows if _execute_filter(row, where, f'{path}.where')]

            elif op == 'map':
                as_field = step['as']
                expr = step['expr']
                rows = [_execute_map(row, as_field, expr, f'{path}.expr') for row in rows]

            elif op == 'rename':
                mapping = step['mapping']
                new_rows = []
                for row in rows:
                    new_rows.append(_execute_rename(row, mapping, path, step_index))
                rows = new_rows

            elif op == 'limit':
                n = step['n']
                rows = _execute_limit(rows, n)

        except ETLError:
            raise
        except Exception as e:
            raise ETLError('EXECUTION_FAILED', f'ETL_ERROR: {e}', path)

    return rows, {'rows_in': rows_in, 'rows_out': len(rows)}


# =============================================================================
# MAIN PROCESSING
# =============================================================================

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
        # Execute the pipeline
        result_rows, metrics = execute_pipeline(normalized_steps, dataset)
        return {'status': 'ok', 'data': result_rows, 'metrics': metrics}
    else:
        # Just return normalized (checkpoint 1 behavior)
        return {'status': 'ok', 'normalized': {'steps': normalized_steps}}


def main():
    # Parse command line arguments
    args = sys.argv[1:]
    execute = False

    i = 0
    while i < len(args):
        arg = args[i]
        if arg == '--execute':
            # Check for value
            if i + 1 < len(args) and args[i + 1].lower() in ('true', 'false'):
                execute = args[i + 1].lower() == 'true'
                i += 2
            else:
                # Default to true if no value provided
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
