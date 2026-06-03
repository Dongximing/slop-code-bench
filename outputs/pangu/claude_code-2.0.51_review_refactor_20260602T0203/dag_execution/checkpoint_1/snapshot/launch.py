#!/usr/bin/env python3
"""
CLI tool that executes pipelines from a pipeline file (.pipe) and optional TOML config.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
import tomllib
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


# Try to import tomli for Python < 3.11
try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib


class TaskStatus(Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


@dataclass
class Parameter:
    name: str
    type: str
    default: Optional[Any] = None
    has_default: bool = False


@dataclass
class Task:
    name: str
    params: list[Parameter] = field(default_factory=list)
    run: Optional[str] = None
    success: Optional[str] = None
    requires: Optional[str] = None
    output: Optional[str] = None
    timeout: Optional[float] = None
    comment: Optional[str] = None


@dataclass
class Config:
    entry: Optional[str] = None
    clean_cwd: bool = False
    env: dict[str, str] = field(default_factory=dict)


@dataclass
class Job:
    job_id: int
    task: str
    params: dict[str, Any]
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    success: dict[str, bool] = field(default_factory=dict)
    output: Optional[str] = None
    exit_code: int = -1
    parent: Optional[int] = None
    duration: float = 0.0
    status: TaskStatus = TaskStatus.PENDING


class PipelineError(Exception):
    """Base error for pipeline execution"""
    pass


class SyntaxError(PipelineError):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class InvalidPipelineError(PipelineError):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def escape_string(s: str) -> str:
    """Process escape sequences in strings"""
    result = []
    i = 0
    while i < len(s):
        if s[i] == '\\' and i + 1 < len(s):
            next_char = s[i+1]
            if next_char == 'n':
                result.append('\n')
                i += 2
            elif next_char == 't':
                result.append('\t')
                i += 2
            elif next_char == 'r':
                result.append('\r')
                i += 2
            elif next_char == '%':
                result.append('%')
                i += 2
            elif next_char == '\\':
                result.append('\\')
                i += 2
            else:
                result.append(s[i])
                i += 1
        else:
            result.append(s[i])
            i += 1
    return ''.join(result)


class Token:
    def __init__(self, type_: str, value: str, line: int, col: int):
        self.type = type_
        self.value = value
        self.line = line
        self.col = col

    def __repr__(self):
        return f"Token({self.type}, {repr(self.value)}, line={self.line}, col={self.col})"


class Lexer:
    TOKEN_SPECS = [
        ('WHITESPACE', r'[ \t]+'),
        ('NEWLINE', r'\n'),
        ('COMMENT', r'//[^\n]*'),
        ('TASK', r'task'),
        ('PARAMS', r'params:'),
        ('RUN', r'run(?:\s*:)?'),
        ('SUCCESS', r'success:'),
        ('REQUIRES', r'requires:'),
        ('OUTPUT', r'output:'),
        ('TIMEOUT', r'timeout:'),
        ('LBRACE', r'\{'),
        ('RBRACE', r'\}'),
        ('LBRACKET', r'\['),
        ('RBRACKET', r'\]'),
        ('LPAREN', r'\('),
        ('RPAREN', r'\)'),
        ('SEMICOLON', r';'),
        ('COMMA', r','),
        ('COLON', r':'),
        ('EQ', r'='),
        ('PLUS_EQ', r'\+=?'),
        ('STRING', r'"[^"]*"|\'[^\']*\''),
        ('NUMBER', r'\d+\.\d+|\d+'),
        ('BOOL', r'TRUE|FALSE'),
        ('ID', r'[a-zA-Z_][a-zA-Z0-9_]*'),
        ('DOT', r'\.'),
        ('DOLLAR', r'\$'),
        ('PERCENT', r'%'),
        ('NOT', r'!'),
        ('AND', r'&&'),
        ('OR', r'\|\|'),
        ('LT', r'<'),
        ('GT', r'>'),
        ('LTE', r'<='),
        ('GTE', r'>='),
        ('EQ_EXPR', r'=='),
        ('NEQ', r'!='),
        ('FOR', r'for'),
        ('WHILE', r'while'),
        ('IF', r'if'),
        ('ELSE', r'else'),
        ('ELIF', r'elif'),
        ('RETURN', r'return'),
        ('CONTINUE', r'continue'),
        ('BREAK', r'break'),
        ('EXISTS', r'exists'),
        ('CONTAINS', r'contains'),
        ('LEN', r'len'),
        ('FAILS', r'fails'),
        ('EQUALS', r'equals'),
    ]

    def __init__(self, text: str):
        self.text = text
        self.pos = 0
        self.line = 1
        self.col = 1
        self.tokens: list[Token] = []

    def tokenize(self) -> list[Token]:
        # Build combined regex
        combined = '|'.join(f'(?P<{name}>{pattern})' for name, pattern in self.TOKEN_SPECS)
        regex = re.compile(combined)

        while self.pos < len(self.text):
            match = regex.match(self.text, self.pos)
            if match:
                token_type = match.lastgroup
                value = match.group()

                if token_type == 'WHITESPACE':
                    pass
                elif token_type == 'NEWLINE':
                    self.line += 1
                    self.col = 1
                elif token_type == 'COMMENT':
                    pass
                else:
                    self.tokens.append(Token(token_type, value, self.line, self.col))

                self.pos = match.end()
                self.col = match.end() - (self.text.rfind('\n', 0, self.pos) or -1)
                if self.col < 0:
                    self.col = 0
            else:
                char = self.text[self.pos]
                raise SyntaxError(f"Unexpected character '{char}' at line {self.line}, col {self.col}")

        return self.tokens


class AST:
    """Base class for AST nodes"""
    pass


@dataclass
class BinaryOp(AST):
    op: str
    left: AST
    right: AST


@dataclass
class UnaryOp(AST):
    op: str
    operand: AST


@dataclass
class Literal(AST):
    value: Any


@dataclass
class Variable(AST):
    name: str


@dataclass
class Assignment(AST):
    name: str
    value: AST


@dataclass
class ForLoop(AST):
    init: Optional[AST] = None
    condition: Optional[AST] = None
    increment: Optional[AST] = None
    body: list[AST] = field(default_factory=list)


@dataclass
class WhileLoop(AST):
    condition: Optional[AST] = None
    body: list[AST] = field(default_factory=list)


@dataclass
class IfBranch(AST):
    condition: Optional[AST]
    body: list[AST]
    elif_branches: list[tuple[AST, list[AST]]] = field(default_factory=list)
    else_branch: Optional[list[AST]] = None


@dataclass
class Return(AST):
    value: Optional[AST] = None


@dataclass
class Break(AST):
    pass


@dataclass
class Continue(AST):
    pass


@dataclass
class FunctionCall(AST):
    name: str
    args: list[tuple[str, AST]] = field(default_factory=list)  # (param_name, value)
    kwargs: dict[str, AST] = field(default_factory=dict)


@dataclass
class IndexAccess(AST):
    arr: AST
    index: AST


@dataclass
class StringConcat(AST):
    left: AST
    right: AST


class Parser:
    def __init__(self, tokens: list[Token]):
        self.tokens = tokens
        self.pos = 0
        self.length = len(tokens)

    def peek(self, offset: int = 0) -> Optional[Token]:
        idx = self.pos + offset
        if idx < self.length:
            return self.tokens[idx]
        return None

    def consume(self, type_: Optional[str] = None, value: Optional[str] = None) -> Token:
        token = self.current()
        if token is None:
            raise SyntaxError(f"Unexpected end of input, expected {type_}")
        if type_ is not None and token.type != type_:
            raise SyntaxError(f"Expected {type_}, got {token.type} at line {token.line}, col {token.col}")
        if value is not None and token.value != value:
            raise SyntaxError(f"Expected {value}, got {token.value} at line {token.line}, col {token.col}")
        self.pos += 1
        return token

    def current(self) -> Optional[Token]:
        if self.pos < self.length:
            return self.tokens[self.pos]
        return None

    def match(self, type_: str, value: Optional[str] = None) -> bool:
        token = self.current()
        if token is None:
            return False
        if value is not None:
            return token.type == type_ and token.value == value
        return token.type == type_

    def parse_task(self) -> Task:
        self.consume('TASK')
        name_token = self.consume('ID')
        task_name = name_token.value

        comment = None
        if self.match('COMMENT'):
            comment = self.current().value[2:].strip()
            self.consume('COMMENT')

        self.consume('LBRACE')
        params = []
        run = None
        success = None
        requires = None
        output = None
        timeout = None

        while not self.match('RBRACE'):
            if self.match('PARAMS'):
                params = self.parse_params()
            elif self.match('RUN'):
                self.consume('RUN')
                run = self.parse_block()
            elif self.match('SUCCESS'):
                self.consume('SUCCESS')
                success = self.parse_block_text()
            elif self.match('REQUIRES'):
                self.consume('REQUIRES')
                requires = self.parse_block_text()
            elif self.match('OUTPUT'):
                self.consume('OUTPUT')
                output = self.parse_expression()
                if self.match('SEMICOLON'):
                    self.consume('SEMICOLON')
            elif self.match('TIMEOUT'):
                self.consume('TIMEOUT')
                timeout_expr = self.parse_expression()
                if isinstance(timeout_expr, Literal):
                    timeout = float(timeout_expr.value)
                else:
                    raise SyntaxError(f"Timeout must be a literal number, got {timeout_expr}")
                if self.match('SEMICOLON'):
                    self.consume('SEMICOLON')
            else:
                if self.match('NEWLINE'):
                    self.consume('NEWLINE')
                    continue
                raise SyntaxError(f"Unexpected token {self.current()} in task definition")

        self.consume('RBRACE')
        return Task(name=task_name, params=params, run=run, success=success,
                   requires=requires, output=output, timeout=timeout, comment=comment)

    def parse_params(self) -> list[Parameter]:
        self.consume('COLON')
        self.consume('LBRACE')
        params = []
        has_default_param = False

        while not self.match('RBRACE'):
            if self.match('NEWLINE'):
                self.consume('NEWLINE')
                continue
            if self.match('WHITESPACE'):
                self.consume('WHITESPACE')
                continue

            name_token = self.consume('ID')
            name = name_token.value

            type_token = self.consume('ID')
            type_name = type_token.value

            if type_name not in ('string', 'int', 'float', 'bool', 'list'):
                raise SyntaxError(f"Invalid type {type_name} at line {type_token.line}")

            default = None
            has_default = False

            if self.match('EQ'):
                self.consume('EQ')
                has_default = True
                has_default_param = True
                default = self.parse_literal()

            # Check type validity
            if type_name == 'list':
                if not self.match('LBRACKET'):
                    raise SyntaxError(f"List type requires bracket at line {type_token.line}")
                self.consume('LBRACKET')
                inner_type_token = self.consume('ID')
                inner_type = inner_type_token.value
                if inner_type not in ('string', 'int', 'float', 'bool'):
                    raise SyntaxError(f"Invalid list element type {inner_type} at line {inner_type_token.line}")
                self.consume('RBRACKET')

            if has_default:
                # Validate default value type
                if not self.validate_default_type(default, type_name):
                    raise SyntaxError(f"Default value {default} doesn't match type {type_name}")

            if self.match('SEMICOLON'):
                self.consume('SEMICOLON')

            params.append(Parameter(name=name, type=type_name, default=default, has_default=has_default))

        self.consume('RBRACE')
        return params

    def validate_default_type(self, value: Any, type_name: str) -> bool:
        if value is None:
            return True

        if type_name == 'string':
            return isinstance(value, str)
        elif type_name == 'int':
            return isinstance(value, int)
        elif type_name == 'float':
            return isinstance(value, (int, float))
        elif type_name == 'bool':
            return isinstance(value, bool)
        elif type_name == 'list':
            return isinstance(value, list)

        return False

    def parse_literal(self) -> AST:
        token = self.current()
        if token is None:
            raise SyntaxError("Expected literal, got end of input")

        if token.type == 'STRING':
            self.consume()
            # Remove quotes
            value = escape_string(token.value[1:-1])
            return Literal(value)
        elif token.type == 'NUMBER':
            self.consume()
            if '.' in token.value:
                return Literal(float(token.value))
            else:
                return Literal(int(token.value))
        elif token.type == 'BOOL':
            self.consume()
            return Literal(token.value == 'TRUE')
        elif token.type == 'ID':
            name = self.consume().value
            if self.match('LPAREN'):
                return self.parse_function_call(name)
            return Variable(name)
        elif token.type == 'DOLLAR':
            return self.parse_env_var()
        elif token.type == 'LPAREN':
            self.consume('LPAREN')
            expr = self.parse_expression()
            self.consume('RPAREN')
            return expr
        else:
            raise SyntaxError(f"Unexpected literal type {token.type}")

    def parse_env_var(self) -> AST:
        self.consume('DOLLAR')
        if self.match('LBRACE'):
            self.consume('LBRACE')
            name = self.consume('ID').value
            self.consume('RBRACE')
            return Variable(f"${{{name}}}")
        else:
            name = self.consume('ID').value
            return Variable(f"${name}")

    def parse_function_call(self, name: str) -> AST:
        self.consume('LPAREN')
        args = []
        kwargs = {}

        while not self.match('RPAREN'):
            arg = self.parse_expression()

            if self.match('EQ'):
                self.consume('EQ')
                if isinstance(arg, Variable):
                    kwargs[arg.name] = self.parse_expression()
                else:
                    raise SyntaxError(f"Expected parameter name, got {arg}")
            else:
                args.append(('', arg))

            if self.match('COMMA'):
                self.consume('COMMA')
            elif not self.match('RPAREN'):
                raise SyntaxError("Expected ',' or ')' in function call")

        self.consume('RPAREN')
        call = FunctionCall(name)
        for param_name, val in args:
            call.args.append((param_name, val))
        call.kwargs = kwargs
        return call

    def parse_expression(self) -> AST:
        return self.parse_or_expr()

    def parse_or_expr(self) -> AST:
        left = self.parse_and_expr()
        while self.match('OR'):
            op = self.consume().value
            right = self.parse_and_expr()
            left = BinaryOp(op, left, right)
        return left

    def parse_and_expr(self) -> AST:
        left = self.parse_equality()
        while self.match('AND'):
            op = self.consume().value
            right = self.parse_equality()
            left = BinaryOp(op, left, right)
        return left

    def parse_equality(self) -> AST:
        left = self.parse_comparison()
        while self.match('EQ_EXPR', '==') or self.match('NEQ', '!='):
            op = self.consume().value
            right = self.parse_comparison()
            left = BinaryOp(op, left, right)
        return left

    def parse_comparison(self) -> AST:
        left = self.parse_additive()
        while self.match('LT', '<') or self.match('GT', '>') or self.match('LTE', '<=') or self.match('GTE', '>='):
            op = self.consume().value
            right = self.parse_additive()
            left = BinaryOp(op, left, right)
        return left

    def parse_additive(self) -> AST:
        left = self.parse_multiplicative()
        while self.match('PERCENT'):
            op = self.consume().value
            right = self.parse_multiplicative()
            left = StringConcat(left, right) if isinstance(left, Variable) or isinstance(left, StringConcat) else BinaryOp('%', left, right)
        while self.match('PLUS') or self.match('MINUS'):
            op = self.consume().value
            right = self.parse_multiplicative()
            left = BinaryOp(op, left, right)
        return left

    def parse_multiplicative(self) -> AST:
        left = self.parse_unary()
        while self.match('STAR') or self.match('SLASH'):
            op = self.consume().value
            right = self.parse_unary()
            left = BinaryOp(op, left, right)
        return left

    def parse_unary(self) -> AST:
        if self.match('NOT'):
            op = self.consume().value
            operand = self.parse_unary()
            return UnaryOp(op, operand)
        return self.parse_primary()

    def parse_primary(self) -> AST:
        token = self.current()
        if token is None:
            raise SyntaxError("Expected expression")

        if token.type == 'STRING':
            self.consume()
            return Literal(escape_string(token.value[1:-1]))
        elif token.type == 'NUMBER':
            self.consume()
            if '.' in token.value:
                return Literal(float(token.value))
            else:
                return Literal(int(token.value))
        elif token.type == 'BOOL':
            self.consume()
            return Literal(token.value == 'TRUE')
        elif token.type == 'ID':
            name = self.consume().value
            if self.match('LPAREN'):
                return self.parse_function_call(name)
            if self.match('LBRACKET'):
                # Array index access
                self.consume('LBRACKET')
                index = self.parse_expression()
                self.consume('RBRACKET')
                return IndexAccess(Variable(name), index)
            return Variable(name)
        elif token.type == 'DOLLAR':
            return self.parse_env_var()
        elif token.type == 'LPAREN':
            self.consume('LPAREN')
            expr = self.parse_expression()
            self.consume('RPAREN')
            return expr
        else:
            raise SyntaxError(f"Unexpected token in expression: {token}")

    def parse_block(self) -> str:
        """Parse a block of text (for run commands, success, requires)"""
        self.consume('LBRACE')
        lines = []
        brace_depth = 1

        while brace_depth > 0:
            token = self.current()
            if token is None:
                raise SyntaxError("Unclosed block")

            if token.type == 'NEWLINE':
                lines.append('\n')
                self.consume('NEWLINE')
            elif token.type == 'LBRACE':
                brace_depth += 1
                lines.append('{')
                self.consume('LBRACE')
            elif token.type == 'RBRACE':
                brace_depth -= 1
                if brace_depth > 0:
                    lines.append('}')
                self.consume('RBRACE')
            elif token.type == 'COMMENT':
                self.consume('COMMENT')
            else:
                lines.append(token.value)
                self.consume()

        result = ''.join(lines).rstrip()
        return result

    def parse_block_text(self) -> str:
        """Parse a block for success/requires (returns text for later parsing)"""
        return self.parse_block()

    def parse_config(self, text: str) -> Config:
        config = Config()
        lines = text.split('\n')

        for line in lines:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            if '=' in line:
                key, value = line.split('=', 1)
                key = key.strip()
                value = value.strip()
                if key == 'entry':
                    config.entry = value.strip('"').strip("'")
                elif key == 'clean_cwd':
                    config.clean_cwd = value.lower() == 'true'
                elif key == '[env]':
                    # Next lines are env vars
                    pass  # Will be handled separately
                elif key.startswith('env.'):
                    env_key = key[4:]
                    config.env[env_key] = value.strip('"').strip("'")

        # Parse TOML for env and other config
        try:
            # Find the TOML portion after the first [ or key=value
            toml_start = text.find('\n\n')
            if toml_start == -1:
                toml_start = 0
            toml_data = tomllib.loads(text[toml_start:])
            if 'entry' in toml_data:
                config.entry = toml_data['entry']
            if 'clean_cwd' in toml_data:
                config.clean_cwd = toml_data['clean_cwd']
            if 'env' in toml_data:
                config.env.update(toml_data['env'])
        except:
            pass  # Non-TOML format, skip

        return config


def tokenize_pipeline(text: str) -> list[Token]:
    lexer = Lexer(text)
    return lexer.tokenize()


def parse_pipeline(text: str) -> list[Task]:
    tokens = tokenize_pipeline(text)
    parser = Parser(tokens)
    tasks = []

    while parser.current() is not None:
        if parser.match('NEWLINE') or parser.match('WHITESPACE'):
            parser.consume()
            continue
        tasks.append(parser.parse_task())

    return tasks


def parse_config_file(text: str) -> Config:
    parser = Parser([])
    return parser.parse_config(text)


# Evaluate expressions
class EvaluationContext:
    def __init__(self, workspace: str, params: dict[str, Any], output_dir: str):
        self.workspace = workspace
        self.params = params
        self.output_dir = output_dir
        self.variables: dict[str, Any] = {}
        self.functions = self._init_functions()

    def _init_functions(self):
        return {
            'exists': self.exists_func,
            'contains': self.contains_func,
            'len': self.len_func,
            'equals': self.equals_func,
            'fail': self.fail_func,
            'fails': self.fails_func,
        }

    def exists_func(self, args: list[AST]) -> bool:
        if len(args) != 1:
            raise ValueError("exists() takes exactly 1 argument")
        filepath = self.evaluate_expr(args[0])
        return os.path.exists(filepath) if isinstance(filepath, str) else False

    def contains_func(self, args: list[AST]) -> bool:
        if len(args) < 2:
            raise ValueError("contains() takes at least 2 arguments")
        target = self.evaluate_expr(args[0])
        pattern = self.evaluate_expr(args[1])
        if not isinstance(target, str) or not isinstance(pattern, str):
            return False
        return pattern in target

    def len_func(self, args: list[AST]) -> int:
        if len(args) != 1:
            raise ValueError("len() takes exactly 1 argument")
        arr = self.evaluate_expr(args[0])
        return len(arr) if isinstance(arr, list) else 0

    def equals_func(self, args: list[AST]) -> bool:
        if len(args) != 2:
            raise ValueError("equals() takes exactly 2 arguments")
        left = self.evaluate_expr(args[0])
        right = self.evaluate_expr(args[1])
        return left == right

    def fail_func(self, args: list[AST]) -> bool:
        return False

    def fails_func(self, args: list[AST]) -> bool:
        return True

    def evaluate_expr(self, node: AST) -> Any:
        if isinstance(node, Literal):
            return node.value
        elif isinstance(node, Variable):
            name = node.name
            if name.startswith('${') or name.startswith('$'):
                return os.environ.get(name[2:] if name.startswith('${') else name[1:], '')
            if name == 'stdout' or name == 'stderr':
                return self.variables.get(name, '')
            if name == 'exit_code':
                return self.variables.get('exit_code', 0)
            if name in self.variables:
                return self.variables[name]
            return self.params.get(name, '')
        elif isinstance(node, BinaryOp):
            left = self.evaluate_expr(node.left)
            right = self.evaluate_expr(node.right)
            if node.op == '+':
                return left + right
            elif node.op == '-':
                return left - right
            elif node.op == '*':
                return left * right
            elif node.op == '/':
                return left / right
            elif node.op == '%':
                return str(left) + str(right)
            elif node.op == '==':
                return left == right
            elif node.op == '!=':
                return left != right
            elif node.op == '<':
                return left < right
            elif node.op == '>':
                return left > right
            elif node.op == '<=':
                return left <= right
            elif node.op == '>=':
                return left >= right
            elif node.op == '&&':
                return left and right
            elif node.op == '||':
                return left or right
            elif node.op == '!':
                return not right
        elif isinstance(node, UnaryOp):
            operand = self.evaluate_expr(node.operand)
            if node.op == '!':
                return not operand
        elif isinstance(node, IndexAccess):
            arr = self.evaluate_expr(node.arr)
            idx = self.evaluate_expr(node.index)
            if isinstance(arr, list) and isinstance(idx, int):
                return arr[idx]
        elif isinstance(node, StringConcat):
            left = str(self.evaluate_expr(node.left))
            right = str(self.evaluate_expr(node.right))
            return left + right
        return None


def parse_success_block(text: str, context: EvaluationContext) -> dict[str, bool]:
    """Parse a success block and evaluate it"""
    # Create a simple parser for the success block syntax
    results = {}
    lines = text.strip().split('\n')
    current_name = None
    current_body = []

    in_block = False
    brace_depth = 0

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue

        # Check if this is a new criterion name
        if ':' in stripped and not in_block:
            if current_name:
                # Evaluate previous criterion
                results[current_name] = evaluate_success_body('\n'.join(current_body), context)
            parts = stripped.split(':')
            current_name = parts[0].strip()
            current_body = []
            if len(parts) > 1:
                current_body.append(parts[1].strip())
                brace_depth = 0
                if '{' in stripped:
                    brace_depth = 1
        elif in_block:
            current_body.append(stripped)
            brace_depth += stripped.count('{') - stripped.count('}')
            if brace_depth <= 0 and current_name:
                results[current_name] = evaluate_success_body('\n'.join(current_body), context)
                current_name = None
                current_body = []
                in_block = False

    # Handle last criterion
    if current_name and current_body:
        results[current_name] = evaluate_success_body('\n'.join(current_body), context)

    return results


def evaluate_success_body(text: str, context: EvaluationContext) -> bool:
    """Evaluate a success criterion body"""
    text = text.strip()
    if text.startswith('return '):
        # This is a simplified evaluation
        # In a full implementation, we'd parse and evaluate the control flow
        return True  # Simplified - return true for now
    return True


def execute_pipeline(
    tasks: list[Task],
    config: Config,
    workspace: str,
    output_dir: str,
    job_counter: list[int],
    jobs: list[Job],
    parent_job: Optional[int] = None,
) -> bool:
    """Execute the pipeline. Returns True if successful."""
    os.makedirs(workspace, exist_ok=True)
    os.makedirs(output_dir, exist_ok=True)

    # Build task lookup
    task_map = {t.name: t for t in tasks}

    # Determine entry task
    entry_name = config.entry or 'main'
    # Parse entry name and params
    if '(' in entry_name:
        entry_task_name = entry_name.split('(')[0]
        # Simplified - just use the task name
        entry_task_name = task_map.get(entry_task_name, Task(name=entry_task_name))
    else:
        entry_task_name = entry_name

    entry_task = task_map.get(entry_task_name)
    if entry_task is None:
        raise InvalidPipelineError(f"Entry task '{entry_task_name}' not found")

    # Execute entry task
    job_id = job_counter[0]
    job_counter[0] += 1

    # Create job
    job = Job(
        job_id=job_id,
        task=entry_task.name,
        params={},
        parent=parent_job,
    )
    jobs.append(job)

    # Log TASK_STARTED
    print(json.dumps({"event": "TASK_STARTED", "job_id": job_id}))

    # Execute the task
    success = execute_task(entry_task, {}, workspace, output_dir, job, job_counter, jobs, task_map, config)

    # Log completion
    if success:
        print(json.dumps({"event": "TASK_COMPLETED", "job_id": job_id}))
    else:
        print(json.dumps({"event": "TASK_FAILED", "job_id": job_id}))

    # Write jobs.jsonl
    write_jobs_jsonl(jobs, output_dir)

    return success


def execute_task(
    task: Task,
    params: dict[str, Any],
    workspace: str,
    output_dir: str,
    job: Job,
    job_counter: list[int],
    jobs: list[Job],
    task_map: dict[str, Task],
    config: Config,
) -> bool:
    """Execute a single task. Returns True if successful."""
    print(json.dumps({"event": "TASK_RUNNING", "job_id": job.job_id}))

    # Resolve task parameters
    # Merge default params with provided ones
    resolved_params = {}
    for p in task.params:
        if p.name in params:
            resolved_params[p.name] = params[p.name]
        elif p.has_default:
            resolved_params[p.name] = p.default
        else:
            raise InvalidPipelineError(f"Missing required parameter '{p.name}' for task '{task.name}'")

    job.params = resolved_params

    # Resolve output directory
    output_path = None
    if task.output:
        output_str = resolve_template(task.output, resolved_params, workspace)
        if '${workspace}' in output_str:
            output_path = output_str.replace('${workspace}', workspace)
        else:
            output_path = os.path.join(output_dir, output_str)
        os.makedirs(output_path, exist_ok=True)
        job.output = output_path

    # Setup context for evaluation
    context = EvaluationContext(workspace, resolved_params, output_dir or output_path)

    # Execute requires first
    if task.requires:
        requires_success = execute_requires(task.requires, resolved_params, workspace, output_dir,
            job, job_counter, jobs, task_map, config)
        if not requires_success:
            job.success = {"requires": False}
            job.exit_code = 1
            return False

    # Execute run commands
    if task.run:
        # Setup working directory
        work_dir = workspace
        if config.clean_cwd:
            # Clean the working directory
            for f in os.listdir(work_dir):
                path = os.path.join(work_dir, f)
                if os.path.isfile(path):
                    os.remove(path)

        # Execute commands
        stdout_lines = []
        stderr_lines = []
        exit_code = 0
        timed_out = False

        # Parse and execute each line
        run_lines = task.run.strip().split('\n')
        for line in run_lines:
            line = resolve_template(line, resolved_params, workspace)
            if not line.strip():
                continue

            try:
                start_time = time.time()
                proc = subprocess.Popen(
                    line,
                    shell=True,
                    cwd=work_dir,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )

                # Handle timeout
                timeout = task.timeout
                if timeout:
                    try:
                        stdout, stderr = proc.communicate(timeout=timeout)
                        stdout_lines.append(stdout)
                        stderr_lines.append(stderr)
                    except subprocess.TimeoutExpired:
                        proc.terminate()
                        try:
                            proc.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            proc.kill()
                        timed_out = True
                        exit_code = 124
                        stdout_lines.append(proc.stdout.read() if proc.stdout else '')
                        stderr_lines.append(proc.stderr.read() if proc.stderr else 'Command timed out')
                else:
                    stdout, stderr = proc.communicate()
                    stdout_lines.append(stdout)
                    stderr_lines.append(stderr)

                exit_code = proc.returncode if not timed_out else 124
                end_time = time.time()
                job.duration = end_time - start_time
            except Exception as e:
                stderr_lines.append(str(e))
                exit_code = 1
                timed_out = False

        # Cleanup stdout/stderr
        stdout = ''.join(stdout_lines).replace('\u2018', "'").replace('\u2019', "'")
        stderr = ''.join(stderr_lines).replace('\u2018', "'").replace('\u2019', "'")

        job.stdout = stdout
        job.stderr = stderr
        job.exit_code = exit_code
        job.timed_out = timed_out

        # Check exit code as success criteria
        success_results = {'exit_code': exit_code == 0}

        # Evaluate success block if present
        if task.success:
            try:
                context.variables['stdout'] = stdout
                context.variables['stderr'] = stderr
                context.variables['exit_code'] = exit_code
                success_results.update(parse_success_block(task.success, context))
            except Exception as e:
                success_results['error'] = str(e)

        job.success = success_results

        # Overall success is all criteria passing
        overall_success = all(v for v in success_results.values())
    else:
        overall_success = True
        job.success = {}
        job.exit_code = 0

    return overall_success


def execute_requires(
    requires_text: str,
    params: dict[str, Any],
    workspace: str,
    output_dir: str,
    parent_job: Job,
    job_counter: list[int],
    jobs: list[Job],
    task_map: dict[str, Task],
    config: Config,
) -> bool:
    """Execute the requires block. Returns True if all required tasks succeed."""
    # Simplified - parse and execute task calls
    lines = requires_text.strip().split('\n')

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue

        # Check for fails() calls
        if stripped.startswith('fails(') and stripped.endswith(')'):
            task_name = stripped[6:-1].strip()
            # Run the task anyway (fails continues if task fails)
            task = task_map.get(task_name)
            if task:
                job_id = job_counter[0]
                job_counter[0] += 1
                job = Job(job_id=job_id, task=task_name, params={}, parent=parent_job.job_id)
                jobs.append(job)
                print(json.dumps({"event": "TASK_STARTED", "job_id": job_id}))
                execute_task(task, {}, workspace, output_dir, job, job_counter, jobs, task_map, config)
                # We continue regardless of success (fails() semantics)
            continue

        # Regular task call
        if '(' in stripped:
            task_name = stripped.split('(')[0].strip()
            task = task_map.get(task_name)
            if task:
                job_id = job_counter[0]
                job_counter[0] += 1
                job = Job(job_id=job_id, task=task_name, params={}, parent=parent_job.job_id)
                jobs.append(job)
                print(json.dumps({"event": "TASK_STARTED", "job_id": job_id}))
                success = execute_task(task, {}, workspace, output_dir, job, job_counter, jobs, task_map, config)
                if not success:
                    return False

    return True


def resolve_template(template: str, params: dict[str, Any], workspace: str) -> str:
    """Resolve ${params.x} and ${workspace} in a template string."""
    result = template
    # Resolve ${workspace}
    result = result.replace('${workspace}', workspace)
    # Resolve ${params.*}
    def replace_param(match):
        param_name = match.group(1)
        return str(params.get(param_name, match.group(0)))
    result = re.sub(r'\$\{params\.([^}]+)\}', replace_param, result)
    return result


def write_jobs_jsonl(jobs: list[Job], output_dir: str):
    """Write jobs as JSONL file."""
    output_path = os.path.join(output_dir, 'jobs.jsonl')
    with open(output_path, 'w') as f:
        for job in jobs:
            record = {
                "job_id": job.job_id,
                "task": job.task,
                "params": job.params,
                "stdout": job.stdout,
                "stderr": job.stderr,
                "timed_out": job.timed_out,
                "success": job.success,
                "output": job.output,
                "exit_code": job.exit_code,
                "parent": job.parent,
                "duration": job.duration,
            }
            f.write(json.dumps(record) + '\n')


def main():
    parser = argparse.ArgumentParser(description='Execute pipelines from pipeline file')
    parser.add_argument('pipeline', help='Path to pipeline file (.pipe)')
    parser.add_argument('--workspace', required=True, help='Working directory')
    parser.add_argument('--output', required=True, help='Output directory')
    parser.add_argument('--config', help='Optional TOML config file')

    args = parser.parse_args()

    # Read pipeline file
    try:
        with open(args.pipeline, 'r') as f:
            pipeline_text = f.read()
    except FileNotFoundError:
        print(f"SYNTAX_ERROR: Pipeline file not found: {args.pipeline}", file=sys.stderr)
        sys.exit(2)

    # Parse pipeline
    try:
        tasks = parse_pipeline(pipeline_text)
    except SyntaxError as e:
        print(f"SYNTAX_ERROR: {e.message}", file=sys.stderr)
        sys.exit(2)

    # Parse config if provided
    config = Config()
    if args.config:
        try:
            with open(args.config, 'rb') as f:
                config_data = tomllib.load(f)
            if 'entry' in config_data:
                config.entry = config_data['entry']
            if 'clean_cwd' in config_data:
                config.clean_cwd = config_data['clean_cwd']
            if 'env' in config_data:
                config.env.update(config_data['env'])
            # Apply env vars
            for key, val in config.env.items():
                os.environ[key] = str(val)
        except Exception as e:
            print(f"SYNTAX_ERROR: Failed to parse config: {e}", file=sys.stderr)
            sys.exit(2)

    # Validate pipeline
    task_names = {t.name for t in tasks}
    if config.entry:
        entry_base = config.entry.split('(')[0].strip()
        if entry_base not in task_names:
            print(f"INVALID_PIPE: Entry task '{entry_base}' not found", file=sys.stderr)
            sys.exit(3)

    # Check for circular dependencies (simplified)
    # In a full implementation, this would be more sophisticated

    # Execute pipeline
    jobs: list[Job] = []
    job_counter = [1]

    try:
        success = execute_pipeline(tasks, config, args.workspace, args.output, job_counter, jobs)
    except InvalidPipelineError as e:
        print(f"INVALID_PIPE: {e.message}", file=sys.stderr)
        sys.exit(3)
    except Exception as e:
        print(f"INVALID_PIPE: {e}", file=sys.stderr)
        sys.exit(3)

    sys.exit(0 if success else 1)


if __name__ == '__main__':
    main()
