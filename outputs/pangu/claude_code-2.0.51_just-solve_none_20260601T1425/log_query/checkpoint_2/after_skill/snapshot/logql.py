#!/usr/bin/env python3
"""
LogQL — Part 2: GROUP BY and Aggregations

Supports:
- SELECT with aggregate functions (COUNT, SUM, AVG, MIN, MAX, UNIQUE)
- GROUP BY field references
- WHERE filtering (from Part 1)
- Field references with dotted paths
"""

import argparse
import json
import sys
from collections import OrderedDict
from typing import Any, Optional


# Tokenizer

class TokenType:
    SELECT = "SELECT"
    FROM = "FROM"
    WHERE = "WHERE"
    GROUP_BY = "GROUP_BY"
    AS = "AS"
    COMMA = ","
    DOT = "."
    STAR = "*"
    LPAREN = "("
    RPAREN = ")"
    EQ = "="
    NE = "!="
    LT = "<"
    LTE = "<="
    GT = ">"
    GTE = ">="
    AND = "AND"
    OR = "OR"
    NOT = "NOT"
    NULL = "NULL"
    TRUE = "TRUE"
    FALSE = "FALSE"
    IDENT = "IDENT"
    STRING = "STRING"
    NUMBER = "NUMBER"
    EOF = "EOF"


class Token:
    def __init__(self, type_: str, value: str, pos: int):
        self.type = type_
        self.value = value
        self.pos = pos

    def __repr__(self):
        return f"Token({self.type}, {repr(self.value)})"


KEYWORDS = {
    "SELECT": TokenType.SELECT,
    "FROM": TokenType.FROM,
    "WHERE": TokenType.WHERE,
    "GROUP": TokenType.IDENT,
    "BY": TokenType.IDENT,
    "AS": TokenType.AS,
    "AND": TokenType.AND,
    "OR": TokenType.OR,
    "NOT": TokenType.NOT,
    "NULL": TokenType.NULL,
    "TRUE": TokenType.TRUE,
    "FALSE": TokenType.FALSE,
    "COUNT": TokenType.IDENT,
    "SUM": TokenType.IDENT,
    "AVG": TokenType.IDENT,
    "AVERAGE": TokenType.IDENT,
    "MIN": TokenType.IDENT,
    "MAX": TokenType.IDENT,
    "UNIQUE": TokenType.IDENT,
}


