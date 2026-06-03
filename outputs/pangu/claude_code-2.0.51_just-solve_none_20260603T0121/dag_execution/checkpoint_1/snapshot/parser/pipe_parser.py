"""Parser for .pipe pipeline files."""
import re
import sys
from enum import Enum
from dataclasses import dataclass, field
from typing import Optional, List
import os

from models.task import Task, Parameter, ParameterType, Value, ExpressionAST


class TokenType(Enum):
    TASK = 'TASK'
    IDENT = 'IDENT'
    PARAM = 'PARAM'
    RUN = 'RUN'
    SUCCESS = 'SUCCESS'
    REQUIRES = 'REQUIRES'
    OUTPUT = 'OUTPUT'
    TIMEOUT = 'TIMEOUT'
    LBRACE = 'LBRACE'
    RBRACE = 'RBRACE'
    COLON = 'COLON'
    SEMICOLON = 'SEMICOLON'
    COMMA = 'COMMA'
    EQ = 'EQ'
    STRING = 'STRING'
    INT = 'INT'
    FLOAT = 'FLOAT'
    TRUE = 'TRUE'
    FALSE = 'FALSE'
    LIST = 'LIST'
    LBRACKET = 'LBRACKET'
    RBRACKET = 'RBRACKET'
    GT = 'GT'
    LT = 'LT'
    GE = 'GE'
    LE = 'LE'
    EQ_EQ = 'EQ_EQ'
    NE = 'NE'
    PLUS = 'PLUS'
    MINUS = 'MINUS'
    STAR = 'STAR'
    SLASH = 'SLASH'
    PERCENT = 'PERCENT'
    IF = 'IF'
    ELSE = 'ELSE'
    ELIF = 'ELIF'
    FOR = 'FOR'
    WHILE = 'WHILE'
    RETURN = 'RETURN'
    BREAK = 'BREAK'
    CONTINUE = 'CONTINUE'
    LPAREN = 'LPAREN'
    RPAREN = 'RPAREN'
    IN = 'IN'
    NOT = 'NOT'
    AND = 'AND'
    OR = 'OR'
    EOF = 'EOF'


@dataclass
class Token:
    type: TokenType
    value: str
    line: int
    col: int

    def __repr__(self):
        return f"Token({self.type}, {repr(self.value)}, line={self.line})"


