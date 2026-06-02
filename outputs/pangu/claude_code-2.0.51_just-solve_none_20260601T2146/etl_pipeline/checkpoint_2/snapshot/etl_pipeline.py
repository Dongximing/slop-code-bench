#!/usr/bin/env python3
"""ETL Pipeline Validator, Normalizer, and Executor.

Reads a JSON ETL pipeline specification from STDIN, validates and either:
- Normalizes the pipeline (without --execute)
- Executes the pipeline and returns transformed data (with --execute)
"""

import argparse
import json
import re
import sys
from typing import Any, Callable, List, Optional, Tuple


# ============================================================================
# Error Handling
# ============================================================================

def error_response(code: str, message: str, path: str) -> None:
    """Output error JSON and exit with code 1."""
    print(json.dumps({
        "status": "error",
        "error_code": code,
        "message": f"ETL_ERROR: {message}",
        "path": path,
    }))
    sys.exit(1)


# ============================================================================
# Input Parsing
# ============================================================================

def _required_field(step: dict, name: str, index: int):
    """Get required field or error."""
    if name not in step:
        error_response("SCHEMA_VALIDATION_FAILED",
                       f"Step is missing required '{name}' field",
                       f"pipeline.steps[{index}]")
    return step[name]


def _non_empty_string(value, field_path: str) -> str:
    """Validate and return non-empty trimmed string."""
    if not isinstance(value, str):
        error_response("SCHEMA_VALIDATION_FAILED",
                       f"'{field_path.split('.')[-1]}' must be a string",
                       field_path)
    trimmed = value.strip()
    if not trimmed:
        error_response("SCHEMA_VALIDATION_FAILED",
                       f"'{field_path.split('.')[-1]}' field is empty after trimming",
                       field_path)
    return trimmed


def parse_input() -> Tuple[dict, list]:
    """Parse JSON from STDIN, return (pipeline, dataset)."""
    try:
        data = json.load(sys.stdin)
    except json.JSONDecodeError as e:
        error_response("SCHEMA_VALIDATION_FAILED", f"Invalid JSON: {e}", "root")

    pipeline = data.get("pipeline", {})
    dataset = data.get("dataset", [])

    if not isinstance(dataset, list):
        error_response("SCHEMA_VALIDATION_FAILED", "dataset must be an array", "dataset")

    return pipeline, dataset


# ============================================================================
# Step Normalizers (from checkpoint 1)
# ============================================================================

def normalize_select(step: dict, index: int) -> dict:
    """Normalize select operation."""
    columns = _required_field(step, "columns", index)
    if not isinstance(columns, list):
        error_response("SCHEMA_VALIDATION_FAILED", "'columns' must be an array", f"pipeline.steps[{index}].columns")
    for i, col in enumerate(columns):
        if not isinstance(col, str):
            error_response("SCHEMA_VALIDATION_FAILED", f"columns[{i}] must be a string", f"pipeline.steps[{index}].columns[{i}]")
        if col == "":
            error_response("SCHEMA_VALIDATION_FAILED", f"columns[{i}] cannot be empty", f"pipeline.steps[{index}].columns[{i}]")
    return {"op": "select", "columns": columns}


def normalize_filter(step: dict, index: int) -> dict:
    """Normalize filter operation."""
    where = _non_empty_string(_required_field(step, "where", index), f"pipeline.steps[{index}].where")
    validate_expression(where, index, "where")
    return {"op": "filter", "where": where}


def normalize_map(step: dict, index: int) -> dict:
    """Normalize map operation."""
    as_field = _non_empty_string(_required_field(step, "as", index), f"pipeline.steps[{index}].as")
    expr = _non_empty_string(_required_field(step, "expr", index), f"pipeline.steps[{index}].expr")
    validate_expression(expr, index, "expr")
    return {"op": "map", "as": as_field, "expr": expr}


