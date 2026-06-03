#!/usr/bin/env python3
"""ETL Pipeline CLI program that reads JSON from STDIN, validates and executes normalized ETL pipeline."""

import argparse
import json
import re
import sys
from typing import Any


class ETLValidationError(Exception):
    """Exception raised for ETL pipeline validation errors."""
    def __init__(self, error_code, message, path):
        self.error_code = error_code
        self.message = message
        self.path = path
        super().__init__(self.message)


class ETLEvaluationError(Exception):
    """Exception raised for ETL pipeline execution errors."""
    def __init__(self, error_code, message, path):
        self.error_code = error_code
        self.message = message
        self.path = path
        super().__init__(self.message)


def parse_request():
    """Parse JSON from STDIN and validate top-level structure."""
    try:
        data = json.load(sys.stdin)
    except json.JSONDecodeError as e:
        raise ETLValidationError(
            "SCHEMA_VALIDATION_FAILED",
            f"ETL_ERROR: Invalid JSON: {e}",
            ""
        )

    if "pipeline" not in data:
        raise ETLValidationError(
            "SCHEMA_VALIDATION_FAILED",
            "ETL_ERROR: Missing 'pipeline' key",
            ""
        )

    if "dataset" not in data:
        raise ETLValidationError(
            "SCHEMA_VALIDATION_FAILED",
            "ETL_ERROR: Missing 'dataset' key",
            ""
        )

    if not isinstance(data["dataset"], list):
        raise ETLValidationError(
            "SCHEMA_VALIDATION_FAILED",
            "ETL_ERROR: 'dataset' must be an array",
            "dataset"
        )

    for i, item in enumerate(data["dataset"]):
        if not isinstance(item, dict):
            raise ETLValidationError(
                "SCHEMA_VALIDATION_FAILED",
                f"ETL_ERROR: dataset[{i}] must be an object",
                f"dataset[{i}]"
            )

    return data


def normalize_step(step, index):
    """Normalize a single step object."""
    if not isinstance(step, dict):
        raise ETLValidationError(
            "SCHEMA_VALIDATION_FAILED",
            "ETL_ERROR: Step must be an object",
            f"pipeline.steps[{index}]"
        )

    if "op" not in step:
        raise ETLValidationError(
            "SCHEMA_VALIDATION_FAILED",
            "ETL_ERROR: Step missing 'op' field",
            f"pipeline.steps[{index}]"
        )

    op = step["op"].strip().lower()
    if not op:
        raise ETLValidationError(
            "SCHEMA_VALIDATION_FAILED",
            "ETL_ERROR: Operation name cannot be empty or whitespace",
            f"pipeline.steps[{index}].op"
        )

    valid_ops = {
        "select": _validate_select,
        "filter": _validate_filter,
        "map": _validate_map,
        "rename": _validate_rename,
        "limit": _validate_limit,
    }

    if op not in valid_ops:
        raise ETLValidationError(
            "UNKNOWN_OP",
            f"ETL_ERROR: unsupported op '{op}'",
            f"pipeline.steps[{index}].op"
        )

    return valid_ops[op](step, index, op)


def _validate_select(step, index, op):
    """Validate and normalize select operation."""
    if "columns" not in step:
        raise ETLValidationError(
            "SCHEMA_VALIDATION_FAILED",
            "ETL_ERROR: 'select' operation requires 'columns' field",
            f"pipeline.steps[{index}]"
        )

    columns = step["columns"]
    if not isinstance(columns, list):
        raise ETLValidationError(
            "SCHEMA_VALIDATION_FAILED",
            "ETL_ERROR: 'columns' must be an array",
            f"pipeline.steps[{index}].columns"
        )

    new_columns = []
    for i, col in enumerate(columns):
        if not isinstance(col, str):
            raise ETLValidationError(
                "SCHEMA_VALIDATION_FAILED",
                f"ETL_ERROR: column[{i}] must be a string",
                f"pipeline.steps[{index}].columns[{i}]"
            )
        if not col.strip():
            raise ETLValidationError(
                "SCHEMA_VALIDATION_FAILED",
                f"ETL_ERROR: column[{i}] cannot be empty or whitespace",
                f"pipeline.steps[{index}].columns[{i}]"
            )
        new_columns.append(col)

    result = {"op": op, "columns": new_columns}
    return result


