#!/usr/bin/env python3
"""ETL Pipeline Parser - Validates and normalizes ETL pipeline specifications."""

import json
import re
import sys
import argparse
from typing import Any, Dict, List, Optional


class ETLError(Exception):
    """Custom exception for ETL errors."""
    def __init__(self, error_code: str, message: str, path: str):
        self.error_code = error_code
        self.message = message
        self.path = path
        super().__init__(message)


def validate_json_structure(data: Any) -> None:
    """Validate the basic JSON structure of the input."""
    if not isinstance(data, dict):
        raise ETLError(
            "SCHEMA_VALIDATION_FAILED",
            "ETL_ERROR: input must be a JSON object",
            "root"
        )

    # Check required fields
    if "pipeline" not in data:
        raise ETLError(
            "SCHEMA_VALIDATION_FAILED",
            "ETL_ERROR: 'pipeline' field is required",
            "pipeline"
        )

    if "dataset" not in data:
        raise ETLError(
            "SCHEMA_VALIDATION_FAILED",
            "ETL_ERROR: 'dataset' field is required",
            "dataset"
        )

    pipeline = data["pipeline"]
    if not isinstance(pipeline, dict):
        raise ETLError(
            "SCHEMA_VALIDATION_FAILED",
            "ETL_ERROR: 'pipeline' must be an object",
            "pipeline"
        )

    if "steps" not in pipeline:
        raise ETLError(
            "SCHEMA_VALIDATION_FAILED",
            "ETL_ERROR: 'steps' field is required in pipeline",
            "pipeline.steps"
        )

    steps = pipeline["steps"]
    if not isinstance(steps, list):
        raise ETLError(
            "SCHEMA_VALIDATION_FAILED",
            "ETL_ERROR: 'steps' must be an array",
            "pipeline.steps"
        )

    dataset = data["dataset"]
    if not isinstance(dataset, list):
        raise ETLError(
            "SCHEMA_VALIDATION_FAILED",
            "ETL_ERROR: 'dataset' must be an array",
            "dataset"
        )

    # Validate dataset elements are objects
    for i, item in enumerate(dataset):
        if not isinstance(item, dict):
            raise ETLError(
                "SCHEMA_VALIDATION_FAILED",
                f"ETL_ERROR: dataset[{i}] must be an object",
                f"dataset[{i}]"
            )


def is_empty_after_trim(value: str) -> bool:
    """Check if a string is empty or whitespace-only after trimming."""
    return not value or not value.strip()


# ============================================================================
# Expression Tokenizer and Parser for ETL Execution
# ============================================================================

class Token:
    """Represents a token in the expression."""
    def __init__(self, type_: str, value: Any):
        self.type = type_
        self.value = value

    def __repr__(self):
        return f"Token({self.type}, {self.value!r})"


class ExpressionTokenizer:
    """Tokenizer for ETL expressions."""

    KEYWORDS = {'true', 'false', 'null'}

    def __init__(self, expr: str):
        self.expr = expr
        self.pos = 0
        self.length = len(expr)

    def tokenize(self) -> List[Token]:
        """Tokenize the expression."""
        tokens = []
        while self.pos < self.length:
            self._skip_whitespace()
            if self.pos >= self.length:
                break

            char = self.expr[self.pos]

            # Handle double-quoted strings
            if char == '"':
                tokens.append(self._read_string())
                continue
            # Handle numbers
            if char.isdigit() or (char == '.' and self.pos + 1 < self.length and self.expr[self.pos + 1].isdigit()):
                tokens.append(self._read_number())
                continue
            # Handle identifiers and keywords
            if char.isalpha() or char == '_':
                tokens.append(self._read_identifier())
                continue

            # Handle two-character operators
            if self.pos + 1 < self.length:
                two_char = self.expr[self.pos:self.pos + 2]
                if two_char in ('==', '!=', '<=', '>=', '&&', '||'):
                    tokens.append(Token(two_char, two_char))
                    self.pos += 2
                    continue
                # Check for unsupported operators like **, ^^
                if two_char == '**':
                    raise ETLError('BAD_EXPR', "ETL_ERROR: unsupported operator '**'", '')
                if two_char == '^^':
                    raise ETLError('BAD_EXPR', "ETL_ERROR: unsupported operator '^'", '')
                if two_char == '>>' or two_char == '<<':
                    raise ETLError('BAD_EXPR', f"ETL_ERROR: unsupported operator '{two_char}'", '')

            # Handle dot operator for member access
            if char == '.':
                tokens.append(Token('.', '.'))
                self.pos += 1
                continue

            # Handle single-character operators
            if char in '+-*/()':
                tokens.append(Token(char, char))
                self.pos += 1
                continue
            if char in '<>':
                tokens.append(Token(char, char))
                self.pos += 1
                continue
            if char == '=':
                tokens.append(Token(char, char))
                self.pos += 1
                continue
            if char == '!':
                tokens.append(Token(char, char))
                self.pos += 1
                continue
            if char == '&':
                raise ETLError('BAD_EXPR', "ETL_ERROR: invalid operator '&'", '')
            if char == '|':
                raise ETLError('BAD_EXPR', "ETL_ERROR: invalid operator '|'", '')

            raise ETLError('BAD_EXPR', f"ETL_ERROR: unexpected character '{char}'", '')

        return tokens

    def _skip_whitespace(self):
        while self.pos < self.length and self.expr[self.pos].isspace():
            self.pos += 1

    def _read_string(self) -> Token:
        """Read a double-quoted string."""
        start = self.pos
        self.pos += 1  # Skip opening quote
        value = []
        while self.pos < self.length:
            char = self.expr[self.pos]
            if char == '"':
                self.pos += 1  # Skip closing quote
                return Token('STRING', ''.join(value))
            elif char == '\\':
                self.pos += 1
                if self.pos < self.length:
                    escaped = self.expr[self.pos]
                    if escaped == 'n':
                        value.append('\n')
                    elif escaped == 't':
                        value.append('\t')
                    elif escaped == '"':
                        value.append('"')
                    elif escaped == '\\':
                        value.append('\\')
                    else:
                        value.append(escaped)
                    self.pos += 1
            else:
                value.append(char)
                self.pos += 1
        raise ETLError('BAD_EXPR', "ETL_ERROR: unterminated string", '')

    def _read_number(self) -> Token:
        """Read a number (integer or float)."""
        start = self.pos
        has_dot = False
        has_digit = False

        while self.pos < self.length:
            char = self.expr[self.pos]
            if char.isdigit():
                has_digit = True
                self.pos += 1
            elif char == '.' and not has_dot:
                has_dot = True
                self.pos += 1
            else:
                break

        if not has_digit:
            raise ETLError('BAD_EXPR', "ETL_ERROR: invalid number", '')

        value_str = self.expr[start:self.pos]
        return Token('NUMBER', float(value_str) if has_dot else int(value_str))

    def _read_identifier(self) -> Token:
        """Read an identifier or keyword."""
        start = self.pos
        while self.pos < self.length and (self.expr[self.pos].isalnum() or self.expr[self.pos] == '_'):
            self.pos += 1
        value = self.expr[start:self.pos]

        if value == 'true':
            return Token('BOOL', True)
        elif value == 'false':
            return Token('BOOL', False)
        elif value == 'null':
            return Token('NULL', None)
        else:
            return Token('IDENT', value)


