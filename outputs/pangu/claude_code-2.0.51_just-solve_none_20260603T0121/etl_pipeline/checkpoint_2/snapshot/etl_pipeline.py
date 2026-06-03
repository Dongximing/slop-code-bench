#!/usr/bin/env python3
"""ETL Pipeline CLI - Parses, validates, normalizes, and executes an ETL pipeline specification."""

import sys
import json
import argparse
from typing import Any

# --- Expression Syntax Validation ---

import re

class ExpressionError(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class UnknownOpError(Exception):
    def __init__(self, op_name: str):
        super().__init__(f"Unknown op: '{op_name}'")
        self.op_name = op_name


class ValidationError(Exception):
    def __init__(self, message: str, field: str = None):
        super().__init__(f"Validation error: {message}")
        self.message = message
        self.field = field


def validate_expression(expr: str) -> None:
    """Reject obvious syntax errors in arithmetic/comparison expressions."""
    stripped = expr.strip()
    if not stripped:
        raise ExpressionError("empty expression")

    # Consecutive operators (e.g., '^^', '+-', '**', '//', '&&', '||')
    if re.search(r'[+\-*/&|]{2,}', stripped):
        raise ExpressionError("consecutive operators")

    # Operators at start/end
    if re.match(r'^[+\-*/]', stripped) or re.search(r'[+\-*/]$', stripped):
        # Single - at start is allowed (negative number)
        if not re.match(r'^[-+*/]', stripped) and re.search(r'[+*/]', stripped):
            # Actually check more carefully
            if stripped[0] in '+*/' or stripped[-1] in '+-*/':
                raise ExpressionError("operator in invalid position")

    # Parentheses balance
    stack = 0
    for ch in stripped:
        if ch == '(':
            stack += 1
        elif ch == ')':
            stack -= 1
            if stack < 0:
                raise ExpressionError("unmatched closing parenthesis")
    if stack != 0:
        raise ExpressionError("unmatched parentheses")

    # Adjacent parens or parens next to operators are fine;
    # we only want to catch obvious syntax errors.
    # Check for unsupported operators - scan for any operator sequences not in our supported list
    # Unsupported: ** (pow), ^^ (power), // (floor div), etc.
    if re.search(r'\*{2}', stripped):
        raise ExpressionError("unsupported operator '**'")
    if re.search(r'\^\^', stripped):
        raise ExpressionError("unsupported operator '^^'")

    # Simple check: if the expression starts with an operator (except - for negation), flag it
    # Also flag if it ends with an operator
    # We allow leading - for negative numbers
    if stripped[0] in '+-*/' and stripped[0] != '-':
        raise ExpressionError("operator in invalid position")
    if stripped[-1] in '+-*/':
        raise ExpressionError("operator in invalid position")

    # Check for obvious consecutive operator patterns beyond what's handled above
    # This catches things like '^^', '++' at the basic level
    if re.search(r'[+]{2,}', stripped):
        raise ExpressionError("consecutive operators")

    # Check for operators at boundaries: next to parentheses or at start/end (basic check)
    # The main validation is done by the full tokenizer during execution


# --- Expression Evaluation ---

from enum import Enum, auto
from functools import lru_cache

class TokenType(Enum):
    NUMBER = auto()
    STRING = auto()
    IDENT = auto()       # column reference
    TRUE = auto()
    FALSE = auto()
    NULL = auto()
    OP = auto()          # includes +, -, *, /, etc.
    BOOL_OP = auto()     # &&, ||
    NOT = auto()         # ! or 'not'
    LPAREN = auto()
    RPAREN = auto()
    EOF = auto()

class Token:
    __slots__ = ('type', 'value')
    def __init__(self, type_, value=None):
        self.type = type_
        self.value = value
    def __repr__(self):
        return f"Token({self.type.name}, {self.value!r})"

OP_PRECEDENCE = {
    '||': 10, '&&': 11,
    '==': 20, '!=': 20,
    '<': 30, '<=': 30, '>': 30, '>=': 30,
    '+': 40, '-': 40,
    '*': 50, '/': 50,
    '!': 60,  # unary prefix
}

RIGHT_ASSOC = {'!'}

def tokenize(expr: str) -> list[Token]:
    """Convert expression string into tokens."""
    tokens = []
    i = 0
    n = len(expr)

    while i < n:
        c = expr[i]

        if c.isspace():
            i += 1
            continue

        # Numbers (including decimals and scientific notation)
        if c.isdigit() or (c == '.' and i + 1 < n and expr[i+1].isdigit()):
            start = i
            # Integer part
            if c == '.':
                # Already a dot, start with 0.
                pass
            else:
                i += 1
                while i < n and expr[i].isdigit():
                    i += 1
                # Optional decimal
                if i < n and expr[i] == '.':
                    i += 1
                    while i < n and expr[i].isdigit():
                        i += 1
            # Optional exponent
            if i < n and expr[i] in 'eE':
                i += 1
                if i < n and expr[i] in '+-':
                    i += 1
                while i < n and expr[i].isdigit():
                    i += 1
            num_str = expr[start:i]
            # Parse number
            if '.' in num_str or 'e' in num_str.lower():
                tokens.append(Token(TokenType.NUMBER, float(num_str)))
            else:
                tokens.append(Token(TokenType.NUMBER, int(num_str)))
            continue

        # Double-quoted string
        if c == '"':
            i += 1
            start = i
            chars = []
            while i < n and expr[i] != '"':
                chars.append(expr[i])
                i += 1
            if i >= n:
                raise ExpressionError("unterminated string literal")
            # i at closing quote
            tokens.append(Token(TokenType.STRING, ''.join(chars)))
            i += 1
            continue

        # Boolean / null literals
        if expr.startswith('true', i):
            tokens.append(Token(TokenType.TRUE))
            i += 4
            continue
        if expr.startswith('false', i):
            tokens.append(Token(TokenType.FALSE))
            i += 5
            continue
        if expr.startswith('null', i):
            tokens.append(Token(TokenType.NULL))
            i += 4
            continue

        # Operators and punctuation (longest match first)
        for op_str in ['==', '!=', '<=', '>=', '&&', '||']:
            if expr.startswith(op_str, i):
                tokens.append(Token(TokenType.BOOL_OP if op_str in ('&&', '||') else TokenType.OP, op_str))
                i += len(op_str)
                break
        else:
            if expr[i] == '!':
                tokens.append(Token(TokenType.NOT, '!'))
                i += 1
                continue
            if expr[i] in '+-*/<>()':
                ch = expr[i]
                if ch in '()':
                    tokens.append(Token(TokenType.LPAREN if ch == '(' else TokenType.RPAREN))
                else:
                    tokens.append(Token(TokenType.OP, ch))
                i += 1
                continue

            # Identifier (column name)
            if c.isalpha() or c == '_':
                start = i
                while i < n and (expr[i].isalnum() or expr[i] == '_'):
                    i += 1
                ident = expr[start:i]
                tokens.append(Token(TokenType.IDENT, ident))
                continue

            raise ExpressionError(f"unexpected character '{c}'")

    tokens.append(Token(TokenType.EOF))
    return tokens


def evaluate_expression(expr: str, row: dict) -> Any:
    """Evaluate expression against a row, returning the result.

    Rules:
    - Arithmetic with null -> null
    - Division by zero -> null
    - Comparisons involving null -> false
    - Null is falsy in boolean context
    - Type mismatch in ordering/comparison returns false
    """
    tokens = tokenize(expr)

    class Parser:
        def __init__(self, tokens: list[Token]):
            self.tokens = tokens
            self.pos = 0

        def cur_tok(self):
            if self.pos < len(self.tokens):
                return self.tokens[self.pos]
            return Token(TokenType.EOF)

        def advance(self):
            self.pos += 1

        def parse_expr(self, rbp=0):
            # Parse left side (prefix operator or operand)
            tok = self.cur_tok()
            if tok.type == TokenType.EOF:
                raise ExpressionError("unexpected end of expression")

            left = self.parse_atom()

            # Parse infix operators, right-associative for '**' (not supported)
            while True:
                tok = self.cur_tok()
                if tok.type == TokenType.EOF:
                    break
                precedence = OP_PRECEDENCE.get(tok.value, 0)
                if precedence < rbp:
                    break
                if tok.type not in (TokenType.OP, TokenType.BOOL_OP):
                    break
                # It's an infix operator
                self.advance()
                right = self.parse_expr(precedence)
                left = ('binop', tok.value, left, right)

            return left

        def parse_atom(self):
            tok = self.cur_tok()
            if tok.type == TokenType.EOF:
                raise ExpressionError("unexpected end of expression")

            # Unary operators
            if tok.value == '!':
                self.advance()
                arg = self.parse_atom()
                return ('unary', '!', arg)
            if tok.type == TokenType.OP and tok.value == '-':
                self.advance()
                arg = self.parse_atom()
                return ('unary', '-', arg)

            # Parenthesized expression
            if tok.type == TokenType.LPAREN:
                self.advance()
                expr = self.parse_expr(0)
                if self.cur_tok().type != TokenType.RPAREN:
                    raise ExpressionError("missing closing parenthesis")
                self.advance()
                return ('parens', expr)

            # Literals
            if tok.type == TokenType.NUMBER:
                self.advance()
                return ('literal', tok.value)
            if tok.type == TokenType.STRING:
                self.advance()
                return ('literal', tok.value)
            if tok.type == TokenType.TRUE:
                self.advance()
                return ('literal', True)
            if tok.type == TokenType.FALSE:
                self.advance()
                return ('literal', False)
            if tok.type == TokenType.NULL:
                self.advance()
                return ('literal', None)

            # Identifier (column reference)
            if tok.type == TokenType.IDENT:
                self.advance()
                return ('ident', tok.value)

            raise ExpressionError(f"unexpected token {tok}")

    parser = Parser(tokens)
    ast = parser.parse_expr(0)

    def evaluate(node):
        kind = node[0]
        if kind == 'literal':
            return node[1]
        if kind == 'ident':
            return row.get(node[1])  # Missing identifiers -> null
        if kind == 'unary':
            op = node[1]
            arg = evaluate(node[2])
            if op == '!':
                # logical NOT - only True is truthy, everything else falsy
                return not (arg is True)
            if op == '-':
                if arg is None:
                    return None
                if not isinstance(arg, (int, float)):
                    return None
                return -arg
            raise ExpressionError(f"unknown unary op {op}")
        if kind == 'binop':
            op = node[1]
            l = evaluate(node[2])
            r = evaluate(node[3])

            # Short-circuit boolean operators
            if op == '&&':
                if l is not True and l is not False:
                    return None  # null falsy
                if l is not True:
                    return False
                return r if r is True else False
            if op == '||':
                l_val = l is True
                if l_val:
                    return l
                # r could be null
                return r if r is True else False

            # Arithmetic: null operand -> null
            if op in ('+', '-', '*', '/'):
                if l is None or r is None:
                    return None
                if op == '+':
                    if isinstance(l, (int, float)) and isinstance(r, (int, float)):
                        return l + r
                    return None
                if op == '-':
                    if isinstance(l, (int, float)) and isinstance(r, (int, float)):
                        return l - r
                    return None
                if op == '*':
                    if isinstance(l, (int, float)) and isinstance(r, (int, float)):
                        return l * r
                    return None
                if op == '/':
                    if isinstance(l, (int, float)) and isinstance(r, (int, float)):
                        if r == 0:
                            return None
                        return l / r
                    return None

            # Comparison operators
            # Comparisons involving null -> false
            if l is None or r is None:
                return False
            # Type mismatch -> false
            if type(l) != type(r):
                # But allow int == float comparison
                if not (isinstance(l, (int, float)) and isinstance(r, (int, float))):
                    return False

            if op == '==':
                return l == r
            if op == '!=':
                return l != r
            if op in ('<', '<=', '>', '>='):
                if not isinstance(l, (int, float)) or not isinstance(r, (int, float)):
                    return False
                if op == '<':
                    return l < r
                if op == '<=':
                    return l <= r
                if op == '>':
                    return l > r
                if op == '>=':
                    return l >= r
            raise ExpressionError(f"unknown binop {op}")
        raise ExpressionError(f"unknown node kind {kind}")

    return evaluate(ast)


# --- Pipeline Execution ---

class ExecutionError(Exception):
    """Error during pipeline execution."""
    def __init__(self, message: str, path: str = None):
        super().__init__()
        self.message = message
        self.path = path


def execute_select(step: dict, dataset: list[dict]) -> list[dict]:
    """Execute select step: keep only specified columns."""
    cols = step['columns']
    result = []
    for row in dataset:
        new_row = {}
        for col in cols:
            if col not in row:
                raise ExecutionError(
                    f"column '{col}' not found in row",
                    path=f"columns with value '{col}'"
                )
            new_row[col] = row[col]
        result.append(new_row)
    return result


def execute_filter(step: dict, dataset: list[dict]) -> list[dict]:
    """Execute filter step: keep rows where where expression evaluates to true."""
    expr = step['where']
    result = []
    for row in dataset:
        try:
            val = evaluate_expression(expr, row)
        except ExpressionError as e:
            raise ExecutionError(f"expression error: {e}")
        # Filter keeps row only if expression evaluates to true
        if val is True:
            result.append(row)
    return result


def execute_map(step: dict, dataset: list[dict]) -> list[dict]:
    """Execute map step: add or overwrite a field with expression result."""
    as_name = step['as']
    expr = step['expr']
    result = []
    for row in dataset:
        new_row = dict(row)  # copy
        try:
            val = evaluate_expression(expr, row)
        except ExpressionError as e:
            raise ExecutionError(f"expression error: {e}")
        new_row[as_name] = val
        result.append(new_row)
    return result


def execute_rename(step: dict, dataset: list[dict]) -> list[dict]:
    """Execute rename step: rename columns according to mapping."""
    mapping = step['mapping']
    result = []
    for row in dataset:
        new_row = {}
        for src, tgt in mapping.items():
            if src not in row:
                raise ExecutionError(
                    f"column '{src}' not found in row",
                    path=f"mapping with key '{src}'"
                )
            new_row[tgt] = row[src]
        # Also include columns not in mapping (preserve them)
        for k, v in row.items():
            if k not in mapping:
                new_row[k] = v
        result.append(new_row)
    return result


def execute_limit(step: dict, dataset: list[dict]) -> list[dict]:
    """Execute limit step: return at most n rows."""
    n = step['n']
    return dataset[:n]


STEP_EXECUTORS = {
    'select': execute_select,
    'filter': execute_filter,
    'map': execute_map,
    'rename': execute_rename,
    'limit': execute_limit,
}


def execute_pipeline(steps: list[dict], dataset: list[dict]) -> tuple[list[dict], dict]:
    """Execute pipeline steps on dataset, return (result, metrics)."""
    rows_in = len(dataset)
    data = dataset
    for i, step in enumerate(steps):
        op = step['op']
        if op not in STEP_EXECUTORS:
            raise ExecutionError(f"unsupported op '{op}'", path=f"steps[{i}].op")
        try:
            data = STEP_EXECUTORS[op](step, data)
        except ExecutionError as e:
            # Add step index to path if not already present
            if e.path and not e.path.startswith('steps['):
                e.path = f"steps[{i}].{e.path}"
            elif not e.path:
                e.path = f"steps[{i}]"
            raise
    rows_out = len(data)
    metrics = {'rows_in': rows_in, 'rows_out': rows_out}
    return data, metrics


# --- Step Normalization ---

def normalize_op(op: str) -> str:
    """Normalize operation name: trim and lower."""
    stripped = op.strip()
    if not stripped:
        raise ValueError("empty op")
    lower = stripped.lower()
    # Validate against known ops (used for error messages)
    return lower


def normalize_select(step: dict, index: int) -> dict:
    """Select step: columns must be a non-empty list of strings."""
    if 'columns' not in step:
        raise KeyError("columns")
    cols = step['columns']
    if not isinstance(cols, list) or len(cols) == 0:
        raise TypeError("columns must be a non-empty array")
    if not all(isinstance(c, str) for c in cols):
        raise TypeError("columns elements must be strings")
    # Column names preserved exactly (spaces significant)
    result = {'op': 'select', 'columns': cols}
    # Known extra keys none (all others dropped)
    return result


def normalize_filter(step: dict, index: int) -> dict:
    """Filter step: 'where' must be a non-empty string expression."""
    if 'where' not in step:
        raise KeyError("where")
    where_expr = step['where']
    if not isinstance(where_expr, str):
        raise TypeError("where must be a string")
    trimmed = where_expr.strip()
    if not trimmed:
        raise ValueError("where expression is empty after trimming")
    validate_expression(trimmed)
    result = {'op': 'filter', 'where': trimmed}
    return result


def normalize_map(step: dict, index: int) -> dict:
    """Map step: 'as' and 'expr' required."""
    if 'as' not in step:
        raise KeyError("as")
    if 'expr' not in step:
        raise KeyError("expr")
    as_name = step['as']
    expr = step['expr']
    if not isinstance(as_name, str):
        raise TypeError("as must be a string")
    if not isinstance(expr, str):
        raise TypeError("expr must be a string")
    as_trimmed = as_name.strip()
    if not as_trimmed:
        raise ValueError("as is empty after trimming")
    expr_trimmed = expr.strip()
    if not expr_trimmed:
        raise ValueError("expr is empty after trimming")
    validate_expression(expr_trimmed)
    result = {'op': 'map', 'as': as_trimmed, 'expr': expr_trimmed}
    return result


def normalize_rename(step: dict, index: int) -> dict:
    """Rename step: either 'from'/'to' or 'mapping'."""
    has_from_to = ('from' in step) and ('to' in step)
    has_mapping = 'mapping' in step

    if not (has_from_to or has_mapping):
        # Determine which is missing
        if 'from' not in step:
            raise KeyError("from")
        elif 'to' not in step:
            raise KeyError("to")
        else:
            raise KeyError("mapping")

    if has_from_to:
        from_val = step['from']
        to_val = step['to']
        if not isinstance(from_val, str):
            raise TypeError("from must be a string")
        if not isinstance(to_val, str):
            raise TypeError("to must be a string")
        from_trimmed = from_val.strip()
        to_trimmed = to_val.strip()
        if not from_trimmed:
            raise ValueError("from is empty after trimming")
        if not to_trimmed:
            raise ValueError("to is empty after trimming")
        mapping = {from_trimmed: to_trimmed}
    else:
        mapping_obj = step['mapping']
        if not isinstance(mapping_obj, dict):
            raise TypeError("mapping must be an object")
        # Preserve keys/values exactly (spaces significant)
        mapping = mapping_obj

    return {'op': 'rename', 'mapping': mapping}


def normalize_limit(step: dict, index: int) -> dict:
    """Limit step: 'n' must be a non-negative integer."""
    if 'n' not in step:
        raise KeyError("n")
    n = step['n']
    if not isinstance(n, int):
        raise TypeError("n must be an integer")
    if n < 0:
        raise ValueError("n must be >= 0")
    return {'op': 'limit', 'n': n}


SUPPORTED_OPS = {
    'select': normalize_select,
    'filter': normalize_filter,
    'map': normalize_map,
    'rename': normalize_rename,
    'limit': normalize_limit,
}


def normalize_step(step: dict, index: int) -> dict:
    """Normalize a single pipeline step."""
    if not isinstance(step, dict):
        raise TypeError("step must be an object")
    if 'op' not in step:
        raise ValidationError("missing 'op' in step", field="op")

    op_raw = step['op']
    if not isinstance(op_raw, str):
        raise TypeError("op must be a string")

    normalized_op = normalize_op(op_raw)

    if normalized_op not in SUPPORTED_OPS:
        raise UnknownOpError(normalized_op)

    # Drop unknown keys before passing to normalizer
    known_op_keys = {
        'select': {'columns'},
        'filter': {'where'},
        'map': {'as', 'expr'},
        'rename': {'from', 'to', 'mapping'},
        'limit': {'n'},
    }
    # Also include 'op' so we don't drop it
    allowed_keys = known_op_keys[normalized_op] | {'op'}
    filtered_step = {k: v for k, v in step.items() if k in allowed_keys}

    try:
        return SUPPORTED_OPS[normalized_op](filtered_step, index)
    except (ValidationError, TypeError, ValueError, ExpressionError) as e:
        # Re-raise with context
        raise


# --- CLI Argument Parsing ---

def parse_args():
    parser = argparse.ArgumentParser(
        description="ETL Pipeline Executor",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:
  # Normalize mode (default)
  echo '{"pipeline":{"steps":[]},"dataset":[]}' | python etl_pipeline.py

  # Execute mode
  echo '{"pipeline":{"steps":[]},"dataset":[]}' | python etl_pipeline.py --execute"""
    )
    parser.add_argument(
        '--execute',
        action='store_true',
        default=False,
        help='Execute the pipeline and return data and metrics (no normalized field)'
    )
    return parser.parse_args()


# --- Main ---

def main() -> None:
    args = parse_args()
    execute_mode = args.execute

    try:
        data = json.load(sys.stdin)
    except json.JSONDecodeError as e:
        # Not valid JSON
        print(json.dumps({
            "status": "error",
            "error_code": "SCHEMA_VALIDATION_FAILED",
            "message": f"ETL_ERROR: invalid JSON: {e}",
            "path": "",
        }), flush=True)
        sys.exit(1)

    # Validate top-level structure
    if not isinstance(data, dict):
        print(json.dumps({
            "status": "error",
            "error_code": "SCHEMA_VALIDATION_FAILED",
            "message": "ETL_ERROR: root must be an object",
            "path": "",
        }), flush=True)
        sys.exit(1)

    # 'pipeline' is required (though steps can be empty)
    if 'pipeline' not in data:
        print(json.dumps({
            "status": "error",
            "error_code": "SCHEMA_VALIDATION_FAILED",
            "message": "ETL_ERROR: missing 'pipeline' key",
            "path": "",
        }), flush=True)
        sys.exit(1)

    pipeline = data['pipeline']
    if not isinstance(pipeline, dict):
        print(json.dumps({
            "status": "error",
            "error_code": "SCHEMA_VALIDATION_FAILED",
            "message": "ETL_ERROR: 'pipeline' must be an object",
            "path": "pipeline",
        }), flush=True)
        sys.exit(1)

    if 'steps' not in pipeline:
        print(json.dumps({
            "status": "error",
            "error_code": "SCHEMA_VALIDATION_FAILED",
            "message": "ETL_ERROR: missing 'pipeline.steps'",
            "path": "pipeline.steps",
        }), flush=True)
        sys.exit(1)

    steps = pipeline['steps']
    if not isinstance(steps, list):
        print(json.dumps({
            "status": "error",
            "error_code": "SCHEMA_VALIDATION_FAILED",
            "message": "ETL_ERROR: 'pipeline.steps' must be an array",
            "path": "pipeline.steps",
        }), flush=True)
        sys.exit(1)

    # dataset is required; each element must be an object
    if 'dataset' not in data:
        print(json.dumps({
            "status": "error",
            "error_code": "SCHEMA_VALIDATION_FAILED",
            "message": "ETL_ERROR: missing 'dataset' key",
            "path": "dataset",
        }), flush=True)
        sys.exit(1)

    dataset = data['dataset']
    if not isinstance(dataset, list):
        print(json.dumps({
            "status": "error",
            "error_code": "SCHEMA_VALIDATION_FAILED",
            "message": "ETL_ERROR: 'dataset' must be an array",
            "path": "dataset",
        }), flush=True)
        sys.exit(1)

    for i, row in enumerate(dataset):
        if not isinstance(row, dict):
            print(json.dumps({
                "status": "error",
                "error_code": "SCHEMA_VALIDATION_FAILED",
                "message": f"ETL_ERROR: dataset[{i}] must be an object",
                "path": f"dataset[{i}]",
            }), flush=True)
            sys.exit(1)

    # Process steps (normalize)
    normalized_steps = []
    for i, step in enumerate(steps):
        path_prefix = f"pipeline.steps[{i}]"
        try:
            norm = normalize_step(step, i)
            normalized_steps.append(norm)
        except UnknownOpError as e:
            print(json.dumps({
                "status": "error",
                "error_code": "UNKNOWN_OP",
                "message": f"ETL_ERROR: unsupported op '{e.op_name}'",
                "path": f"{path_prefix}.op",
            }), flush=True)
            sys.exit(1)
        except ValidationError as e:
            field = e.field if e.field else ""
            print(json.dumps({
                "status": "error",
                "error_code": "SCHEMA_VALIDATION_FAILED",
                "message": f"ETL_ERROR: {e.message}",
                "path": f"{path_prefix}.{field}" if field else path_prefix,
            }), flush=True)
            sys.exit(1)
        except TypeError as e:
            msg = str(e)
            # Infer field from message
            if "must be a string" in msg:
                if "op" in msg:
                    print(json.dumps({
                        "status": "error",
                        "error_code": "SCHEMA_VALIDATION_FAILED",
                        "message": f"ETL_ERROR: {msg}",
                        "path": f"{path_prefix}.op",
                    }), flush=True)
                else:
                    print(json.dumps({
                        "status": "error",
                        "error_code": "SCHEMA_VALIDATION_FAILED",
                        "message": f"ETL_ERROR: {msg}",
                        "path": f"{path_prefix}",
                    }), flush=True)
            else:
                print(json.dumps({
                    "status": "error",
                    "error_code": "SCHEMA_VALIDATION_FAILED",
                    "message": f"ETL_ERROR: {msg}",
                    "path": f"{path_prefix}",
                }), flush=True)
            sys.exit(1)
        except ValueError as e:
            msg = str(e)
            if "empty" in msg:
                # Infer which field
                if "where expression" in msg:
                    print(json.dumps({
                        "status": "error",
                        "error_code": "BAD_EXPR",
                        "message": f"ETL_ERROR: {msg}",
                        "path": f"{path_prefix}.where",
                    }), flush=True)
                elif "expr" in msg:
                    print(json.dumps({
                        "status": "error",
                        "error_code": "BAD_EXPR",
                        "message": f"ETL_ERROR: {msg}",
                        "path": f"{path_prefix}.expr",
                    }), flush=True)
                elif "from" in msg:
                    print(json.dumps({
                        "status": "error",
                        "error_code": "SCHEMA_VALIDATION_FAILED",
                        "message": f"ETL_ERROR: {msg}",
                        "path": f"{path_prefix}.from",
                    }), flush=True)
                elif "to" in msg:
                    print(json.dumps({
                        "status": "error",
                        "error_code": "SCHEMA_VALIDATION_FAILED",
                        "message": f"ETL_ERROR: {msg}",
                        "path": f"{path_prefix}.to",
                    }), flush=True)
                elif "as" in msg:
                    print(json.dumps({
                        "status": "error",
                        "error_code": "SCHEMA_VALIDATION_FAILED",
                        "message": f"ETL_ERROR: {msg}",
                        "path": f"{path_prefix}.as",
                    }), flush=True)
                else:
                    print(json.dumps({
                        "status": "error",
                        "error_code": "SCHEMA_VALIDATION_FAILED",
                        "message": f"ETL_ERROR: {msg}",
                        "path": f"{path_prefix}",
                    }), flush=True)
            else:
                print(json.dumps({
                    "status": "error",
                    "error_code": "SCHEMA_VALIDATION_FAILED",
                    "message": f"ETL_ERROR: {msg}",
                    "path": f"{path_prefix}",
                }), flush=True)
            sys.exit(1)
        except ExpressionError as e:
            # Determine if this is a filter or map expr
            step_fields = list(step.keys())
            if 'where' in step_fields:
                print(json.dumps({
                    "status": "error",
                    "error_code": "BAD_EXPR",
                    "message": f"ETL_ERROR: {e.message}",
                    "path": f"{path_prefix}.where",
                }), flush=True)
            else:
                print(json.dumps({
                    "status": "error",
                    "error_code": "BAD_EXPR",
                    "message": f"ETL_ERROR: {e.message}",
                    "path": f"{path_prefix}.expr",
                }), flush=True)
            sys.exit(1)

    if execute_mode:
        # Execute the pipeline
        try:
            result_data, metrics = execute_pipeline(normalized_steps, dataset)
            output = {
                "status": "ok",
                "data": result_data,
                "metrics": metrics
            }
        except ExecutionError as e:
            # Map ExecutionError to appropriate error code
            error_code = "EXECUTION_FAILED"
            if "column" in e.message.lower() and "not found" in e.message.lower():
                error_code = "MISSING_COLUMN"
            elif "expression" in e.message.lower():
                error_code = "BAD_EXPR"

            print(json.dumps({
                "status": "error",
                "error_code": error_code,
                "message": f"ETL_ERROR: {e.message}",
                "path": e.path,
            }), flush=True)
            sys.exit(1)
        except Exception as e:
            # Catch any unexpected errors during execution
            print(json.dumps({
                "status": "error",
                "error_code": "EXECUTION_FAILED",
                "message": f"ETL_ERROR: {e}",
                "path": "pipeline",
            }), flush=True)
            sys.exit(1)
    else:
        # Normal mode - return normalized
        output = {
            "status": "ok",
            "normalized": {
                "steps": normalized_steps
            }
        }

    print(json.dumps(output), flush=True)
    sys.exit(0)


if __name__ == "__main__":
    main()
