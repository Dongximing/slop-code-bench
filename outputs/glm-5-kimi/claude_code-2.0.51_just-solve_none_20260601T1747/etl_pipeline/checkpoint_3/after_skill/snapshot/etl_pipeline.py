#!/usr/bin/env python3
"""ETL Pipeline Parser - reads JSON from STDIN, validates and normalizes it."""

import sys
import json
import re
import argparse


def output_error(error_code, message, path):
    """Output an error response and exit with code 1."""
    output = {
        "status": "error",
        "error_code": error_code,
        "message": f"ETL_ERROR: {message}",
        "path": path
    }
    print(json.dumps(output))
    sys.exit(1)


# =============================================================================
# Expression Lexer and Parser
# =============================================================================

class TokenType:
    NUMBER = 'NUMBER'
    STRING = 'STRING'
    TRUE = 'TRUE'
    FALSE = 'FALSE'
    NULL = 'NULL'
    IDENTIFIER = 'IDENTIFIER'
    PLUS = 'PLUS'
    MINUS = 'MINUS'
    STAR = 'STAR'
    SLASH = 'SLASH'
    EQ = 'EQ'
    NEQ = 'NEQ'
    LT = 'LT'
    LTE = 'LTE'
    GT = 'GT'
    GTE = 'GTE'
    AND = 'AND'
    OR = 'OR'
    NOT = 'NOT'
    LPAREN = 'LPAREN'
    RPAREN = 'RPAREN'
    EOF = 'EOF'


class Token:
    def __init__(self, type_, value, pos):
        self.type = type_
        self.value = value
        self.pos = pos

    def __repr__(self):
        return f'Token({self.type}, {self.value}, {self.pos})'


class Lexer:
    def __init__(self, text, path):
        self.text = text
        self.pos = 0
        self.path = path
        self.current_char = self.text[0] if text else None

    def error(self, msg):
        output_error("BAD_EXPR", msg, self.path)

    def advance(self):
        self.pos += 1
        if self.pos >= len(self.text):
            self.current_char = None
        else:
            self.current_char = self.text[self.pos]

    def peek(self, offset=1):
        peek_pos = self.pos + offset
        if peek_pos >= len(self.text):
            return None
        return self.text[peek_pos]

    def skip_whitespace(self):
        while self.current_char is not None and self.current_char.isspace():
            self.advance()

    def number(self):
        result = ''
        start_pos = self.pos

        while self.current_char is not None and (self.current_char.isdigit() or self.current_char == '.'):
            result += self.current_char
            self.advance()

        try:
            if '.' in result:
                return Token(TokenType.NUMBER, float(result), start_pos)
            else:
                return Token(TokenType.NUMBER, int(result), start_pos)
        except ValueError:
            self.error(f"invalid number '{result}'")

    def string(self):
        result = ''
        start_pos = self.pos
        quote_char = self.current_char
        self.advance()  # skip opening quote

        while self.current_char is not None and self.current_char != quote_char:
            if self.current_char == '\\':
                self.advance()
                if self.current_char is None:
                    self.error("unterminated string")
                escape_chars = {'n': '\n', 't': '\t', 'r': '\r', '\\': '\\', '"': '"', "'": "'"}
                result += escape_chars.get(self.current_char, self.current_char)
                self.advance()
            else:
                result += self.current_char
                self.advance()

        if self.current_char is None:
            self.error("unterminated string")

        self.advance()  # skip closing quote
        return Token(TokenType.STRING, result, start_pos)

    def identifier(self):
        result = ''
        start_pos = self.pos

        while self.current_char is not None and (self.current_char.isalnum() or self.current_char == '_'):
            result += self.current_char
            self.advance()

        keywords = {
            'true': Token(TokenType.TRUE, True, start_pos),
            'false': Token(TokenType.FALSE, False, start_pos),
            'null': Token(TokenType.NULL, None, start_pos),
        }

        if result.lower() in keywords:
            return keywords[result.lower()]

        return Token(TokenType.IDENTIFIER, result, start_pos)

    def get_next_token(self):
        while self.current_char is not None:
            if self.current_char.isspace():
                self.skip_whitespace()
                continue

            if self.current_char.isdigit():
                return self.number()

            if self.current_char == '"' or self.current_char == "'":
                return self.string()

            if self.current_char.isalpha() or self.current_char == '_':
                return self.identifier()

            # Two-character operators
            if self.current_char == '=' and self.peek() == '=':
                pos = self.pos
                self.advance()
                self.advance()
                return Token(TokenType.EQ, '==', pos)

            if self.current_char == '!' and self.peek() == '=':
                pos = self.pos
                self.advance()
                self.advance()
                return Token(TokenType.NEQ, '!=', pos)

            if self.current_char == '<' and self.peek() == '=':
                pos = self.pos
                self.advance()
                self.advance()
                return Token(TokenType.LTE, '<=', pos)

            if self.current_char == '>' and self.peek() == '=':
                pos = self.pos
                self.advance()
                self.advance()
                return Token(TokenType.GTE, '>=', pos)

            if self.current_char == '&' and self.peek() == '&':
                pos = self.pos
                self.advance()
                self.advance()
                return Token(TokenType.AND, '&&', pos)

            if self.current_char == '|' and self.peek() == '|':
                pos = self.pos
                self.advance()
                self.advance()
                return Token(TokenType.OR, '||', pos)

            # Check for unsupported operators
            if self.current_char == '*' and self.peek() == '*':
                self.error("unsupported operator '**'")

            if self.current_char == '^':
                self.error("unsupported operator '^'")

            # Single-character operators
            if self.current_char == '+':
                token = Token(TokenType.PLUS, '+', self.pos)
                self.advance()
                return token

            if self.current_char == '-':
                token = Token(TokenType.MINUS, '-', self.pos)
                self.advance()
                return token

            if self.current_char == '*':
                token = Token(TokenType.STAR, '*', self.pos)
                self.advance()
                return token

            if self.current_char == '/':
                token = Token(TokenType.SLASH, '/', self.pos)
                self.advance()
                return token

            if self.current_char == '<':
                token = Token(TokenType.LT, '<', self.pos)
                self.advance()
                return token

            if self.current_char == '>':
                token = Token(TokenType.GT, '>', self.pos)
                self.advance()
                return token

            if self.current_char == '!':
                token = Token(TokenType.NOT, '!', self.pos)
                self.advance()
                return token

            if self.current_char == '(':
                token = Token(TokenType.LPAREN, '(', self.pos)
                self.advance()
                return token

            if self.current_char == ')':
                token = Token(TokenType.RPAREN, ')', self.pos)
                self.advance()
                return token

            self.error(f"unexpected character '{self.current_char}'")

        return Token(TokenType.EOF, None, self.pos)