class ExpressionParser:
    """Parser for ETL expressions using recursive descent."""

    def __init__(self, tokens: List[Token]):
        self.tokens = tokens
        self.pos = 0

    def parse(self) -> 'ASTNode':
        """Parse the expression and return an AST."""
        if not self.tokens:
            raise ETLError('BAD_EXPR', "ETL_ERROR: empty expression", '')
        result = self._parse_or()
        if self.pos < len(self.tokens):
            raise ETLError('BAD_EXPR', f"ETL_ERROR: unexpected token", '')
        return result

    def _current(self) -> Optional[Token]:
        if self.pos < len(self.tokens):
            return self.tokens[self.pos]
        return None

    def _advance(self) -> Token:
        token = self.tokens[self.pos]
        self.pos += 1
        return token

    def _parse_or(self) -> 'ASTNode':
        """Parse || operator (lowest precedence)."""
        left = self._parse_and()
        while self._current() and self._current().type == '||':
            self._advance()
            right = self._parse_and()
            left = BinaryOpNode('||', left, right)
        return left

    def _parse_and(self) -> 'ASTNode':
        """Parse && operator."""
        left = self._parse_equality()
        while self._current() and self._current().type == '&&':
            self._advance()
            right = self._parse_equality()
            left = BinaryOpNode('&&', left, right)
        return left

    def _parse_equality(self) -> 'ASTNode':
        """Parse == and != operators."""
        left = self._parse_comparison()
        while self._current() and self._current().type in ('==', '!='):
            op = self._advance().type
            right = self._parse_comparison()
            left = BinaryOpNode(op, left, right)
        return left

    def _parse_comparison(self) -> 'ASTNode':
        """Parse <, <=, >, >= operators."""
        left = self._parse_additive()
        while self._current() and self._current().type in ('<', '<=', '>', '>='):
            op = self._advance().type
            right = self._parse_additive()
            left = BinaryOpNode(op, left, right)
        return left

    def _parse_additive(self) -> 'ASTNode':
        """Parse + and - operators."""
        left = self._parse_multiplicative()
        while self._current() and self._current().type in ('+', '-'):
            op = self._advance().type
            right = self._parse_multiplicative()
            left = BinaryOpNode(op, left, right)
        return left

    def _parse_multiplicative(self) -> 'ASTNode':
        """Parse * and / operators."""
        left = self._parse_unary()
        while self._current() and self._current().type in ('*', '/'):
            op = self._advance().type
            right = self._parse_unary()
            left = BinaryOpNode(op, left, right)
        return left

    def _parse_unary(self) -> 'ASTNode':
        """Parse unary operators (!, -)."""
        if self._current() and self._current().type == '!':
            self._advance()
            operand = self._parse_unary()
            return UnaryOpNode('!', operand)
        if self._current() and self._current().type == '-':
            self._advance()
            operand = self._parse_unary()
            return UnaryOpNode('-', operand)
        return self._parse_primary()

    def _parse_primary(self) -> 'ASTNode':
        """Parse primary expressions: literals, identifiers, parentheses, and member access."""
        token = self._current()
        if token is None:
            raise ETLError('BAD_EXPR', "ETL_ERROR: unexpected end of expression", '')

        if token.type == 'NUMBER':
            self._advance()
            return self._parse_member_access(LiteralNode(token.value))
        elif token.type == 'STRING':
            self._advance()
            return self._parse_member_access(LiteralNode(token.value))
        elif token.type == 'BOOL':
            self._advance()
            return self._parse_member_access(LiteralNode(token.value))
        elif token.type == 'NULL':
            self._advance()
            return self._parse_member_access(LiteralNode(None))
        elif token.type == 'IDENT':
            self._advance()
            return self._parse_member_access(IdentifierNode(token.value))
        elif token.type == '(':
            self._advance()
            expr = self._parse_or()
            if not self._current() or self._current().type != ')':
                raise ETLError('BAD_EXPR', "ETL_ERROR: missing closing parenthesis", '')
            self._advance()
            return self._parse_member_access(expr)
        else:
            raise ETLError('BAD_EXPR', f"ETL_ERROR: unexpected token '{token.value}'", '')

    def _parse_member_access(self, left: 'ASTNode') -> 'ASTNode':
        """Parse member access (e.g., params.mean)."""
        while self._current() and self._current().type == '.':
            self._advance()  # consume '.'
            if not self._current() or self._current().type != 'IDENT':
                raise ETLError('BAD_EXPR', "ETL_ERROR: expected identifier after '.'", '')
            member = self._advance()
            left = MemberAccessNode(left, member.value)
        return left


# AST Nodes

class ASTNode:
    """Base class for AST nodes."""
    pass