def normalize_rename(step: dict, index: int) -> dict:
    """Normalize rename operation."""
    has_from_to = "from" in step and "to" in step
    has_mapping = "mapping" in step

    if not (has_from_to or has_mapping):
        error_response("SCHEMA_VALIDATION_FAILED",
                       "rename step requires either ('from' and 'to') or 'mapping'", f"pipeline.steps[{index}]")
    if has_from_to and has_mapping:
        error_response("SCHEMA_VALIDATION_FAILED",
                       "rename step cannot have both ('from' and 'to') and 'mapping'", f"pipeline.steps[{index}]")

    if has_from_to:
        from_field = _non_empty_string(step["from"], f"pipeline.steps[{index}].from")
        to_field = _non_empty_string(step["to"], f"pipeline.steps[{index}].to")
        mapping = {from_field: to_field}
    else:
        mapping_obj = _required_field(step, "mapping", index)
        if not isinstance(mapping_obj, dict):
            error_response("SCHEMA_VALIDATION_FAILED", "'mapping' must be an object", f"pipeline.steps[{index}].mapping")
        mapping = {}
        for key, value in mapping_obj.items():
            if not isinstance(key, str):
                error_response("SCHEMA_VALIDATION_FAILED", "mapping keys must be strings", f"pipeline.steps[{index}].mapping")
            if not isinstance(value, str):
                error_response("SCHEMA_VALIDATION_FAILED", "mapping values must be strings", f"pipeline.steps[{index}].mapping[{json.dumps(key)}]")
            mapping[key] = value
    return {"op": "rename", "mapping": mapping}


def normalize_limit(step: dict, index: int) -> dict:
    """Normalize limit operation."""
    n = _required_field(step, "n", index)
    if not isinstance(n, int):
        error_response("SCHEMA_VALIDATION_FAILED", "'n' must be an integer", f"pipeline.steps[{index}].n")
    if n < 0:
        error_response("SCHEMA_VALIDATION_FAILED", "'n' must be >= 0", f"pipeline.steps[{index}].n")
    return {"op": "limit", "n": n}


def validate_expression(expr: str, index: int, field: str) -> None:
    """Validate expression syntax for filter/where fields."""
    # Check for unsupported double-character operators
    consecutive_ops = re.search(
        r'(\+\+|--|\*\*|/\/|<<|>>|\|\||&&|\^%|[<>]\s*[<>])',
        expr
    )
    if consecutive_ops:
        error_response("BAD_EXPR", f"unsupported operator '{consecutive_ops.group()}'", f"pipeline.steps[{index}].{field}")
    if not expr.strip():
        error_response("SCHEMA_VALIDATION_FAILED", f"{field} expression is empty", f"pipeline.steps[{index}].{field}")


def normalize_step(step: dict, index: int) -> dict:
    """Normalize a single step object."""
    if not isinstance(step, dict):
        error_response("SCHEMA_VALIDATION_FAILED",
                       "Each step must be an object", f"pipeline.steps[{index}]")
    if "op" not in step:
        error_response("SCHEMA_VALIDATION_FAILED",
                       "Step is missing required 'op' field", f"pipeline.steps[{index}]")

    op = step["op"]
    if not isinstance(op, str):
        error_response("SCHEMA_VALIDATION_FAILED", "'op' must be a string", f"pipeline.steps[{index}].op")

    op_normalized = op.strip().lower()
    if not op_normalized:
        error_response("SCHEMA_VALIDATION_FAILED", "'op' field is empty after trimming", f"pipeline.steps[{index}].op")

    dispatch = {
        "select": normalize_select,
        "filter": normalize_filter,
        "map": normalize_map,
        "rename": normalize_rename,
        "limit": normalize_limit,
    }
    handler = dispatch.get(op_normalized)
    if handler is None:
        error_response("UNKNOWN_OP", f"unsupported op '{op_normalized}'", f"pipeline.steps[{index}].op")
    return handler(step, index)


# ============================================================================
# Expression Parser and Evaluator
# ============================================================================

class TokenType:
    LITERAL = "LITERAL"
    IDENTIFIER = "IDENTIFIER"
    OPERATOR = "OPERATOR"
    LPAREN = "LPAREN"
    RPAREN = "RPAREN"
    EOF = "EOF"