class ASTNode:
    pass


class NumberNode(ASTNode):
    def __init__(self, value):
        self.value = value


class StringNode(ASTNode):
    def __init__(self, value):
        self.value = value


class BooleanNode(ASTNode):
    def __init__(self, value):
        self.value = value


class NullNode(ASTNode):
    pass


class IdentifierNode(ASTNode):
    def __init__(self, name):
        self.name = name


class BinaryOpNode(ASTNode):
    def __init__(self, left, op, right):
        self.left = left
        self.op = op
        self.right = right


class UnaryOpNode(ASTNode):
    def __init__(self, op, operand):
        self.op = op
        self.operand = operand


class Parser:
    """
    Recursive descent parser for expressions.

    Precedence (lowest to highest):
    1. || (or)
    2. && (and)
    3. == != (equality)
    4. < <= > >= (comparison)
    5. + - (addition)
    6. * / (multiplication)
    7. ! (unary not)
    8. () (parentheses)
    """

    def __init__(self, lexer):
        self.lexer = lexer
        self.current_token = self.lexer.get_next_token()

    def error(self, msg):
        output_error("BAD_EXPR", msg, self.lexer.path)

    def eat(self, token_type):
        if self.current_token.type == token_type:
            self.current_token = self.lexer.get_next_token()
        else:
            self.error(f"expected {token_type}, got {self.current_token.type}")

    def parse(self):
        node = self.or_expr()
        if self.current_token.type != TokenType.EOF:
            self.error(f"unexpected token after expression")
        return node

    def or_expr(self):
        node = self.and_expr()

        while self.current_token.type == TokenType.OR:
            token = self.current_token
            self.eat(TokenType.OR)
            node = BinaryOpNode(node, '||', self.and_expr())

        return node

    def and_expr(self):
        node = self.equality_expr()

        while self.current_token.type == TokenType.AND:
            token = self.current_token
            self.eat(TokenType.AND)
            node = BinaryOpNode(node, '&&', self.equality_expr())

        return node

    def equality_expr(self):
        node = self.comparison_expr()

        while self.current_token.type in (TokenType.EQ, TokenType.NEQ):
            if self.current_token.type == TokenType.EQ:
                self.eat(TokenType.EQ)
                node = BinaryOpNode(node, '==', self.comparison_expr())
            else:
                self.eat(TokenType.NEQ)
                node = BinaryOpNode(node, '!=', self.comparison_expr())

        return node

    def comparison_expr(self):
        node = self.additive_expr()

        while self.current_token.type in (TokenType.LT, TokenType.LTE, TokenType.GT, TokenType.GTE):
            if self.current_token.type == TokenType.LT:
                self.eat(TokenType.LT)
                node = BinaryOpNode(node, '<', self.additive_expr())
            elif self.current_token.type == TokenType.LTE:
                self.eat(TokenType.LTE)
                node = BinaryOpNode(node, '<=', self.additive_expr())
            elif self.current_token.type == TokenType.GT:
                self.eat(TokenType.GT)
                node = BinaryOpNode(node, '>', self.additive_expr())
            else:
                self.eat(TokenType.GTE)
                node = BinaryOpNode(node, '>=', self.additive_expr())

        return node

    def additive_expr(self):
        node = self.multiplicative_expr()

        while self.current_token.type in (TokenType.PLUS, TokenType.MINUS):
            if self.current_token.type == TokenType.PLUS:
                self.eat(TokenType.PLUS)
                node = BinaryOpNode(node, '+', self.multiplicative_expr())
            else:
                self.eat(TokenType.MINUS)
                node = BinaryOpNode(node, '-', self.multiplicative_expr())

        return node

    def multiplicative_expr(self):
        node = self.unary_expr()

        while self.current_token.type in (TokenType.STAR, TokenType.SLASH):
            if self.current_token.type == TokenType.STAR:
                self.eat(TokenType.STAR)
                node = BinaryOpNode(node, '*', self.unary_expr())
            else:
                self.eat(TokenType.SLASH)
                node = BinaryOpNode(node, '/', self.unary_expr())

        return node

    def unary_expr(self):
        if self.current_token.type == TokenType.NOT:
            self.eat(TokenType.NOT)
            return UnaryOpNode('!', self.unary_expr())
        elif self.current_token.type == TokenType.MINUS:
            self.eat(TokenType.MINUS)
            return UnaryOpNode('-', self.unary_expr())
        else:
            return self.primary()

    def primary(self):
        token = self.current_token

        if token.type == TokenType.NUMBER:
            self.eat(TokenType.NUMBER)
            return NumberNode(token.value)

        if token.type == TokenType.STRING:
            self.eat(TokenType.STRING)
            return StringNode(token.value)

        if token.type == TokenType.TRUE:
            self.eat(TokenType.TRUE)
            return BooleanNode(True)

        if token.type == TokenType.FALSE:
            self.eat(TokenType.FALSE)
            return BooleanNode(False)

        if token.type == TokenType.NULL:
            self.eat(TokenType.NULL)
            return NullNode()

        if token.type == TokenType.IDENTIFIER:
            self.eat(TokenType.IDENTIFIER)
            return IdentifierNode(token.value)

        if token.type == TokenType.LPAREN:
            self.eat(TokenType.LPAREN)
            node = self.or_expr()
            self.eat(TokenType.RPAREN)
            return node

        self.error(f"unexpected token {token.type}")