class LiteralNode(ASTNode):
    def __init__(self, value: Any):
        self.value = value


class IdentifierNode(ASTNode):
    def __init__(self, name: str):
        self.name = name


class BinaryOpNode(ASTNode):
    def __init__(self, op: str, left: ASTNode, right: ASTNode):
        self.op = op
        self.left = left
        self.right = right


class UnaryOpNode(ASTNode):
    def __init__(self, op: str, operand: ASTNode):
        self.op = op
        self.operand = operand


class MemberAccessNode(ASTNode):
    def __init__(self, obj: ASTNode, member: str):
        self.obj = obj
        self.member = member


class ExpressionEvaluator:
    """Evaluates an AST against a row context."""

    def __init__(self, row: Dict[str, Any], params: Optional[Dict[str, Any]] = None):
        self.row = row
        self.params = params or {}

    def evaluate(self, node: ASTNode) -> Any:
        """Evaluate the AST node and return the result."""
        if isinstance(node, LiteralNode):
            return node.value
        elif isinstance(node, IdentifierNode):
            return self.row.get(node.name)
        elif isinstance(node, MemberAccessNode):
            return self._eval_member_access(node)
        elif isinstance(node, UnaryOpNode):
            return self._eval_unary(node)
        elif isinstance(node, BinaryOpNode):
            return self._eval_binary(node)
        else:
            raise ETLError('EXECUTION_FAILED', "ETL_ERROR: unknown AST node type", '')

    def _eval_member_access(self, node: MemberAccessNode) -> Any:
        """Evaluate member access (e.g., params.mean)."""
        # Check if the object is 'params' identifier
        if isinstance(node.obj, IdentifierNode) and node.obj.name == 'params':
            # Access params dictionary
            if node.member not in self.params:
                return None
            return self.params[node.member]

        # For other cases, evaluate the object and access its member
        obj_value = self.evaluate(node.obj)
        if obj_value is None:
            return None
        if isinstance(obj_value, dict):
            return obj_value.get(node.member)
        return None

    def _eval_unary(self, node: UnaryOpNode) -> Any:
        """Evaluate unary operators."""
        operand = self.evaluate(node.operand)

        if node.op == '!':
            return operand is None or not self._to_bool(operand)
        elif node.op == '-':
            return None if operand is None or not isinstance(operand, (int, float)) else -operand

        raise ETLError('EXECUTION_FAILED', f"ETL_ERROR: unknown unary operator '{node.op}'", '')

    def _eval_binary(self, node: BinaryOpNode) -> Any:
        """Evaluate binary operators."""
        left = self.evaluate(node.left)
        right = self.evaluate(node.right)

        op = node.op

        # Logical operators
        if op == '&&':
            return self._to_bool(left) and self._to_bool(right)
        elif op == '||':
            return self._to_bool(left) or self._to_bool(right)

        # Arithmetic operators
        if op in ('+', '-', '*', '/'):
            if left is None or right is None:
                return None
            if not isinstance(left, (int, float)) or not isinstance(right, (int, float)):
                return None
            if op == '+':
                return left + right
            elif op == '-':
                return left - right
            elif op == '*':
                return left * right
            elif op == '/':
                if right == 0:
                    return None
                return left / right

        # Comparison operators
        if op == '==':
            return left is not None and right is not None and type(left) == type(right) and left == right
        if op == '!=':
            return left is not None and right is not None and type(left) == type(right) and left != right
        if op in ('<', '<=', '>', '>='):
            if left is None or right is None:
                return False
            # Allow comparison between int and float
            if not isinstance(left, (int, float, str)) or not isinstance(right, (int, float, str)):
                return False
            # Check type compatibility: str can only compare with str, numbers with numbers
            if isinstance(left, str) != isinstance(right, str):
                return False
            if op == '<':
                return left < right
            elif op == '<=':
                return left <= right
            elif op == '>':
                return left > right
            else:  # op == '>='
                return left >= right

        raise ETLError('EXECUTION_FAILED', f"ETL_ERROR: unknown operator '{op}'", '')

    def _to_bool(self, value: Any) -> bool:
        """Convert a value to boolean for logical operations."""
        if value is None:
            return False
        elif isinstance(value, bool):
            return value
        elif isinstance(value, (int, float)):
            return value != 0
        elif isinstance(value, str):
            return len(value) > 0
        else:
            return True


def evaluate_expression(expr: str, row: Dict[str, Any], path: str, params: Optional[Dict[str, Any]] = None) -> Any:
    """Parse and evaluate an expression against a row."""
    try:
        tokenizer = ExpressionTokenizer(expr)
        tokens = tokenizer.tokenize()
        parser = ExpressionParser(tokens)
        ast = parser.parse()
        evaluator = ExpressionEvaluator(row, params)
        return evaluator.evaluate(ast)
    except ETLError:
        raise
    except Exception as e:
        raise ETLError('BAD_EXPR', f"ETL_ERROR: invalid expression", path)


def validate_expression(expr: str, path: str) -> None:
    """Validate expression syntax for filter and map operations."""
    if is_empty_after_trim(expr):
        raise ETLError(
            "BAD_EXPR",
            "ETL_ERROR: expression cannot be empty",
            path
        )

    expr = expr.strip()

    # Use the expression parser to check for syntax errors
    try:
        tokenizer = ExpressionTokenizer(expr)
        tokens = tokenizer.tokenize()
        parser = ExpressionParser(tokens)
        parser.parse()
    except ETLError as e:
        # Re-raise with proper path
        raise ETLError(e.error_code, e.message, path)
    except Exception:
        raise ETLError("BAD_EXPR", "ETL_ERROR: invalid expression syntax", path)


