#!/usr/bin/env python3
"""
Pipeline execution CLI tool.
Executes pipelines from a pipeline file and an optional TOML config file.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import toml
import hashlib
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple, Union
from pathlib import Path
import shlex


# =============================================================================
# Data Structures
# =============================================================================

class TaskStatus(Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


@dataclass
class Param:
    name: str
    type: str  # "string", "int", "float", "bool", "list"
    default: Optional[Any] = None
    has_default: bool = False


@dataclass
class Task:
    name: str
    params: List[Param] = field(default_factory=list)
    run: List[str] = field(default_factory=list)  # commands
    success: Dict[str, 'Expression'] = field(default_factory=dict)
    requires: Optional['RequiresBlock'] = None
    output: Optional[str] = None
    timeout: Optional[float] = None
    inputs: Optional[List[str]] = None
    cache: Optional[CacheConfig] = None


@dataclass
class RequiresBlock:
    expressions: List['Expression'] = field(default_factory=list)


@dataclass
class TTL:
    seconds: Optional[int] = None
    minutes: Optional[int] = None
    hours: Optional[int] = None
    days: Optional[int] = None

    def to_seconds(self) -> Optional[int]:
        total = 0
        if self.seconds is not None:
            total += self.seconds
        if self.minutes is not None:
            total += self.minutes * 60
        if self.hours is not None:
            total += self.hours * 3600
        if self.days is not None:
            total += self.days * 86400
        return total if total > 0 else None


@dataclass
class CacheKey:
    include: List[str] = field(default_factory=list)
    exclude: List[str] = field(default_factory=list)


@dataclass
class CacheConfig:
    enabled: bool = False
    strategy: str = "content"
    location: str = ".pipe-cache"
    version: Optional[str] = None
    ttl: Optional[TTL] = None
    key: Optional[CacheKey] = None


@dataclass
class Job:
    job_id: int
    task: str
    params: Dict[str, Any]
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    success: Dict[str, bool] = field(default_factory=dict)
    output: Optional[str] = None
    exit_code: int = 0
    parent: Optional[int] = None
    duration: float = 0.0
    inputs: Optional[List[str]] = None
    cache: Optional[CacheConfig] = None
    cache_hit: bool = False
    cache_key: Optional[str] = None


class Expression:
    """Base class for expressions"""
    pass


@dataclass
class Literal(Expression):
    value: Any


@dataclass
class VariableRef(Expression):
    name: str


@dataclass
class BinaryOp(Expression):
    op: str
    left: Expression
    right: Expression


@dataclass
class UnaryOp(Expression):
    op: str
    operand: Expression


@dataclass
class FunctionCall(Expression):
    name: str
    args: List[Expression] = field(default_factory=list)


@dataclass
class ForLoop(Expression):
    var_name: str
    start: Expression
    end: Expression
    body: List[Expression]


@dataclass
class IfBlock(Expression):
    condition: Expression
    then_body: List[Expression]
    elif_blocks: List[Tuple[Expression, List[Expression]]] = field(default_factory=list)
    else_body: Optional[List[Expression]] = None


@dataclass
class Assignment(Expression):
    var_name: str
    value: Expression


@dataclass
class ReturnStmt(Expression):
    value: Expression


@dataclass
class BreakStmt(Expression):
    pass


@dataclass
class ContinueStmt(Expression):
    pass


@dataclass
class ExistsCall(Expression):
    path: Expression


@dataclass
class ContainsCall(Expression):
    file_or_stream: Expression
    pattern: Expression


@dataclass
class EqualsCall(Expression):
    left: Expression
    right: Expression


@dataclass
class FailCall(Expression):
    task_name: Expression


# =============================================================================
# Tokenizer
# =============================================================================

class TokenType(Enum):
    TASK = "TASK"
    IDENTIFIER = "IDENTIFIER"
    LBRACE = "LBRACE"
    RBRACE = "RBRACE"
    LBRACKET = "LBRACKET"
    RBRACKET = "RBRACKET"
    LPAREN = "LPAREN"
    RPAREN = "RPAREN"
    COLON = "COLON"
    COMMA = "COMMA"
    SEMICOLON = "SEMICOLON"
    ASSIGN = "ASSIGN"
    STRING = "STRING"
    NUMBER = "NUMBER"
    BOOL = "BOOL"
    PARAM_TYPE = "PARAM_TYPE"
    EQUALS = "EQUALS"
    PERCENT = "PERCENT"
    FOR = "FOR"
    WHILE = "WHILE"
    IF = "IF"
    ELIF = "ELIF"
    ELSE = "ELSE"
    RETURN = "RETURN"
    BREAK = "BREAK"
    CONTINUE = "CONTINUE"
    IN = "IN"
    DOT = "DOT"
    NOT = "NOT"
    AND = "AND"
    OR = "OR"
    LT = "LT"
    GT = "GT"
    LTE = "LTE"
    GTE = "GTE"
    EQ = "EQ"
    NEQ = "NEQ"
    PLUS = "PLUS"
    MINUS = "MINUS"
    MULT = "MULT"
    DIV = "DIV"
    MOD = "MOD"
    EOF = "EOF"


@dataclass
class Token:
    type: TokenType
    value: str
    line: int
    column: int


class Tokenizer:
    KEYWORDS = {
        "task": TokenType.TASK,
        "run": TokenType.IDENTIFIER,  # treated as identifier
        "success": TokenType.IDENTIFIER,
        "requires": TokenType.IDENTIFIER,
        "params": TokenType.IDENTIFIER,
        "output": TokenType.IDENTIFIER,
        "timeout": TokenType.IDENTIFIER,
        "for": TokenType.FOR,
        "while": TokenType.WHILE,
        "if": TokenType.IF,
        "elif": TokenType.ELIF,
        "else": TokenType.ELSE,
        "return": TokenType.RETURN,
        "break": TokenType.BREAK,
        "continue": TokenType.CONTINUE,
        "TRUE": TokenType.BOOL,
        "FALSE": TokenType.BOOL,
        "string": TokenType.PARAM_TYPE,
        "int": TokenType.PARAM_TYPE,
        "float": TokenType.PARAM_TYPE,
        "bool": TokenType.PARAM_TYPE,
        "list": TokenType.PARAM_TYPE,
    }

    def __init__(self, source: str):
        self.source = source
        self.pos = 0
        self.line = 1
        self.column = 1
        self.tokens: List[Token] = []

    def tokenize(self) -> List[Token]:
        while self.pos < len(self.source):
            char = self.source[self.pos]

            if char.isspace():
                self._handle_whitespace(char)
            elif char == '{':
                self._add_token(TokenType.LBRACE, '{')
            elif char == '}':
                self._add_token(TokenType.RBRACE, '}')
            elif char == '[':
                self._add_token(TokenType.LBRACKET, '[')
            elif char == ']':
                self._add_token(TokenType.RBRACKET, ']')
            elif char == '(':
                self._add_token(TokenType.LPAREN, '(')
            elif char == ')':
                self._add_token(TokenType.RPAREN, ')')
            elif char == ':':
                if self.pos + 1 < len(self.source) and self.source[self.pos + 1] == '=':
                    self._add_token(TokenType.EQUALS, ':=')
                    self.pos += 1
                    self.column += 1
                else:
                    self._add_token(TokenType.COLON, ':')
            elif char == ',':
                self._add_token(TokenType.COMMA, ',')
            elif char == ';':
                self._add_token(TokenType.SEMICOLON, ';')
            elif char == '%':
                self._add_token(TokenType.PERCENT, '%')
            elif char == '!':
                if self.pos + 1 < len(self.source) and self.source[self.pos + 1] == '=':
                    self._add_token(TokenType.NEQ, '!=')
                    self.pos += 1
                    self.column += 1
                else:
                    self._add_token(TokenType.NOT, '!')
            elif char == '<':
                if self.pos + 1 < len(self.source) and self.source[self.pos + 1] == '=':
                    self._add_token(TokenType.LTE, '<=')
                    self.pos += 1
                    self.column += 1
                else:
                    self._add_token(TokenType.LT, '<')
            elif char == '>':
                # Check for >> (double greater than) before >=
                if self.pos + 1 < len(self.source) and self.source[self.pos + 1] == '>':
                    self._add_token(TokenType.STRING, '>>')
                    self.pos += 1
                    self.column += 1
                elif self.pos + 1 < len(self.source) and self.source[self.pos + 1] == '=':
                    self._add_token(TokenType.GTE, '>=')
                    self.pos += 1
                    self.column += 1
                else:
                    self._add_token(TokenType.GT, '>')
            elif char == '=':
                if self.pos + 1 < len(self.source) and self.source[self.pos + 1] == '=':
                    self._add_token(TokenType.EQ, '==')
                    self.pos += 1
                    self.column += 1
                else:
                    self._add_token(TokenType.ASSIGN, '=')
            elif char == '+':
                self._add_token(TokenType.PLUS, '+')
            elif char == '-':
                self._add_token(TokenType.MINUS, '-')
            elif char == '*':
                self._add_token(TokenType.MULT, '*')
            elif char == '/':
                self._add_token(TokenType.DIV, '/')
            elif char == '"' or char == "'":
                self._handle_string(char)
            elif char == '$':
                self._handle_dollar()
            elif char.isalpha() or char == '_':
                self._handle_identifier()
            elif char.isdigit() or char == '.':
                self._handle_number()
            else:
                raise SyntaxError(f"Unexpected character '{char}' at line {self.line}")

        self.tokens.append(Token(TokenType.EOF, '', self.line, self.column))
        return self.tokens

    def _handle_whitespace(self, char: str):
        if char == '\n':
            self.line += 1
            self.column = 1
        else:
            self.column += 1
        self.pos += 1

    def _handle_string(self, quote: str):
        start = self.pos
        self.pos += 1
        self.column += 1
        result = []

        while self.pos < len(self.source):
            char = self.source[self.pos]
            if char == '\\':
                # Handle escape sequences
                self.pos += 1
                self.column += 1
                if self.pos >= len(self.source):
                    break
                escape_char = self.source[self.pos]
                escape_map = {
                    'n': '\n',
                    't': '\t',
                    'r': '\r',
                    '"': '"',
                    "'": "'",
                    '\\': '\\',
                }
                result.append(escape_map.get(escape_char, escape_char))
            elif char == quote:
                break
            else:
                result.append(char)
            self.pos += 1
            self.column += 1

        if self.pos >= len(self.source) or self.source[self.pos] != quote:
            raise SyntaxError(f"Unterminated string at line {self.line}")

        self.pos += 1
        self.column += 1
        value = ''.join(result)
        self.tokens.append(Token(TokenType.STRING, value, self.line, start))

    def _handle_identifier(self):
        start = self.pos
        while self.pos < len(self.source) and (self.source[self.pos].isalnum() or self.source[self.pos] == '_'):
            self.pos += 1
            self.column += 1

        value = self.source[start:self.pos]
        token_type = self.KEYWORDS.get(value.lower(), TokenType.IDENTIFIER)
        self.tokens.append(Token(token_type, value, self.line, start))

    def _handle_dollar(self):
        """Handle ${params.x} and ${workspace} patterns"""
        if self.pos + 1 < len(self.source) and self.source[self.pos + 1] == '{':
            # Found ${...}
            start = self.pos
            self.pos += 2  # Skip ${"
            self.column += 2
            var_name = []
            while self.pos < len(self.source) and self.source[self.pos] != '}':
                var_name.append(self.source[self.pos])
                self.pos += 1
                self.column += 1

            if self.pos >= len(self.source) or self.source[self.pos] != '}':
                raise SyntaxError("Unterminated ${ at line " + str(self.line))

            self.pos += 1  # Skip }"
            self.column += 1
            value = ''.join(var_name)
            self.tokens.append(Token(TokenType.STRING, value, self.line, start))
        else:
            # Single $ - treat as literal
            self._add_token(TokenType.STRING, '$')

    def _handle_number(self):
        start = self.pos
        has_dot = False

        while self.pos < len(self.source) and (self.source[self.pos].isdigit() or self.source[self.pos] == '.'):
            if self.source[self.pos] == '.':
                if has_dot:
                    raise SyntaxError(f"Multiple decimal points at line {self.line}")
                has_dot = True
            self.pos += 1
            self.column += 1

        value = self.source[start:self.pos]
        self.tokens.append(Token(TokenType.NUMBER, value, self.line, start))

    def _add_token(self, token_type: TokenType, value: str):
        self.tokens.append(Token(token_type, value, self.line, self.column))
        self.pos += 1
        self.column += 1


# =============================================================================
# Parser
# =============================================================================

class Parser:
    def __init__(self, tokens: List[Token]):
        self.tokens = tokens
        self.pos = 0

    def parse(self) -> Dict[str, Task]:
        tasks = {}

        while self.pos < len(self.tokens) and self.tokens[self.pos].type != TokenType.EOF:
            if self.check(TokenType.TASK):
                task = self.parse_task()
                tasks[task.name] = task
            else:
                self.raise_error(f"Expected 'task', got {self.current().value}")

        return tasks

    def parse_task(self) -> Task:
        self.expect(TokenType.TASK)
        name = self.expect(TokenType.IDENTIFIER).value
        self.expect(TokenType.LBRACE)

        params = []
        run = []
        success: Dict[str, Expression] = {}
        requires: Optional[RequiresBlock] = None
        output = None
        timeout = None
        inputs = None
        cache = None

        while not self.check(TokenType.RBRACE) and not self.check(TokenType.EOF):
            if self.match(TokenType.IDENTIFIER):
                field_name = self.current().value

                # Check if there's a colon after (optional)
                has_colon = self.match(TokenType.COLON)

                if field_name == 'params':
                    params = self.parse_params()
                elif field_name == 'run':
                    run = self.parse_run_block()
                elif field_name == 'success':
                    success = self.parse_success()
                elif field_name == 'requires':
                    requires = self.parse_requires()
                elif field_name == 'output':
                    output = self.parse_string_or_expr_value()
                elif field_name == 'timeout':
                    timeout = self.parse_number_value()
                elif field_name == 'inputs':
                    inputs = self.parse_inputs()
                elif field_name == 'cache':
                    cache = self.parse_cache()
                else:
                    self.raise_error(f"Unknown field '{field_name}'")
            else:
                self.raise_error(f"Expected identifier, got {self.current().value}")

        self.expect(TokenType.RBRACE)
        return Task(name=name, params=params, run=run, success=success,
                   requires=requires, output=output, timeout=timeout,
                   inputs=inputs, cache=cache)

    def parse_params(self) -> List[Param]:
        params = []
        self.expect(TokenType.LBRACE)

        while not self.check(TokenType.RBRACE) and not self.check(TokenType.EOF):
            if self.match(TokenType.IDENTIFIER):
                name = self.current().value
                if self.match(TokenType.COLON):
                    # x: int syntax
                    type_token = self.expect(TokenType.PARAM_TYPE)
                    type_name = type_token.value
                    default = None
                    has_default = False

                    # Check for optional = default
                    if self.match(TokenType.ASSIGN):
                        has_default = True
                        default = self.parse_default_value(type_name)

                    params.append(Param(name=name, type=type_name, default=default,
                                      has_default=has_default))
                else:
                    self.raise_error("Expected ':' after parameter name")
            elif self.match(TokenType.COMMA):
                continue
            else:
                break

        self.expect(TokenType.RBRACE)
        return params

    def parse_default_value(self, expected_type: str) -> Any:
        if self.match(TokenType.STRING):
            val = self.current().value
            if expected_type not in ['string', 'list']:
                self.raise_error(f"String value not expected for type {expected_type}")
            return val
        elif self.match(TokenType.NUMBER):
            val = self.current().value
            if expected_type == 'int':
                return int(float(val))
            elif expected_type == 'float':
                return float(val)
            else:
                self.raise_error(f"Number value not expected for type {expected_type}")
        elif self.match(TokenType.BOOL):
            val = self.current().value == 'TRUE'
            if expected_type != 'bool':
                self.raise_error(f"Bool value not expected for type {expected_type}")
            return val
        elif self.match(TokenType.LBRACKET):
            # Parse list
            items = []
            while not self.check(TokenType.RBRACKET):
                if self.match(TokenType.STRING):
                    items.append(self.current().value)
                elif self.match(TokenType.NUMBER):
                    items.append(self.current().value)
                elif self.match(TokenType.BOOL):
                    items.append(self.current().value == 'TRUE')
                else:
                    self.raise_error("Invalid list element")
                if not self.match(TokenType.COMMA):
                    break
            self.expect(TokenType.RBRACKET)
            return items
        else:
            self.raise_error(f"Unexpected default value: {self.current().value}")

    def parse_run_block(self) -> List[str]:
        """Parse run block content - just collect strings"""
        self.expect(TokenType.LBRACE)
        lines = []

        while not self.check(TokenType.RBRACE) and not self.check(TokenType.EOF):
            if self.match(TokenType.STRING):
                lines.append(self.current().value)
            elif self.match(TokenType.IDENTIFIER):
                # Handle identifiers that might be part of command
                lines.append(self.current().value)
            else:
                self.advance()

        self.expect(TokenType.RBRACE)
        return lines

    def parse_string_or_expr_value(self) -> str:
        """Parse a string or expression value"""
        if self.match(TokenType.STRING):
            return self.current().value
        self.raise_error("Expected string value")

    def parse_success(self) -> Dict[str, Expression]:
        success = {}
        self.expect(TokenType.LBRACE)

        while not self.check(TokenType.RBRACE) and not self.check(TokenType.EOF):
            name = self.expect(TokenType.IDENTIFIER).value
            self.expect(TokenType.LBRACE)

            # Parse block of expressions
            expressions = []
            while not self.check(TokenType.RBRACE) and not self.check(TokenType.EOF):
                expr = self.parse_expression()
                expressions.append(expr)
                if self.match(TokenType.SEMICOLON):
                    pass

            success[name] = self._make_block_expression(expressions)
            self.expect(TokenType.RBRACE)

        self.expect(TokenType.RBRACE)
        return success

    def _make_block_expression(self, expressions: List[Expression]) -> Expression:
        """Combine multiple expressions in a block"""
        if len(expressions) == 0:
            return Literal(True)
        # Last expression is the return value
        return expressions[-1]

    def parse_requires(self) -> RequiresBlock:
        self.expect(TokenType.LBRACE)
        expressions = []

        while not self.check(TokenType.RBRACE) and not self.check(TokenType.EOF):
            expr = self.parse_expression()
            expressions.append(expr)
            if self.match(TokenType.SEMICOLON):
                pass
            elif not self.check(TokenType.RBRACE):
                pass

        self.expect(TokenType.RBRACE)
        return RequiresBlock(expressions=expressions)

    def parse_inputs(self) -> List[str]:
        """Parse inputs block - list of input file paths"""
        self.expect(TokenType.LBRACKET)
        inputs = []

        while not self.check(TokenType.RBRACKET) and not self.check(TokenType.EOF):
            if self.match(TokenType.STRING):
                inputs.append(self.current().value)
            elif self.match(TokenType.COMMA):
                continue
            else:
                self.raise_error(f"Expected string in inputs, got {self.current().value}")

        self.expect(TokenType.RBRACKET)
        return inputs

    def parse_cache(self) -> CacheConfig:
        """Parse cache configuration block"""
        self.expect(TokenType.LBRACE)

        enabled = False
        strategy = "content"
        location = ".pipe-cache"
        version = None
        ttl = None
        key = None

        while not self.check(TokenType.RBRACE) and not self.check(TokenType.EOF):
            if self.match(TokenType.IDENTIFIER):
                field_name = self.current().value

                if field_name == 'enabled':
                    self.expect(TokenType.COLON)
                    if self.match(TokenType.STRING):
                        enabled = self.current().value.lower() == 'true'
                    elif self.match(TokenType.NUMBER):
                        enabled = self.current().value != '0'
                    elif self.match(TokenType.BOOL):
                        enabled = self.current().value == 'TRUE'
                    else:
                        self.raise_error(f"Expected boolean for enabled, got {self.current().value}")
                elif field_name == 'strategy':
                    self.expect(TokenType.COLON)
                    if self.match(TokenType.STRING):
                        strategy = self.current().value
                    else:
                        self.raise_error(f"Expected string for strategy, got {self.current().value}")
                elif field_name == 'location':
                    self.expect(TokenType.COLON)
                    if self.match(TokenType.STRING):
                        location = self.current().value
                    else:
                        self.raise_error(f"Expected string for location, got {self.current().value}")
                elif field_name == 'version':
                    self.expect(TokenType.COLON)
                    if self.match(TokenType.STRING):
                        version = self.current().value
                    else:
                        self.raise_error(f"Expected string for version, got {self.current().value}")
                elif field_name == 'ttl':
                    self.expect(TokenType.COLON)
                    ttl = self.parse_ttl()
                elif field_name == 'key':
                    self.expect(TokenType.COLON)
                    key = self.parse_cache_key()
                else:
                    self.raise_error(f"Unknown cache field '{field_name}'")
            else:
                self.advance()

        self.expect(TokenType.RBRACE)
        return CacheConfig(
            enabled=enabled,
            strategy=strategy,
            location=location,
            version=version,
            ttl=ttl,
            key=key
        )

    def parse_ttl(self) -> TTL:
        """Parse TTL configuration block"""
        self.expect(TokenType.LBRACE)

        ttl = TTL()

        while not self.check(TokenType.RBRACE) and not self.check(TokenType.EOF):
            if self.match(TokenType.IDENTIFIER):
                field_name = self.current().value

                if field_name == 'seconds':
                    self.expect(TokenType.COLON)
                    if self.match(TokenType.NUMBER):
                        ttl.seconds = int(float(self.current().value))
                    else:
                        self.raise_error(f"Expected number for seconds, got {self.current().value}")
                elif field_name == 'minutes':
                    self.expect(TokenType.COLON)
                    if self.match(TokenType.NUMBER):
                        ttl.minutes = int(float(self.current().value))
                    else:
                        self.raise_error(f"Expected number for minutes, got {self.current().value}")
                elif field_name == 'hours':
                    self.expect(TokenType.COLON)
                    if self.match(TokenType.NUMBER):
                        ttl.hours = int(float(self.current().value))
                    else:
                        self.raise_error(f"Expected number for hours, got {self.current().value}")
                elif field_name == 'days':
                    self.expect(TokenType.COLON)
                    if self.match(TokenType.NUMBER):
                        ttl.days = int(float(self.current().value))
                    else:
                        self.raise_error(f"Expected number for days, got {self.current().value}")
                else:
                    self.raise_error(f"Unknown TTL field '{field_name}'")
            else:
                self.advance()

        self.expect(TokenType.RBRACE)
        return ttl

    def parse_cache_key(self) -> CacheKey:
        """Parse cache key configuration block"""
        self.expect(TokenType.LBRACE)

        key = CacheKey()

        while not self.check(TokenType.RBRACE) and not self.check(TokenType.EOF):
            if self.match(TokenType.IDENTIFIER):
                field_name = self.current().value

                if field_name == 'include':
                    self.expect(TokenType.COLON)
                    key.include = self.parse_string_list()
                elif field_name == 'exclude':
                    self.expect(TokenType.COLON)
                    key.exclude = self.parse_string_list()
                else:
                    self.raise_error(f"Unknown cache key field '{field_name}'")
            else:
                self.advance()

        self.expect(TokenType.RBRACE)
        return key

    def parse_string_list(self) -> List[str]:
        """Parse a list of strings"""
        self.expect(TokenType.LBRACKET)
        items = []

        while not self.check(TokenType.RBRACKET) and not self.check(TokenType.EOF):
            if self.match(TokenType.STRING):
                items.append(self.current().value)
            elif self.match(TokenType.COMMA):
                continue
            else:
                self.raise_error(f"Expected string in list, got {self.current().value}")

        self.expect(TokenType.RBRACKET)
        return items

    def parse_expression(self) -> Expression:
        if self.match(TokenType.FOR):
            return self.parse_for_loop()
        elif self.match(TokenType.WHILE):
            return self.parse_while_loop()
        elif self.match(TokenType.IF):
            return self.parse_if_block()
        elif self.match(TokenType.RETURN):
            return self.parse_return()
        elif self.match(TokenType.BREAK):
            return BreakStmt()
        elif self.match(TokenType.CONTINUE):
            return ContinueStmt()
        elif self.match(TokenType.IDENTIFIER):
            name = self.current().value
            if self.match(TokenType.LPAREN):
                return self.parse_function_call(name)
            elif self.match(TokenType.ASSIGN):
                value = self.parse_expression()
                return Assignment(var_name=name, value=value)
            else:
                # Could be a function call without parens or a variable reference
                if self.match(TokenType.DOT):
                    return self.parse_function_call(name)
                return VariableRef(name=name)
        elif self.match(TokenType.LBRACE):
            # Block of expressions
            expressions = []
            while not self.check(TokenType.RBRACE):
                expr = self.parse_expression()
                expressions.append(expr)
                self.match(TokenType.SEMICOLON)
            self.expect(TokenType.RBRACE)
            return self._make_block_expression(expressions)
        elif self.match(TokenType.NOT):
            operand = self.parse_expression()
            return UnaryOp(op='!', operand=operand)
        elif self.match(TokenType.LPAREN):
            expr = self.parse_expression()
            self.expect(TokenType.RPAREN)
            return expr
        else:
            # Could be binary operation
            return self.parse_binary_op()

    def parse_for_loop(self) -> ForLoop:
        self.expect(TokenType.LPAREN)
        var_name = self.expect(TokenType.IDENTIFIER).value
        self.expect(TokenType.IDENTIFIER)  # '='
        self.expect(TokenType.NUMBER)  # start value
        start_val = self.current().value
        self.expect(TokenType.SEMICOLON)
        cond = self.parse_expression()
        self.expect(TokenType.SEMICOLON)
        self.expect(TokenType.NUMBER)  # increment value
        increment_val = self.current().value
        self.expect(TokenType.RPAREN)
        self.expect(TokenType.LBRACE)

        body = []
        while not self.check(TokenType.RBRACE):
            body.append(self.parse_expression())
            self.match(TokenType.SEMICOLON)

        self.expect(TokenType.RBRACE)

        # Simplified: just track the pattern
        start_expr = Literal(int(start_val))
        end_expr = Literal(int(increment_val))  # This should be the end condition
        return ForLoop(var_name=var_name, start=start_expr, end=end_expr, body=body)

    def parse_while_loop(self) -> Expression:
        self.expect(TokenType.LPAREN)
        cond = self.parse_expression()
        self.expect(TokenType.RPAREN)
        self.expect(TokenType.LBRACE)

        body = []
        while not self.check(TokenType.RBRACE):
            body.append(self.parse_expression())
            self.match(TokenType.SEMICOLON)

        self.expect(TokenType.RBRACE)
        return ForLoop(var_name="", start=cond, end=None, body=body)  # Simplified

    def parse_if_block(self) -> IfBlock:
        self.expect(TokenType.LPAREN)
        cond = self.parse_expression()
        self.expect(TokenType.RPAREN)
        self.expect(TokenType.LBRACE)

        then_body = []
        while not self.check(TokenType.RBRACE) and not self.check(TokenType.ELIF):
            then_body.append(self.parse_expression())
            self.match(TokenType.SEMICOLON)

        self.expect(TokenType.RBRACE)

        elif_blocks = []
        else_body = None

        while self.match(TokenType.ELIF):
            self.expect(TokenType.LPAREN)
            elif_cond = self.parse_expression()
            self.expect(TokenType.RPAREN)
            self.expect(TokenType.LBRACE)

            elif_body = []
            while not self.check(TokenType.RBRACE):
                elif_body.append(self.parse_expression())
                self.match(TokenType.SEMICOLON)

            self.expect(TokenType.RBRACE)
            elif_blocks.append((elif_cond, elif_body))

        if self.match(TokenType.ELSE):
            self.expect(TokenType.LBRACE)
            else_body = []
            while not self.check(TokenType.RBRACE):
                else_body.append(self.parse_expression())
                self.match(TokenType.SEMICOLON)
            self.expect(TokenType.RBRACE)

        return IfBlock(condition=cond, then_body=then_body, elif_blocks=elif_blocks,
                      else_body=else_body)

    def parse_return(self) -> ReturnStmt:
        value = self.parse_expression()
        return ReturnStmt(value=value)

    def parse_function_call(self, name: str) -> Expression:
        args = []
        if not self.check(TokenType.RPAREN):
            while True:
                # Handle named arguments: x=10
                if self.match(TokenType.IDENTIFIER):
                    arg_name = self.current().value
                    if self.match(TokenType.EQUALS):
                        arg_value = self.parse_expression()
                        args.append((arg_name, arg_value))
                    else:
                        args.append(VariableRef(name=arg_name))
                else:
                    arg_value = self.parse_expression()
                    args.append(arg_value)

                if not self.match(TokenType.COMMA):
                    break

        self.expect(TokenType.RPAREN)

        if name in ['exists', 'contains', 'equals']:
            if len(args) < 2:
                self.raise_error(f"Function {name} requires at least 2 arguments")
            if name == 'exists':
                return ExistsCall(path=args[0])
            elif name == 'contains':
                return ContainsCall(file_or_stream=args[0], pattern=args[1])
            elif name == 'equals':
                return EqualsCall(left=args[0], right=args[1])
        elif name == 'fail':
            if len(args) != 1:
                self.raise_error("fail() requires exactly 1 argument")
            return FailCall(task_name=args[0])

        # Generic function call
        return FunctionCall(name=name, args=[a if not isinstance(a, tuple) else a[1] for a in args])

    def parse_binary_op(self) -> Expression:
        left = self.parse_term()

        while True:
            op_type = self.current().type
            if op_type in [TokenType.PLUS, TokenType.MINUS, TokenType.MULT, TokenType.DIV,
                          TokenType.EQ, TokenType.NEQ, TokenType.LT, TokenType.GT,
                          TokenType.LTE, TokenType.GTE, TokenType.AND, TokenType.OR,
                          TokenType.PERCENT]:
                op = self.current().value
                self.advance()
                right = self.parse_term()
                left = BinaryOp(op=op, left=left, right=right)
            else:
                break

        return left

    def parse_term(self) -> Expression:
        if self.match(TokenType.STRING):
            return Literal(value=self.current().value)
        elif self.match(TokenType.NUMBER):
            val = self.current().value
            if '.' in val:
                return Literal(value=float(val))
            return Literal(value=int(val))
        elif self.match(TokenType.BOOL):
            return Literal(value=self.current().value == 'TRUE')
        elif self.match(TokenType.LBRACKET):
            items = []
            while not self.check(TokenType.RBRACKET):
                if self.match(TokenType.STRING):
                    items.append(self.current().value)
                elif self.match(TokenType.NUMBER):
                    items.append(float(self.current().value) if '.' in self.current().value else int(self.current().value))
                elif self.match(TokenType.BOOL):
                    items.append(self.current().value == 'TRUE')
                if self.match(TokenType.COMMA):
                    pass
            self.expect(TokenType.RBRACKET)
            return Literal(value=items)
        elif self.match(TokenType.LPAREN):
            expr = self.parse_expression()
            self.expect(TokenType.RPAREN)
            return expr
        elif self.match(TokenType.IDENTIFIER):
            name = self.current().value
            if self.match(TokenType.DOT):
                # Handle field access like stdout, stderr
                field = self.expect(TokenType.IDENTIFIER).value
                return BinaryOp(op='.', left=VariableRef(name=name), right=Literal(value=field))
            return VariableRef(name=name)
        elif self.match(TokenType.MINUS):
            operand = self.parse_term()
            return UnaryOp(op='-', operand=operand)
        else:
            self.raise_error(f"Unexpected token: {self.current().value}")

    def parse_string_value(self) -> str:
        if self.match(TokenType.STRING):
            return self.current().value
        self.raise_error("Expected string value")

    def parse_number_value(self) -> float:
        if self.match(TokenType.NUMBER):
            return float(self.current().value)
        self.raise_error("Expected number value")

    # Helper methods
    def current(self) -> Token:
        return self.tokens[self.pos]

    def check(self, token_type: TokenType) -> bool:
        return self.tokens[self.pos].type == token_type

    def match(self, token_type: TokenType) -> bool:
        if self.check(token_type):
            self.advance()
            return True
        return False

    def expect(self, token_type: TokenType) -> Token:
        token = self.current()
        if token.type != token_type:
            self.raise_error(f"Expected {token_type}, got {token.type}: {token.value}")
        self.advance()
        return token

    def advance(self):
        if self.pos < len(self.tokens) - 1:
            self.pos += 1

    def raise_error(self, msg: str):
        token = self.current()
        raise SyntaxError(f"Line {token.line}: {msg}")


# =============================================================================
# Config Parser
# =============================================================================

@dataclass
class CacheSettings:
    enabled: bool = False
    location: str = ".pipe-cache"
    force_refresh: List[str] = field(default_factory=list)


@dataclass
class Config:
    entry: Optional[str] = None
    clean_cwd: bool = False
    env: Dict[str, str] = field(default_factory=dict)
    cache: CacheSettings = field(default_factory=CacheSettings)


def parse_config(path: Optional[str]) -> Config:
    if path is None or not os.path.exists(path):
        return Config()

    with open(path, 'r') as f:
        data = toml.load(f)

    config = Config()

    if 'entry' in data:
        config.entry = str(data['entry'])

    if 'clean_cwd' in data:
        config.clean_cwd = bool(data['clean_cwd'])

    if 'env' in data:
        config.env = {k: str(v) for k, v in data['env'].items()}

    if 'cache' in data:
        cache_data = data['cache']
        if isinstance(cache_data, dict):
            cache_settings = CacheSettings()
            if 'enabled' in cache_data:
                cache_settings.enabled = bool(cache_data['enabled'])
            if 'location' in cache_data:
                cache_settings.location = str(cache_data['location'])
            if 'force_refresh' in cache_data:
                force_refresh = cache_data['force_refresh']
                if isinstance(force_refresh, list):
                    cache_settings.force_refresh = [str(t) for t in force_refresh]
            config.cache = cache_settings

    return config


# =============================================================================
# Expression Evaluator
# =============================================================================

@dataclass
class EvaluationContext:
    params: Dict[str, Any]
    variables: Dict[str, Any] = field(default_factory=dict)
    workspace: str = ""
    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0
    file_exists_func = None


class ExpressionEvaluator:
    def __init__(self, context: EvaluationContext):
        self.context = context

    def evaluate(self, expr: Expression) -> Any:
        if isinstance(expr, Literal):
            return expr.value
        elif isinstance(expr, VariableRef):
            if expr.name in self.context.variables:
                return self.context.variables[expr.name]
            elif expr.name == 'stdout':
                return self.context.stdout
            elif expr.name == 'stderr':
                return self.context.stderr
            elif expr.name == 'exit_code':
                return self.context.exit_code
            raise ValueError(f"Undefined variable: {expr.name}")
        elif isinstance(expr, BinaryOp):
            return self._eval_binary_op(expr)
        elif isinstance(expr, UnaryOp):
            left = self.evaluate(expr.operand)
            if expr.op == '!':
                return not left
            elif expr.op == '-':
                return -left
        elif isinstance(expr, ForLoop):
            return self._eval_for_loop(expr)
        elif isinstance(expr, IfBlock):
            return self._eval_if_block(expr)
        elif isinstance(expr, ReturnStmt):
            return self.evaluate(expr.value)
        elif isinstance(expr, Assignment):
            value = self.evaluate(expr.value)
            self.context.variables[expr.var_name] = value
            return value
        elif isinstance(expr, FunctionCall):
            return self._eval_function_call(expr)
        elif isinstance(expr, ExistsCall):
            path = self.evaluate(expr.path)
            return os.path.exists(path)
        elif isinstance(expr, ContainsCall):
            content = self.evaluate(expr.file_or_stream)
            pattern = self.evaluate(expr.pattern)
            return pattern in content
        elif isinstance(expr, EqualsCall):
            left = self.evaluate(expr.left)
            right = self.evaluate(expr.right)
            return left == right
        elif isinstance(expr, FailCall):
            # Special handling for fail() - just return True (placeholder)
            return True

        raise ValueError(f"Unknown expression type: {type(expr)}")

    def _eval_binary_op(self, expr: BinaryOp) -> Any:
        left = self.evaluate(expr.left)
        right = self.evaluate(expr.right)

        if expr.op == '+':
            return left + right
        elif expr.op == '-':
            return left - right
        elif expr.op == '*':
            return left * right
        elif expr.op == '/':
            return left / right
        elif expr.op == '%':
            return str(left) + str(right)  # String concatenation
        elif expr.op == '==':
            return left == right
        elif expr.op == '!=':
            return left != right
        elif expr.op == '<':
            return left < right
        elif expr.op == '>':
            return left > right
        elif expr.op == '<=':
            return left <= right
        elif expr.op == '>=':
            return left >= right
        elif expr.op == 'and':
            return left and right
        elif expr.op == 'or':
            return left or right

        raise ValueError(f"Unknown operator: {expr.op}")

    def _eval_for_loop(self, expr: ForLoop) -> Any:
        result = None
        if expr.start is not None and expr.end is not None:
            start = self.evaluate(expr.start) if isinstance(expr.start, Literal) else 0
            end = self.evaluate(expr.end) if isinstance(expr.end, Literal) else 10
            for i in range(start, end):
                self.context.variables[expr.var_name] = i
                for stmt in expr.body:
                    if isinstance(stmt, ReturnStmt):
                        result = self.evaluate(stmt)
                    elif isinstance(stmt, BreakStmt):
                        return result
                    elif isinstance(stmt, ContinueStmt):
                        continue
                    else:
                        self.evaluate(stmt)
        return result or True

    def _eval_if_block(self, expr: IfBlock) -> Any:
        cond = self.evaluate(expr.condition)
        if cond:
            result = None
            for stmt in expr.then_body:
                if isinstance(stmt, ReturnStmt):
                    result = self.evaluate(stmt)
                else:
                    self.evaluate(stmt)
            return result

        for elif_cond, elif_body in expr.elif_blocks:
            if self.evaluate(elif_cond):
                result = None
                for stmt in elif_body:
                    if isinstance(stmt, ReturnStmt):
                        result = self.evaluate(stmt)
                    else:
                        self.evaluate(stmt)
                return result

        if expr.else_body:
            result = None
            for stmt in expr.else_body:
                if isinstance(stmt, ReturnStmt):
                    result = self.evaluate(stmt)
                else:
                    self.evaluate(stmt)
            return result

        return False

    def _eval_function_call(self, expr: FunctionCall) -> Any:
        evalled_args = [self.evaluate(arg) if not isinstance(arg, tuple) else
                       self.evaluate(arg[1]) for arg in expr.args]
        return evalled_args[0] if evalled_args else None


# =============================================================================
# Pipeline Executor
# =============================================================================

class PipelineExecutor:
    def __init__(self, tasks: Dict[str, Task], config: Config, workspace: str,
                 output_dir: str):
        self.tasks = tasks
        self.config = config
        self.workspace = os.path.abspath(workspace)
        self.output_dir = os.path.abspath(output_dir)
        self.jobs: List[Job] = []
        self.job_counter = 0
        self.completed_tasks: set[str] = set()

    def resolve_params(self, task_name: str, call_params: Dict[str, Any]) -> Dict[str, Any]:
        """Resolve parameters with defaults and type checking"""
        task = self.tasks[task_name]
        resolved = {}
        seen_default = False

        for param in task.params:
            if param.name in call_params:
                if seen_default:
                    raise ValueError(f"Parameter {param.name} cannot follow a parameter with default")
                resolved[param.name] = call_params[param.name]
            elif param.has_default:
                resolved[param.name] = param.default
            else:
                raise ValueError(f"Missing required parameter: {param.name}")

        return resolved

    def substitute(self, text: str, params: Dict[str, Any]) -> str:
        """Substitute ${params.x} and ${workspace} in text"""
        result = text

        # Substitute params
        for key, value in params.items():
            pattern = r'\$\{params\.' + re.escape(key) + r'\}'
            result = re.sub(pattern, str(value), result)

        # Substitute workspace
        result = result.replace('${workspace}', self.workspace)

        return result

    def _is_task_force_refresh(self, task_name: str) -> bool:
        """Check if this task is in the force_refresh list"""
        if self.config.cache and task_name in self.config.cache.force_refresh:
            return True
        return False

    def _is_cache_enabled(self, task_cache: Optional[CacheConfig]) -> bool:
        """Check if caching is enabled globally and for the task"""
        if not self.config.cache or not self.config.cache.enabled:
            return False
        if not task_cache or not task_cache.enabled:
            return False
        return True

    def _generate_cache_key(self, task: Task, params: Dict[str, Any], task_cache: CacheConfig) -> str:
        """Generate cache key based on task name, params and inputs"""
        # Determine which params to include in the key
        include_params = set()
        exclude_params = set()

        if task_cache.key:
            include_params = set(task_cache.key.include)
            exclude_params = set(task_cache.key.exclude)

        # If include is specified, use only those params
        if include_params:
            params_for_key = {k: v for k, v in params.items() if k in include_params}
        else:
            params_for_key = {k: v for k, v in params.items() if k not in exclude_params}

        # Hash task name
        hash_input = f"{task.name}:"

        # Include version if specified
        if task_cache.version:
            hash_input += f"v{task_cache.version}:"

        # Include sorted params
        params_str = '&'.join(f"{k}={v}" for k, v in sorted(params_for_key.items()))
        hash_input += params_str + ":"

        # Include inputs if present
        if task.inputs:
            # Compute hash of each input file content
            input_hashes = []
            for input_pattern in task.inputs:
                # Expand any variables in the pattern
                input_path = self.substitute(input_pattern, params)
                # Handle glob patterns
                if '*' in input_path:
                    import glob
                    matched_files = sorted(glob.glob(input_path))
                else:
                    if os.path.exists(input_path):
                        matched_files = [input_path]
                    else:
                        matched_files = []

                for filepath in matched_files:
                    if os.path.isfile(filepath):
                        file_hash = self._compute_file_hash(filepath)
                        input_hashes.append(f"{filepath}@{file_hash}")

            hash_input += ",".join(input_hashes) + ":"

        return hashlib.sha256(hash_input.encode('utf-8')).hexdigest()

    def _compute_file_hash(self, filepath: str) -> str:
        """Compute MD5 hash of a file"""
        hash_md5 = hashlib.md5()
        try:
            with open(filepath, "rb") as f:
                for chunk in iter(lambda: f.read(4096), b""):
                    hash_md5.update(chunk)
            return hash_md5.hexdigest()
        except Exception:
            return ""

    def _validate_cache_by_content(self, cache_entry: Dict, task: Task, params: Dict[str, Any],
                                    task_cache: CacheConfig) -> bool:
        """Validate cache by checking if input files are unchanged"""
        if not cache_entry.get('input_hashes'):
            return True  # No inputs to check

        for input_pattern in task.inputs or []:
            input_path = self.substitute(input_pattern, params)
            if '*' in input_path:
                import glob
                matched_files = sorted(glob.glob(input_path))
            else:
                if os.path.exists(input_path):
                    matched_files = [input_path]
                else:
                    matched_files = []

            for filepath in matched_files:
                if os.path.isfile(filepath):
                    current_hash = self._compute_file_hash(filepath)
                    expected_key = filepath
                    if expected_key in cache_entry.get('input_hashes', {}):
                        if cache_entry['input_hashes'][expected_key] != current_hash:
                            return False

        return True

    def _validate_cache_by_stale(self, cache_entry: Dict, task_cache: CacheConfig) -> bool:
        """Validate cache by checking if it's older than TTL"""
        if not cache_entry.get('timestamp'):
            return True
        ttl_seconds = None
        if task_cache.ttl:
            ttl_seconds = task_cache.ttl.to_seconds()
        if ttl_seconds:
            age = time.time() - cache_entry['timestamp']
            return age < ttl_seconds
        return True

    def _check_cache(self, task: Task, params: Dict[str, Any]) -> tuple[Optional[bool], Optional[str], Optional[Dict]]:
        """
        Check cache for a task result
        Returns: (hit: bool, cache_key: str, cache_entry: Dict)
        hit=True means cache hit, hit=False means cache miss, hit=None means no cache
        """
        if not self._is_cache_enabled(task.cache):
            return (None, None, None)

        # Skip if force_refresh
        if self._is_task_force_refresh(task.name):
            return (None, None, None)

        assert task.cache is not None
        cache_key = self._generate_cache_key(task, params, task.cache)

        # Determine cache location
        cache_location = self.config.cache.location if self.config.cache and hasattr(self.config.cache, 'location') else task.cache.location
        cache_dir = os.path.join(self.workspace, cache_location, task.name)
        cache_file = os.path.join(cache_dir, f"{cache_key}.json")

        if not os.path.exists(cache_file):
            return (False, cache_key, None)

        try:
            with open(cache_file, 'r') as f:
                cache_entry = json.load(f)
        except Exception:
            return (False, cache_key, None)

        # Validate cache based on strategy
        strategy = task.cache.strategy or "content"

        if strategy == "always":
            # Always use cache
            pass
        elif strategy == "stale":
            if not self._validate_cache_by_stale(cache_entry, task.cache):
                return (False, cache_key, None)
        elif strategy == "content":
            if not self._validate_cache_by_content(cache_entry, task, params, task.cache):
                return (False, cache_key, None)

        return (True, cache_key, cache_entry)

    def _save_cache(self, task_name: str, cache_key: str, job: Job, input_hashes: Dict[str, str],
                    task_cache: CacheConfig) -> None:
        """Save task result to cache"""
        cache_dir = os.path.join(self.workspace, self.config.cache.location if self.config.cache and hasattr(self.config.cache, 'location') else task_cache.location, task_name)
        os.makedirs(cache_dir, exist_ok=True)

        cache_entry = {
            'timestamp': time.time(),
            'stdout': job.stdout,
            'stderr': job.stderr,
            'exit_code': job.exit_code,
            'success': job.success,
            'input_hashes': input_hashes
        }

        cache_file = os.path.join(cache_dir, f"{cache_key}.json")
        with open(cache_file, 'w') as f:
            json.dump(cache_entry, f, indent=2)

    def run_task(self, task: Task, params: Dict[str, Any], parent_job_id: Optional[int] = None,
                 job_index: int = 0) -> Job:
        """Run a single task and return the job result"""
        job_id = job_index if job_index > 0 else len(self.jobs) + 1

        # Output JSONL line
        def log_stdout(msg: str):
            print(json.dumps({"event": "TASK_RUNNING", "job_id": job_id}))

        print(json.dumps({"event": "TASK_STARTED", "job_id": job_id}))

        job = Job(
            job_id=job_id,
            task=task.name,
            params=params,
            parent=parent_job_id,
            inputs=task.inputs,
            cache=task.cache
        )

        start_time = time.time()

        # Check cache first
        cache_hit, cache_key, cache_entry = self._check_cache(task, params)

        if cache_hit is not None:
            job.cache = task.cache
            job.cache_key = cache_key
            job.cache_hit = cache_hit

            if cache_hit:
                # Use cached result
                print(json.dumps({"event": "CACHE_HIT", "job_id": job_id, "cache_key": cache_key}))
                job.stdout = cache_entry.get('stdout', '')
                job.stderr = cache_entry.get('stderr', '')
                job.exit_code = cache_entry.get('exit_code', 0)
                job.success = cache_entry.get('success', {})
                job.duration = time.time() - start_time

                print(json.dumps({"event": "TASK_COMPLETED", "job_id": job_id}))
                self.jobs.append(job)
                self.completed_tasks.add(task.name)
                return job
            else:
                # Cache miss
                print(json.dumps({"event": "CACHE_MISS", "job_id": job_id, "cache_key": cache_key}))

        # Execute requires block first
        requires_success = True
        if task.requires:
            for expr in task.requires.expressions:
                if isinstance(expr, FunctionCall) and expr.name == 'fail':
                    # Handle fail() - always passes
                    pass
                elif isinstance(expr, FunctionCall) and expr.name in self.tasks:
                    # Regular task call in requires
                    call_params = {}
                    if expr.args:
                        for arg in expr.args:
                            if isinstance(arg, tuple):
                                call_params[arg[0]] = self._eval_arg(arg[1], params)
                            else:
                                # Positional args not supported
                                pass
                    try:
                        self.run_task(self.tasks[expr.name], call_params, job_id)
                    except Exception as e:
                        requires_success = False
                        break
                elif isinstance(expr, IfBlock):
                    evaluator = ExpressionEvaluator(EvaluationContext(
                        params=params, workspace=self.workspace
                    ))
                    if not evaluator.evaluate(expr.condition):
                        for stmt in expr.then_body:
                            if isinstance(stmt, FunctionCall) and stmt.name in self.tasks:
                                # Skip
                                pass

        # Run the main task
        if task.run:
            log_stdout("Executing commands")
            stdout_parts = []
            stderr_parts = []
            exit_code = 0
            timeout_occurred = False

            for cmd_template in task.run:
                cmd = self.substitute(cmd_template, params)
                try:
                    result = subprocess.run(
                        cmd,
                        shell=True,
                        capture_output=True,
                        text=True,
                        timeout=task.timeout if task.timeout else 300,
                        cwd=self.workspace
                    )
                    # Clean Unicode characters
                    clean_stdout = result.stdout.replace('\u2018', "'").replace('\u2019', "'")
                    clean_stderr = result.stderr.replace('\u2018', "'").replace('\u2019', "'")
                    stdout_parts.append(clean_stdout)
                    stderr_parts.append(clean_stderr)
                    exit_code = result.returncode

                    # Output to stdout
                    if clean_stdout:
                        print(clean_stdout, end='')
                    if clean_stderr:
                        print(json.dumps({"event": "TASK_RUNNING", "job_id": job_id,
                                        "stderr": clean_stderr}))
                except subprocess.TimeoutExpired:
                    timeout_occurred = True
                    exit_code = 124
                    stderr_parts.append("Command timed out")
                    break

            job.stdout = ''.join(stdout_parts)
            job.stderr = ''.join(stderr_parts)
            job.exit_code = exit_code
            job.timed_out = timeout_occurred

        # Evaluate success criteria
        success_results = {}
        if task.success:
            for name, expr in task.success.items():
                evaluator = ExpressionEvaluator(EvaluationContext(
                    params=params,
                    workspace=self.workspace,
                    stdout=job.stdout,
                    stderr=job.stderr,
                    exit_code=job.exit_code,
                    file_exists_func=lambda p: os.path.exists(self.substitute(p, params))
                ))
                try:
                    result = evaluator.evaluate(expr)
                    success_results[name] = bool(result)
                except Exception as e:
                    success_results[name] = False

        # Default success criteria: exit_code == 0
        if not task.success and task.run:
            success_results['exit_code'] = job.exit_code == 0

        job.success = success_results
        job.duration = time.time() - start_time

        # Determine overall success
        all_success = all(success_results.values()) and job.exit_code == 0 and not job.timed_out

        if all_success:
            print(json.dumps({"event": "TASK_COMPLETED", "job_id": job_id}))
        else:
            print(json.dumps({"event": "TASK_FAILED", "job_id": job_id}))

        # Save to cache if enabled and not a cache hit
        if cache_hit is False and task.cache and task.cache.enabled and task.cache.strategy != "always":
            # Compute input hashes for cache
            input_hashes = {}
            if task.inputs:
                for input_pattern in task.inputs:
                    input_path = self.substitute(input_pattern, params)
                    if '*' in input_path:
                        import glob
                        matched_files = sorted(glob.glob(input_path))
                    else:
                        if os.path.exists(input_path):
                            matched_files = [input_path]
                        else:
                            matched_files = []
                    for filepath in matched_files:
                        if os.path.isfile(filepath):
                            file_hash = self._compute_file_hash(filepath)
                            input_hashes[filepath] = file_hash
            self._save_cache(task.name, cache_key, job, input_hashes, task.cache)

        self.jobs.append(job)
        self.completed_tasks.add(task.name)

        return job

    def _eval_arg(self, arg: Any, params: Dict[str, Any]) -> Any:
        """Evaluate an argument that might be a literal or variable reference"""
        if isinstance(arg, Literal):
            return arg.value
        elif isinstance(arg, VariableRef):
            if arg.name in params:
                return params[arg.name]
            raise ValueError(f"Undefined parameter: {arg.name}")
        return arg

    def execute(self, entry_task_call: str) -> int:
        """Execute the pipeline with the given entry task call"""
        # Parse entry task call (e.g., "main" or "A(1, x=2)")
        match = re.match(r'^(\w+)\((.*)\)$', entry_task_call)
        if match:
            entry_task = match.group(1)
            args_str = match.group(2)
            call_params = self._parse_task_call_args(args_str)
        else:
            entry_task = entry_task_call
            call_params = {}

        if entry_task not in self.tasks:
            raise ValueError(f"Undefined task: {entry_task}")

        # Resolve parameters
        resolved_params = self.resolve_params(entry_task, call_params)

        # Run entry task
        self.run_task(self.tasks[entry_task], resolved_params)

        # Write jobs.jsonl
        with open(os.path.join(self.output_dir, 'jobs.jsonl'), 'w') as f:
            for job in self.jobs:
                job_dict = {
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
                    "cache_key": job.cache_key
                }
                f.write(json.dumps(job_dict) + '\n')

        # Check if entry task succeeded
        entry_job = next((j for j in self.jobs if j.task == entry_task), None)
        if entry_job and all(entry_job.success.values()) and entry_job.exit_code == 0:
            return 0
        return 1

    def _parse_task_call_args(self, args_str: str) -> Dict[str, Any]:
        """Parse arguments from task call like '1, x=2'"""
        params = {}
        args = [a.strip() for a in args_str.split(',')] if args_str else []

        for arg in args:
            if '=' in arg:
                key, value = arg.split('=', 1)
                key = key.strip()
                value = value.strip()
                # Try to parse as appropriate type
                if value.isdigit():
                    params[key] = int(value)
                elif re.match(r'^\d+\.\d+$', value):
                    params[key] = float(value)
                elif value == 'TRUE':
                    params[key] = True
                elif value == 'FALSE':
                    params[key] = False
                else:
                    # Remove quotes if present
                    if (value.startswith('"') and value.endswith('"')) or \
                       (value.startswith("'") and value.endswith("'")):
                        value = value[1:-1]
                    params[key] = value
            else:
                # Positional arg - would need to track which param position
                pass

        return params


