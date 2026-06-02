"""Lexer for the pipeline file format."""

from dataclasses import dataclass
from enum import Enum, auto
from typing import Any, List, Optional


class TokenType(Enum):
    TASK = auto()
    PARAMS = auto()
    RUN = auto()
    SUCCESS = auto()
    REQUIRES = auto()
    OUTPUT = auto()
    TIMEOUT = auto()
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
    CACHE = auto()
    CACHEDTASK = auto()
    INPUTS = auto()
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
    STRING_TYPE = auto()
    INT_TYPE = auto()
    FLOAT_TYPE = auto()
    BOOL_TYPE = auto()
    LIST_TYPE = auto()
    IDENTIFIER = auto()
    STRING = auto()
    INT = auto()
    FLOAT = auto()
    PLUS = auto()
    MINUS = auto()
    STAR = auto()
    SLASH = auto()
    PERCENT = auto()
    EQ = auto()
    NEQ = auto()
    LT = auto()
    GT = auto()
    LTE = auto()
    GTE = auto()
    AND = auto()
    OR = auto()
    NOT = auto()
    ASSIGN = auto()
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
    DOLLAR = auto()
    COMMENT = auto()
    NEWLINE = auto()
    EOF = auto()


@dataclass
class Token:
    type: TokenType
    value: Any
    line: int
    column: int


KEYWORDS = {
    'task': TokenType.TASK, 'params': TokenType.PARAMS, 'run': TokenType.RUN,
    'success': TokenType.SUCCESS, 'requires': TokenType.REQUIRES, 'output': TokenType.OUTPUT,
    'timeout': TokenType.TIMEOUT, 'if': TokenType.IF, 'elif': TokenType.ELIF,
    'else': TokenType.ELSE, 'for': TokenType.FOR, 'while': TokenType.WHILE,
    'return': TokenType.RETURN, 'break': TokenType.BREAK, 'continue': TokenType.CONTINUE,
    'TRUE': TokenType.TRUE, 'FALSE': TokenType.FALSE, 'true': TokenType.TRUE,
    'false': TokenType.FALSE, 'fails': TokenType.FAILS,
    'string': TokenType.STRING_TYPE, 'int': TokenType.INT_TYPE, 'float': TokenType.FLOAT_TYPE,
    'bool': TokenType.BOOL_TYPE, 'list': TokenType.LIST_TYPE,
    'cache': TokenType.CACHE, 'CachedTask': TokenType.CACHEDTASK, 'inputs': TokenType.INPUTS, 'enabled': TokenType.ENABLED,
    'strategy': TokenType.STRATEGY, 'location': TokenType.LOCATION, 'version': TokenType.VERSION,
    'ttl': TokenType.TTL, 'key': TokenType.KEY, 'include': TokenType.INCLUDE,
    'exclude': TokenType.EXCLUDE, 'seconds': TokenType.SECONDS, 'minutes': TokenType.MINUTES,
    'hours': TokenType.HOURS, 'days': TokenType.DAYS,
}

_TWO_CHAR_OPS = {
    '==': TokenType.EQ, '!=': TokenType.NEQ, '<=': TokenType.LTE,
    '>=': TokenType.GTE, '&&': TokenType.AND, '||': TokenType.OR,
}

_SINGLE_CHAR_OPS = {
    '+': TokenType.PLUS, '-': TokenType.MINUS, '*': TokenType.STAR,
    '/': TokenType.SLASH, '%': TokenType.PERCENT, '<': TokenType.LT,
    '>': TokenType.GT, '!': TokenType.NOT, '=': TokenType.ASSIGN,
    '(': TokenType.LPAREN, ')': TokenType.RPAREN, '{': TokenType.LBRACE,
    '}': TokenType.RBRACE, '[': TokenType.LBRACKET, ']': TokenType.RBRACKET,
    ':': TokenType.COLON, ';': TokenType.SEMICOLON, ',': TokenType.COMMA,
    '.': TokenType.DOT, '$': TokenType.DOLLAR,
}

_ESCAPE_CHARS = {'n': '\n', 't': '\t', 'r': '\r', '\\': '\\'}


class LexerError(Exception):
    def __init__(self, message: str, line: int, column: int):
        self.message = message
        self.line = line
        self.column = column
        super().__init__(f"SYNTAX_ERROR: {message} at line {line}, column {column}")