def parse_expression(expr, path):
    """Parse an expression string and return an AST."""
    lexer = Lexer(expr, path)
    parser = Parser(lexer)
    return parser.parse()


def evaluate_ast(node, row):
    """Evaluate an AST node against a row."""
    if isinstance(node, NumberNode):
        return node.value

    if isinstance(node, StringNode):
        return node.value

    if isinstance(node, BooleanNode):
        return node.value

    if isinstance(node, NullNode):
        return None

    if isinstance(node, IdentifierNode):
        return row.get(node.name)

    if isinstance(node, UnaryOpNode):
        operand = evaluate_ast(node.operand, row)
        if node.op == '!':
            if operand is None:
                return True
            return not operand
        if node.op == '-':
            if operand is None:
                return None
            return -operand

    if isinstance(node, BinaryOpNode):
        left = evaluate_ast(node.left, row)
        right = evaluate_ast(node.right, row)

        # Arithmetic operators
        if node.op in ('+', '-', '*', '/'):
            if left is None or right is None:
                return None
            try:
                if node.op == '+':
                    return left + right
                if node.op == '-':
                    return left - right
                if node.op == '*':
                    return left * right
                if node.op == '/':
                    if right == 0:
                        return None
                    return left / right
            except (TypeError, ValueError):
                return None

        # Comparison operators
        if node.op in ('<', '<=', '>', '>='):
            if left is None or right is None:
                return False
            try:
                if node.op == '<':
                    return left < right
                if node.op == '<=':
                    return left <= right
                if node.op == '>':
                    return left > right
                if node.op == '>=':
                    return left >= right
            except TypeError:
                return False

        # Equality operators
        if node.op == '==':
            if left is None and right is None:
                return True
            if left is None or right is None:
                return False
            try:
                return left == right
            except TypeError:
                return False

        if node.op == '!=':
            if left is None and right is None:
                return False
            if left is None or right is None:
                return True
            try:
                return left != right
            except TypeError:
                return True

        # Boolean operators
        if node.op == '&&':
            left_bool = left is not None and left
            if not left_bool:
                return False
            return right is not None and right

        if node.op == '||':
            left_bool = left is not None and left
            if left_bool:
                return True
            return right is not None and right

    return None


# =============================================================================
# Validation Functions
# =============================================================================

def validate_expression(expr, path):
    """Validate an expression for basic syntax errors."""
    count = 0
    for char in expr:
        if char == '(':
            count += 1
        elif char == ')':
            count -= 1
            if count < 0:
                output_error("BAD_EXPR", "unbalanced parentheses", path)
    if count != 0:
        output_error("BAD_EXPR", "unbalanced parentheses", path)

    if '^' in expr:
        output_error("BAD_EXPR", "unsupported operator '^'", path)

    if '**' in expr:
        output_error("BAD_EXPR", "unsupported operator '**'", path)

    temp = expr
    for op in ['>=', '<=', '==', '!=']:
        temp = temp.replace(op, '  ')

    for word in ['and', 'or', 'not']:
        temp = re.sub(r'\b' + word + r'\b', ' ' * len(word), temp, flags=re.IGNORECASE)

    if re.search(r'[+*/><=!]{2,}', temp):
        output_error("BAD_EXPR", "consecutive operators", path)


def validate_select(step, index):
    """Validate and normalize a select step."""
    if "columns" not in step:
        output_error("MISSING_COLUMN", "missing 'columns' in select", f"pipeline.steps[{index}]")

    columns = step.get("columns")
    if not isinstance(columns, list):
        output_error("SCHEMA_VALIDATION_FAILED", "'columns' must be an array", f"pipeline.steps[{index}].columns")

    for j, col in enumerate(columns):
        if not isinstance(col, str):
            output_error("SCHEMA_VALIDATION_FAILED", "column must be a string", f"pipeline.steps[{index}].columns[{j}]")

    return {"op": "select", "columns": columns}


def validate_filter(step, index):
    """Validate and normalize a filter step."""
    if "where" not in step:
        output_error("MISSING_COLUMN", "missing 'where' in filter", f"pipeline.steps[{index}]")

    where = step.get("where")
    if not isinstance(where, str):
        output_error("SCHEMA_VALIDATION_FAILED", "'where' must be a string", f"pipeline.steps[{index}].where")

    where_normalized = where.strip()
    if not where_normalized:
        output_error("SCHEMA_VALIDATION_FAILED", "'where' cannot be empty", f"pipeline.steps[{index}].where")

    validate_expression(where_normalized, f"pipeline.steps[{index}].where")

    return {"op": "filter", "where": where_normalized}