# =============================================================================
# Main Entry Point
# =============================================================================

def main():
    parser = argparse.ArgumentParser(description='Execute pipelines from pipeline files')
    parser.add_argument('pipeline', help='Path to the pipeline file (.pipe)')
    parser.add_argument('--workspace', required=True, help='Working directory')
    parser.add_argument('--output', required=True, help='Output directory for results')
    parser.add_argument('--config', help='Optional TOML config file')

    args = parser.parse_args()

    # Create output directory
    os.makedirs(args.output, exist_ok=True)
    os.makedirs(args.workspace, exist_ok=True)

    try:
        # Read and tokenize pipeline file
        with open(args.pipeline, 'r') as f:
            source = f.read()

        tokenizer = Tokenizer(source)
        tokens = tokenizer.tokenize()

        # Parse
        parser_obj = Parser(tokens)
        tasks = parser_obj.parse()

        # Parse config
        config = parse_config(args.config)

        # Determine entry task
        entry_task = config.entry or 'main'

        # Execute
        executor = PipelineExecutor(tasks, config, args.workspace, args.output)
        exit_code = executor.execute(entry_task)

        sys.exit(exit_code)

    except SyntaxError as e:
        print(f"SYNTAX_ERROR: {e}", file=sys.stderr)
        sys.exit(2)
    except Exception as e:
        print(f"INVALID_PIPE: {e}", file=sys.stderr)
        sys.exit(3)


if __name__ == '__main__':
    main()