class Tokenizer:
    def __init__(self, query: str):
        self.query = query
        self.pos = 0
        self.tokens: list[Token] = []
        self.current_char = query[self.pos] if query else None

    def advance(self):
        self.pos += 1
        if self.pos >= len(self.query):
            self.current_char = None
        else:
            self.current_char = self.query[self.pos]

    def skip_whitespace(self):
        while self.current_char is not None and self.current_char.isspace():
            self.advance()

    def read_identifier(self) -> str:
        start = self.pos
        while self.current_char is not None and (self.current_char.isalnum() or self.current_char == '_'):
            self.advance()
        return self.query[start:self.pos]

    def read_number(self) -> str:
        start = self.pos
        while self.current_char is not None and (self.current_char.isdigit() or self.current_char == '.'):
            self.advance()
        return self.query[start:self.pos]

    def read_string(self) -> str:
        start = self.pos + 1
        self.advance()  # skip opening quote
        result = []
        while self.current_char is not None and self.current_char != '"':
            if self.current_char == '\\':
                self.advance()
                if self.current_char is not None:
                    result.append(self.current_char)
            else:
                result.append(self.current_char)
            self.advance()
        if self.current_char == '"':
            self.advance()
        return ''.join(result)

    def tokenize(self) -> list[Token]:
        while self.current_char is not None:
            if self.current_char.isspace():
                self.skip_whitespace()
                continue

            if self.current_char.isalpha() or self.current_char == '_':
                ident = self.read_identifier()
                upper_ident = ident.upper()
                if upper_ident in KEYWORDS:
                    # Special handling for GROUP BY
                    if upper_ident == "GROUP":
                        self.tokens.append(Token(TokenType.IDENT, ident, self.pos))
                    else:
                        self.tokens.append(Token(KEYWORDS[upper_ident], ident, self.pos))
                else:
                    self.tokens.append(Token(TokenType.IDENT, ident, self.pos))
                continue

            if self.current_char.isdigit() or (self.current_char == '.' and self.pos + 1 < len(self.query) and self.query[self.pos + 1].isdigit()):
                num = self.read_number()
                self.tokens.append(Token(TokenType.NUMBER, num, self.pos))
                continue

            if self.current_char == '"':
                self.tokens.append(Token(TokenType.STRING, self.read_string(), self.pos))
                continue

            # Operators and punctuation
            if self.current_char == '=':
                self.advance()
                self.tokens.append(Token(TokenType.EQ, '=', self.pos))
                continue
            if self.current_char == '!' and self.pos + 1 < len(self.query) and self.query[self.pos + 1] == '=':
                self.advance()
                self.advance()
                self.tokens.append(Token(TokenType.NE, '!=', self.pos))
                continue
            if self.current_char == '<' and self.pos + 1 < len(self.query) and self.query[self.pos + 1] == '=':
                self.advance()
                self.advance()
                self.tokens.append(Token(TokenType.LTE, '<=', self.pos))
                continue
            if self.current_char == '>' and self.pos + 1 < len(self.query) and self.query[self.pos + 1] == '=':
                self.advance()
                self.advance()
                self.tokens.append(Token(TokenType.GTE, '>=', self.pos))
                continue
            if self.current_char == '<':
                self.advance()
                self.tokens.append(Token(TokenType.LT, '<', self.pos))
                continue
            if self.current_char == '>':
                self.advance()
                self.tokens.append(Token(TokenType.GT, '>', self.pos))
                continue
            if self.current_char == ',':
                self.advance()
                self.tokens.append(Token(TokenType.COMMA, ',', self.pos))
                continue
            if self.current_char == '.':
                self.advance()
                self.tokens.append(Token(TokenType.DOT, '.', self.pos))
                continue
            if self.current_char == '*':
                self.advance()
                self.tokens.append(Token(TokenType.STAR, '*', self.pos))
                continue
            if self.current_char == '(':
                self.advance()
                self.tokens.append(Token(TokenType.LPAREN, '(', self.pos))
                continue
            if self.current_char == ')':
                self.advance()
                self.tokens.append(Token(TokenType.RPAREN, ')', self.pos))
                continue

            # Unknown character
            raise ValueError(f"Unexpected character: {self.current_char} at position {self.pos}")

        self.tokens.append(Token(TokenType.EOF, '', self.pos))
        return self.tokens


# Parser

class ParseError(Exception):
    pass


class ASTNode:
    pass


class FieldRef(ASTNode):
    def __init__(self, parts: list[str]):
        self.parts = parts

    def __repr__(self):
        return f"FieldRef({self.parts})"

    def as_text(self) -> str:
        return ".".join(self.parts)


class Literal(ASTNode):
    def __init__(self, value: Any):
        self.value = value

    def __repr__(self):
        return f"Literal({repr(self.value)})"


class AggCall(ASTNode):
    def __init__(self, func: str, arg: Optional[FieldRef]):  # arg is None for COUNT(*)
        self.func = func.upper()
        self.arg = arg

    def __repr__(self):
        return f"AggCall({self.func}, {self.arg})"

    def canonical_key(self) -> str:
        if self.arg is None:
            return f"{self.func}(*)"
        return f"{self.func}({self.arg.as_text()})"


class SelectItem(ASTNode):
    def __init__(self, item: Any, alias: Optional[str]):
        self.item = item  # FieldRef or AggCall
        self.alias = alias

    def __repr__(self):
        return f"SelectItem({self.item}, alias={self.alias})"

    def output_key(self) -> str:
        if self.alias is not None:
            return self.alias
        if isinstance(self.item, FieldRef):
            return self.item.as_text()
        # AggCall
        return self.item.canonical_key()


class BinaryExpr(ASTNode):
    def __init__(self, op: str, left: ASTNode, right: ASTNode):
        self.op = op
        self.left = left
        self.right = right

    def __repr__(self):
        return f"BinaryExpr({repr(self.op)}, {self.left}, {self.right})"


class UnaryExpr(ASTNode):
    def __init__(self, op: str, expr: ASTNode):
        self.op = op
        self.expr = expr

    def __repr__(self):
        return f"UnaryExpr({repr(self.op)}, {self.expr})"


class Query(ASTNode):
    def __init__(self, select_items: list[SelectItem], where_expr: Optional[ASTNode], group_by: Optional[list[FieldRef]]):
        self.select_items = select_items
        self.where_expr = where_expr
        self.group_by = group_by

    def __repr__(self):
        return f"Query(select={self.select_items}, where={self.where_expr}, group_by={self.group_by})"