class Token:
    def __init__(self, type_: str, value: Any):
        self.type = type_
        self.value = value

    def __repr__(self):
        return f"Token({self.type}, {repr(self.value)})"


# Operator precedence (higher number = higher precedence)
PRECEDENCE = {
    "||": 10,
    "&&": 20,
    "==": 30, "!=": 30,
    "<": 40, "<=": 40, ">": 40, ">=": 40,
    "|": 50,  # unsupported but for completeness
    "&": 60,
    "+": 70, "-": 70,
    "*": 80, "/": 80,
    "!": 90,  # unary
}

RIGHT_ASSOC = {"!": True}  # Operators that are right-associative


class Tokenizer:
    """Tokenizes expression strings."""

    OPERATOR_PATTERN = re.compile(r"==|!=|<=|>=|\|\||&&|\+\+|--|\*\*|/\/|<<|>>|\^%|[!\*\+/<>|&]=?|[|&]")

    # Recognized two-character operators first, then single char
    TWO_CHAR_OPS = {'==', '!=', '<=', '>=', '||', '&&'}
    SINGLE_CHAR_OPS = {'!', '*', '/', '+', '-', '<', '>', '(', ')', ' ', '\t', '\n'}

    def __init__(self, expr: str):
        self.expr = expr
        self.pos = 0

    def peek(self) -> Optional[str]:
        if self.pos >= len(self.expr):
            return None
        return self.expr[self.pos]

    def consume(self) -> str:
        ch = self.peek()
        self.pos += 1
        return ch

    def next_token(self) -> Token:
        # Skip whitespace
        while self.pos < len(self.expr) and self.expr[self.pos] in ' \t\n\r':
            self.pos += 1

        if self.pos >= len(self.expr):
            return Token(TokenType.EOF, None)

        ch = self.expr[self.pos]

        # Two-character operators
        if self.pos + 1 < len(self.expr):
            two_char = self.expr[self.pos:self.pos + 2]
            if two_char in self.TWO_CHAR_OPS:
                self.pos += 2
                return Token(TokenType.OPERATOR, two_char)

        # Single character operators/parentheses
        if ch in '()':
            self.pos += 1
            return Token(TokenType.LPAREN if ch == '(' else TokenType.RPAREN, ch)

        if ch in self.SINGLE_CHAR_OPS:
            self.pos += 1
            # Check for unary !
            if ch == '!':
                return Token(TokenType.OPERATOR, '!')
            if ch in '*/+-<>|&':
                return Token(TokenType.OPERATOR, ch)

        # String literal (double-quoted)
        if ch == '"':
            self.pos += 1  # consume opening quote
            result = []
            while self.pos < len(self.expr) and self.expr[self.pos] != '"':
                result.append(self.expr[self.pos])
                self.pos += 1
            if self.pos >= len(self.expr):
                raise ValueError("Unterminated string literal")
            self.pos += 1  # consume closing quote
            return Token(TokenType.LITERAL, ''.join(result))

        # Number literal
        if ch.isdigit() or (ch == '-' and self.pos + 1 < len(self.expr) and self.expr[self.pos + 1].isdigit()):
            start = self.pos
            # Handle negative sign
            if ch == '-':
                self.pos += 1
            # Read the number
            while self.pos < len(self.expr) and (self.expr[self.pos].isdigit() or self.expr[self.pos] == '.'):
                self.pos += 1
            num_str = self.expr[start:self.pos]
            if '.' in num_str:
                return Token(TokenType.LITERAL, float(num_str))
            return Token(TokenType.LITERAL, int(num_str))

        # true, false, null literals
        for keyword in ['true', 'false', 'null']:
            if self.expr.startswith(keyword, self.pos):
                self.pos += len(keyword)
                if keyword == 'true':
                    return Token(TokenType.LITERAL, True)
                elif keyword == 'false':
                    return Token(TokenType.LITERAL, False)
                else:
                    return Token(TokenType.LITERAL, None)

        # Identifier (field name)
        start = self.pos
        while self.pos < len(self.expr) and (self.expr[self.pos].isalnum() or self.expr[self.pos] == '_'):
            self.pos += 1
        ident = self.expr[start:self.pos]
        if ident:
            return Token(TokenType.IDENTIFIER, ident)

        raise ValueError(f"Unexpected character: {ch}")