def _validate_filter(step, index, op):
    """Validate and normalize filter operation."""
    if "where" not in step:
        raise ETLValidationError(
            "SCHEMA_VALIDATION_FAILED",
            "ETL_ERROR: 'filter' operation requires 'where' field",
            f"pipeline.steps[{index}]"
        )

    where_expr = step["where"].strip()
    if not where_expr:
        raise ETLValidationError(
            "SCHEMA_VALIDATION_FAILED",
            "ETL_ERROR: 'where' expression cannot be empty or whitespace",
            f"pipeline.steps[{index}].where"
        )

    validate_expression(where_expr, f"pipeline.steps[{index}].where")

    result = {"op": op, "where": where_expr}
    return result


def _validate_map(step, index, op):
    """Validate and normalize map operation."""
    if "as" not in step:
        raise ETLValidationError(
            "SCHEMA_VALIDATION_FAILED",
            "ETL_ERROR: 'map' operation requires 'as' field",
            f"pipeline.steps[{index}]"
        )

    as_field = step["as"].strip()
    if not as_field:
        raise ETLValidationError(
            "SCHEMA_VALIDATION_FAILED",
            "ETL_ERROR: 'as' field cannot be empty or whitespace",
            f"pipeline.steps[{index}].as"
        )

    if "expr" not in step:
        raise ETLValidationError(
            "SCHEMA_VALIDATION_FAILED",
            "ETL_ERROR: 'map' operation requires 'expr' field",
            f"pipeline.steps[{index}]"
        )

    expr = step["expr"].strip()
    if not expr:
        raise ETLValidationError(
            "SCHEMA_VALIDATION_FAILED",
            "ETL_ERROR: 'expr' field cannot be empty or whitespace",
            f"pipeline.steps[{index}].expr"
        )

    validate_expression(expr, f"pipeline.steps[{index}].expr")

    result = {"op": op, "as": as_field, "expr": expr}
    return result


def _validate_rename(step, index, op):
    """Validate and normalize rename operation."""
    has_from_to = "from" in step and "to" in step
    has_mapping = "mapping" in step

    if not (has_from_to or has_mapping):
        raise ETLValidationError(
            "SCHEMA_VALIDATION_FAILED",
            "ETL_ERROR: 'rename' operation requires either ('from' and 'to') or 'mapping' field",
            f"pipeline.steps[{index}]"
        )

    if has_from_to and has_mapping:
        raise ETLValidationError(
            "SCHEMA_VALIDATION_FAILED",
            "ETL_ERROR: 'rename' operation cannot have both 'from'/'to' and 'mapping' fields",
            f"pipeline.steps[{index}]"
        )

    if has_from_to:
        from_field = step["from"]
        to_field = step["to"]

        if not isinstance(from_field, str) or not isinstance(to_field, str):
            raise ETLValidationError(
                "SCHEMA_VALIDATION_FAILED",
                "ETL_ERROR: 'from' and 'to' must be strings",
                f"pipeline.steps[{index}].from"
            )

        from_field = from_field.strip()
        to_field = to_field.strip()

        if not from_field:
            raise ETLValidationError(
                "SCHEMA_VALIDATION_FAILED",
                "ETL_ERROR: 'from' field cannot be empty or whitespace",
                f"pipeline.steps[{index}].from"
            )

        if not to_field:
            raise ETLValidationError(
                "SCHEMA_VALIDATION_FAILED",
                "ETL_ERROR: 'to' field cannot be empty or whitespace",
                f"pipeline.steps[{index}].to"
            )

        mapping = {from_field: to_field}
        result = {"op": op, "mapping": mapping}
    else:
        mapping = step["mapping"]
        if not isinstance(mapping, dict):
            raise ETLValidationError(
                "SCHEMA_VALIDATION_FAILED",
                "ETL_ERROR: 'mapping' must be an object",
                f"pipeline.steps[{index}].mapping"
            )

        new_mapping = {}
        for k, v in mapping.items():
            if not isinstance(k, str) or not isinstance(v, str):
                raise ETLValidationError(
                    "SCHEMA_VALIDATION_FAILED",
                    "ETL_ERROR: mapping keys and values must be strings",
                    f"pipeline.steps[{index}].mapping"
                )
            new_mapping[k] = v

        result = {"op": op, "mapping": new_mapping}

    return result