class Parser:
    def __init__(self, tokens: list[Token]):
        self.tokens = tokens
        self.pos = 0

    def peek(self) -> Token:
        if self.pos >= len(self.tokens):
            return Token(TokenType.EOF, '', self.pos)
        return self.tokens[self.pos]

    def consume(self, type_: str, value: Optional[str] = None) -> Token:
        token = self.peek()
        if token.type != type_:
            raise ParseError(f"Expected {type_}, got {token.type}")
        if value is not None and token.value != value:
            raise ParseError(f"Expected {value}, got {token.value}")
        self.pos += 1
        return token

    def parse_query(self) -> Query:
        select_items = self.parse_select()
        self.consume(TokenType.FROM)
        self.consume(TokenType.IDENT, "logs")

        where_expr = None
        if self.peek().type == TokenType.WHERE:
            self.consume(TokenType.WHERE)
            where_expr = self.parse_boolean_expr()

        group_by = None
        # Handle GROUP BY (GROUP is an IDENT token, followed by BY IDENT)
        if (self.peek().type == TokenType.IDENT and
            self.peek().value.upper() == "GROUP" and
            self.pos + 2 < len(self.tokens) and
            self.tokens[self.pos + 1].type == TokenType.IDENT and
            self.tokens[self.pos + 1].value.upper() == "BY"):
            self.consume(TokenType.IDENT)  # consume GROUP
            self.consume(TokenType.IDENT)  # consume BY
            group_by = self.parse_group_by()

        self.consume(TokenType.EOF)
        return Query(select_items, where_expr, group_by)

    def parse_select(self) -> list[SelectItem]:
        self.consume(TokenType.SELECT)
        items = [self.parse_select_item()]
        while self.peek().type == TokenType.COMMA:
            self.consume(TokenType.COMMA)
            items.append(self.parse_select_item())
        return items

    def parse_select_item(self) -> SelectItem:
        token = self.peek()

        # Check if it's an aggregate call
        if token.type == TokenType.IDENT and token.value.upper() in ("COUNT", "SUM", "AVG", "AVERAGE", "MIN", "MAX", "UNIQUE"):
            agg = self.parse_agg_call()
            alias = None
            if self.peek().type == TokenType.IDENT and self.peek().value.upper() == "AS":
                self.consume(TokenType.IDENT)  # consume AS
                alias = self.consume(TokenType.IDENT).value
            return SelectItem(agg, alias)

        # Check for SELECT *
        if token.type == TokenType.STAR:
            self.consume(TokenType.STAR)
            # SELECT * with no aggregates - handled specially
            return SelectItem(FieldRef(["*"]), None)

        # Field reference
        field = self.parse_field_ref()
        alias = None
        if self.peek().type == TokenType.IDENT and self.peek().value.upper() == "AS":
            self.consume(TokenType.IDENT)  # consume AS
            alias = self.consume(TokenType.IDENT).value
        return SelectItem(field, alias)

    def parse_agg_call(self) -> AggCall:
        func_token = self.consume(TokenType.IDENT)
        func = func_token.value.upper()
        self.consume(TokenType.LPAREN)

        # Check for COUNT(*)
        if self.peek().type == TokenType.STAR:
            self.consume(TokenType.STAR)
            self.consume(TokenType.RPAREN)
            return AggCall(func, None)

        field = self.parse_field_ref()
        self.consume(TokenType.RPAREN)
        return AggCall(func, field)

    def parse_field_ref(self) -> FieldRef:
        ident = self.consume(TokenType.IDENT).value
        parts = [ident]

        while self.peek().type == TokenType.DOT:
            self.consume(TokenType.DOT)
            parts.append(self.consume(TokenType.IDENT).value)

        return FieldRef(parts)

    def parse_group_by(self) -> list[FieldRef]:
        fields = [self.parse_field_ref()]
        while self.peek().type == TokenType.COMMA:
            self.consume(TokenType.COMMA)
            fields.append(self.parse_field_ref())
        return fields

    def parse_boolean_expr(self) -> ASTNode:
        return self.parse_or_expr()

    def parse_or_expr(self) -> ASTNode:
        left = self.parse_and_expr()
        while self.peek().type == TokenType.OR:
            self.consume(TokenType.OR)
            right = self.parse_and_expr()
            left = BinaryExpr("OR", left, right)
        return left

    def parse_and_expr(self) -> ASTNode:
        left = self.parse_not_expr()
        while self.peek().type == TokenType.AND:
            self.consume(TokenType.AND)
            right = self.parse_not_expr()
            left = BinaryExpr("AND", left, right)
        return left

    def parse_not_expr(self) -> ASTNode:
        if self.peek().type == TokenType.NOT:
            self.consume(TokenType.NOT)
            expr = self.parse_not_expr()
            return UnaryExpr("NOT", expr)
        return self.parse_comparison()

    def parse_comparison(self) -> ASTNode:
        left = self.parse_primary()
        token = self.peek()
        if token.type in (TokenType.EQ, TokenType.NE, TokenType.LT, TokenType.LTE, TokenType.GT, TokenType.GTE):
            op_type = self.consume(token.type).type
            op_map = {
                TokenType.EQ: "==",
                TokenType.NE: "!=",
                TokenType.LT: "<",
                TokenType.LTE: "<=",
                TokenType.GT: ">",
                TokenType.GTE: ">=",
            }
            op = op_map[op_type]
            right = self.parse_primary()
            return BinaryExpr(op, left, right)
        return left

    def parse_primary(self) -> ASTNode:
        token = self.peek()

        if token.type == TokenType.IDENT:
            ident = self.consume(TokenType.IDENT).value
            # Check for NULL, TRUE, FALSE
            upper = ident.upper()
            if upper == "NULL":
                return Literal(None)
            if upper == "TRUE":
                return Literal(True)
            if upper == "FALSE":
                return Literal(False)

            # Field reference (may have dots)
            parts = [ident]
            while self.peek().type == TokenType.DOT:
                self.consume(TokenType.DOT)
                parts.append(self.consume(TokenType.IDENT).value)
            return FieldRef(parts)

        if token.type == TokenType.STRING:
            self.consume(TokenType.STRING)
            # Re-read the value since consume advances
            val = self.tokens[self.pos - 1].value
            return Literal(val)

        if token.type == TokenType.NUMBER:
            self.consume(TokenType.NUMBER)
            val = self.tokens[self.pos - 1].value
            # Determine if it's integer or float
            if '.' in val:
                return Literal(float(val))
            return Literal(int(val))

        if token.type == TokenType.LPAREN:
            self.consume(TokenType.LPAREN)
            expr = self.parse_boolean_expr()
            self.consume(TokenType.RPAREN)
            return expr

        raise ParseError(f"Unexpected token: {token}")


