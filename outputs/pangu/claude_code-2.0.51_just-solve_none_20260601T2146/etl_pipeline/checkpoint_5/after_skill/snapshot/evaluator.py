#!/usr/bin/env python3
"""Expression evaluator for ETL pipeline."""

import re

TWO_CHAR_OPS = {'==', '!=', '<=', '>=', '||', '&&'}


def tokenize(expr):
    """Tokenize expression string."""
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
        if pos + 1 < length and expr[pos:pos + 2] in TWO_CHAR_OPS:
            yield ('OPERATOR', expr[pos:pos + 2])
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
                yield ('LITERAL', True if keyword == 'true' else False if keyword == 'false' else None)
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
    """Parse tokens into AST."""
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
                expect('IDENTIFIER', "Expected identifier after '.'"))
                parts.append(t[1])
            return ('ident', parts[0]) if len(parts) == 1 else ('member_access', tuple(parts))
        if t[0] == 'LITERAL':
            advance()
            return ('literal', t[1])
        raise ValueError(f"Unexpected token: {t}")

    def parse_unary():
        if peek()[0] == 'OPERATOR' and peek()[1] in ('!', '-'):
            op = peek()[1]
            advance()
            return (op, parse_unary())
        return parse_primary()

    def parse_multiplicative():
        node = parse_unary()
        while peek()[0] == 'OPERATOR' and peek()[1] in ('*', '/'):
            op = peek()[1]
            advance()
            node = (op, node, parse_unary())
        return node

    def parse_additive():
        node = parse_multiplicative()
        while peek()[0] == 'OPERATOR' and peek()[1] in ('+', '-'):
            op = peek()[1]
            advance()
            node = (op, node, parse_multiplicative())
        return node

    def parse_comparison():
        node = parse_additive()
        while peek()[0] == 'OPERATOR' and peek()[1] in ('<', '<=', '>', '>='):
            op = peek()[1]
            advance()
            node = (op, node, parse_additive())
        return node

    def parse_equality():
        node = parse_comparison()
        while peek()[0] == 'OPERATOR' and peek()[1] in ('==', '!='):
            op = peek()[1]
            advance()
            node = (op, node, parse_comparison())
        return node

    def parse_and():
        node = parse_equality()
        while peek()[0] == 'OPERATOR' and peek()[1] == '&&':
            advance()
            node = ('and', node, parse_equality())
        return node

    def parse_or():
        node = parse_and()
        while peek()[0] == 'OPERATOR' and peek()[1] == '||':
            advance()
            node = ('or', node, parse_and())
        return node

    current = advance()
    return parse_or()


def eval_literal(val):
    return val


def eval_ident(name, context):
    return context.get(name)


def eval_member_access(parts, params):
    base, member = parts
    return params.get(member) if base == 'params' else None


def eval_unary(op, arg):
    if op == '!':
        return not bool(arg)
    if op == '-':
        return None if arg is None else (-arg if isinstance(arg, (int, float)) else None)
    return None


def eval_binary(op, left, right):
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
        if left is None or right is None:
            return False
        if type(left) == type(right):
            return left == right
        if isinstance(left, (int, float)) and isinstance(right, (int, float)):
            return left == right
        return False
    if op == '!=':
        if left is None or right is None:
            return True
        if type(left) == type(right):
            return left != right
        if isinstance(left, (int, float)) and isinstance(right, (int, float)):
            return left != right
        return True
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


_OP_DISPATCH = {
    'literal': eval_literal,
    'ident': eval_ident,
    'member_access': eval_member_access,
    '!': eval_unary,
    '-': eval_unary,
    '+': eval_binary,
    '-': eval_binary,
    '*': eval_binary,
    '/': eval_binary,
    '==': eval_binary,
    '!=': eval_binary,
    '<': eval_binary,
    '<=': eval_binary,
    '>': eval_binary,
    '>=': eval_binary,
    'or': eval_binary,
    'and': eval_binary,
}


def evaluate_ast(ast, context, params=None):
    """Evaluate AST with context and params."""
    if params is None:
        params = {}

    node_type = ast[0]

    if node_type in ('literal', 'ident'):
        return _OP_DISPATCH[node_type](ast[1], context)
    if node_type == 'member_access':
        return _OP_DISPATCH[node_type](ast[1], params)
    if node_type in ('!', '-'):
        return eval_unary(node_type, evaluate_ast(ast[1], context, params))

    left = evaluate_ast(ast[1], context, params)
    if len(ast) > 2:
        right = evaluate_ast(ast[2], context, params)
        return eval_binary(node_type, left, right)
    return None


def evaluate_expression(expr, context, params=None):
    """Evaluate expression string with context."""
    ast = parse_expression(tokenize(expr))
    return evaluate_ast(ast, context, params)