def _validate_limit(step, index, op):
    """Validate and normalize limit operation."""
    if "n" not in step:
        raise ETLValidationError(
            "SCHEMA_VALIDATION_FAILED",
            "ETL_ERROR: 'limit' operation requires 'n' field",
            f"pipeline.steps[{index}]"
        )

    n = step["n"]
    if not isinstance(n, int):
        raise ETLValidationError(
            "SCHEMA_VALIDATION_FAILED",
            "ETL_ERROR: 'n' must be an integer",
            f"pipeline.steps[{index}].n"
        )

    if n < 0:
        raise ETLValidationError(
            "SCHEMA_VALIDATION_FAILED",
            "ETL_ERROR: 'n' must be >= 0",
            f"pipeline.steps[{index}].n"
        )

    result = {"op": op, "n": n}
    return result


# Expression tokenization and validation
EXPRESSION_TOKEN_PATTERN = re.compile(
    r'''
        (?P<NUMBER>\d+(?:\.\d+)?)
      | (?P<STRING>"[^"]*"|'[^']*')
      | (?P<BOOLEAN_OP>\band\b|\bor\b|\bnot\b)
      | (?P<OPERATOR>>=|<=|!=|==|>|<|\+|-|\*|/)
      | (?P<IDENTIFIER>[a-zA-Z_][a-zA-Z0-9_]*)
      | (?P<PAREN>[()])
      | (?P<WHITESPACE>\s+)
    ''',
    re.VERBOSE
)

SUPPORTED_OPERATORS = {"+", "-", "*", "/", ">", "<", ">=", "<=", "==", "!=", "and", "or", "not"}


def validate_expression(expr, path):
    """Validate expression syntax."""
    if not expr:
        return

    # First check for unsupported operators like **, ^^
    if re.search(r'[\*\^]{2,}', expr):
        unsupported = re.search(r'[\*\^]{2,}', expr)
        raise ETLValidationError(
            "BAD_EXPR",
            f"ETL_ERROR: unsupported operator '{unsupported.group()}'",
            path
        )

    tokens = []
    pos = 0
    length = len(expr)

    while pos < length:
        match = EXPRESSION_TOKEN_PATTERN.match(expr, pos)
        if match:
            token_type = match.lastgroup
            value = match.group()
            pos = match.end()

            if token_type == "WHITESPACE":
                continue

            tokens.append((token_type, value))
        else:
            raise ETLValidationError(
                "BAD_EXPR",
                "ETL_ERROR: Invalid expression syntax",
                path
            )

    # Check for unsupported operators (single char like ^)
    remaining = EXPRESSION_TOKEN_PATTERN.sub('', expr)
    if remaining:
        for char in remaining:
            if not char.isspace():
                raise ETLValidationError(
                    "BAD_EXPR",
                    f"ETL_ERROR: unsupported operator '{char}'",
                    path
                )

    # Check for consecutive operators (valid operators like -* would be caught above)
    for i in range(len(tokens) - 1):
        curr_type, curr_val = tokens[i]
        next_type, next_val = tokens[i + 1]

        if curr_type == "OPERATOR" and next_type == "OPERATOR":
            raise ETLValidationError(
                "BAD_EXPR",
                "ETL_ERROR: Consecutive operators in expression",
                path
            )

    if not tokens:
        raise ETLValidationError(
            "BAD_EXPR",
            "ETL_ERROR: Empty expression",
            path
        )