def validate_step(step: Dict[str, Any], index: int, path_prefix: str = "pipeline.steps") -> None:
    """Validate a single step in the pipeline."""
    if path_prefix == "pipeline.steps":
        path_prefix = f"pipeline.steps[{index}]"
    else:
        path_prefix = f"{path_prefix}.steps[{index}]"

    if not isinstance(step, dict):
        raise ETLError(
            "SCHEMA_VALIDATION_FAILED",
            "ETL_ERROR: step must be an object",
            path_prefix
        )

    if "op" not in step:
        raise ETLError(
            "SCHEMA_VALIDATION_FAILED",
            "ETL_ERROR: 'op' field is required",
            f"{path_prefix}.op"
        )

    op_value = step["op"]
    if not isinstance(op_value, str):
        raise ETLError(
            "SCHEMA_VALIDATION_FAILED",
            "ETL_ERROR: 'op' must be a string",
            f"{path_prefix}.op"
        )

    if is_empty_after_trim(op_value):
        raise ETLError(
            "SCHEMA_VALIDATION_FAILED",
            "ETL_ERROR: 'op' cannot be empty",
            f"{path_prefix}.op"
        )

    op_normalized = op_value.strip().lower()

    # Check for unknown operations
    valid_ops = {"select", "filter", "map", "rename", "limit", "branch", "call"}
    if op_normalized not in valid_ops:
        raise ETLError(
            "UNKNOWN_OP",
            f"ETL_ERROR: unsupported op '{op_normalized}'",
            f"{path_prefix}.op"
        )

    # Validate operation-specific fields
    if op_normalized == "select":
        if "columns" not in step:
            raise ETLError(
                "SCHEMA_VALIDATION_FAILED",
                "ETL_ERROR: 'columns' field is required for 'select' operation",
                f"{path_prefix}.columns"
            )
        columns = step["columns"]
        if not isinstance(columns, list):
            raise ETLError(
                "SCHEMA_VALIDATION_FAILED",
                "ETL_ERROR: 'columns' must be an array",
                f"{path_prefix}.columns"
            )
        for i, col in enumerate(columns):
            if not isinstance(col, str):
                raise ETLError(
                    "SCHEMA_VALIDATION_FAILED",
                    f"ETL_ERROR: column name must be a string",
                    f"{path_prefix}.columns[{i}]"
                )
            if is_empty_after_trim(col):
                raise ETLError(
                    "MISSING_COLUMN",
                    f"ETL_ERROR: column name cannot be empty",
                    f"{path_prefix}.columns[{i}]"
                )

    elif op_normalized == "filter":
        if "where" not in step:
            raise ETLError(
                "SCHEMA_VALIDATION_FAILED",
                "ETL_ERROR: 'where' field is required for 'filter' operation",
                f"{path_prefix}.where"
            )
        where = step["where"]
        if not isinstance(where, str):
            raise ETLError(
                "SCHEMA_VALIDATION_FAILED",
                "ETL_ERROR: 'where' must be a string",
                f"{path_prefix}.where"
            )
        if is_empty_after_trim(where):
            raise ETLError(
                "BAD_EXPR",
                "ETL_ERROR: 'where' expression cannot be empty",
                f"{path_prefix}.where"
            )
        validate_expression(where, f"{path_prefix}.where")

    elif op_normalized == "map":
        if "as" not in step:
            raise ETLError(
                "SCHEMA_VALIDATION_FAILED",
                "ETL_ERROR: 'as' field is required for 'map' operation",
                f"{path_prefix}.as"
            )
        if "expr" not in step:
            raise ETLError(
                "SCHEMA_VALIDATION_FAILED",
                "ETL_ERROR: 'expr' field is required for 'map' operation",
                f"{path_prefix}.expr"
            )

        as_value = step["as"]
        if not isinstance(as_value, str):
            raise ETLError(
                "SCHEMA_VALIDATION_FAILED",
                "ETL_ERROR: 'as' must be a string",
                f"{path_prefix}.as"
            )
        if is_empty_after_trim(as_value):
            raise ETLError(
                "SCHEMA_VALIDATION_FAILED",
                "ETL_ERROR: 'as' cannot be empty",
                f"{path_prefix}.as"
            )

        expr = step["expr"]
        if not isinstance(expr, str):
            raise ETLError(
                "SCHEMA_VALIDATION_FAILED",
                "ETL_ERROR: 'expr' must be a string",
                f"{path_prefix}.expr"
            )
        if is_empty_after_trim(expr):
            raise ETLError(
                "BAD_EXPR",
                "ETL_ERROR: 'expr' expression cannot be empty",
                f"{path_prefix}.expr"
            )
        validate_expression(expr, f"{path_prefix}.expr")

    elif op_normalized == "rename":
        has_from_to = "from" in step and "to" in step
        has_mapping = "mapping" in step

        if not has_from_to and not has_mapping:
            raise ETLError(
                "SCHEMA_VALIDATION_FAILED",
                "ETL_ERROR: 'rename' requires either 'from'/'to' or 'mapping'",
                path_prefix
            )

        if has_from_to and has_mapping:
            # Both present - we'll process from/to and ignore mapping or vice versa
            # Based on the spec, let's check which one to use
            # The spec says "requires either", so having both is ambiguous
            # Let's prefer from/to conversion as shown in example
            pass

        if has_from_to:
            from_value = step["from"]
            to_value = step["to"]

            if not isinstance(from_value, str):
                raise ETLError(
                    "SCHEMA_VALIDATION_FAILED",
                    "ETL_ERROR: 'from' must be a string",
                    f"{path_prefix}.from"
                )
            if not isinstance(to_value, str):
                raise ETLError(
                    "SCHEMA_VALIDATION_FAILED",
                    "ETL_ERROR: 'to' must be a string",
                    f"{path_prefix}.to"
                )

            if is_empty_after_trim(from_value):
                raise ETLError(
                    "MISSING_COLUMN",
                    "ETL_ERROR: 'from' cannot be empty",
                    f"{path_prefix}.from"
                )
            if is_empty_after_trim(to_value):
                raise ETLError(
                    "MISSING_COLUMN",
                    "ETL_ERROR: 'to' cannot be empty",
                    f"{path_prefix}.to"
                )

        if has_mapping and not has_from_to:
            mapping = step["mapping"]
            if not isinstance(mapping, dict):
                raise ETLError(
                    "SCHEMA_VALIDATION_FAILED",
                    "ETL_ERROR: 'mapping' must be an object",
                    f"{path_prefix}.mapping"
                )
            for key, value in mapping.items():
                if not isinstance(key, str):
                    raise ETLError(
                        "SCHEMA_VALIDATION_FAILED",
                        "ETL_ERROR: mapping key must be a string",
                        f"{path_prefix}.mapping"
                    )
                if not isinstance(value, str):
                    raise ETLError(
                        "SCHEMA_VALIDATION_FAILED",
                        "ETL_ERROR: mapping value must be a string",
                        f"{path_prefix}.mapping"
                    )

    elif op_normalized == "limit":
        if "n" not in step:
            raise ETLError(
                "SCHEMA_VALIDATION_FAILED",
                "ETL_ERROR: 'n' field is required for 'limit' operation",
                f"{path_prefix}.n"
            )
        n_value = step["n"]
        if not isinstance(n_value, int):
            raise ETLError(
                "SCHEMA_VALIDATION_FAILED",
                "ETL_ERROR: 'n' must be an integer",
                f"{path_prefix}.n"
            )
        if n_value < 0:
            raise ETLError(
                "SCHEMA_VALIDATION_FAILED",
                "ETL_ERROR: 'n' must be >= 0",
                f"{path_prefix}.n"
            )

    elif op_normalized == "branch":
        if "branches" not in step:
            raise ETLError(
                "SCHEMA_VALIDATION_FAILED",
                "ETL_ERROR: 'branches' field is required for 'branch' operation",
                f"{path_prefix}.branches"
            )
        branches = step["branches"]
        if not isinstance(branches, list):
            raise ETLError(
                "SCHEMA_VALIDATION_FAILED",
                "ETL_ERROR: 'branches' must be an array",
                f"{path_prefix}.branches"
            )
        if len(branches) == 0:
            raise ETLError(
                "MALFORMED_STEP",
                "ETL_ERROR: 'branches' must be a non-empty array",
                f"{path_prefix}.branches"
            )

        # Check for at most one 'otherwise' branch and that it's last
        otherwise_count = 0
        otherwise_index = -1
        for i, branch in enumerate(branches):
            if not isinstance(branch, dict):
                raise ETLError(
                    "SCHEMA_VALIDATION_FAILED",
                    "ETL_ERROR: branch must be an object",
                    f"{path_prefix}.branches[{i}]"
                )

            # Check 'when' field
            if "when" not in branch:
                raise ETLError(
                    "SCHEMA_VALIDATION_FAILED",
                    "ETL_ERROR: 'when' field is required for each branch",
                    f"{path_prefix}.branches[{i}].when"
                )
            when_value = branch["when"]
            if not isinstance(when_value, str):
                raise ETLError(
                    "SCHEMA_VALIDATION_FAILED",
                    "ETL_ERROR: 'when' must be a string",
                    f"{path_prefix}.branches[{i}].when"
                )

            # Check for 'otherwise'
            if when_value == "otherwise":
                otherwise_count += 1
                otherwise_index = i
            else:
                # Validate expression if not 'otherwise'
                if is_empty_after_trim(when_value):
                    raise ETLError(
                        "BAD_EXPR",
                        "ETL_ERROR: 'when' expression cannot be empty",
                        f"{path_prefix}.branches[{i}].when"
                    )
                validate_expression(when_value, f"{path_prefix}.branches[{i}].when")

            # Check 'steps' field
            if "steps" not in branch:
                raise ETLError(
                    "SCHEMA_VALIDATION_FAILED",
                    "ETL_ERROR: 'steps' field is required for each branch",
                    f"{path_prefix}.branches[{i}].steps"
                )
            steps_value = branch["steps"]
            if not isinstance(steps_value, list):
                raise ETLError(
                    "SCHEMA_VALIDATION_FAILED",
                    "ETL_ERROR: 'steps' must be an array",
                    f"{path_prefix}.branches[{i}].steps"
                )

            # Recursively validate nested steps
            for j, nested_step in enumerate(steps_value):
                validate_step(nested_step, j, path_prefix=f"{path_prefix}.branches[{i}]")

        # Check 'otherwise' constraints
        if otherwise_count > 1:
            raise ETLError(
                "MALFORMED_STEP",
                "ETL_ERROR: at most one 'otherwise' branch is allowed",
                f"{path_prefix}.branches"
            )
        if otherwise_count == 1 and otherwise_index != len(branches) - 1:
            raise ETLError(
                "MALFORMED_STEP",
                "ETL_ERROR: 'otherwise' branch must be last",
                f"{path_prefix}.branches"
            )

        # Validate merge field if present
        if "merge" in step:
            merge = step["merge"]
            if not isinstance(merge, dict):
                raise ETLError(
                    "SCHEMA_VALIDATION_FAILED",
                    "ETL_ERROR: 'merge' must be an object",
                    f"{path_prefix}.merge"
                )
            if "strategy" in merge:
                strategy = merge["strategy"]
                if not isinstance(strategy, str):
                    raise ETLError(
                        "SCHEMA_VALIDATION_FAILED",
                        "ETL_ERROR: 'merge.strategy' must be a string",
                        f"{path_prefix}.merge.strategy"
                    )
                if strategy != "concat":
                    raise ETLError(
                        "MALFORMED_STEP",
                        f"ETL_ERROR: unsupported merge strategy '{strategy}'",
                        f"{path_prefix}.merge.strategy"
                    )

    elif op_normalized == "call":
        # Validate 'name' field
        if "name" not in step:
            raise ETLError(
                "SCHEMA_VALIDATION_FAILED",
                "ETL_ERROR: 'name' field is required for 'call' operation",
                f"{path_prefix}.name"
            )
        name_value = step["name"]
        if not isinstance(name_value, str):
            raise ETLError(
                "SCHEMA_VALIDATION_FAILED",
                "ETL_ERROR: 'name' must be a string",
                f"{path_prefix}.name"
            )
        if is_empty_after_trim(name_value):
            raise ETLError(
                "SCHEMA_VALIDATION_FAILED",
                "ETL_ERROR: 'name' cannot be empty",
                f"{path_prefix}.name"
            )
        # Validate name matches ^[A-Za-z_][A-Za-z0-9_]*$
        import re
        if not re.match(r'^[A-Za-z_][A-Za-z0-9_]*$', name_value):
            raise ETLError(
                "SCHEMA_VALIDATION_FAILED",
                f"ETL_ERROR: 'name' must match ^[A-Za-z_][A-Za-z0-9_]*$",
                f"{path_prefix}.name"
            )

        # Validate 'params' field if present
        if "params" in step:
            params_value = step["params"]
            if not isinstance(params_value, dict):
                raise ETLError(
                    "SCHEMA_VALIDATION_FAILED",
                    "ETL_ERROR: 'params' must be an object",
                    f"{path_prefix}.params"
                )
            # Validate that params values are JSON scalars or arrays (no nested objects)
            for key, val in params_value.items():
                if not isinstance(key, str):
                    raise ETLError(
                        "SCHEMA_VALIDATION_FAILED",
                        "ETL_ERROR: params key must be a string",
                        f"{path_prefix}.params"
                    )
                if isinstance(val, dict):
                    raise ETLError(
                        "SCHEMA_VALIDATION_FAILED",
                        "ETL_ERROR: params value cannot be a nested object",
                        f"{path_prefix}.params.{key}"
                    )