def validate_map(step, index):
    """Validate and normalize a map step."""
    if "as" not in step:
        output_error("MISSING_COLUMN", "missing 'as' in map", f"pipeline.steps[{index}]")

    if "expr" not in step:
        output_error("MISSING_COLUMN", "missing 'expr' in map", f"pipeline.steps[{index}]")

    as_val = step.get("as")
    if not isinstance(as_val, str):
        output_error("SCHEMA_VALIDATION_FAILED", "'as' must be a string", f"pipeline.steps[{index}].as")

    expr = step.get("expr")
    if not isinstance(expr, str):
        output_error("SCHEMA_VALIDATION_FAILED", "'expr' must be a string", f"pipeline.steps[{index}].expr")

    as_normalized = as_val.strip()
    if not as_normalized:
        output_error("SCHEMA_VALIDATION_FAILED", "'as' cannot be empty", f"pipeline.steps[{index}].as")

    expr_normalized = expr.strip()
    if not expr_normalized:
        output_error("SCHEMA_VALIDATION_FAILED", "'expr' cannot be empty", f"pipeline.steps[{index}].expr")

    validate_expression(expr_normalized, f"pipeline.steps[{index}].expr")

    return {"op": "map", "as": as_normalized, "expr": expr_normalized}


def validate_rename(step, index):
    """Validate and normalize a rename step."""
    has_from = "from" in step
    has_to = "to" in step
    has_mapping = "mapping" in step

    if has_mapping:
        mapping = step.get("mapping")
        if not isinstance(mapping, dict):
            output_error("SCHEMA_VALIDATION_FAILED", "'mapping' must be an object", f"pipeline.steps[{index}].mapping")

        for k, v in mapping.items():
            if not isinstance(k, str):
                output_error("SCHEMA_VALIDATION_FAILED", "mapping keys must be strings", f"pipeline.steps[{index}].mapping")
            if not isinstance(v, str):
                output_error("SCHEMA_VALIDATION_FAILED", "mapping values must be strings", f"pipeline.steps[{index}].mapping")

        return {"op": "rename", "mapping": mapping}

    elif has_from and has_to:
        from_val = step.get("from")
        to_val = step.get("to")

        if not isinstance(from_val, str):
            output_error("SCHEMA_VALIDATION_FAILED", "'from' must be a string", f"pipeline.steps[{index}].from")

        if not isinstance(to_val, str):
            output_error("SCHEMA_VALIDATION_FAILED", "'to' must be a string", f"pipeline.steps[{index}].to")

        from_normalized = from_val.strip()
        to_normalized = to_val.strip()

        if not from_normalized:
            output_error("SCHEMA_VALIDATION_FAILED", "'from' cannot be empty", f"pipeline.steps[{index}].from")

        if not to_normalized:
            output_error("SCHEMA_VALIDATION_FAILED", "'to' cannot be empty", f"pipeline.steps[{index}].to")

        mapping = {from_normalized: to_normalized}
        return {"op": "rename", "mapping": mapping}

    else:
        output_error("MISSING_COLUMN", "rename requires 'from'/'to' or 'mapping'", f"pipeline.steps[{index}]")


def validate_limit(step, index):
    """Validate and normalize a limit step."""
    if "n" not in step:
        output_error("MISSING_COLUMN", "missing 'n' in limit", f"pipeline.steps[{index}]")

    n = step.get("n")
    if not isinstance(n, int) or isinstance(n, bool):
        output_error("SCHEMA_VALIDATION_FAILED", "'n' must be an integer", f"pipeline.steps[{index}].n")

    if n < 0:
        output_error("SCHEMA_VALIDATION_FAILED", "'n' must be >= 0", f"pipeline.steps[{index}].n")

    return {"op": "limit", "n": n}


def validate_branch_step(step, path_prefix):
    """Validate and normalize a single step within a branch. Returns normalized step."""
    if not isinstance(step, dict):
        output_error("SCHEMA_VALIDATION_FAILED", "step must be an object", path_prefix)

    if "op" not in step:
        output_error("SCHEMA_VALIDATION_FAILED", "missing 'op' in step", path_prefix)

    op = step.get("op")
    if not isinstance(op, str):
        output_error("SCHEMA_VALIDATION_FAILED", "'op' must be a string", f"{path_prefix}.op")

    op_normalized = op.strip().lower()

    if not op_normalized:
        output_error("SCHEMA_VALIDATION_FAILED", "'op' cannot be empty", f"{path_prefix}.op")

    validators = {
        "select": validate_select_step,
        "filter": validate_filter_step,
        "map": validate_map_step,
        "rename": validate_rename_step,
        "limit": validate_limit_step,
        "branch": validate_branch_step_inner
    }

    if op_normalized not in validators:
        output_error("UNKNOWN_OP", f"unsupported op '{op_normalized}'", f"{path_prefix}.op")

    return validators[op_normalized](step, path_prefix)


def validate_select_step(step, path_prefix):
    """Validate and normalize a select step within a branch."""
    if "columns" not in step:
        output_error("MISSING_COLUMN", "missing 'columns' in select", path_prefix)

    columns = step.get("columns")
    if not isinstance(columns, list):
        output_error("SCHEMA_VALIDATION_FAILED", "'columns' must be an array", f"{path_prefix}.columns")

    for j, col in enumerate(columns):
        if not isinstance(col, str):
            output_error("SCHEMA_VALIDATION_FAILED", "column must be a string", f"{path_prefix}.columns[{j}]")

    return {"op": "select", "columns": columns}