class ExpressionEvaluator:
    """Evaluates ETL filter/map expressions."""

    def __init__(self, expr: str):
        self.expr = expr
        self.tokens = []
        self.pos = 0
        self._tokenize()

    def _tokenize(self):
        """Tokenize the expression."""
        self.tokens = []
        pos = 0
        length = len(self.expr)

        while pos < length:
            match = EXPRESSION_TOKEN_PATTERN.match(self.expr, pos)
            if match:
                token_type = match.lastgroup
                value = match.group()
                pos = match.end()

                if token_type == "WHITESPACE":
                    continue

                # Convert literals to proper types
                if token_type == "NUMBER":
                    if '.' in value:
                        self.tokens.append(("NUMBER", float(value)))
                    else:
                        self.tokens.append(("NUMBER", int(value)))
                elif token_type == "STRING":
                    # Remove quotes
                    self.tokens.append(("STRING", value[1:-1]))
                elif token_type == "BOOLEAN_OP":
                    self.tokens.append(("BOOLEAN_OP", value.lower()))
                elif token_type == "OPERATOR":
                    self.tokens.append(("OPERATOR", value))
                elif token_type == "IDENTIFIER":
                    self.tokens.append(("IDENTIFIER", value))
                elif token_type == "PAREN":
                    self.tokens.append(("PAREN", value))
            else:
                raise ETLEvaluationError(
                    "BAD_EXPR",
                    f"ETL_ERROR: Invalid expression syntax in '{self.expr}'",
                    ""
                )

    def evaluate(self, row: dict) -> Any:
        """Evaluate the expression against a row."""
        self.pos = 0
        result = self._parse_expression()
        if self.pos < len(self.tokens):
            raise ETLEvaluationError(
                "BAD_EXPR",
                f"ETL_ERROR: Unexpected tokens after expression",
                ""
            )
        return result

    def _parse_expression(self, precedence=0):
        """Parse expression with given minimum precedence."""
        if self.pos >= len(self.tokens):
            raise ETLEvaluationError(
                "BAD_EXPR",
                "ETL_ERROR: Unexpected end of expression",
                ""
            )

        # Parse left operand
        left = self._parse_primary()

        while self.pos < len(self.tokens):
            # Check for operator
            if self.tokens[self.pos][0] == "OPERATOR":
                op = self.tokens[self.pos][1]
                op_prec = self._get_precedence(op)
                if op_prec <= precedence:
                    break

                # Right-associative operators
                if op in ('and', 'or'):
                    right_prec = op_prec
                else:
                    right_prec = op_prec

                self.pos += 1
                right = self._parse_expression(right_prec)
                left = self._apply_operator(left, op, right)
            elif self.tokens[self.pos][0] == "BOOLEAN_OP":
                op = self.tokens[self.pos][1]
                op_prec = self._get_precedence(op)
                if op_prec <= precedence:
                    break

                self.pos += 1
                right = self._parse_expression(op_prec)
                left = self._apply_operator(left, op, right)
            else:
                break

        return left

    def _parse_primary(self):
        """Parse primary expression (literal, identifier, parenthesized)."""
        if self.pos >= len(self.tokens):
            raise ETLEvaluationError(
                "BAD_EXPR",
                "ETL_ERROR: Unexpected end of expression",
                ""
            )

        token_type, value = self.tokens[self.pos]

        # Handle unary operators
        if token_type == "OPERATOR" and value == "-":
            self.pos += 1
            operand = self._parse_primary()
            if operand is None:
                return None
            return -operand

        if token_type == "BOOLEAN_OP" and value == "not":
            self.pos += 1
            operand = self._parse_expression(self._get_precedence("not"))
            return not self._to_bool(operand)

        # Parenthesized expression
        if token_type == "PAREN" and value == "(":
            self.pos += 1
            expr = self._parse_expression()
            if self.pos >= len(self.tokens) or self.tokens[self.pos] != ("PAREN", ")"):
                raise ETLEvaluationError(
                    "BAD_EXPR",
                    "ETL_ERROR: Missing closing parenthesis",
                    ""
                )
            self.pos += 1
            return expr

        # Literal or identifier
        self.pos += 1

        if token_type == "NUMBER":
            return value
        elif token_type == "STRING":
            return value
        elif token_type == "IDENTIFIER":
            return value  # Return as identifier name, will be resolved later
        else:
            raise ETLEvaluationError(
                "BAD_EXPR",
                f"ETL_ERROR: Unexpected token '{value}'",
                ""
            )

    def _get_precedence(self, op):
        """Get operator precedence."""
        prec = {
            'or': 1,
            'and': 2,
            'not': 3,
            '==': 4, '!=': 4,
            '<': 5, '>': 5, '<=': 5, '>=': 5,
            '+': 6, '-': 6,
            '*': 7, '/': 7,
        }
        return prec.get(op, 0)

    def _apply_operator(self, left, op, right):
        """Apply an operator to operands."""
        # Handle null in operands
        if left is None or right is None:
            if op in ('==', '!=', '<', '>', '<=', '>='):
                return False
            elif op in ('and', 'or', 'not'):
                return self._to_bool(left or right)
            else:
                return None

        if op == '+':
            if isinstance(left, (int, float)) and isinstance(right, (int, float)):
                return left + right
            return None
        elif op == '-':
            if isinstance(left, (int, float)) and isinstance(right, (int, float)):
                return left - right
            return None
        elif op == '*':
            if isinstance(left, (int, float)) and isinstance(right, (int, float)):
                return left * right
            return None
        elif op == '/':
            if isinstance(left, (int, float)) and isinstance(right, (int, float)):
                if right == 0:
                    return None
                return left / right
            return None
        elif op == '==':
            return left == right
        elif op == '!=':
            return left != right
        elif op == '<':
            if isinstance(left, (int, float)) and isinstance(right, (int, float)):
                return left < right
            return False
        elif op == '>':
            if isinstance(left, (int, float)) and isinstance(right, (int, float)):
                return left > right
            return False
        elif op == '<=':
            if isinstance(left, (int, float)) and isinstance(right, (int, float)):
                return left <= right
            return False
        elif op == '>=':
            if isinstance(left, (int, float)) and isinstance(right, (int, float)):
                return left >= right
            return False
        elif op == 'and':
            return self._to_bool(left) and self._to_bool(right)
        elif op == 'or':
            return self._to_bool(left) or self._to_bool(right)
        else:
            raise ETLEvaluationError(
                "BAD_EXPR",
                f"ETL_ERROR: Unknown operator '{op}'",
                ""
            )

    def _to_bool(self, value):
        """Convert value to boolean (null is falsy)."""
        if value is None:
            return False
        return bool(value)

    def resolve_identifiers(self, row: dict):
        """Resolve identifiers in the parsed expression tree against a row."""
        def resolve_node(node):
            if isinstance(node, tuple):
                token_type, value = node
                if token_type == "IDENTIFIER":
                    return row.get(value)
                elif token_type == "NUMBER":
                    return value
                elif token_type == "STRING":
                    return value
                elif token_type == "PAREN":
                    return value
                else:
                    return value
            elif isinstance(node, list):
                return [resolve_node(n) for n in node]
            else:
                return node

        # Re-tokenize and parse to get a proper tree
        self.pos = 0
        return self._parse_and_resolve(row)

    def _parse_and_resolve(self, row: dict):
        """Parse expression and resolve identifiers against row."""
        self.pos = 0
        result = self._parse_expression_with_resolution(row)
        if self.pos < len(self.tokens):
            raise ETLEvaluationError(
                "BAD_EXPR",
                "ETL_ERROR: Unexpected tokens after expression",
                ""
            )
        return result

    def _parse_expression_with_resolution(self, row: dict, precedence=0):
        """Parse expression with resolution against row."""
        if self.pos >= len(self.tokens):
            raise ETLEvaluationError(
                "BAD_EXPR",
                "ETL_ERROR: Unexpected end of expression",
                ""
            )

        left = self._parse_primary_with_resolution(row)

        while self.pos < len(self.tokens):
            if self.tokens[self.pos][0] == "OPERATOR":
                op = self.tokens[self.pos][1]
                op_prec = self._get_precedence(op)
                if op_prec <= precedence:
                    break

                self.pos += 1
                right = self._parse_expression_with_resolution(row, op_prec)
                left = self._apply_operator(left, op, right)
            elif self.tokens[self.pos][0] == "BOOLEAN_OP":
                op = self.tokens[self.pos][1]
                op_prec = self._get_precedence(op)
                if op_prec <= precedence:
                    break

                self.pos += 1
                right = self._parse_expression_with_resolution(row, op_prec)
                left = self._apply_operator(left, op, right)
            else:
                break

        return left

    def _parse_primary_with_resolution(self, row: dict):
        """Parse primary and resolve identifiers."""
        if self.pos >= len(self.tokens):
            raise ETLEvaluationError(
                "BAD_EXPR",
                "ETL_ERROR: Unexpected end of expression",
                ""
            )

        token_type, value = self.tokens[self.pos]

        # Handle unary minus
        if token_type == "OPERATOR" and value == "-":
            self.pos += 1
            operand = self._parse_primary_with_resolution(row)
            if operand is None:
                return None
            return -operand

        # Handle unary not
        if token_type == "BOOLEAN_OP" and value == "not":
            self.pos += 1
            operand = self._parse_expression_with_resolution(row, self._get_precedence("not"))
            return not self._to_bool(operand)

        # Parenthesized expression
        if token_type == "PAREN" and value == "(":
            self.pos += 1
            expr = self._parse_expression_with_resolution(row)
            if self.pos >= len(self.tokens) or self.tokens[self.pos] != ("PAREN", ")"):
                raise ETLEvaluationError(
                    "BAD_EXPR",
                    "ETL_ERROR: Missing closing parenthesis",
                    ""
                )
            self.pos += 1
            return expr

        # Literal or identifier
        self.pos += 1

        if token_type == "NUMBER":
            return value
        elif token_type == "STRING":
            return value
        elif token_type == "IDENTIFIER":
            return row.get(value)
        else:
            raise ETLEvaluationError(
                "BAD_EXPR",
                f"ETL_ERROR: Unexpected token '{value}'",
                ""
            )