def validate_defs(defs: Dict[str, Any], defs_path: str = "defs") -> None:
    """Validate defs structure and all definitions."""
    if not isinstance(defs, dict):
        raise ETLError(
            "SCHEMA_VALIDATION_FAILED",
            "ETL_ERROR: 'defs' must be an object",
            defs_path
        )

    for name, def_value in defs.items():
        # Validate name matches ^[A-Za-z_][A-Za-z0-9_]*$
        import re
        if not isinstance(name, str) or not re.match(r'^[A-Za-z_][A-Za-z0-9_]*$', name):
            raise ETLError(
                "SCHEMA_VALIDATION_FAILED",
                f"ETL_ERROR: def name must match ^[A-Za-z_][A-Za-z0-9_]*$",
                f"{defs_path}[{name}]"
            )

        if not isinstance(def_value, dict):
            raise ETLError(
                "SCHEMA_VALIDATION_FAILED",
                "ETL_ERROR: definition must be an object",
                f"{defs_path}[{name}]"
            )

        if "steps" not in def_value:
            raise ETLError(
                "SCHEMA_VALIDATION_FAILED",
                "ETL_ERROR: 'steps' field is required in definition",
                f"{defs_path}[{name}].steps"
            )

        steps = def_value["steps"]
        if not isinstance(steps, list):
            raise ETLError(
                "SCHEMA_VALIDATION_FAILED",
                "ETL_ERROR: 'steps' must be an array",
                f"{defs_path}[{name}].steps"
            )

        # Validate each step in the definition
        for i, step in enumerate(steps):
            validate_step(step, i, f"{defs_path}[{name}]")