def validate_filter_step(step, path_prefix):
    """Validate and normalize a filter step within a branch."""
    if "where" not in step:
        output_error("MISSING_COLUMN", "missing 'where' in filter", path_prefix)

    where = step.get("where")
    if not isinstance(where, str):
        output_error("SCHEMA_VALIDATION_FAILED", "'where' must be a string", f"{path_prefix}.where")

    where_normalized = where.strip()
    if not where_normalized:
        output_error("SCHEMA_VALIDATION_FAILED", "'where' cannot be empty", f"{path_prefix}.where")

    validate_expression(where_normalized, f"{path_prefix}.where")

    return {"op": "filter", "where": where_normalized}


def validate_map_step(step, path_prefix):
    """Validate and normalize a map step within a branch."""
    if "as" not in step:
        output_error("MISSING_COLUMN", "missing 'as' in map", path_prefix)

    if "expr" not in step:
        output_error("MISSING_COLUMN", "missing 'expr' in map", path_prefix)

    as_val = step.get("as")
    if not isinstance(as_val, str):
        output_error("SCHEMA_VALIDATION_FAILED", "'as' must be a string", f"{path_prefix}.as")

    expr = step.get("expr")
    if not isinstance(expr, str):
        output_error("SCHEMA_VALIDATION_FAILED", "'expr' must be a string", f"{path_prefix}.expr")

    as_normalized = as_val.strip()
    if not as_normalized:
        output_error("SCHEMA_VALIDATION_FAILED", "'as' cannot be empty", f"{path_prefix}.as")

    expr_normalized = expr.strip()
    if not expr_normalized:
        output_error("SCHEMA_VALIDATION_FAILED", "'expr' cannot be empty", f"{path_prefix}.expr")

    validate_expression(expr_normalized, f"{path_prefix}.expr")

    return {"op": "map", "as": as_normalized, "expr": expr_normalized}


def validate_rename_step(step, path_prefix):
    """Validate and normalize a rename step within a branch."""
    has_from = "from" in step
    has_to = "to" in step
    has_mapping = "mapping" in step

    if has_mapping:
        mapping = step.get("mapping")
        if not isinstance(mapping, dict):
            output_error("SCHEMA_VALIDATION_FAILED", "'mapping' must be an object", f"{path_prefix}.mapping")

        for k, v in mapping.items():
            if not isinstance(k, str):
                output_error("SCHEMA_VALIDATION_FAILED", "mapping keys must be strings", f"{path_prefix}.mapping")
            if not isinstance(v, str):
                output_error("SCHEMA_VALIDATION_FAILED", "mapping values must be strings", f"{path_prefix}.mapping")

        return {"op": "rename", "mapping": mapping}

    elif has_from and has_to:
        from_val = step.get("from")
        to_val = step.get("to")

        if not isinstance(from_val, str):
            output_error("SCHEMA_VALIDATION_FAILED", "'from' must be a string", f"{path_prefix}.from")

        if not isinstance(to_val, str):
            output_error("SCHEMA_VALIDATION_FAILED", "'to' must be a string", f"{path_prefix}.to")

        from_normalized = from_val.strip()
        to_normalized = to_val.strip()

        if not from_normalized:
            output_error("SCHEMA_VALIDATION_FAILED", "'from' cannot be empty", f"{path_prefix}.from")

        if not to_normalized:
            output_error("SCHEMA_VALIDATION_FAILED", "'to' cannot be empty", f"{path_prefix}.to")

        mapping = {from_normalized: to_normalized}
        return {"op": "rename", "mapping": mapping}

    else:
        output_error("MISSING_COLUMN", "rename requires 'from'/'to' or 'mapping'", path_prefix)


def validate_limit_step(step, path_prefix):
    """Validate and normalize a limit step within a branch."""
    if "n" not in step:
        output_error("MISSING_COLUMN", "missing 'n' in limit", path_prefix)

    n = step.get("n")
    if not isinstance(n, int) or isinstance(n, bool):
        output_error("SCHEMA_VALIDATION_FAILED", "'n' must be an integer", f"{path_prefix}.n")

    if n < 0:
        output_error("SCHEMA_VALIDATION_FAILED", "'n' must be >= 0", f"{path_prefix}.n")

    return {"op": "limit", "n": n}