# ==============================================================================
# Evaluation
# ==============================================================================

def deep_equal(a: Any, b: Any) -> bool:
    """Deep equality for JSON values that ignores object key order."""
    if type(a) != type(b):
        return False

    if a is None or b is None:
        return a is None and b is None

    if isinstance(a, (int, float, str, bool)):
        return a == b

    if isinstance(a, list):
        if len(a) != len(b):
            return False
        return all(deep_equal(x, y) for x, y in zip(a, b))

    if isinstance(a, dict):
        if len(a) != len(b):
            return False
        # Check if all keys match and values are equal
        if set(a.keys()) != set(b.keys()):
            return False
        return all(deep_equal(a[k], b[k]) for k in a.keys())

    return False


def get_nested_value(obj: dict, parts: list[str]) -> Any:
    """Get a nested value from a dict using dotted path parts."""
    current = obj
    for part in parts:
        if not isinstance(current, dict):
            return None
        current = current.get(part)
        if current is None and part not in current:  # handle explicit None
            # Check if key exists with None value
            if not any(k == part for k in current.keys()):
                return None
    return current


def eval_expr(expr: ASTNode, row: dict) -> Any:
    """Evaluate an expression against a data row."""
    if isinstance(expr, FieldRef):
        if expr.parts == ["*"]:
            return "*"  # Special marker
        return get_nested_value(row, expr.parts)

    if isinstance(expr, Literal):
        return expr.value

    if isinstance(expr, BinaryExpr):
        left = eval_expr(expr.left, row)
        right = eval_expr(expr.right, row)

        if expr.op == "==":
            if left is None or right is None:
                return False
            return left == right
        if expr.op == "!=":
            if left is None or right is None:
                return False
            return left != right
        if expr.op == "<":
            if left is None or right is None:
                return False
            if isinstance(left, (int, float)) and isinstance(right, (int, float)):
                return left < right
            if isinstance(left, str) and isinstance(right, str):
                return left < right
            return False
        if expr.op == "<=":
            if left is None or right is None:
                return False
            if isinstance(left, (int, float)) and isinstance(right, (int, float)):
                return left <= right
            if isinstance(left, str) and isinstance(right, str):
                return left <= right
            return False
        if expr.op == ">":
            if left is None or right is None:
                return False
            if isinstance(left, (int, float)) and isinstance(right, (int, float)):
                return left > right
            if isinstance(left, str) and isinstance(right, str):
                return left > right
            return False
        if expr.op == ">=":
            if left is None or right is None:
                return False
            if isinstance(left, (int, float)) and isinstance(right, (int, float)):
                return left >= right
            if isinstance(left, str) and isinstance(right, str):
                return left >= right
            return False

    if isinstance(expr, UnaryExpr):
        val = eval_expr(expr.expr, row)
        if expr.op == "NOT":
            return not val

    raise ValueError(f"Unknown expr type: {type(expr)}")