def execute_select_step(data: list, step: dict) -> list:
    """Execute select operation."""
    columns = step["columns"]
    result = []

    for row in data:
        new_row = {}
        for col in columns:
            if col not in row:
                raise ETLEvaluationError(
                    "MISSING_COLUMN",
                    f"ETL_ERROR: column '{col}' not found in row",
                    f"pipeline.steps[{step.get('_index', 0)}].columns[{columns.index(col)}]"
                )
            new_row[col] = row[col]
        result.append(new_row)

    return result


def execute_filter_step(data: list, step: dict) -> list:
    """Execute filter operation."""
    evaluator = ExpressionEvaluator(step["where"])
    result = []

    for row in data:
        try:
            value = evaluator._parse_and_resolve(row)
            if evaluator._to_bool(value):
                result.append(row)
        except Exception as e:
            if isinstance(e, ETLEvaluationError):
                raise
            raise ETLEvaluationError(
                "EXECUTION_FAILED",
                f"ETL_ERROR: Failed to evaluate filter: {e}",
                f"pipeline.steps[{step.get('_index', 0)}].where"
            )

    return result


def execute_map_step(data: list, step: dict) -> list:
    """Execute map operation."""
    evaluator = ExpressionEvaluator(step["expr"])
    result = []

    for row in data:
        new_row = row.copy()
        try:
            value = evaluator._parse_and_resolve(new_row)
            new_row[step["as"]] = value
        except Exception as e:
            if isinstance(e, ETLEvaluationError):
                raise
            raise ETLEvaluationError(
                "EXECUTION_FAILED",
                f"ETL_ERROR: Failed to evaluate map expression: {e}",
                f"pipeline.steps[{step.get('_index', 0)}].expr"
            )
        result.append(new_row)

    return result


