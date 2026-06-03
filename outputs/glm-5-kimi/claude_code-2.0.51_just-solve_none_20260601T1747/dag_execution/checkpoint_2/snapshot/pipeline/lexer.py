"""
Lexer for the pipeline DSL.
"""

import re
from enum import Enum, auto
from dataclasses import dataclass
from typing import List, Optional


class TokenType(Enum):
    # Keywords
    TASK = auto()
    PARAMS = auto()
    RUN = auto()
    SUCCESS = auto()
    REQUIRES = auto()
    OUTPUT = auto()
    TIMEOUT = auto()
    INPUTS = auto()
    CACHE = auto()
    ENABLED = auto()
    STRATEGY = auto()
    LOCATION = auto()
    VERSION = auto()
    TTL = auto()
    KEY = auto()
    INCLUDE = auto()
    EXCLUDE = auto()
    SECONDS = auto()
    MINUTES = auto()
    HOURS = auto()
    DAYS = auto()
    IF = auto()
    ELIF = auto()
    ELSE = auto()
    FOR = auto()
    WHILE = auto()
    RETURN = auto()
    BREAK = auto()
    CONTINUE = auto()
    TRUE = auto()
    FALSE = auto()
    FAILS = auto()
    FAIL = auto()

    # Types
    STRING_TYPE = auto()
    INT_TYPE = auto()
    FLOAT_TYPE = auto()
    BOOL_TYPE = auto()
    LIST_TYPE = auto()

    # Literals
    IDENTIFIER = auto()
    STRING = auto()
    INT = auto()
    FLOAT = auto()

    # Operators
    PLUS = auto()
    MINUS = auto()
    STAR = auto()
    SLASH = auto()
    PERCENT = auto()
    EQUALS = auto()
    EQ = auto()
    NEQ = auto()
    LT = auto()
    GT = auto()
    LTE = auto()
    GTE = auto()
    GT_GT = auto()  # >> for shell append
    AND = auto()
    OR = auto()
    NOT = auto()

    # Delimiters
    LPAREN = auto()
    RPAREN = auto()
    LBRACE = auto()
    RBRACE = auto()
    LBRACKET = auto()
    RBRACKET = auto()
    COLON = auto()
    SEMICOLON = auto()
    COMMA = auto()
    DOT = auto()
    ARROW = auto()

    # Special
    DOLLAR = auto()
    NEWLINE = auto()
    EOF = auto()
    COMMENT = auto()


@dataclass
class Token:
    type: TokenType
    value: any
    line: int
    column: int
    start_pos: int = 0  # Position in source where token starts
    end_pos: int = 0    # Position in source where token ends


KEYWORDS = {
    'task': TokenType.TASK,
    'params': TokenType.PARAMS,
    'run': TokenType.RUN,
    'success': TokenType.SUCCESS,
    'requires': TokenType.REQUIRES,
    'output': TokenType.OUTPUT,
    'timeout': TokenType.TIMEOUT,
    'inputs': TokenType.INPUTS,
    'cache': TokenType.CACHE,
    'enabled': TokenType.ENABLED,
    'strategy': TokenType.STRATEGY,
    'location': TokenType.LOCATION,
    'version': TokenType.VERSION,
    'ttl': TokenType.TTL,
    'key': TokenType.KEY,
    'include': TokenType.INCLUDE,
    'exclude': TokenType.EXCLUDE,
    'seconds': TokenType.SECONDS,
    'minutes': TokenType.MINUTES,
    'hours': TokenType.HOURS,
    'days': TokenType.DAYS,
    'if': TokenType.IF,
    'elif': TokenType.ELIF,
    'else': TokenType.ELSE,
    'for': TokenType.FOR,
    'while': TokenType.WHILE,
    'return': TokenType.RETURN,
    'break': TokenType.BREAK,
    'continue': TokenType.CONTINUE,
    'true': TokenType.TRUE,
    'false': TokenType.FALSE,
    'fails': TokenType.FAILS,
    'fail': TokenType.FAIL,
    'string': TokenType.STRING_TYPE,
    'int': TokenType.INT_TYPE,
    'float': TokenType.FLOAT_TYPE,
    'bool': TokenType.BOOL_TYPE,
    'list': TokenType.LIST_TYPE,
}