class Parser:
    """Recursive descent parser for expression language."""

    def __init__(self, tokens: Tokenizer):
        self.tokens = tokens
        self.current = None
        self._advance()

    def _advance(self):
        self.current = self.tokens.next_token()

    def _match(self, type_: str) -> bool:
        return self.current.type == type_

    def _check(self, type_: str) -> bool:
        return self.current.type == type_

    def _consume(self, type_: str, error_msg: str) -> Token:
        if self._match(type_):
            token = self.current
            self._advance()
            return token
        raise ValueError(error_msg)

    def parse(self) -> 'ASTNode':
        return self._parse_expression()

    def _parse_expression(self) -> 'ASTNode':
        return self._parse_or()

    def _parse_or(self) -> 'ASTNode':
        node = self._parse_and()
        while self._check(TokenType.OPERATOR) and self.current.value == '||':
            op = self.current
            self._advance()
            right = self._parse_and()
            node = BinaryNode('||', node, right)
        return node

    def _parse_and(self) -> 'ASTNode':
        node = self._parse_equality()
        while self._check(TokenType.OPERATOR) and self.current.value == '&&':
            op = self.current
            self._advance()
            right = self._parse_equality()
            node = BinaryNode('&&', node, right)
        return node

    def _parse_equality(self) -> 'ASTNode':
        node = self._parse_comparison()
        while self._check(TokenType.OPERATOR) and self.current.value in ('==', '!='):
            op = self.current
            self._advance()
            right = self._parse_comparison()
            node = BinaryNode(op.value, node, right)
        return node

    def _parse_comparison(self) -> 'ASTNode':
        node = self._parse_additive()
        while self._check(TokenType.OPERATOR) and self.current.value in ('<', '<=', '>', '>='):
            op = self.current
            self._advance()
            right = self._parse_additive()
            node = BinaryNode(op.value, node, right)
        return node

    def _parse_additive(self) -> 'ASTNode':
        node = self._parse_multiplicative()
        while self._check(TokenType.OPERATOR) and self.current.value in ('+', '-'):
            op = self.current
            self._advance()
            right = self._parse_multiplicative()
            node = BinaryNode(op.value, node, right)
        return node

    def _parse_multiplicative(self) -> 'ASTNode':
        node = self._parse_unary()
        while self._check(TokenType.OPERATOR) and self.current.value in ('*', '/'):
            op = self.current
            self._advance()
            right = self._parse_unary()
            node = BinaryNode(op.value, node, right)
        return node

    def _parse_unary(self) -> 'ASTNode':
        if self._check(TokenType.OPERATOR) and self.current.value == '!':
            op = self.current
            self._advance()
            operand = self._parse_unary()
            return UnaryNode('!', operand)
        if self._check(TokenType.OPERATOR) and self.current.value == '-':
            op = self.current
            self._advance()
            operand = self._parse_unary()
            return UnaryNode('-', operand)
        return self._parse_primary()

    def _parse_primary(self) -> 'ASTNode':
        if self._match(TokenType.LPAREN):
            self._advance()  # consume '
            node = self._parse_expression()
            self._consume(TokenType.RPAREN, "Expected ')'")
            return node

        if self._match(TokenType.IDENTIFIER):
            token = self.current
            self._advance()
            return IdentifierNode(token.value)

        if self._match(TokenType.LITERAL):
            token = self.current
            self._advance()
            return LiteralNode(token.value)

        raise ValueError(f"Unexpected token: {self.current}")


class ASTNode:
    """Base class for AST nodes."""
    pass


class LiteralNode(ASTNode):
    def __init__(self, value):
        self.value = value

    def evaluate(self, context: dict) -> Any:
        return self.value


class IdentifierNode(ASTNode):
    def __init__(self, name: str):
        self.name = name

    def evaluate(self, context: dict) -> Any:
        return context.get(self.name)