class Lexer:
    """Lexical analyzer for pipeline files."""

    KEYWORDS = {
        'TRUE': TokenType.TRUE,
        'FALSE': TokenType.FALSE,
        'string': TokenType.PARAM,
        'int': TokenType.PARAM,
        'float': TokenType.PARAM,
        'bool': TokenType.PARAM,
        'list': TokenType.LIST,
        'for': TokenType.FOR,
        'while': TokenType.WHILE,
        'if': TokenType.IF,
        'else': TokenType.ELSE,
        'elif': TokenType.ELIF,
        'return': TokenType.RETURN,
        'break': TokenType.BREAK,
        'continue': TokenType.CONTINUE,
        'in': TokenType.IN,
        'not': TokenType.NOT,
        'and': TokenType.AND,
        'or': TokenType.OR,
    }

    OPERATORS = [
        ('>=', TokenType.GE),
        ('<=', TokenType.LE),
        ('==', TokenType.EQ_EQ),
        ('!=', TokenType.NE),
        ('%', TokenType.PERCENT),
        ('+', TokenType.PLUS),
        ('-', TokenType.MINUS),
        ('*', TokenType.STAR),
        ('/', TokenType.SLASH),
        ('>', TokenType.GT),
        ('<', TokenType.LT),
        ('=', TokenType.EQ),
    ]

    def __init__(self, source: str):
        self.source = source
        self.pos = 0
        self.line = 1
        self.col = 1

    def peek(self, n=0):
        if self.pos + n >= len(self.source):
            return None
        return self.source[self.pos + n]

    def consume(self):
        ch = self.peek()
        if ch == '\n':
            self.line += 1
            self.col = 1
        else:
            self.col += 1
        self.pos += 1
        return ch

    def skip_whitespace(self):
        while self.pos < len(self.source):
            ch = self.peek()
            if ch in ' \t':
                self.consume()
            elif ch == '\n':
                self.consume()
                # Keep newlines for line counting but skip for most contexts
            elif ch == '/' and self.peek(1) == '/':
                # Single line comment
                while self.pos < len(self.source) and self.peek() != '\n':
                    self.consume()
                # consume the newline
                if self.peek() == '\n':
                    self.consume()
            elif ch == '/' and self.peek(1) == '*':
                # Multi-line comment
                self.consume()  # consume /
                self.consume()  # consume *
                while self.pos < len(self.source):
                    if self.peek() == '*' and self.peek(1) == '/':
                        self.consume()  # consume *
                        self.consume()  # consume /
                        break
                    self.consume()
            else:
                break

    def read_string(self):
        """Read a string literal."""
        start = self.pos
        quote = self.consume()  # consume opening quote
        result = []

        while self.pos < len(self.source):
            ch = self.peek()
            if ch == '\\':
                # Escape sequence
                self.consume()
                escape = self.peek()
                if escape == 'n':
                    result.append('\n')
                elif escape == 't':
                    result.append('\t')
                elif escape == 'r':
                    result.append('\r')
                elif escape == '"':
                    result.append('"')
                elif escape == '\\':
                    result.append('\\')
                else:
                    result.append(escape)
                self.consume()
            elif ch == quote:
                self.consume()
                break
            elif ch == '\n' and quote == '"':
                # Unterminated string (newline in quoted string)
                raise SyntaxError(f"Unterminated string at line {self.line}")
            else:
                result.append(ch)
                self.consume()

        return ''.join(result)

    def read_number(self, first_digit):
        """Read a number (int or float)."""
        result = first_digit
        has_decimal = first_digit == '.'

        while self.pos < len(self.source):
            ch = self.peek()
            if ch.isdigit():
                result += ch
                self.consume()
            elif ch == '.' and not has_decimal:
                result += ch
                has_decimal = True
                self.consume()
            else:
                break

        return result

    def read_ident(self):
        """Read an identifier."""
        result = []
        while self.pos < len(self.source):
            ch = self.peek()
            if ch.isalnum() or ch == '_':
                result.append(ch)
                self.consume()
            else:
                break
        return ''.join(result)

    def next_token(self) -> Token:
        """Get the next token from the input."""
        self.skip_whitespace()

        if self.pos >= len(self.source):
            return Token(TokenType.EOF, '', self.line, self.col)

        ch = self.peek()
        start_line = self.line
        start_col = self.col

        # Check for operators (longest match first)
        for op_str, op_type in self.OPERATORS:
            if self.source.startswith(op_str, self.pos):
                self.pos += len(op_str)
                self.col += len(op_str)
                return Token(op_type, op_str, start_line, start_col)

        # String literal
        if ch == '"':
            self.consume()  # consume opening quote
            try:
                value = self.read_string()
                return Token(TokenType.STRING, value, start_line, start_col)
            except SyntaxError as e:
                print(f"SYNTAX_ERROR: {e}", file=sys.stderr)
                sys.exit(2)

        # Number
        if ch.isdigit() or (ch == '.' and self.peek(1) and self.peek(1).isdigit()):
            if ch == '.':
                self.consume()
            number = self.read_number(ch if ch != '.' else '0')
            if '.' in number:
                try:
                    float(number)
                    return Token(TokenType.FLOAT, number, start_line, start_col)
                except ValueError:
                    print(f"SYNTAX_ERROR: Invalid float '{number}' at line {start_line}", file=sys.stderr)
                    sys.exit(2)
            else:
                return Token(TokenType.INT, number, start_line, start_col)

        # Identifier or keyword
        if ch.isalpha() or ch == '_':
            ident = self.read_ident()
            if ident in self.KEYWORDS:
                return Token(self.KEYWORDS[ident], ident, start_line, start_col)
            if ident == 'task':
                return Token(TokenType.TASK, ident, start_line, start_col)
            return Token(TokenType.IDENT, ident, start_line, start_col)

        # Single character tokens
        single = {
            '{': TokenType.LBRACE,
            '}': TokenType.RBRACE,
            '(': TokenType.LPAREN,
            ')': TokenType.RPAREN,
            '[': TokenType.LBRACKET,
            ']': TokenType.RBRACKET,
            ':': TokenType.COLON,
            ';': TokenType.SEMICOLON,
            ',': TokenType.COMMA,
            '=': TokenType.EQ,
        }

        if ch in single:
            self.consume()
            return Token(single[ch], ch, start_line, start_col)

        # Unknown character
        print(f"SYNTAX_ERROR: Unknown character '{ch}' at line {start_line}", file=sys.stderr)
        sys.exit(2)


