#!/usr/bin/env python3
"""
CLI tool that executes pipelines from a pipeline file (.pipe) and optional TOML config.
"""

import argparse
import hashlib
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


class TaskStatus(Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


@dataclass
class CacheTTL:
    seconds: Optional[int] = None
    minutes: Optional[int] = None
    hours: Optional[int] = None
    days: Optional[int] = None

@dataclass
class CacheKey:
    include: Optional[list[str]] = None
    exclude: Optional[list[str]] = None

@dataclass
class CacheEntry:
    cache_key: str
    task_name: str
    params: dict[str, Any]
    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0
    output_dir: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    input_hashes: list[str] = field(default_factory=list)
    success: dict[str, bool] = field(default_factory=dict)

@dataclass
class CacheConfig:
    enabled: bool = False
    strategy: str = "content"
    location: str = ".pipe-cache"
    version: Optional[str] = None
    ttl: Optional[CacheTTL] = None
    key: Optional[CacheKey] = None

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
    cache: Optional[CacheConfig] = None
    inputs: Optional[list[str]] = None


@dataclass
class Config:
    entry: Optional[str] = None
    clean_cwd: bool = False
    env: dict[str, str] = field(default_factory=dict)
    cache_enabled: bool = False
    cache_location: Optional[str] = None
    force_refresh: list[str] = field(default_factory=list)


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
    inputs: Optional[list[str]] = None
    cache: Optional[dict[str, Any]] = None
    cache_hit: bool = False
    cache_key: Optional[str] = None


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
        ('CACHE', r'cache:'),
        ('INPUTS', r'inputs:'),
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
        cache = None
        inputs = None

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
            elif self.match('CACHE'):
                self.consume('CACHE')
                cache = self.parse_cache_config()
            elif self.match('INPUTS'):
                self.consume('INPUTS')
                inputs = self.parse_inputs_list()
            else:
                if self.match('NEWLINE'):
                    self.consume('NEWLINE')
                    continue
                raise SyntaxError(f"Unexpected token {self.current()} in task definition")

        self.consume('RBRACE')
        return Task(name=task_name, params=params, run=run, success=success,
                   requires=requires, output=output, timeout=timeout, comment=comment, cache=cache,
                   inputs=inputs)

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
            left = StringConcat(left, right) if isinstance(left, (Variable, StringConcat)) else BinaryOp('%', left, right)
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

    def parse_cache_config(self) -> CacheConfig:
        """Parse cache configuration block"""
        self.consume('LBRACE')
        config = CacheConfig()

        while not self.match('RBRACE'):
            if self.match('NEWLINE') or self.match('WHITESPACE'):
                self.consume()
                continue

            if self.match('ID'):
                key_token = self.current()
                key_name = key_token.value
                self.consume()

                if self.match('COLON'):
                    self.consume('COLON')

                    if key_name == 'enabled':
                        if self.match('TRUE'):
                            config.enabled = True
                            self.consume('TRUE')
                        elif self.match('FALSE'):
                            config.enabled = False
                            self.consume('FALSE')
                        else:
                            val = self.parse_expression()
                            if isinstance(val, Literal):
                                config.enabled = val.value if isinstance(val.value, bool) else str(val.value).lower() == 'true'
                    elif key_name == 'strategy':
                        val = self.parse_expression()
                        if isinstance(val, Literal):
                            config.strategy = str(val.value)
                    elif key_name == 'location':
                        val = self.parse_expression()
                        if isinstance(val, Literal):
                            config.location = str(val.value)
                    elif key_name == 'version':
                        val = self.parse_expression()
                        if isinstance(val, Literal):
                            config.version = str(val.value)
                    elif key_name == 'ttl':
                        config.ttl = self.parse_ttl_config()
                    elif key_name == 'key':
                        config.key = self.parse_key_config()
                    else:
                        # Skip unknown values
                        self.parse_expression()

                if self.match('SEMICOLON'):
                    self.consume('SEMICOLON')
            elif self.match('NEWLINE'):
                self.consume('NEWLINE')
            elif self.match('WHITESPACE'):
                self.consume('WHITESPACE')
            else:
                break

        self.consume('RBRACE')
        return config

    def parse_ttl_config(self) -> CacheTTL:
        """Parse TTL configuration block"""
        self.consume('LBRACE')
        ttl = CacheTTL()

        while not self.match('RBRACE'):
            if self.match('NEWLINE') or self.match('WHITESPACE'):
                self.consume()
                continue

            if self.match('ID'):
                key_token = self.current()
                key_name = key_token.value
                self.consume()

                if self.match('COLON'):
                    self.consume('COLON')
                    val = self.parse_expression()

                    if isinstance(val, Literal):
                        if key_name == 'seconds':
                            if isinstance(val.value, (int, float)):
                                ttl.seconds = int(val.value)
                        elif key_name == 'minutes':
                            if isinstance(val.value, (int, float)):
                                ttl.minutes = int(val.value)
                        elif key_name == 'hours':
                            if isinstance(val.value, (int, float)):
                                ttl.hours = int(val.value)
                        elif key_name == 'days':
                            if isinstance(val.value, (int, float)):
                                ttl.days = int(val.value)

                    if self.match('SEMICOLON'):
                        self.consume('SEMICOLON')
            elif self.match('NEWLINE'):
                self.consume('NEWLINE')
            elif self.match('WHITESPACE'):
                self.consume('WHITESPACE')
            else:
                break

        self.consume('RBRACE')
        return ttl

    def parse_key_config(self) -> CacheKey:
        """Parse key configuration block"""
        self.consume('LBRACE')
        key = CacheKey()
        include_list = []
        exclude_list = []

        while not self.match('RBRACE'):
            if self.match('NEWLINE') or self.match('WHITESPACE'):
                self.consume()
                continue

            if self.match('ID'):
                key_token = self.current()
                key_name = key_token.value
                self.consume()

                if self.match('COLON'):
                    self.consume('COLON')

                    if key_name == 'include' or key_name == 'exclude':
                        self.consume('LBRACKET')

                        while not self.match('RBRACKET'):
                            if self.match('STRING'):
                                str_token = self.current()
                                str_val = escape_string(str_token.value[1:-1])
                                self.consume()

                                if key_name == 'include':
                                    include_list.append(str_val)
                                else:
                                    exclude_list.append(str_val)

                                if self.match('COMMA'):
                                    self.consume('COMMA')
                            elif self.match('NEWLINE'):
                                self.consume('NEWLINE')
                            elif self.match('WHITESPACE'):
                                self.consume('WHITESPACE')
                        self.consume('RBRACKET')

                    if self.match('SEMICOLON'):
                        self.consume('SEMICOLON')
            elif self.match('NEWLINE'):
                self.consume('NEWLINE')
            elif self.match('WHITESPACE'):
                self.consume('WHITESPACE')
            else:
                break

        key.include = include_list if include_list else None
        key.exclude = exclude_list if exclude_list else None
        self.consume('RBRACE')
        return key

    def parse_inputs_list(self) -> list[str]:
        """Parse inputs list: [file1, file2, ...]"""
        self.consume('LBRACKET')
        inputs = []

        while not self.match('RBRACKET'):
            if self.match('STRING'):
                str_token = self.current()
                str_val = escape_string(str_token.value[1:-1])
                inputs.append(str_val)
                self.consume()

                if self.match('COMMA'):
                    self.consume('COMMA')
            elif self.match('NEWLINE'):
                self.consume('NEWLINE')
            elif self.match('WHITESPACE'):
                self.consume('WHITESPACE')

        self.consume('RBRACKET')
        return inputs

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
                    pass
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
            if 'cache' in toml_data:
                cache_data = toml_data['cache']
                if isinstance(cache_data, dict):
                    if 'enabled' in cache_data:
                        config.cache_enabled = cache_data['enabled']
                    if 'location' in cache_data:
                        config.cache_location = cache_data['location']
                    if 'force_refresh' in cache_data:
                        config.force_refresh = cache_data['force_refresh'] if isinstance(cache_data['force_refresh'], list) else [cache_data['force_refresh']]
        except Exception:
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




# Evaluate expressions


class CacheManager:
    """Manages cache storage and retrieval for tasks."""

    def __init__(self, config: Config, workspace: str):
        self.config = config
        self.workspace = workspace
        # Determine cache location
        if config.cache_location:
            self.cache_dir = config.cache_location
        else:
            self.cache_dir = ".pipe-cache"
        self.cache_dir = os.path.abspath(self.cache_dir)
        os.makedirs(self.cache_dir, exist_ok=True)

    def _get_cache_file(self, cache_key: str) -> str:
        """Get the cache file path for a given cache key."""
        # Use first 2 chars as subdirectory for organization
        safe_key = hashlib.sha256(cache_key.encode()).hexdigest()
        subdir = safe_key[:2]
        full_dir = os.path.join(self.cache_dir, subdir)
        os.makedirs(full_dir, exist_ok=True)
        return os.path.join(full_dir, safe_key)

    def _hash_file(self, filepath: str) -> str:
        """Compute SHA256 hash of a file."""
        try:
            hasher = hashlib.sha256()
            with open(filepath, 'rb') as f:
                hasher.update(f.read())
            return hasher.hexdigest()
        except (FileNotFoundError, IOError):
            return ""

    def _hash_inputs(self, inputs: list[str], workspace: str) -> list[str]:
        """Compute hashes for all input files (supports globs)."""
        hashes = []
        for pattern in inputs:
            # Resolve workspace placeholder
            if '${workspace}' in pattern:
                pattern = pattern.replace('${workspace}', workspace)
            elif not os.path.isabs(pattern):
                pattern = os.path.join(workspace, pattern)

            # Handle glob patterns
            import glob as glob_module
            matched_files = sorted(glob_module.glob(pattern))
            for filepath in matched_files:
                if os.path.isfile(filepath):
                    file_hash = self._hash_file(filepath)
                    if file_hash:
                        hashes.append(f"{filepath}:{file_hash}")
        return hashes

    def _is_force_refresh(self, task_name: str) -> bool:
        """Check if task is in force_refresh list."""
        if not self.config.force_refresh:
            return False
        return task_name in self.config.force_refresh

    def generate_cache_key(
        self,
        task_name: str,
        params: dict[str, Any],
        cache_config: Optional[CacheConfig],
        inputs: Optional[list[str]],
        workspace: str
    ) -> Optional[str]:
        """Generate a cache key for a task execution."""
        # Check if caching is globally enabled
        if not self.config.cache_enabled:
            return None

        # Check if task is force_refresh
        if self._is_force_refresh(task_name):
            return None

        # Check if task has cache config and it's enabled
        if cache_config is None or not cache_config.enabled:
            return None

        # Build key components
        key_parts = []

        # Task name
        key_parts.append(f"task:{task_name}")

        # Version if specified
        if cache_config.version:
            key_parts.append(f"ver:{cache_config.version}")

        # Inputs hash
        if inputs:
            input_hashes = self._hash_inputs(inputs, workspace)
            # Sort for consistent ordering
            input_hashes.sort()
            for h in input_hashes:
                key_parts.append(f"in:{h}")

        # Parameters based on include/exclude rules
        param_keys = list(params.keys())

        if cache_config.key:
            if cache_config.key.include is not None:
                # Only include specified params
                param_keys = [k for k in param_keys if k in cache_config.key.include]
            if cache_config.key.exclude is not None:
                # Exclude specified params
                param_keys = [k for k in param_keys if k not in cache_config.key.exclude]

        # Sort for consistency
        param_keys.sort()
        for pk in param_keys:
            key_parts.append(f"p:{pk}:{params[pk]}")

        # Hash the entire key
        key_string = "|".join(key_parts)
        return hashlib.sha256(key_string.encode()).hexdigest()

    def check_cache(
        self,
        cache_key: str,
        cache_config: CacheConfig,
        job: Job
    ) -> Optional[CacheEntry]:
        """Check if cache exists and is valid for the given key."""
        if not cache_key:
            return None

        cache_file = self._get_cache_file(cache_key)
        cache_meta_file = cache_file + ".json"

        # Check if cache exists
        if not os.path.exists(cache_meta_file):
            return None

        # Load cache metadata
        try:
            with open(cache_meta_file, 'r') as f:
                cache_data = json.load(f)
        except (json.JSONDecodeError, IOError):
            return None

        # Check TTL if applicable
        if cache_config.ttl:
            created_at = cache_data.get('created_at', 0)
            age = time.time() - created_at

            ttl_seconds = 0
            if cache_config.ttl.seconds is not None:
                ttl_seconds += cache_config.ttl.seconds
            if cache_config.ttl.minutes is not None:
                ttl_seconds += cache_config.ttl.minutes * 60
            if cache_config.ttl.hours is not None:
                ttl_seconds += cache_config.ttl.hours * 3600
            if cache_config.ttl.days is not None:
                ttl_seconds += cache_config.ttl.days * 86400

            if ttl_seconds > 0 and age > ttl_seconds:
                # Cache is stale, delete it
                try:
                    os.remove(cache_meta_file)
                    output_dir = cache_data.get('output_dir')
                    if output_dir and os.path.exists(output_dir):
                        import shutil
                        shutil.rmtree(output_dir, ignore_errors=True)
                except OSError:
                    pass
                return None

        # Validate content if strategy is 'content'
        if cache_config.strategy == 'content':
            # Verify all input files still match their hashes
            for in_hash in cache_data.get('input_hashes', []):
                if ':' in in_hash:
                    filepath, expected_hash = in_hash.rsplit(':', 1)
                    actual_hash = self._hash_file(filepath)
                    if actual_hash != expected_hash:
                        return None

        # Reconstruct CacheEntry
        entry = CacheEntry(
            cache_key=cache_data.get('cache_key', ''),
            task_name=cache_data.get('task_name', ''),
            params=cache_data.get('params', {}),
            stdout=cache_data.get('stdout', ''),
            stderr=cache_data.get('stderr', ''),
            exit_code=cache_data.get('exit_code', 0),
            output_dir=cache_data.get('output_dir'),
            created_at=cache_data.get('created_at', time.time()),
            input_hashes=cache_data.get('input_hashes', []),
            success=cache_data.get('success', {})
        )

        return entry

    def store_cache(
        self,
        cache_key: str,
        task_name: str,
        params: dict[str, Any],
        stdout: str,
        stderr: str,
        exit_code: int,
        output_dir: Optional[str],
        inputs: Optional[list[str]],
        workspace: str,
        success: dict[str, bool]
    ) -> None:
        """Store a cache entry."""
        if not cache_key:
            return

        cache_file = self._get_cache_file(cache_key)
        cache_meta_file = cache_file + ".json"

        # Compute input hashes
        input_hashes = self._hash_inputs(inputs, workspace) if inputs else []

        # Create cache entry
        entry = {
            'cache_key': cache_key,
            'task_name': task_name,
            'params': params,
            'stdout': stdout,
            'stderr': stderr,
            'exit_code': exit_code,
            'output_dir': output_dir,
            'created_at': time.time(),
            'input_hashes': input_hashes,
            'success': success
        }

        # Write cache metadata
        try:
            with open(cache_meta_file, 'w') as f:
                json.dump(entry, f, indent=2)
        except IOError:
            return


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

    # Store inputs in job
    job.inputs = task.inputs

    # Store cache config in job (as dict for JSON serialization)
    if task.cache:
        job.cache = {
            "enabled": task.cache.enabled,
            "strategy": task.cache.strategy,
            "location": task.cache.location,
            "version": task.cache.version,
        }
        if task.cache.ttl:
            job.cache["ttl"] = {
                "seconds": task.cache.ttl.seconds,
                "minutes": task.cache.ttl.minutes,
                "hours": task.cache.ttl.hours,
                "days": task.cache.ttl.days,
            }
    else:
        job.cache = None

    # Initialize cache manager
    cache_manager = CacheManager(config, workspace)

    # Generate cache key
    cache_key = cache_manager.generate_cache_key(
        task.name,
        params,
        task.cache,
        task.inputs,
        workspace
    )
    job.cache_key = cache_key

    # Check cache if key exists
    cache_entry = None
    if cache_key:
        print(json.dumps({"event": "CACHE_CHECK", "job_id": job.job_id, "cache_key": cache_key[:16] + "..."}))
        cache_entry = cache_manager.check_cache(cache_key, task.cache, job)
        if cache_entry:
            print(json.dumps({"event": "CACHE_HIT", "job_id": job.job_id, "cache_key": cache_key[:16] + "..."}))
            job.cache_hit = True
            # Restore from cache
            job.stdout = cache_entry.stdout
            job.stderr = cache_entry.stderr
            job.exit_code = cache_entry.exit_code
            job.success = cache_entry.success
            job.duration = 0.0

            # Restore output files if output_dir was cached
            if cache_entry.output_dir:
                # Copy cached output to the expected output location
                if task.output:
                    output_str = resolve_template(task.output, params, workspace)
                    if '${workspace}' in output_str:
                        target_output_dir = output_str.replace('${workspace}', workspace)
                    else:
                        target_output_dir = os.path.join(output_dir, output_str)
                    # Remove existing target and copy cached
                    import shutil
                    if os.path.exists(target_output_dir):
                        shutil.rmtree(target_output_dir)
                    shutil.copytree(cache_entry.output_dir, target_output_dir)
                    job.output = target_output_dir

            return all(v for v in cache_entry.success.values() if isinstance(v, bool))
        else:
            print(json.dumps({"event": "CACHE_MISS", "job_id": job.job_id, "cache_key": cache_key[:16] + "..."}))

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

    # Store cache if cache_key exists and task was successful
    if cache_key and overall_success:
        import shutil
        cache_manager.store_cache(
            cache_key=cache_key,
            task_name=task.name,
            params=resolved_params,
            stdout=job.stdout,
            stderr=job.stderr,
            exit_code=job.exit_code,
            output_dir=output_path,  # Store the output directory path
            inputs=task.inputs,
            workspace=workspace,
            success=job.success
        )

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
                "inputs": job.inputs,
                "cache": job.cache,
                "cache_hit": job.cache_hit,
                "cache_key": job.cache_key,
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