class BinaryNode(ASTNode):
    def __init__(self, op: str, left: ASTNode, right: ASTNode):
        self.op = op
        self.left = left
        self.right = right

    def evaluate(self, context: dict) -> Any:
        left_val = self.left.evaluate(context)
        right_val = self.right.evaluate(context)

        # Null handling for arithmetic
        if self.op in ('+', '-', '*', '/'):
            if left_val is None or right_val is None:
                return None

        if self.op == '+':
            if isinstance(left_val, (int, float)) and isinstance(right_val, (int, float)):
                return left_val + right_val
            return None  # Type mismatch

        if self.op == '-':
            if isinstance(left_val, (int, float)) and isinstance(right_val, (int, float)):
                return left_val - right_val
            return None  # Type mismatch

        if self.op == '*':
            if isinstance(left_val, (int, float)) and isinstance(right_val, (int, float)):
                return left_val * right_val
            return None  # Type mismatch

        if self.op == '/':
            if isinstance(left_val, (int, float)) and isinstance(right_val, (int, float)):
                if right_val == 0:
                    return None  # Division by zero
                return left_val / right_val
            return None  # Type mismatch

        if self.op == '==':
            # Type mismatch returns false
            if type(left_val) != type(right_val):
                # Special case: None vs any type, or int vs float
                if left_val is None or right_val is None:
                    return False
                # Allow int and float comparison
                if isinstance(left_val, (int, float)) and isinstance(right_val, (int, float)):
                    return left_val == right_val
                return False
            return left_val == right_val

        if self.op == '!=':
            if type(left_val) != type(right_val):
                if left_val is None or right_val is None:
                    return True
                if isinstance(left_val, (int, float)) and isinstance(right_val, (int, float)):
                    return left_val != right_val
                return True
            return left_val != right_val

        if self.op == '<':
            if isinstance(left_val, (int, float)) and isinstance(right_val, (int, float)):
                return left_val < right_val
            return False  # Type mismatch or null

        if self.op == '<=':
            if isinstance(left_val, (int, float)) and isinstance(right_val, (int, float)):
                return left_val <= right_val
            return False

        if self.op == '>':
            if isinstance(left_val, (int, float)) and isinstance(right_val, (int, float)):
                return left_val > right_val
            return False

        if self.op == '>=':
            if isinstance(left_val, (int, float)) and isinstance(right_val, (int, float)):
                return left_val >= right_val
            return False

        if self.op == '||':
            return bool(left_val) or bool(right_val)

        if self.op == '&&':
            return bool(left_val) and bool(right_val)

        return None


class UnaryNode(ASTNode):
    def __init__(self, op: str, operand: ASTNode):
        self.op = op
        self.operand = operand

    def evaluate(self, context: dict) -> Any:
        val = self.operand.evaluate(context)

        if self.op == '!':
            return not bool(val)

        if self.op == '-':
            if val is None:
                return None
            if isinstance(val, (int, float)):
                return -val
            return None

        return None


class ExpressionEvaluator:
    """Evaluates expression strings against row contexts."""

    def __init__(self, expr: str):
        self.expr = expr
        self._ast = None

    def _parse(self) -> ASTNode:
        tokenizer = Tokenizer(self.expr)
        parser = Parser(tokenizer)
        return parser.parse()

    def evaluate(self, context: dict) -> Any:
        if self._ast is None:
            self._ast = self._parse()
        return self._ast.evaluate(context)


# ============================================================================
# Pipeline Execution
# ============================================================================

def execute_select(row: dict, step: dict, step_index: int) -> dict:
    """Execute select step - keep only specified columns."""
    columns = step["columns"]
    result = {}
    for col in columns:
        if col not in row:
            error_response(
                "MISSING_COLUMN",
                f"column '{col}' not found in row",
                f"pipeline.steps[{step_index}].columns[{columns.index(col)}]"
            )
        result[col] = row[col]
    return result