class Parser:
    """Parser for pipeline files."""

    def __init__(self, lexer: Lexer):
        self.lexer = lexer
        self.current_token = None
        self.peek_token = None
        self._advance()
        self._advance()

    def _advance(self):
        self.current_token = self.peek_token
        self.peek_token = self.lexer.next_token()

    def _match(self, token_type: TokenType) -> Token:
        if self.current_token.type != token_type:
            print(f"SYNTAX_ERROR: Expected {token_type}, got {self.current_token.type} at line {self.current_token.line}", file=sys.stderr)
            sys.exit(2)
        token = self.current_token
        self._advance()
        return token

    def _check(self, token_type: TokenType) -> bool:
        return self.current_token.type == token_type

    def _match_any(self, *types) -> bool:
        return self.current_token.type in types

    def parse(self) -> dict:
        """Parse a complete pipeline."""
        tasks = {}

        while not self._check(TokenType.EOF):
            if self._check(TokenType.TASK):
                task = self.parse_task()
                if task.name in tasks:
                    print(f"INVALID_PIPE: Duplicate task name '{task.name}'", file=sys.stderr)
                    sys.exit(3)
                tasks[task.name] = task
            else:
                print(f"SYNTAX_ERROR: Expected 'task', got {self.current_token.type} at line {self.current_token.line}", file=sys.stderr)
                sys.exit(2)

        return tasks

    def parse_task(self) -> Task:
        """Parse a task definition."""
        self._match(TokenType.TASK)
        name_token = self._match(TokenType.IDENT)

        task = Task(name=name_token.value)

        # Parse fields until closing brace
        self._match(TokenType.LBRACE)

        while not self._check(TokenType.RBRACE):
            if self._check(TokenType.PARAM):
                self.parse_params(task)
            elif self._check(TokenType.RUN):
                task.run = self.parse_run()
            elif self._check(TokenType.SUCCESS):
                task.success = self.parse_success_block()
            elif self._check(TokenType.REQUIRES):
                task.requires = self.parse_expression_block()
            elif self._check(TokenType.OUTPUT):
                task.output = self.parse_output()
            elif self._check(TokenType.TIMEOUT):
                task.timeout = self.parse_timeout()
            else:
                print(f"SYNTAX_ERROR: Unexpected token {self.current_token.type} in task {task.name}", file=sys.stderr)
                sys.exit(2)

        self._match(TokenType.RBRACE)
        return task

    def parse_params(self, task: Task):
        """Parse params block."""
        self._match(TokenType.PARAM)
        self._match(TokenType.COLON)
        self._match(TokenType.LBRACE)

        has_default = False
        last_param_had_default = False

        while not self._check(TokenType.RBRACE):
            name_token = self._match(TokenType.IDENT)
            self._match(TokenType.COLON)

            # Parse type
            if self._check(TokenType.LIST):
                type_str = 'list[T]'
                self._match(TokenType.LIST)
                # Skip type parameter for now
            else:
                type_token = self._match(TokenType.PARAM)
                type_str = type_token.value

            # Check for default value
            default_value = None
            if self._check(TokenType.EQ):
                if last_param_had_default:
                    print(f"SYNTAX_ERROR: Parameter without default cannot follow one with default in task {task.name}", file=sys.stderr)
                    sys.exit(2)
                last_param_had_default = True
                self._advance()  # consume =
                default_value = self.parse_value()
            else:
                last_param_had_default = False

            param_type = ParameterType.from_str(type_str)
            if param_type is None:
                print(f"SYNTAX_ERROR: Unknown parameter type '{type_str}'", file=sys.stderr)
                sys.exit(2)

            param = Parameter(
                name=name_token.value,
                type=param_type,
                default=default_value,
                has_default=default_value is not None
            )
            task.params.append(param)

            # Check for semicolon
            if self._check(TokenType.SEMICOLON):
                self._advance()

        self._match(TokenType.RBRACE)

    def parse_value(self) -> Value:
        """Parse a value literal."""
        if self._check(TokenType.STRING):
            val = Value(self.current_token.value, ParameterType.STRING)
            self._advance()
            return val
        elif self._check(TokenType.INT):
            val = Value(int(self.current_token.value), ParameterType.INT)
            self._advance()
            return val
        elif self._check(TokenType.FLOAT):
            val = Value(float(self.current_token.value), ParameterType.FLOAT)
            self._advance()
            return val
        elif self._check(TokenType.TRUE):
            val = Value(True, ParameterType.BOOL)
            self._advance()
            return val
        elif self._check(TokenType.FALSE):
            val = Value(False, ParameterType.BOOL)
            self._advance()
            return val
        else:
            print(f"SYNTAX_ERROR: Expected value, got {self.current_token.type}", file=sys.stderr)
            sys.exit(2)

    def parse_run(self) -> str:
        """Parse run block - just return the raw commands string."""
        self._match(TokenType.RUN)

        if self._check(TokenType.COLON):
            self._advance()

        if self._check(TokenType.LBRACE):
            # Parse block content
            start = self.lexer.pos
            brace_count = 1
            self._advance()

            while brace_count > 0 and self.lexer.pos < len(self.lexer.source):
                ch = self.lexer.peek()
                if ch == '{':
                    brace_count += 1
                elif ch == '}':
                    brace_count -= 1
                self.lexer.consume()

            # Rewind to after the closing brace
            if brace_count == 0:
                self.lexer.pos -= 1  # will be consumed in next token
            content_start = start + 1
            content_end = self.lexer.pos - (1 if brace_count == 0 else 0)

            # Reset lexer to start of content
            self.lexer.pos = content_start
            self.lexer.col = 1  # reset line tracking

            # Read content
            content = []
            while self.lexer.pos < content_end:
                content.append(self.lexer.consume())

            # Advance past closing brace
            self._advance()

            return ''.join(content)
        else:
            # Single line format
            content = []
            while not self._check(TokenType.EOF) and not self._check(TokenType.SEMICOLON) and not self._check(TokenType.RBRACE):
                if self.current_token.type in (TokenType.PARAM, TokenType.RUN, TokenType.SUCCESS, TokenType.REQUIRES, TokenType.OUTPUT, TokenType.TIMEOUT):
                    break
                content.append(self.current_token.value)
                self._advance()
            return ' '.join(content)

    def parse_success_block(self) -> dict:
        """Parse success block."""
        self._match(TokenType.SUCCESS)
        self._match(TokenType.COLON)
        self._match(TokenType.LBRACE)

        success = {}

        while not self._check(TokenType.RBRACE):
            name_token = self._match(TokenType.IDENT)
            self._match(TokenType.COLON)

            if self._check(TokenType.LBRACE):
                # Parse block expression
                expr = self.parse_block_expression()
            else:
                print(f"SYNTAX_ERROR: Expected '{{' after success criteria name", file=sys.stderr)
                sys.exit(2)

            success[name_token.value] = expr

        self._match(TokenType.RBRACE)
        return success

    def parse_expression_block(self) -> ExpressionAST:
        """Parse an expression block."""
        self._match(TokenType.REQUIRES)
        self._match(TokenType.COLON)
        self._match(TokenType.LBRACE)

        block = self.parse_block_expression()
        self._match(TokenType.RBRACE)
        return block

    def parse_block_expression(self) -> ExpressionAST:
        """Parse a block expression (for if/elif/else, for, while, etc.)."""
        if self._check(TokenType.LBRACE):
            self._advance()
            body = self.parse_block()
            self._match(TokenType.RBRACE)
            return body
        else:
            return self.parse_expression()

    def parse_block(self) -> ExpressionAST:
        """Parse a block of statements."""
        statements = []

        while not self._check(TokenType.RBRACE) and not self._check(TokenType.EOF):
            stmt = self.parse_statement()
            if stmt:
                statements.append(stmt)

        if len(statements) == 1:
            return statements[0]
        else:
            return ExpressionAST(type='block', children=statements)

    def parse_statement(self) -> ExpressionAST:
        """Parse a single statement."""
        if self._check(TokenType.IF):
            return self.parse_if_statement()
        elif self._check(TokenType.FOR):
            return self.parse_for_statement()
        elif self._check(TokenType.WHILE):
            return self.parse_while_statement()
        elif self._check(TokenType.RETURN):
            return self.parse_return_statement()
        elif self._check(TokenType.BREAK):
            self._advance()
            return ExpressionAST(type='break')
        elif self._check(TokenType.CONTINUE):
            self._advance()
            return ExpressionAST(type='continue')
        else:
            # Try parsing as a function call or expression
            return self.parse_expression_or_call()

    def parse_if_statement(self) -> ExpressionAST:
        """Parse if/elif/else statement."""
        self._match(TokenType.IF)
        self._match(TokenType.LPAREN)
        condition = self.parse_expression()
        self._match(TokenType.RPAREN)

        if self._check(TokenType.LBRACE):
            self._advance()
            true_branch = self.parse_block()
            self._match(TokenType.RBRACE)
        else:
            true_branch = self.parse_expression_or_call()

        elif_branch = None
        else_branch = None

        if self._check(TokenType.ELIF):
            elif_branch = self.parse_elif_statement()
        elif self._check(TokenType.ELSE):
            self._advance()
            if self._check(TokenType.LBRACE):
                self._advance()
                else_branch = self.parse_block()
                self._match(TokenType.RBRACE)
            else:
                else_branch = self.parse_expression_or_call()

        return ExpressionAST(
            type='if',
            condition=condition,
            true_branch=true_branch,
            false_branch=else_branch,
            children=[elif_branch] if elif_branch else []
        )

    def parse_elif_statement(self) -> ExpressionAST:
        """Parse elif chain."""
        self._match(TokenType.ELIF)
        self._match(TokenType.LPAREN)
        condition = self.parse_expression()
        self._match(TokenType.RPAREN)

        if self._check(TokenType.LBRACE):
            self._advance()
            body = self.parse_block()
            self._match(TokenType.RBRACE)
        else:
            body = self.parse_expression_or_call()

        elif_branch = None
        if self._check(TokenType.ELIF):
            elif_branch = self.parse_elif_statement()
        elif self._check(TokenType.ELSE):
            self._advance()
            if self._check(TokenType.LBRACE):
                self._advance()
                else_branch = self.parse_block()
                self._match(TokenType.RBRACE)
            else:
                else_branch = self.parse_expression_or_call()

        return ExpressionAST(type='elif', condition=condition, true_branch=body, false_branch=else_branch, children=[elif_branch] if elif_branch else [])

    def parse_for_statement(self) -> ExpressionAST:
        """Parse for loop."""
        self._match(TokenType.FOR)
        self._match(TokenType.LPAREN)

        # Parse init
        if self._check(TokenType.IDENT):
            var = self.current_token.value
            self._advance()
            self._match(TokenType.EQ)
            init_val = self.parse_expression()
            init = ExpressionAST(type='assign', value=var, children=[init_val])
        else:
            init = self.parse_expression()

        self._match(TokenType.SEMICOLON)

        # Parse condition
        condition = self.parse_expression()
        self._match(TokenType.SEMICOLON)

        # Parse update
        update = self.parse_expression()
        self._match(TokenType.RPAREN)

        # Parse body
        if self._check(TokenType.LBRACE):
            self._advance()
            body = self.parse_block()
            self._match(TokenType.RBRACE)
        else:
            body = self.parse_expression_or_call()

        return ExpressionAST(
            type='for',
            init=init,
            condition=condition,
            update=update,
            body=body
        )

    def parse_while_statement(self) -> ExpressionAST:
        """Parse while loop."""
        self._match(TokenType.WHILE)
        self._match(TokenType.LPAREN)
        condition = self.parse_expression()
        self._match(TokenType.RPAREN)

        if self._check(TokenType.LBRACE):
            self._advance()
            body = self.parse_block()
            self._match(TokenType.RBRACE)
        else:
            body = self.parse_expression_or_call()

        return ExpressionAST(type='while', condition=condition, body=body)

    def parse_return_statement(self) -> ExpressionAST:
        """Parse return statement."""
        self._match(TokenType.RETURN)
        value = self.parse_expression()
        self._match(TokenType.SEMICOLON)
        return ExpressionAST(type='return', children=[value])

    def parse_expression_or_call(self) -> ExpressionAST:
        """Parse an expression or a function call."""
        # Check if it's a function call (task name followed by parenthesis)
        if self._check(TokenType.IDENT) and self.peek_token and self.peek_token.type == TokenType.LPAREN:
            name = self.current_token.value
            self._advance()
            self._match(TokenType.LPAREN)

            args = []
            kwargs = {}

            # Parse first argument
            if not self._check(TokenType.RPAREN):
                # Parse first positional arg or kwarg
                if self.peek_token and self.peek_token.type == TokenType.EQ:
                    # Named argument
                    key = self.current_token.value
                    self._advance()
                    self._advance()  # consume =
                    value = self.parse_expression()
                    kwargs[key] = value
                else:
                    # Positional argument
                    arg = self.parse_expression()
                    args.append(arg)

                # Parse remaining arguments
                while self._check(TokenType.COMMA):
                    self._advance()
                    if self.peek_token and self.peek_token.type == TokenType.EQ:
                        key = self.current_token.value
                        self._advance()
                        self._advance()
                        value = self.parse_expression()
                        kwargs[key] = value
                    else:
                        arg = self.parse_expression()
                        args.append(arg)

            self._match(TokenType.RPAREN)

            call = ExpressionAST(type='function_call', value=name, children=args, kwargs=kwargs)

            # Check if it's followed by fails()
            if self.current_token.type == TokenType.IDENT and self.current_token.value == 'fails':
                # This is actually fails(<call>)
                self._advance()
                self._match(TokenType.LPAREN)
                self._match(TokenType.RPAREN)
                return ExpressionAST(type='fails', children=[call])

            return call
        else:
            return self.parse_expression()

    def parse_expression(self) -> ExpressionAST:
        """Parse an expression."""
        return self.parse_logical_or()

    def parse_logical_or(self) -> ExpressionAST:
        left = self.parse_logical_and()
        while self._check(TokenType.OR):
            op = self.current_token
            self._advance()
            right = self.parse_logical_and()
            left = ExpressionAST(type='operator', operator='or', children=[left, right])
        return left

    def parse_logical_and(self) -> ExpressionAST:
        left = self.parse_equality()
        while self._check(TokenType.AND):
            op = self.current_token
            self._advance()
            right = self.parse_equality()
            left = ExpressionAST(type='operator', operator='and', children=[left, right])
        return left

    def parse_equality(self) -> ExpressionAST:
        left = self.parse_relational()
        while self._check_any(TokenType.EQ_EQ, TokenType.NE):
            op = self.current_token
            self._advance()
            right = self.parse_relational()
            left = ExpressionAST(type='operator', operator=op.value, children=[left, right])
        return left

    def parse_relational(self) -> ExpressionAST:
        left = self.parse_additive()
        while self._check_any(TokenType.GT, TokenType.LT, TokenType.GE, TokenType.LE):
            op = self.current_token
            self._advance()
            right = self.parse_additive()
            left = ExpressionAST(type='operator', operator=op.value, children=[left, right])
        return left

    def parse_additive(self) -> ExpressionAST:
        left = self.parse_multiplicative()
        while self._check_any(TokenType.PLUS, TokenType.MINUS):
            op = self.current_token
            self._advance()
            right = self.parse_multiplicative()
            left = ExpressionAST(type='operator', operator=op.value, children=[left, right])
        return left

    def parse_multiplicative(self) -> ExpressionAST:
        left = self.parse_unary()
        while self._check_any(TokenType.STAR, TokenType.SLASH, TokenType.PERCENT):
            op = self.current_token
            self._advance()
            right = self.parse_unary()
            left = ExpressionAST(type='operator', operator=op.value, children=[left, right])
        return left

    def parse_unary(self) -> ExpressionAST:
        if self._check_any(TokenType.NOT, TokenType.PLUS, TokenType.MINUS):
            op = self.current_token.value
            self._advance()
            expr = self.parse_unary()
            return ExpressionAST(type='operator', operator=op, children=[expr])
        return self.parse_primary()

    def parse_primary(self) -> ExpressionAST:
        if self._check(TokenType.IDENT):
            name = self.current_token.value
            self._advance()

            if self._check(TokenType.LPAREN):
                # Function call
                self._advance()
                args = []
                kwargs = {}

                if not self._check(TokenType.RPAREN):
                    if self.peek_token and self.peek_token.type == TokenType.EQ:
                        key = name
                        self._advance()
                        self._advance()
                        value = self.parse_expression()
                        kwargs[key] = value
                    else:
                        args.append(self.parse_expression())

                    while self._check(TokenType.COMMA):
                        self._advance()
                        if self.peek_token and self.peek_token.type == TokenType.EQ:
                            key = self.current_token.value
                            self._advance()
                            self._advance()
                            value = self.parse_expression()
                            kwargs[key] = value
                        else:
                            arg = self.parse_expression()
                            args.append(arg)

                self._match(TokenType.RPAREN)

                # Check for fails wrapper
                call = ExpressionAST(type='function_call', value=name, children=args, kwargs=kwargs)
                if name == 'fails' and args:
                    return args[0]  # The call is already the fails wrapper

                return call

            return ExpressionAST(type='identifier', value=name)

        elif self._check(TokenType.STRING):
            node = ExpressionAST(type='literal', value=self.current_token.value)
            self._advance()
            return node

        elif self._check(TokenType.INT):
            node = ExpressionAST(type='literal', value=int(self.current_token.value))
            self._advance()
            return node

        elif self._check(TokenType.FLOAT):
            node = ExpressionAST(type='literal', value=float(self.current_token.value))
            self._advance()
            return node

        elif self._check(TokenType.TRUE):
            node = ExpressionAST(type='literal', value=True)
            self._advance()
            return node

        elif self._check(TokenType.FALSE):
            node = ExpressionAST(type='literal', value=False)
            self._advance()
            return node

        elif self._check(TokenType.LBRACKET):
            self._advance()
            items = []

            if not self._check(TokenType.RBRACKET):
                items.append(self.parse_expression())
                while self._check(TokenType.COMMA):
                    self._advance()
                    items.append(self.parse_expression())

            self._match(TokenType.RBRACKET)
            return ExpressionAST(type='list', children=items)

        elif self._check(TokenType.LPAREN):
            self._advance()
            expr = self.parse_expression()
            self._match(TokenType.RPAREN)
            return expr

        else:
            print(f"SYNTAX_ERROR: Unexpected token {self.current_token.type} at line {self.current_token.line}", file=sys.stderr)
            sys.exit(2)

    def parse_output(self) -> str:
        """Parse output field."""
        self._match(TokenType.OUTPUT)
        self._match(TokenType.COLON)

        if self._check(TokenType.STRING):
            output = self.current_token.value
            self._advance()
            return output
        else:
            print(f"SYNTAX_ERROR: Expected string for output, got {self.current_token.type}", file=sys.stderr)
            sys.exit(2)

    def parse_timeout(self) -> float:
        """Parse timeout field."""
        self._match(TokenType.TIMEOUT)
        self._match(TokenType.COLON)

        if self._check(TokenType.FLOAT) or self._check(TokenType.INT):
            timeout = float(self.current_token.value)
            self._advance()
            if timeout < 0:
                print(f"SYNTAX_ERROR: Timeout must be non-negative", file=sys.stderr)
                sys.exit(2)
            return timeout
        else:
            print(f"SYNTAX_ERROR: Expected number for timeout, got {self.current_token.type}", file=sys.stderr)
            sys.exit(2)


def parse_pipeline(source: str) -> dict:
    """Parse a pipeline source string into a dict of Task objects."""
    lexer = Lexer(source)
    parser = Parser(lexer)
    return parser.parse()