class Lexer:
    def __init__(self, source: str):
        self.source = source
        self.pos = 0
        self.line = 1
        self.column = 1
        self.tokens: List[Token] = []

    def error(self, message: str) -> LexerError:
        return LexerError(message, self.line, self.column)

    def peek(self, offset: int = 0) -> Optional[str]:
        pos = self.pos + offset
        return self.source[pos] if pos < len(self.source) else None

    def advance(self) -> Optional[str]:
        if self.pos >= len(self.source):
            return None
        char = self.source[self.pos]
        self.pos += 1
        if char == '\n':
            self.line += 1
            self.column = 1
        else:
            self.column += 1
        return char

    def skip_whitespace(self):
        while self.peek() and self.peek() in ' \t\r':
            self.advance()

    def read_string(self) -> str:
        quote = self.advance()
        result = []
        while self.peek() is not None and self.peek() != quote:
            char = self.advance()
            if char == '\\' and self.peek() is not None:
                escaped = self.advance()
                result.append(_ESCAPE_CHARS.get(escaped, escaped if escaped == quote else '\\' + escaped))
            else:
                result.append(char)
        if self.peek() is None:
            raise self.error("Unterminated string")
        self.advance()
        return ''.join(result)

    def read_number(self) -> Token:
        start_line, start_col = self.line, self.column
        result = []
        has_dot = False

        if self.peek() == '-':
            result.append(self.advance())

        while self.peek() is not None and (self.peek().isdigit() or self.peek() == '.'):
            if self.peek() == '.':
                if has_dot:
                    break
                has_dot = True
            result.append(self.advance())

        value_str = ''.join(result)
        value = float(value_str) if has_dot else int(value_str)
        return Token(TokenType.FLOAT if has_dot else TokenType.INT, value, start_line, start_col)

    def read_identifier(self) -> Token:
        start_line, start_col = self.line, self.column
        result = []
        while self.peek() is not None and (self.peek().isalnum() or self.peek() == '_'):
            result.append(self.advance())
        value = ''.join(result)
        return Token(KEYWORDS.get(value, TokenType.IDENTIFIER), value, start_line, start_col)

    def read_comment(self) -> Token:
        start_line, start_col = self.line, self.column
        self.advance()
        self.advance()
        result = []
        while self.peek() is not None and self.peek() != '\n':
            result.append(self.advance())
        return Token(TokenType.COMMENT, ''.join(result).strip(), start_line, start_col)

    def read_template_string(self) -> Token:
        start_line, start_col = self.line, self.column
        result = []
        self.advance()
        self.advance()
        result.append('${')
        brace_count = 1
        while self.peek() is not None and brace_count > 0:
            char = self.advance()
            if char == '{':
                brace_count += 1
            elif char == '}':
                brace_count -= 1
            result.append(char)
        return Token(TokenType.STRING, ''.join(result), start_line, start_col)

    def tokenize(self) -> List[Token]:
        while self.pos < len(self.source):
            self.skip_whitespace()
            if self.pos >= len(self.source):
                break

            char = self.peek()
            start_line, start_col = self.line, self.column

            if char == '\n':
                self.advance()
                self.tokens.append(Token(TokenType.NEWLINE, '\n', start_line, start_col))
                continue

            if char == '/' and self.peek(1) == '/':
                self.tokens.append(self.read_comment())
                continue

            if char in '"\'':
                self.tokens.append(Token(TokenType.STRING, self.read_string(), start_line, start_col))
                continue

            if char == '$' and self.peek(1) == '{':
                self.tokens.append(self.read_template_string())
                continue

            if char.isdigit() or (char == '-' and self.peek(1) and self.peek(1).isdigit()):
                self.tokens.append(self.read_number())
                continue

            if char.isalpha() or char == '_':
                self.tokens.append(self.read_identifier())
                continue

            two_char = char + (self.peek(1) or '')
            if two_char in _TWO_CHAR_OPS:
                self.advance()
                self.advance()
                self.tokens.append(Token(_TWO_CHAR_OPS[two_char], two_char, start_line, start_col))
            elif char in _SINGLE_CHAR_OPS:
                self.advance()
                self.tokens.append(Token(_SINGLE_CHAR_OPS[char], char, start_line, start_col))
            else:
                raise self.error(f"Unexpected character: {char}")

        self.tokens.append(Token(TokenType.EOF, None, self.line, self.column))
        return self.tokens

    def filter_tokens(self, tokens: List[Token] = None) -> List[Token]:
        if tokens is None:
            tokens = self.tokens
        result = []
        prev_was_newline = True
        for token in tokens:
            if token.type == TokenType.COMMENT:
                continue
            if token.type == TokenType.NEWLINE:
                if not prev_was_newline:
                    result.append(token)
                    prev_was_newline = True
            else:
                result.append(token)
                prev_was_newline = False
        return result