def execute_map(row: dict, step: dict, step_index: int) -> dict:
    """Execute map step - add/compute new field."""
    result = row.copy()
    as_field = step["as"]
    expr = step["expr"]

    evaluator = ExpressionEvaluator(expr)
    try:
        result[as_field] = evaluator.evaluate(row)
    except ValueError as e:
        error_response(
            "BAD_EXPR",
            f"expression error: {str(e)}",
            f"pipeline.steps[{step_index}].expr"
        )
    return result


def execute_rename(row: dict, step: dict, step_index: int) -> dict:
    """Execute rename step - rename columns."""
    result = row.copy()
    mapping = step["mapping"]

    # Apply mappings in iteration order (Python 3.7+ preserves dict insertion order)
    for source, target in mapping.items():
        if source not in result:
            error_response(
                "MISSING_COLUMN",
                f"column '{source}' not found in row",
                f"pipeline.steps[{step_index}].mapping"
            )
        result[target] = result[source]
        del result[source]

    return result


def execute_filter(row: dict, step: dict, step_index: int) -> Optional[dict]:
    """Execute filter step - keep row if condition is true."""
    where = step["where"]
    evaluator = ExpressionEvaluator(where)
    try:
        result = evaluator.evaluate(row)
        # Filter keeps row only if boolean result is true
        if result:
            return row
        return None
    except ValueError as e:
        error_response(
            "BAD_EXPR",
            f"expression error: {str(e)}",
            f"pipeline.steps[{step_index}].where"
        )


def execute_limit(rows: list, step: dict, step_index: int) -> list:
    """Execute limit step - return only first n rows."""
    n = step["n"]
    return rows[:n]


def execute_pipeline(steps: list, dataset: list) -> Tuple[list, int, int]:
    """
    Execute pipeline and return (result_data, rows_in, rows_out).
    """
    rows_in = len(dataset)
    current_rows = dataset

    for i, step in enumerate(steps):
        op = step["op"]

        if op == "select":
            current_rows = [execute_select(row, step, i) for row in current_rows]

        elif op == "map":
            current_rows = [execute_map(row, step, i) for row in current_rows]

        elif op == "rename":
            current_rows = [execute_rename(row, step, i) for row in current_rows]

        elif op == "filter":
            filtered = []
            for row in current_rows:
                result = execute_filter(row, step, i)
                if result is not None:
                    filtered.append(result)
            current_rows = filtered

        elif op == "limit":
            current_rows = execute_limit(current_rows, step, i)

        else:
            error_response("UNKNOWN_OP", f"unsupported op '{op}'", f"pipeline.steps[{i}].op")

    rows_out = len(current_rows)
    return current_rows, rows_in, rows_out


# ============================================================================
# Main
# ============================================================================

def main() -> None:
    parser = argparse.ArgumentParser(description="ETL Pipeline Executor")
    parser.add_argument(
        "--execute",
        action="store_true",
        default=False,
        help="Execute the pipeline and return data/metrics (default: false)"
    )
    args = parser.parse_args()

    pipeline, dataset = parse_input()

    if not isinstance(pipeline, dict):
        error_response("SCHEMA_VALIDATION_FAILED", "pipeline must be an object", "pipeline")

    steps = pipeline.get("steps")
    if steps is None:
        error_response("SCHEMA_VALIDATION_FAILED", "pipeline is missing required 'steps' field", "pipeline")
    if not isinstance(steps, list):
        error_response("SCHEMA_VALIDATION_FAILED", "'steps' must be an array", "pipeline.steps")

    if not args.execute:
        # Normalization mode (checkpoint 1 behavior)
        normalized_steps = [normalize_step(step, i) for i, step in enumerate(steps)]
        print(json.dumps({"status": "ok", "normalized": {"steps": normalized_steps}}))
    else:
        # Execution mode
        # First normalize to validate, then execute
        normalized_steps = [normalize_step(step, i) for i, step in enumerate(steps)]
        result_data, rows_in, rows_out = execute_pipeline(normalized_steps, dataset)
        print(json.dumps({
            "status": "ok",
            "data": result_data,
            "metrics": {
                "rows_in": rows_in,
                "rows_out": rows_out
            }
        }))


if __name__ == "__main__":
    main()