def validate_branch_step_inner(step, path_prefix):
    """Validate and normalize a nested branch step."""
    if "branches" not in step:
        output_error("MISSING_COLUMN", "missing 'branches' in branch", path_prefix)

    branches = step.get("branches")
    if not isinstance(branches, list):
        output_error("SCHEMA_VALIDATION_FAILED", "'branches' must be an array", f"{path_prefix}.branches")

    if len(branches) == 0:
        output_error("MALFORMED_STEP", "'branches' must be non-empty", f"{path_prefix}.branches")

    otherwise_index = None
    normalized_branches = []

    for j, branch in enumerate(branches):
        branch_path = f"{path_prefix}.branches[{j}]"

        if not isinstance(branch, dict):
            output_error("SCHEMA_VALIDATION_FAILED", "branch must be an object", branch_path)

        branch_id = None
        if "id" in branch:
            id_val = branch.get("id")
            if not isinstance(id_val, str):
                output_error("SCHEMA_VALIDATION_FAILED", "'id' must be a string", f"{branch_path}.id")
            branch_id = id_val

        if "when" not in branch:
            output_error("MISSING_COLUMN", "missing 'when' in branch", branch_path)

        when = branch.get("when")
        if not isinstance(when, str):
            output_error("SCHEMA_VALIDATION_FAILED", "'when' must be a string", f"{branch_path}.when")

        when_normalized = when.strip()
        if not when_normalized:
            output_error("SCHEMA_VALIDATION_FAILED", "'when' cannot be empty", f"{branch_path}.when")

        is_otherwise = (when_normalized == "otherwise")

        if is_otherwise:
            if otherwise_index is not None:
                output_error("MALFORMED_STEP", "at most one 'otherwise' branch allowed", f"{path_prefix}.branches")
            otherwise_index = j

        if not is_otherwise:
            validate_expression(when_normalized, f"{branch_path}.when")

        if "steps" not in branch:
            output_error("MISSING_COLUMN", "missing 'steps' in branch", branch_path)

        steps = branch.get("steps")
        if not isinstance(steps, list):
            output_error("SCHEMA_VALIDATION_FAILED", "'steps' must be an array", f"{branch_path}.steps")

        normalized_steps = []
        for k, nested_step in enumerate(steps):
            normalized_step = validate_branch_step(nested_step, f"{branch_path}.steps[{k}]")
            normalized_steps.append(normalized_step)

        normalized_branch = {
            "when": when_normalized,
            "steps": normalized_steps
        }
        if branch_id is not None:
            normalized_branch["id"] = branch_id

        normalized_branches.append(normalized_branch)

    if otherwise_index is not None and otherwise_index != len(branches) - 1:
        output_error("MALFORMED_STEP", "'otherwise' branch must be last", f"{path_prefix}.branches")

    merge_strategy = "concat"
    if "merge" in step:
        merge = step.get("merge")
        if not isinstance(merge, dict):
            output_error("SCHEMA_VALIDATION_FAILED", "'merge' must be an object", f"{path_prefix}.merge")

        if "strategy" in merge:
            strategy = merge.get("strategy")
            if not isinstance(strategy, str):
                output_error("SCHEMA_VALIDATION_FAILED", "'strategy' must be a string", f"{path_prefix}.merge.strategy")

            if strategy != "concat":
                output_error("MALFORMED_STEP", "'strategy' must be 'concat'", f"{path_prefix}.merge.strategy")
            merge_strategy = strategy

    return {
        "op": "branch",
        "branches": normalized_branches,
        "merge": {"strategy": merge_strategy}
    }


def validate_branch(step, index):
    """Validate and normalize a branch step."""
    path_prefix = f"pipeline.steps[{index}]"

    if "branches" not in step:
        output_error("MISSING_COLUMN", "missing 'branches' in branch", path_prefix)

    branches = step.get("branches")
    if not isinstance(branches, list):
        output_error("SCHEMA_VALIDATION_FAILED", "'branches' must be an array", f"{path_prefix}.branches")

    if len(branches) == 0:
        output_error("MALFORMED_STEP", "'branches' must be non-empty", f"{path_prefix}.branches")

    otherwise_index = None
    normalized_branches = []

    for j, branch in enumerate(branches):
        branch_path = f"{path_prefix}.branches[{j}]"

        if not isinstance(branch, dict):
            output_error("SCHEMA_VALIDATION_FAILED", "branch must be an object", branch_path)

        branch_id = None
        if "id" in branch:
            id_val = branch.get("id")
            if not isinstance(id_val, str):
                output_error("SCHEMA_VALIDATION_FAILED", "'id' must be a string", f"{branch_path}.id")
            branch_id = id_val

        if "when" not in branch:
            output_error("MISSING_COLUMN", "missing 'when' in branch", branch_path)

        when = branch.get("when")
        if not isinstance(when, str):
            output_error("SCHEMA_VALIDATION_FAILED", "'when' must be a string", f"{branch_path}.when")

        when_normalized = when.strip()
        if not when_normalized:
            output_error("SCHEMA_VALIDATION_FAILED", "'when' cannot be empty", f"{branch_path}.when")

        is_otherwise = (when_normalized == "otherwise")

        if is_otherwise:
            if otherwise_index is not None:
                output_error("MALFORMED_STEP", "at most one 'otherwise' branch allowed", f"{path_prefix}.branches")
            otherwise_index = j

        if not is_otherwise:
            validate_expression(when_normalized, f"{branch_path}.when")

        if "steps" not in branch:
            output_error("MISSING_COLUMN", "missing 'steps' in branch", branch_path)

        steps = branch.get("steps")
        if not isinstance(steps, list):
            output_error("SCHEMA_VALIDATION_FAILED", "'steps' must be an array", f"{branch_path}.steps")

        normalized_steps = []
        for k, nested_step in enumerate(steps):
            normalized_step = validate_branch_step(nested_step, f"{branch_path}.steps[{k}]")
            normalized_steps.append(normalized_step)

        normalized_branch = {
            "when": when_normalized,
            "steps": normalized_steps
        }
        if branch_id is not None:
            normalized_branch["id"] = branch_id

        normalized_branches.append(normalized_branch)

    if otherwise_index is not None and otherwise_index != len(branches) - 1:
        output_error("MALFORMED_STEP", "'otherwise' branch must be last", f"{path_prefix}.branches")

    merge_strategy = "concat"
    if "merge" in step:
        merge = step.get("merge")
        if not isinstance(merge, dict):
            output_error("SCHEMA_VALIDATION_FAILED", "'merge' must be an object", f"{path_prefix}.merge")

        if "strategy" in merge:
            strategy = merge.get("strategy")
            if not isinstance(strategy, str):
                output_error("SCHEMA_VALIDATION_FAILED", "'strategy' must be a string", f"{path_prefix}.merge.strategy")

            if strategy != "concat":
                output_error("MALFORMED_STEP", "'strategy' must be 'concat'", f"{path_prefix}.merge.strategy")
            merge_strategy = strategy

    return {
        "op": "branch",
        "branches": normalized_branches,
        "merge": {"strategy": merge_strategy}
    }