class Lexer:
    def __init__(self, source: str):
        self.source = source
        self.pos = 0
        self.line = 1
        self.column = 1
        self.tokens: List[Token] = []
        self._token_start = 0  # Track start of current token

    def error(self, msg: str):
        raise SyntaxError(f"SYNTAX_ERROR: {msg} at line {self.line}, column {self.column}")

    def peek(self, offset: int = 0) -> str:
        pos = self.pos + offset
        if pos >= len(self.source):
            return '\0'
        return self.source[pos]

    def advance(self) -> str:
        ch = self.peek()
        self.pos += 1
        if ch == '\n':
            self.line += 1
            self.column = 1
        else:
            self.column += 1
        return ch

    def skip_whitespace(self):
        while self.peek() in ' \t\r':
            self.advance()

    def skip_comment(self):
        if self.peek() == '/' and self.peek(1) == '/':
            while self.peek() != '\n' and self.peek() != '\0':
                self.advance()

    def read_string(self) -> str:
        quote = self.advance()  # consume opening quote
        result = []
        while self.peek() != quote and self.peek() != '\0':
            ch = self.peek()
            if ch == '\\':
                self.advance()
                escape = self.advance()
                if escape == 'n':
                    result.append('\n')
                elif escape == 't':
                    result.append('\t')
                elif escape == 'r':
                    result.append('\r')
                elif escape == '\\':
                    result.append('\\')
                elif escape == '"':
                    result.append('"')
                elif escape == "'":
                    result.append("'")
                else:
                    result.append(escape)
            else:
                result.append(self.advance())
        if self.peek() == '\0':
            self.error("Unterminated string")
        self.advance()  # consume closing quote
        return ''.join(result)

    def read_identifier(self) -> str:
        result = []
        while self.peek().isalnum() or self.peek() == '_':
            result.append(self.advance())
        return ''.join(result)

    def read_number(self) -> tuple:
        result = []
        is_float = False
        while self.peek().isdigit() or self.peek() == '.':
            if self.peek() == '.':
                if is_float:
                    break
                is_float = True
            result.append(self.advance())
        value = ''.join(result)
        if is_float:
            return float(value), TokenType.FLOAT
        return int(value), TokenType.INT

    def read_dollar_var(self) -> str:
        self.advance()  # consume $
        result = ['$']
        if self.peek() == '{':
            result.append(self.advance())  # consume {
            brace_count = 1
            while brace_count > 0 and self.peek() != '\0':
                ch = self.advance()
                result.append(ch)
                if ch == '{':
                    brace_count += 1
                elif ch == '}':
                    brace_count -= 1
        else:
            # Simple $VAR format
            while self.peek().isalnum() or self.peek() == '_':
                result.append(self.advance())
        return ''.join(result)

    def add_token(self, type: TokenType, value: any = None):
        self.tokens.append(Token(type, value, self.line, self.column, start_pos=self._token_start, end_pos=self.pos))

    def tokenize(self) -> List[Token]:
        while self.peek() != '\0':
            self.skip_whitespace()
            self.skip_comment()

            if self.peek() == '\0':
                break

            self._token_start = self.pos
            ch = self.peek()

            if ch == '\n':
                self.add_token(TokenType.NEWLINE)
                self.advance()
            elif ch in '"\'':
                value = self.read_string()
                self.add_token(TokenType.STRING, value)
            elif ch.isdigit():
                value, token_type = self.read_number()
                self.add_token(token_type, value)
            elif ch.isalpha() or ch == '_':
                ident = self.read_identifier()
                if ident in KEYWORDS:
                    self.add_token(KEYWORDS[ident], ident)
                else:
                    self.add_token(TokenType.IDENTIFIER, ident)
            elif ch == '$':
                value = self.read_dollar_var()
                self.add_token(TokenType.DOLLAR, value)
            elif ch == '+':
                self.advance()
                self.add_token(TokenType.PLUS)
            elif ch == '-':
                self.advance()
                if self.peek() == '>':
                    self.advance()
                    self.add_token(TokenType.ARROW)
                else:
                    self.add_token(TokenType.MINUS)
            elif ch == '*':
                self.advance()
                self.add_token(TokenType.STAR)
            elif ch == '/':
                self.advance()
                if self.peek() == '/':
                    # Comment, skip
                    continue
                self.add_token(TokenType.SLASH)
            elif ch == '%':
                self.advance()
                self.add_token(TokenType.PERCENT)
            elif ch == '=':
                self.advance()
                if self.peek() == '=':
                    self.advance()
                    self.add_token(TokenType.EQ)
                else:
                    self.add_token(TokenType.EQUALS)
            elif ch == '!':
                self.advance()
                if self.peek() == '=':
                    self.advance()
                    self.add_token(TokenType.NEQ)
                else:
                    self.add_token(TokenType.NOT)
            elif ch == '<':
                self.advance()
                if self.peek() == '=':
                    self.advance()
                    self.add_token(TokenType.LTE)
                else:
                    self.add_token(TokenType.LT)
            elif ch == '>':
                self.advance()
                if self.peek() == '=':
                    self.advance()
                    self.add_token(TokenType.GTE)
                elif self.peek() == '>':
                    self.advance()
                    self.add_token(TokenType.GT_GT)
                else:
                    self.add_token(TokenType.GT)
            elif ch == '&':
                self.advance()
                if self.peek() == '&':
                    self.advance()
                    self.add_token(TokenType.AND)
                else:
                    self.error(f"Unexpected character: {ch}")
            elif ch == '|':
                self.advance()
                if self.peek() == '|':
                    self.advance()
                    self.add_token(TokenType.OR)
                else:
                    self.error(f"Unexpected character: {ch}")
            elif ch == '(':
                self.advance()
                self.add_token(TokenType.LPAREN)
            elif ch == ')':
                self.advance()
                self.add_token(TokenType.RPAREN)
            elif ch == '{':
                self.advance()
                self.add_token(TokenType.LBRACE)
            elif ch == '}':
                self.advance()
                self.add_token(TokenType.RBRACE)
            elif ch == '[':
                self.advance()
                self.add_token(TokenType.LBRACKET)
            elif ch == ']':
                self.advance()
                self.add_token(TokenType.RBRACKET)
            elif ch == ':':
                self.advance()
                self.add_token(TokenType.COLON)
            elif ch == ';':
                self.advance()
                self.add_token(TokenType.SEMICOLON)
            elif ch == ',':
                self.advance()
                self.add_token(TokenType.COMMA)
            elif ch == '.':
                self.advance()
                self.add_token(TokenType.DOT)
            else:
                self.error(f"Unexpected character: {ch}")

        self.add_token(TokenType.EOF)
        return self.tokens