def execute_rename_step(data: list, step: dict) -> list:
    """Execute rename operation."""
    mapping = step["mapping"]
    result = []

    for row in data:
        new_row = {}
        # Copy all fields first
        for k, v in row.items():
            new_row[k] = v

        # Apply renaming in iteration order
        # Later mappings can overwrite earlier ones
        for src, tgt in mapping.items():
            if src not in new_row:
                raise ETLEvaluationError(
                    "MISSING_COLUMN",
                    f"ETL_ERROR: column '{src}' not found in row",
                    f"pipeline.steps[{step.get('_index', 0)}].mapping.{src}"
                )
            new_row[tgt] = new_row[src]
            del new_row[src]

        result.append(new_row)

    return result


def execute_limit_step(data: list, step: dict) -> list:
    """Execute limit operation."""
    n = step["n"]
    return data[:n]


def execute_pipeline(dataset: list, steps: list) -> tuple:
    """Execute pipeline and return (data, metrics)."""
    rows_in = len(dataset)
    data = dataset

    for i, step in enumerate(steps):
        step_with_index = dict(step)
        step_with_index["_index"] = i

        op = step["op"]

        if op == "select":
            data = execute_select_step(data, step_with_index)
        elif op == "filter":
            data = execute_filter_step(data, step_with_index)
        elif op == "map":
            data = execute_map_step(data, step_with_index)
        elif op == "rename":
            data = execute_rename_step(data, step_with_index)
        elif op == "limit":
            data = execute_limit_step(data, step_with_index)

    rows_out = len(data)
    metrics = {"rows_in": rows_in, "rows_out": rows_out}

    return data, metrics


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description='ETL Pipeline Executor - Process JSON from STDIN'
    )
    parser.add_argument(
        '--execute',
        action='store_true',
        default=False,
        help='Execute the pipeline (default: false, returns normalized form)'
    )

    args = parser.parse_args()

    try:
        data = parse_request()

        pipeline = data["pipeline"]
        if not isinstance(pipeline, dict):
            raise ETLValidationError(
                "SCHEMA_VALIDATION_FAILED",
                "ETL_ERROR: 'pipeline' must be an object",
                "pipeline"
            )

        if "steps" not in pipeline:
            raise ETLValidationError(
                "SCHEMA_VALIDATION_FAILED",
                "ETL_ERROR: 'pipeline' must have a 'steps' field",
                "pipeline"
            )

        steps = pipeline["steps"]
        if not isinstance(steps, list):
            raise ETLValidationError(
                "SCHEMA_VALIDATION_FAILED",
                "ETL_ERROR: 'steps' must be an array",
                "pipeline.steps"
            )

        # Normalize each step
        normalized_steps = []
        for i, step in enumerate(steps):
            normalized_step = normalize_step(step, i)
            normalized_steps.append(normalized_step)

        if not args.execute:
            # Return normalized form (checkpoint 1 behavior)
            output = {
                "status": "ok",
                "normalized": {
                    "steps": normalized_steps
                }
            }
        else:
            # Execute the pipeline
            dataset = data["dataset"]
            data_result, metrics = execute_pipeline(dataset, normalized_steps)

            output = {
                "status": "ok",
                "data": data_result,
                "metrics": metrics
            }

        print(json.dumps(output))
        sys.exit(0)

    except ETLValidationError as e:
        output = {
            "status": "error",
            "error_code": e.error_code,
            "message": e.message,
            "path": e.path
        }
        print(json.dumps(output))
        sys.exit(1)
    except ETLEvaluationError as e:
        output = {
            "status": "error",
            "error_code": e.error_code,
            "message": e.message,
            "path": e.path
        }
        print(json.dumps(output))
        sys.exit(1)
    except Exception as e:
        output = {
            "status": "error",
            "error_code": "EXECUTION_FAILED",
            "message": f"ETL_ERROR: {e}",
            "path": ""
        }
        print(json.dumps(output))
        sys.exit(1)


if __name__ == "__main__":
    main()