def validate_and_normalize_step(step, index):
    """Validate and normalize a single step."""
    if not isinstance(step, dict):
        output_error("SCHEMA_VALIDATION_FAILED", "step must be an object", f"pipeline.steps[{index}]")

    if "op" not in step:
        output_error("SCHEMA_VALIDATION_FAILED", "missing 'op' in step", f"pipeline.steps[{index}]")

    op = step.get("op")
    if not isinstance(op, str):
        output_error("SCHEMA_VALIDATION_FAILED", "'op' must be a string", f"pipeline.steps[{index}].op")

    op_normalized = op.strip().lower()

    if not op_normalized:
        output_error("SCHEMA_VALIDATION_FAILED", "'op' cannot be empty", f"pipeline.steps[{index}].op")

    validators = {
        "select": validate_select,
        "filter": validate_filter,
        "map": validate_map,
        "rename": validate_rename,
        "limit": validate_limit,
        "branch": validate_branch
    }

    if op_normalized not in validators:
        output_error("UNKNOWN_OP", f"unsupported op '{op_normalized}'", f"pipeline.steps[{index}].op")

    return validators[op_normalized](step, index)


# =============================================================================
# Execution Functions
# =============================================================================

def execute_select(step, data, step_index):
    """Execute a select step."""
    columns = step["columns"]
    result = []

    for row_idx, row in enumerate(data):
        new_row = {}
        for col_idx, col in enumerate(columns):
            if col not in row:
                output_error(
                    "MISSING_COLUMN",
                    f"column '{col}' not found in row",
                    f"pipeline.steps[{step_index}].columns[{col_idx}]"
                )
            new_row[col] = row[col]
        result.append(new_row)

    return result


def execute_map(step, data, step_index):
    """Execute a map step."""
    expr_str = step["expr"]
    as_field = step["as"]
    path = f"pipeline.steps[{step_index}].expr"

    ast = parse_expression(expr_str, path)

    result = []
    for row in data:
        value = evaluate_ast(ast, row)
        new_row = dict(row)
        new_row[as_field] = value
        result.append(new_row)

    return result


def execute_filter(step, data, step_index):
    """Execute a filter step."""
    where_str = step["where"]
    path = f"pipeline.steps[{step_index}].where"

    ast = parse_expression(where_str, path)

    result = []
    for row in data:
        value = evaluate_ast(ast, row)
        if value is True:
            result.append(row)

    return result


def execute_rename(step, data, step_index):
    """Execute a rename step."""
    mapping = step["mapping"]
    result = []

    for row in data:
        new_row = dict(row)
        for src, dst in mapping.items():
            if src not in new_row:
                output_error(
                    "MISSING_COLUMN",
                    f"column '{src}' not found in row",
                    f"pipeline.steps[{step_index}].mapping.{src}"
                )
            value = new_row[src]
            del new_row[src]
            new_row[dst] = value
        result.append(new_row)

    return result


def execute_limit(step, data, step_index):
    """Execute a limit step."""
    n = step["n"]
    return data[:n]


def execute_step_inner(step, data, path_prefix):
    """Execute a single step within a branch. Returns transformed data."""
    op = step["op"]

    if op == "select":
        return execute_select_inner(step, data, path_prefix)
    elif op == "map":
        return execute_map_inner(step, data, path_prefix)
    elif op == "filter":
        return execute_filter_inner(step, data, path_prefix)
    elif op == "rename":
        return execute_rename_inner(step, data, path_prefix)
    elif op == "limit":
        return execute_limit_inner(step, data, path_prefix)
    elif op == "branch":
        return execute_branch_inner(step, data, path_prefix)
    else:
        output_error("UNKNOWN_OP", f"unsupported op '{op}'", f"{path_prefix}.op")


def execute_select_inner(step, data, path_prefix):
    """Execute a select step within a branch."""
    columns = step["columns"]
    result = []

    for row in data:
        new_row = {}
        for col_idx, col in enumerate(columns):
            if col not in row:
                output_error(
                    "MISSING_COLUMN",
                    f"column '{col}' not found in row",
                    f"{path_prefix}.columns[{col_idx}]"
                )
            new_row[col] = row[col]
        result.append(new_row)

    return result


def execute_map_inner(step, data, path_prefix):
    """Execute a map step within a branch."""
    expr_str = step["expr"]
    as_field = step["as"]
    path = f"{path_prefix}.expr"

    ast = parse_expression(expr_str, path)

    result = []
    for row in data:
        value = evaluate_ast(ast, row)
        new_row = dict(row)
        new_row[as_field] = value
        result.append(new_row)

    return result


def execute_filter_inner(step, data, path_prefix):
    """Execute a filter step within a branch."""
    where_str = step["where"]
    path = f"{path_prefix}.where"

    ast = parse_expression(where_str, path)

    result = []
    for row in data:
        value = evaluate_ast(ast, row)
        if value is True:
            result.append(row)

    return result


def execute_rename_inner(step, data, path_prefix):
    """Execute a rename step within a branch."""
    mapping = step["mapping"]
    result = []

    for row in data:
        new_row = dict(row)
        for src, dst in mapping.items():
            if src not in new_row:
                output_error(
                    "MISSING_COLUMN",
                    f"column '{src}' not found in row",
                    f"{path_prefix}.mapping.{src}"
                )
            value = new_row[src]
            del new_row[src]
            new_row[dst] = value
        result.append(new_row)

    return result


def execute_limit_inner(step, data, path_prefix):
    """Execute a limit step within a branch."""
    n = step["n"]
    return data[:n]