def normalize_step(step: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize a single step according to the rules."""
    result = {}

    # Normalize op
    op_value = step["op"].strip().lower()
    result["op"] = op_value

    # Process operation-specific fields
    if op_value == "select":
        columns = step.get("columns", [])
        # Column names are preserved exactly (not trimmed)
        result["columns"] = columns

    elif op_value == "filter":
        # Trim expression
        where = step.get("where", "").strip()
        result["where"] = where

    elif op_value == "map":
        # Trim expression and as field
        expr = step.get("expr", "").strip()
        as_value = step.get("as", "").strip()
        result["as"] = as_value
        result["expr"] = expr

    elif op_value == "rename":
        has_from_to = "from" in step and "to" in step
        has_mapping = "mapping" in step

        if has_from_to:
            # Convert from/to to mapping
            from_value = step["from"].strip()
            to_value = step["to"].strip()
            result["mapping"] = {from_value: to_value}
        elif has_mapping:
            # Preserve mapping keys/values exactly
            mapping = step.get("mapping", {})
            result["mapping"] = mapping

    elif op_value == "limit":
        n_value = step.get("n", 0)
        result["n"] = n_value

    elif op_value == "branch":
        # Normalize branches
        branches = step.get("branches", [])
        normalized_branches = []
        for branch in branches:
            normalized_branch = {}
            # Preserve id if present
            if "id" in branch:
                normalized_branch["id"] = branch["id"]
            # Normalize when (trim if not 'otherwise')
            when = branch.get("when", "")
            if when != "otherwise":
                when = when.strip()
            normalized_branch["when"] = when
            # Recursively normalize nested steps
            nested_steps = branch.get("steps", [])
            normalized_branch["steps"] = [normalize_step(s) for s in nested_steps]
            normalized_branches.append(normalized_branch)
        result["branches"] = normalized_branches
        # Normalize merge (default to concat if missing)
        merge = step.get("merge", {"strategy": "concat"})
        if isinstance(merge, dict) and "strategy" not in merge:
            merge["strategy"] = "concat"
        result["merge"] = merge

    elif op_value == "call":
        # Normalize name (preserve case)
        name_value = step.get("name", "")
        result["name"] = name_value
        # Default params to {} if missing
        params_value = step.get("params", {})
        result["params"] = params_value

    # Sort keys: op first, then alphabetically
    sorted_result = {"op": result["op"]}
    for key in sorted(k for k in result.keys() if k != "op"):
        sorted_result[key] = result[key]

    return sorted_result


def normalize_defs(defs: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize all definitions."""
    normalized_defs = {}
    for name, def_value in defs.items():
        normalized_steps = [normalize_step(step) for step in def_value.get("steps", [])]
        normalized_defs[name] = {"steps": normalized_steps}
    return normalized_defs


def process_pipeline(input_data: str) -> Tuple[Dict[str, Any], int]:
    """Process the ETL pipeline input and return result with exit code."""
    try:
        data = json.loads(input_data)
    except json.JSONDecodeError as e:
        return {
            "status": "error",
            "error_code": "SCHEMA_VALIDATION_FAILED",
            "message": f"ETL_ERROR: invalid JSON: {str(e)}",
            "path": "root"
        }, 1

    try:
        # Validate structure
        validate_json_structure(data)

        steps = data["pipeline"]["steps"]

        # Get and validate defs if present
        defs = data.get("defs", {})
        if defs:
            validate_defs(defs)

        # Validate each step
        for i, step in enumerate(steps):
            validate_step(step, i)

        # Normalize steps
        normalized_steps = [normalize_step(step) for step in steps]

        # Build result
        result = {
            "status": "ok",
            "normalized": {
                "steps": normalized_steps
            }
        }

        # Include normalized defs if present
        if defs:
            result["normalized"]["defs"] = normalize_defs(defs)

        return result, 0

    except ETLError as e:
        return {
            "status": "error",
            "error_code": e.error_code,
            "message": e.message,
            "path": e.path
        }, 1


# ============================================================================
# ETL Operation Executors
# ============================================================================

def execute_select(rows: List[Dict[str, Any]], step: Dict[str, Any], step_index: int, path_prefix: str = "pipeline.steps") -> List[Dict[str, Any]]:
    """Execute a select operation."""
    columns = step["columns"]
    result = []
    path = f"{path_prefix}[{step_index}]"

    for row_idx, row in enumerate(rows):
        new_row = {}
        for col_idx, col in enumerate(columns):
            if col not in row:
                raise ETLError(
                    "MISSING_COLUMN",
                    f"ETL_ERROR: column '{col}' not found in row",
                    f"{path}.columns[{col_idx}]"
                )
            new_row[col] = row[col]
        result.append(new_row)

    return result


def execute_filter(rows: List[Dict[str, Any]], step: Dict[str, Any], step_index: int, path_prefix: str = "pipeline.steps") -> List[Dict[str, Any]]:
    """Execute a filter operation."""
    return execute_filter_with_params(rows, step, step_index, path_prefix, {})


def execute_filter_with_params(rows: List[Dict[str, Any]], step: Dict[str, Any], step_index: int, path_prefix: str, params: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Execute a filter operation with params support."""
    where_expr = step["where"]
    path = f"{path_prefix}[{step_index}].where"
    result = []

    for row in rows:
        try:
            condition = evaluate_expression(where_expr, row, path, params)
            # Treat null/false as false (null is falsy)
            if condition is True:
                result.append(row)
        except ETLError:
            raise
        except Exception:
            # If evaluation fails, row is filtered out
            pass

    return result


def execute_map(rows: List[Dict[str, Any]], step: Dict[str, Any], step_index: int, path_prefix: str = "pipeline.steps") -> List[Dict[str, Any]]:
    """Execute a map operation."""
    return execute_map_with_params(rows, step, step_index, path_prefix, {})


def execute_map_with_params(rows: List[Dict[str, Any]], step: Dict[str, Any], step_index: int, path_prefix: str, params: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Execute a map operation with params support."""
    expr = step["expr"]
    as_field = step["as"]
    path = f"{path_prefix}[{step_index}].expr"
    result = []

    for row in rows:
        new_row = dict(row)  # Make a copy
        try:
            value = evaluate_expression(expr, row, path, params)
            new_row[as_field] = value
        except ETLError:
            raise
        except Exception as e:
            raise ETLError('EXECUTION_FAILED', f"ETL_ERROR: expression evaluation failed", path)
        result.append(new_row)

    return result


def execute_rename(rows: List[Dict[str, Any]], step: Dict[str, Any], step_index: int, path_prefix: str = "pipeline.steps") -> List[Dict[str, Any]]:
    """Execute a rename operation."""
    mapping = step.get("mapping", {})
    path = f"{path_prefix}[{step_index}]"
    result = []

    for row_idx, row in enumerate(rows):
        new_row = dict(row)
        for src, tgt in mapping.items():
            if src not in new_row:
                raise ETLError(
                    "MISSING_COLUMN",
                    f"ETL_ERROR: column '{src}' not found in row",
                    f"{path}.mapping.{src}"
                )
            # Move value from src to tgt
            new_row[tgt] = new_row.pop(src)
        result.append(new_row)

    return result


def execute_limit(rows: List[Dict[str, Any]], step: Dict[str, Any], step_index: int, path_prefix: str = "pipeline.steps") -> List[Dict[str, Any]]:
    """Execute a limit operation."""
    n = step["n"]
    return rows[:n]


def execute_call(rows: List[Dict[str, Any]], step: Dict[str, Any], step_index: int, path_prefix: str, defs: Dict[str, Any], call_stack: set) -> List[Dict[str, Any]]:
    """Execute a call operation."""
    name = step.get("name", "")
    params = step.get("params", {})
    path = f"{path_prefix}[{step_index}].name"

    # Check if definition exists
    if name not in defs:
        raise ETLError(
            "UNKNOWN_DEF",
            f"ETL_ERROR: definition '{name}' not found",
            path
        )

    # Check for recursion
    if name in call_stack:
        raise ETLError(
            "RECURSION_FORBIDDEN",
            f"ETL_ERROR: recursive call to '{name}' detected",
            path
        )

    # Add to call stack
    new_call_stack = call_stack | {name}

    # Get the definition's steps
    def_steps = defs[name].get("steps", [])
    def_path = f"defs[{name}]"

    # Execute each step in the definition with params
    result_rows = rows
    for j, def_step in enumerate(def_steps):
        op = def_step.get("op", "").strip().lower()
        normalized = normalize_step(def_step)
        step_path = f"{def_path}.steps[{j}]"

        if op == "select":
            result_rows = execute_select(result_rows, normalized, j, f"{def_path}.steps")
        elif op == "filter":
            result_rows = execute_filter_with_params(result_rows, normalized, j, f"{def_path}.steps", params)
        elif op == "map":
            result_rows = execute_map_with_params(result_rows, normalized, j, f"{def_path}.steps", params)
        elif op == "rename":
            result_rows = execute_rename(result_rows, normalized, j, f"{def_path}.steps")
        elif op == "limit":
            result_rows = execute_limit(result_rows, normalized, j, f"{def_path}.steps")
        elif op == "branch":
            result_rows = execute_branch_with_params(result_rows, normalized, j, f"{def_path}.steps", defs, new_call_stack, params)
        elif op == "call":
            result_rows = execute_call(result_rows, normalized, j, f"{def_path}.steps", defs, new_call_stack)

    return result_rows


def execute_branch(rows: List[Dict[str, Any]], step: Dict[str, Any], step_index: int, path_prefix: str = "pipeline.steps") -> List[Dict[str, Any]]:
    """Execute a branch operation."""
    return execute_branch_with_params(rows, step, step_index, path_prefix, {}, set(), {})


def execute_branch_with_params(rows: List[Dict[str, Any]], step: Dict[str, Any], step_index: int, path_prefix: str, defs: Dict[str, Any], call_stack: set, params: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Execute a branch operation with params and defs support."""
    branches = step.get("branches", [])
    merge_strategy = step.get("merge", {}).get("strategy", "concat")
    base_path = f"{path_prefix}[{step_index}]"

    # Collect rows for each branch in order
    branch_results = [[] for _ in branches]

    for row in rows:
        matched = False
        for i, branch in enumerate(branches):
            when_value = branch.get("when", "")

            # Check if this is the 'otherwise' branch
            if when_value == "otherwise":
                # Only match if no previous branch matched
                if not matched:
                    branch_results[i].append(row)
                    matched = True
            else:
                # Evaluate the condition
                cond_path = f"{base_path}.branches[{i}].when"
                try:
                    condition = evaluate_expression(when_value, row, cond_path, params)
                    if condition is True:
                        branch_results[i].append(row)
                        matched = True
                        break  # First match wins
                except ETLError:
                    raise
                except Exception:
                    # If evaluation fails, treat as false
                    pass

        # If no branch matched, row is dropped (unless there's an otherwise)
        # The otherwise branch handling is above

    # Process each branch's steps
    for i, branch in enumerate(branches):
        branch_steps = branch.get("steps", [])
        branch_rows = branch_results[i]
        branch_path = f"{base_path}.branches[{i}]"

        # Execute nested steps for this branch
        for j, nested_step in enumerate(branch_steps):
            op = nested_step.get("op", "").strip().lower()
            normalized = normalize_step(nested_step)

            if op == "select":
                branch_rows = execute_select(branch_rows, normalized, j, f"{branch_path}.steps")
            elif op == "filter":
                branch_rows = execute_filter_with_params(branch_rows, normalized, j, f"{branch_path}.steps", params)
            elif op == "map":
                branch_rows = execute_map_with_params(branch_rows, normalized, j, f"{branch_path}.steps", params)
            elif op == "rename":
                branch_rows = execute_rename(branch_rows, normalized, j, f"{branch_path}.steps")
            elif op == "limit":
                branch_rows = execute_limit(branch_rows, normalized, j, f"{branch_path}.steps")
            elif op == "branch":
                branch_rows = execute_branch_with_params(branch_rows, normalized, j, f"{branch_path}.steps", defs, call_stack, params)
            elif op == "call":
                branch_rows = execute_call(branch_rows, normalized, j, f"{branch_path}.steps", defs, call_stack)

        branch_results[i] = branch_rows

    # Merge results based on strategy
    if merge_strategy == "concat":
        # Concatenate in branch declaration order
        result = []
        for branch_rows in branch_results:
            result.extend(branch_rows)
        return result

    return []


def execute_pipeline(data: Dict[str, Any]) -> Tuple[Dict[str, Any], int]:
    """Execute the ETL pipeline and return results with metrics."""
    try:
        # Validate structure first
        validate_json_structure(data)

        steps = data["pipeline"]["steps"]
        rows = data["dataset"]
        rows_in = len(rows)

        # Get defs if present
        defs = data.get("defs", {})

        # Validate defs if present
        if defs:
            validate_defs(defs)

        # Validate each step
        for i, step in enumerate(steps):
            validate_step(step, i)

        # Execute each step
        for i, step in enumerate(steps):
            op = step["op"].strip().lower()

            # Normalize the step first
            normalized = normalize_step(step)

            if op == "select":
                rows = execute_select(rows, normalized, i)
            elif op == "filter":
                rows = execute_filter(rows, normalized, i)
            elif op == "map":
                rows = execute_map(rows, normalized, i)
            elif op == "rename":
                rows = execute_rename(rows, normalized, i)
            elif op == "limit":
                rows = execute_limit(rows, normalized, i)
            elif op == "branch":
                rows = execute_branch_with_params(rows, normalized, i, "pipeline.steps", defs, set(), {})
            elif op == "call":
                rows = execute_call(rows, normalized, i, "pipeline.steps", defs, set())

        rows_out = len(rows)

        return {
            "status": "ok",
            "data": rows,
            "metrics": {
                "rows_in": rows_in,
                "rows_out": rows_out
            }
        }, 0

    except ETLError as e:
        return {
            "status": "error",
            "error_code": e.error_code,
            "message": e.message,
            "path": e.path
        }, 1


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(description='ETL Pipeline Processor')
    parser.add_argument('--execute', action='store_true', default=False,
                        help='Execute the pipeline (default: false, returns normalized)')
    args = parser.parse_args()

    input_data = sys.stdin.read()

    if args.execute:
        try:
            data = json.loads(input_data)
            result, exit_code = execute_pipeline(data)
        except json.JSONDecodeError as e:
            result = {
                "status": "error",
                "error_code": "SCHEMA_VALIDATION_FAILED",
                "message": f"ETL_ERROR: invalid JSON: {str(e)}",
                "path": "root"
            }
            exit_code = 1
    else:
        result, exit_code = process_pipeline(input_data)

    print(json.dumps(result))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