# ==============================================================================
# Error codes
# ==============================================================================

def error_syntax(msg: str):
    print(f"Syntax error: {msg}", file=sys.stderr)
    sys.exit(1)


def error_semantic(msg: str):
    print(f"Semantic error: {msg}", file=sys.stderr)
    sys.exit(1)


# ==============================================================================
# Main execution
# ==============================================================================

def load_logs(log_file: str) -> list[dict]:
    logs = []
    with open(log_file, 'r') as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                logs.append(json.loads(line))
            except json.JSONDecodeError as e:
                error_semantic(f"Invalid JSON on line {line_num}: {e}")
    return logs


def has_aggregates(select_items: list[SelectItem]) -> bool:
    for item in select_items:
        if isinstance(item.item, AggCall):
            return True
    return False


def validate_select_items(select_items: list[SelectItem], group_by: Optional[list[FieldRef]]) -> list[str]:
    """Validate SELECT items and return output keys."""
    output_keys = []
    seen_keys = set()

    for item in select_items:
        key = item.output_key()
        if key in seen_keys:
            error_semantic(f"Duplicate output key: {key}")
        seen_keys.add(key)
        output_keys.append(key)

    return output_keys


def validate_group_by(select_items: list[SelectItem], group_by: Optional[list[FieldRef]]):
    """Validate GROUP BY rules."""
    if group_by is None:
        return

    # Check for duplicate GROUP BY fields
    seen = set()
    for field in group_by:
        text = field.as_text()
        if text in seen:
            error_semantic(f"Duplicate GROUP BY field: {text}")
        seen.add(text)

    # Check that all non-aggregate SELECT items are in GROUP BY
    for item in select_items:
        if isinstance(item.item, AggCall):
            continue
        # It's a field reference
        field_text = item.item.as_text()
        if item.alias is not None:
            field_text = item.alias
        group_by_fields = {f.as_text() for f in group_by}
        if field_text not in group_by_fields:
            error_semantic(f"Field '{field_text}' in SELECT must appear in GROUP BY")


def execute_query(logs: list[dict], query: Query) -> list[dict]:
    """Execute the query and return results."""
    select_items = query.select_items
    where_expr = query.where_expr
    group_by = query.group_by

    has_agg = has_aggregates(select_items)

    if has_agg and any(isinstance(item.item, FieldRef) and item.item.parts == ["*"] for item in select_items):
        error_semantic("SELECT * is invalid when any aggregate appears")

    validate_select_items(select_items, group_by)
    validate_group_by(select_items, group_by)

    # Apply WHERE filtering
    filtered_logs = []
    for row in logs:
        if where_expr is None:
            filtered_logs.append(row)
        else:
            result = eval_expr(where_expr, row)
            if result:
                filtered_logs.append(row)

    if not filtered_logs:
        # Return empty result with appropriate structure
        return [{}]

    # Determine if global aggregation or grouped
    if group_by is None and has_agg:
        # Global aggregation
        return compute_global_aggregation(filtered_logs, select_items)

    if group_by is None and not has_agg:
        # No aggregation, no grouping - Part 1 behavior
        return compute_no_aggregation(filtered_logs, select_items)

    # Grouped aggregation or grouped field selection without aggregates
    return compute_grouped(filtered_logs, select_items, group_by)


def compute_global_aggregation(logs: list[dict], select_items: list[SelectItem]) -> list[dict]:
    """Compute global aggregates over all rows."""
    result = {}

    for item in select_items:
        key = item.output_key()
        agg = item.item

        if isinstance(agg, AggCall):
            result[key] = evaluate_agg(agg, logs)

    return [result]


def compute_no_aggregation(logs: list[dict], select_items: list[SelectItem]) -> list[dict]:
    """No aggregation, no GROUP BY - return filtered rows with selected fields."""
    results = []
    for row in logs:
        result = {}
        for item in select_items:
            key = item.output_key()
            field_ref = item.item
            val = get_nested_value(row, field_ref.parts)
            result[key] = val
        results.append(result)
    return results