def execute_branch_inner(step, data, path_prefix):
    """Execute a branch step within another branch."""
    branches = step["branches"]

    branch_asts = []
    for j, branch in enumerate(branches):
        when = branch["when"]
        if when == "otherwise":
            branch_asts.append(None)
        else:
            ast = parse_expression(when, f"{path_prefix}.branches[{j}].when")
            branch_asts.append(ast)

    branch_results = []
    for j, branch in enumerate(branches):
        branch_results.append([])

    for row in data:
        matched = False
        for j, branch in enumerate(branches):
            if branch_asts[j] is None:
                if not matched:
                    branch_results[j].append(row)
                    matched = True
            else:
                result = evaluate_ast(branch_asts[j], row)
                if result is True:
                    branch_results[j].append(row)
                    matched = True
                    break

    final_result = []
    for j, branch in enumerate(branches):
        branch_rows = branch_results[j]
        if len(branch_rows) > 0:
            branch_path = f"{path_prefix}.branches[{j}]"
            branch_data = branch_rows
            for k, nested_step in enumerate(branch["steps"]):
                branch_data = execute_step_inner(nested_step, branch_data, f"{branch_path}.steps[{k}]")
            final_result.extend(branch_data)

    return final_result


def execute_branch(step, data, step_index):
    """Execute a branch step."""
    path_prefix = f"pipeline.steps[{step_index}]"
    branches = step["branches"]

    branch_asts = []
    for j, branch in enumerate(branches):
        when = branch["when"]
        if when == "otherwise":
            branch_asts.append(None)
        else:
            ast = parse_expression(when, f"{path_prefix}.branches[{j}].when")
            branch_asts.append(ast)

    branch_results = []
    for j, branch in enumerate(branches):
        branch_results.append([])

    for row in data:
        matched = False
        for j, branch in enumerate(branches):
            if branch_asts[j] is None:
                if not matched:
                    branch_results[j].append(row)
                    matched = True
            else:
                result = evaluate_ast(branch_asts[j], row)
                if result is True:
                    branch_results[j].append(row)
                    matched = True
                    break

    final_result = []
    for j, branch in enumerate(branches):
        branch_rows = branch_results[j]
        if len(branch_rows) > 0:
            branch_path = f"{path_prefix}.branches[{j}]"
            branch_data = branch_rows
            for k, nested_step in enumerate(branch["steps"]):
                branch_data = execute_step_inner(nested_step, branch_data, f"{branch_path}.steps[{k}]")
            final_result.extend(branch_data)

    return final_result


def execute_step(step, data, step_index):
    """Execute a single step and return the transformed data."""
    op = step["op"]

    executors = {
        "select": execute_select,
        "map": execute_map,
        "filter": execute_filter,
        "rename": execute_rename,
        "limit": execute_limit,
        "branch": execute_branch
    }

    return executors[op](step, data, step_index)


def execute_pipeline(steps, dataset):
    """Execute the full pipeline and return the result."""
    data = dataset
    rows_in = len(dataset)

    for i, step in enumerate(steps):
        data = execute_step(step, data, i)

    rows_out = len(data)

    return {
        "status": "ok",
        "data": data,
        "metrics": {
            "rows_in": rows_in,
            "rows_out": rows_out
        }
    }


# =============================================================================
# Main Entry Point
# =============================================================================

def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(description='ETL Pipeline Processor')
    parser.add_argument('--execute', action='store_true', default=False,
                        help='Execute the pipeline instead of just normalizing')
    args = parser.parse_args()

    try:
        input_data = json.load(sys.stdin)
    except json.JSONDecodeError as e:
        output_error("SCHEMA_VALIDATION_FAILED", f"invalid JSON: {e}", "input")

    if not isinstance(input_data, dict):
        output_error("SCHEMA_VALIDATION_FAILED", "input must be an object", "input")

    if "pipeline" not in input_data:
        output_error("SCHEMA_VALIDATION_FAILED", "missing 'pipeline'", "pipeline")

    if "dataset" not in input_data:
        output_error("SCHEMA_VALIDATION_FAILED", "missing 'dataset'", "dataset")

    pipeline = input_data.get("pipeline")
    if not isinstance(pipeline, dict):
        output_error("SCHEMA_VALIDATION_FAILED", "'pipeline' must be an object", "pipeline")

    if "steps" not in pipeline:
        output_error("SCHEMA_VALIDATION_FAILED", "missing 'steps' in pipeline", "pipeline.steps")

    steps = pipeline.get("steps")
    if not isinstance(steps, list):
        output_error("SCHEMA_VALIDATION_FAILED", "'steps' must be an array", "pipeline.steps")

    dataset = input_data.get("dataset")
    if not isinstance(dataset, list):
        output_error("SCHEMA_VALIDATION_FAILED", "'dataset' must be an array", "dataset")

    for i, item in enumerate(dataset):
        if not isinstance(item, dict):
            output_error("SCHEMA_VALIDATION_FAILED", f"dataset[{i}] must be an object", f"dataset[{i}]")

    normalized_steps = []
    for i, step in enumerate(steps):
        normalized = validate_and_normalize_step(step, i)
        normalized_steps.append(normalized)

    # If --execute is False, behave like checkpoint 1 (return normalized)
    if not args.execute:
        output = {
            "status": "ok",
            "normalized": {
                "steps": normalized_steps
            }
        }
        print(json.dumps(output))
        sys.exit(0)

    # Otherwise, execute the pipeline
    result = execute_pipeline(normalized_steps, dataset)
    print(json.dumps(result))
    sys.exit(0)


if __name__ == "__main__":
    main()