def compute_grouped(logs: list[dict], select_items: list[SelectItem], group_by: list[FieldRef]) -> list[dict]:
    """Compute grouped results with first-encountered ordering."""
    groups = OrderedDict()  # group_key -> list of rows
    group_key_order = []    # preserve first-encountered order

    # Build groups
    for row in logs:
        # Create group key
        key_parts = []
        for field in group_by:
            val = get_nested_value(row, field.parts)
            # Coerce arrays and objects to None for grouping
            if isinstance(val, (list, dict)):
                val = None
            key_parts.append(val)
        group_key = tuple(key_parts)

        if group_key not in groups:
            groups[group_key] = []
            group_key_order.append(group_key)
        groups[group_key].append(row)

    # Compute results for each group
    results = []
    for group_key in group_key_order:
        group_logs = groups[group_key]
        result = {}

        for item in select_items:
            key = item.output_key()

            if isinstance(item.item, AggCall):
                result[key] = evaluate_agg(item.item, group_logs)
            else:
                # Field reference - get from group key
                field_text = item.item.as_text()
                if item.alias is not None:
                    field_text = item.alias

                # Find the index in group_by
                idx = None
                for i, gb_field in enumerate(group_by):
                    if gb_field.as_text() == field_text:
                        idx = i
                        break

                if idx is not None:
                    result[key] = group_key[idx]
                else:
                    # Should not happen due to validation
                    result[key] = None

        results.append(result)

    return results


def evaluate_agg(agg: AggCall, logs: list[dict]) -> Any:
    """Evaluate an aggregate function over a list of rows."""
    func = agg.func

    if func == "COUNT":
        if agg.arg is None:  # COUNT(*)
            return len(logs)
        # COUNT(field)
        count = 0
        for row in logs:
            val = get_nested_value(row, agg.arg.parts)
            # Arrays and objects count as present
            if val is not None:
                count += 1
        return count

    if func in ("SUM", "AVG"):
        total = 0.0
        count = 0
        for row in logs:
            val = get_nested_value(row, agg.arg.parts)
            if isinstance(val, (int, float)):
                total += val
                count += 1
        if count == 0:
            return None
        if func == "SUM":
            return total if total == int(total) else total
        return total / count

    if func == "MIN":
        # Try numbers first, then strings
        numbers = []
        strings = []
        for row in logs:
            val = get_nested_value(row, agg.arg.parts)
            if isinstance(val, (int, float)):
                numbers.append(val)
            elif isinstance(val, str):
                strings.append(val)

        if numbers:
            return min(numbers)
        if strings:
            return min(strings)
        return None

    if func == "MAX":
        numbers = []
        strings = []
        for row in logs:
            val = get_nested_value(row, agg.arg.parts)
            if isinstance(val, (int, float)):
                numbers.append(val)
            elif isinstance(val, str):
                strings.append(val)

        if numbers:
            return max(numbers)
        if strings:
            return max(strings)
        return None

    if func == "UNIQUE":
        seen = []
        for row in logs:
            val = get_nested_value(row, agg.arg.parts)
            # For grouping purposes, coerce arrays and objects to their canonical form
            # but keep them as values in the output
            found = False
            for existing in seen:
                if deep_equal(existing, val):
                    found = True
                    break
            if not found:
                seen.append(val)
        return seen

    raise ValueError(f"Unknown aggregate function: {func}")


def main():
    parser = argparse.ArgumentParser(
        description="LogQL - Part 2: GROUP BY and Aggregations"
    )
    parser.add_argument("--log-file", required=True, help="Path to NDJSON log file")
    parser.add_argument("--query", required=True, help="SQL-like query string")
    parser.add_argument("--output", help="Output file path (default: stdout)")

    args = parser.parse_args()

    try:
        query_str = args.query.strip()

        # Tokenize and parse
        tokenizer = Tokenizer(query_str)
        tokens = tokenizer.tokenize()

        parser = Parser(tokens)
        query = parser.parse_query()

        # Load and execute
        logs = load_logs(args.log_file)
        results = execute_query(logs, query)

        # Output
        output_json = json.dumps(results)
        if args.output:
            with open(args.output, 'w') as f:
                f.write(output_json)
        else:
            print(output_json)

    except ParseError as e:
        error_syntax(str(e))
    except Exception as e:
        error_semantic(str(e))


if __name__ == "__main__":
    main()
