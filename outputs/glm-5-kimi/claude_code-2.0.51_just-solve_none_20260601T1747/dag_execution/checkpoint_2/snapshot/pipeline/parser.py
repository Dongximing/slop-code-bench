"""
Parser for the pipeline DSL.
"""

from typing import List, Optional, Dict, Any, Tuple
from .lexer import Lexer, Token, TokenType
from .ast_nodes import (
    Parameter, TaskDef, Pipeline, Expr, Stmt, BlockStmt,
    IntLiteral, FloatLiteral, StringLiteral, BoolLiteral, ListLiteral,
    IdentifierExpr, ArrayIndexExpr, DollarVar, BinaryExpr, UnaryExpr,
    FunctionCallExpr, AssignmentExpr, ExprStmt, ReturnStmt, BreakStmt,
    ContinueStmt, IfStmt, ForStmt, WhileStmt, TaskCallStmt, FailsStmt,
    SuccessBlock, CacheConfig, CacheTTL, CacheKey
)


class ParseError(Exception):
    pass


class Parser:
    def __init__(self, source: str):
        self.source = source
        self.lexer = Lexer(source)
        self.tokens = self.lexer.tokenize()
        self.pos = 0

    def error(self, msg: str):
        token = self.current()
        raise ParseError(f"SYNTAX_ERROR: {msg} at line {token.line}")

    def current(self) -> Token:
        if self.pos >= len(self.tokens):
            return self.tokens[-1]
        return self.tokens[self.pos]

    def peek(self, offset: int = 0) -> Token:
        pos = self.pos + offset
        if pos >= len(self.tokens):
            return self.tokens[-1]
        return self.tokens[pos]

    def advance(self) -> Token:
        token = self.current()
        self.pos += 1
        return token

    def match(self, *types: TokenType) -> bool:
        return self.current().type in types

    def expect(self, type: TokenType, msg: str = None) -> Token:
        if not self.match(type):
            self.error(msg or f"Expected {type}, got {self.current().type}")
        return self.advance()

    def skip_newlines(self):
        while self.match(TokenType.NEWLINE):
            self.advance()

    def parse(self) -> Pipeline:
        tasks = {}
        self.skip_newlines()
        while not self.match(TokenType.EOF):
            task = self.parse_task()
            tasks[task.name] = task
            self.skip_newlines()
        return Pipeline(tasks)

    def parse_task(self) -> TaskDef:
        self.expect(TokenType.TASK)
        name = self.expect(TokenType.IDENTIFIER).value
        self.skip_newlines()
        self.expect(TokenType.LBRACE)
        self.skip_newlines()

        params = []
        run_block = None
        success_block = None
        requires_block = None
        output = None
        timeout = None
        inputs = None
        cache = None

        while not self.match(TokenType.RBRACE):
            if self.match(TokenType.PARAMS):
                self.advance()
                self.expect(TokenType.COLON)
                self.skip_newlines()
                self.expect(TokenType.LBRACE)
                self.skip_newlines()
                while not self.match(TokenType.RBRACE):
                    param = self.parse_parameter()
                    params.append(param)
                    if self.match(TokenType.SEMICOLON):
                        self.advance()
                    self.skip_newlines()
                self.expect(TokenType.RBRACE)
                self.skip_newlines()
            elif self.match(TokenType.INPUTS):
                self.advance()
                self.expect(TokenType.COLON)
                self.skip_newlines()
                inputs = self.parse_inputs_block()
                self.skip_newlines()
            elif self.match(TokenType.CACHE):
                self.advance()
                self.expect(TokenType.COLON)
                self.skip_newlines()
                cache = self.parse_cache_block()
                self.skip_newlines()
            elif self.match(TokenType.RUN):
                self.advance()
                self.expect(TokenType.COLON)
                self.skip_newlines()
                run_block = self.parse_run_block()
                self.skip_newlines()
            elif self.match(TokenType.SUCCESS):
                self.advance()
                self.expect(TokenType.COLON)
                self.skip_newlines()
                success_block = self.parse_success_block()
                self.skip_newlines()
            elif self.match(TokenType.REQUIRES):
                self.advance()
                self.expect(TokenType.COLON)
                self.skip_newlines()
                requires_block = self.parse_requires_block()
                self.skip_newlines()
            elif self.match(TokenType.OUTPUT):
                self.advance()
                self.expect(TokenType.COLON)
                output = self.parse_expression()
                if self.match(TokenType.SEMICOLON):
                    self.advance()
                self.skip_newlines()
            elif self.match(TokenType.TIMEOUT):
                self.advance()
                self.expect(TokenType.COLON)
                timeout_token = self.expect(TokenType.FLOAT) if self.match(TokenType.FLOAT) else self.expect(TokenType.INT)
                timeout = float(timeout_token.value)
                if self.match(TokenType.SEMICOLON):
                    self.advance()
                self.skip_newlines()
            else:
                self.error(f"Unexpected token in task: {self.current().type}")

        self.expect(TokenType.RBRACE)
        self.skip_newlines()

        # Validate: task must have run or requires
        if run_block is None and requires_block is None:
            self.error(f"Task '{name}' must have either 'run' or 'requires' field")

        return TaskDef(
            name=name,
            params=params,
            run_block=run_block,
            success_block=success_block,
            requires_block=requires_block,
            output=output,
            timeout=timeout,
            inputs=inputs,
            cache=cache
        )

    def parse_parameter(self) -> Parameter:
        name = self.expect(TokenType.IDENTIFIER).value
        self.expect(TokenType.COLON)
        type_name = self.parse_type_name()
        inner_type = None
        if type_name == 'list':
            self.expect(TokenType.LBRACKET)
            inner_type = self.parse_type_name()
            self.expect(TokenType.RBRACKET)

        has_default = False
        default_value = None
        env_var = None

        if self.match(TokenType.EQUALS):
            self.advance()
            has_default = True
            if self.match(TokenType.DOLLAR):
                # Environment variable
                dollar_token = self.advance()
                env_var = dollar_token.value
                default_value = None  # Will be resolved later
            else:
                default_value = self.parse_literal_value(type_name, inner_type)

        full_type = f"list[{inner_type}]" if inner_type else type_name
        return Parameter(
            name=name,
            type_name=full_type,
            inner_type=inner_type,
            default_value=default_value,
            has_default=has_default,
            env_var=env_var
        )

    def parse_type_name(self) -> str:
        if self.match(TokenType.STRING_TYPE):
            self.advance()
            return 'string'
        elif self.match(TokenType.INT_TYPE):
            self.advance()
            return 'int'
        elif self.match(TokenType.FLOAT_TYPE):
            self.advance()
            return 'float'
        elif self.match(TokenType.BOOL_TYPE):
            self.advance()
            return 'bool'
        elif self.match(TokenType.LIST_TYPE):
            self.advance()
            return 'list'
        else:
            self.error(f"Expected type name, got {self.current().type}")

    def parse_literal_value(self, type_name: str, inner_type: str = None) -> Any:
        if type_name == 'string':
            return self.expect(TokenType.STRING).value
        elif type_name == 'int':
            return self.expect(TokenType.INT).value
        elif type_name == 'float':
            if self.match(TokenType.INT):
                return float(self.advance().value)
            return self.expect(TokenType.FLOAT).value
        elif type_name == 'bool':
            if self.match(TokenType.TRUE):
                self.advance()
                return True
            elif self.match(TokenType.FALSE):
                self.advance()
                return False
            else:
                self.error(f"Expected boolean value")
        elif type_name == 'list':
            self.expect(TokenType.LBRACKET)
            elements = []
            while not self.match(TokenType.RBRACKET):
                elements.append(self.parse_literal_value(inner_type))
                if self.match(TokenType.COMMA):
                    self.advance()
            self.expect(TokenType.RBRACKET)
            return elements
        else:
            self.error(f"Unknown type: {type_name}")

    def parse_run_block(self) -> str:
        """Parse run block - extract raw shell commands preserving original formatting."""
        # Record the position before the { token
        self.expect(TokenType.LBRACE)

        # Now we need to find the matching } and extract raw text from source
        # Track positions
        start_pos = self.current().start_pos if hasattr(self.current(), 'start_pos') else 0

        # Count braces to find the matching close
        depth = 1
        tokens_start = self.pos

        while depth > 0 and not self.match(TokenType.EOF):
            if self.match(TokenType.LBRACE):
                depth += 1
            elif self.match(TokenType.RBRACE):
                depth -= 1
                if depth == 0:
                    break
            self.advance()

        # Now we have the end position
        tokens_end = self.pos

        # Get the source positions from the tokens
        if tokens_start < len(self.tokens) and tokens_end < len(self.tokens):
            start_token = self.tokens[tokens_start]
            end_token = self.tokens[tokens_end]

            # Extract from source using positions
            if hasattr(start_token, 'start_pos') and hasattr(end_token, 'start_pos'):
                raw_text = self.source[start_token.start_pos:end_token.start_pos].strip()
            else:
                raw_text = ""
        else:
            raw_text = ""

        # Reset position to process tokens properly
        self.pos = tokens_start

        # Actually consume all the tokens until we reach the RBRACE
        depth = 1
        while depth > 0:
            if self.match(TokenType.LBRACE):
                depth += 1
                self.advance()
            elif self.match(TokenType.RBRACE):
                depth -= 1
                if depth == 0:
                    self.advance()
                    break
                self.advance()
            else:
                self.advance()

        return raw_text

    def parse_inputs_block(self) -> List[Expr]:
        """Parse inputs block - list of file patterns."""
        self.expect(TokenType.LBRACKET)
        self.skip_newlines()
        inputs = []
        while not self.match(TokenType.RBRACKET):
            inputs.append(self.parse_expression())
            self.skip_newlines()
            if self.match(TokenType.COMMA):
                self.advance()
                self.skip_newlines()
        self.expect(TokenType.RBRACKET)
        return inputs

    def parse_cache_block(self) -> CacheConfig:
        """Parse cache configuration block."""
        self.expect(TokenType.LBRACE)
        self.skip_newlines()

        enabled = False
        strategy = "content"
        location = ".pipe-cache"
        version = None
        ttl = None
        key = None

        while not self.match(TokenType.RBRACE):
            if self.match(TokenType.ENABLED):
                self.advance()
                self.expect(TokenType.COLON)
                if self.match(TokenType.TRUE):
                    self.advance()
                    enabled = True
                elif self.match(TokenType.FALSE):
                    self.advance()
                    enabled = False
                else:
                    self.error("Expected true or false for enabled")
                if self.match(TokenType.SEMICOLON):
                    self.advance()
            elif self.match(TokenType.STRATEGY):
                self.advance()
                self.expect(TokenType.COLON)
                strategy = self.expect(TokenType.STRING).value
                if self.match(TokenType.SEMICOLON):
                    self.advance()
            elif self.match(TokenType.LOCATION):
                self.advance()
                self.expect(TokenType.COLON)
                location = self.expect(TokenType.STRING).value
                if self.match(TokenType.SEMICOLON):
                    self.advance()
            elif self.match(TokenType.VERSION):
                self.advance()
                self.expect(TokenType.COLON)
                if self.match(TokenType.STRING):
                    version = self.advance().value
                elif self.match(TokenType.INT):
                    version = str(self.advance().value)
                else:
                    self.error("Expected string or int for version")
                if self.match(TokenType.SEMICOLON):
                    self.advance()
            elif self.match(TokenType.TTL):
                self.advance()
                self.expect(TokenType.COLON)
                self.skip_newlines()
                ttl = self.parse_ttl_block()
            elif self.match(TokenType.KEY):
                self.advance()
                self.expect(TokenType.COLON)
                self.skip_newlines()
                key = self.parse_key_block()
            else:
                self.error(f"Unexpected token in cache block: {self.current().type}")
            self.skip_newlines()

        self.expect(TokenType.RBRACE)
        return CacheConfig(
            enabled=enabled,
            strategy=strategy,
            location=location,
            version=version,
            ttl=ttl,
            key=key
        )

    def parse_ttl_block(self) -> CacheTTL:
        """Parse TTL configuration block."""
        self.expect(TokenType.LBRACE)
        self.skip_newlines()

        seconds = None
        minutes = None
        hours = None
        days = None

        while not self.match(TokenType.RBRACE):
            if self.match(TokenType.SECONDS):
                self.advance()
                self.expect(TokenType.COLON)
                seconds = self.expect(TokenType.INT).value
                if self.match(TokenType.SEMICOLON):
                    self.advance()
            elif self.match(TokenType.MINUTES):
                self.advance()
                self.expect(TokenType.COLON)
                minutes = self.expect(TokenType.INT).value
                if self.match(TokenType.SEMICOLON):
                    self.advance()
            elif self.match(TokenType.HOURS):
                self.advance()
                self.expect(TokenType.COLON)
                hours = self.expect(TokenType.INT).value
                if self.match(TokenType.SEMICOLON):
                    self.advance()
            elif self.match(TokenType.DAYS):
                self.advance()
                self.expect(TokenType.COLON)
                days = self.expect(TokenType.INT).value
                if self.match(TokenType.SEMICOLON):
                    self.advance()
            else:
                self.error(f"Unexpected token in ttl block: {self.current().type}")
            self.skip_newlines()

        self.expect(TokenType.RBRACE)
        return CacheTTL(seconds=seconds, minutes=minutes, hours=hours, days=days)

    def parse_key_block(self) -> CacheKey:
        """Parse cache key configuration block."""
        self.expect(TokenType.LBRACE)
        self.skip_newlines()

        include = None
        exclude = None

        while not self.match(TokenType.RBRACE):
            if self.match(TokenType.INCLUDE):
                self.advance()
                self.expect(TokenType.COLON)
                self.skip_newlines()
                include = self.parse_string_list()
                if self.match(TokenType.SEMICOLON):
                    self.advance()
            elif self.match(TokenType.EXCLUDE):
                self.advance()
                self.expect(TokenType.COLON)
                self.skip_newlines()
                exclude = self.parse_string_list()
                if self.match(TokenType.SEMICOLON):
                    self.advance()
            else:
                self.error(f"Unexpected token in key block: {self.current().type}")
            self.skip_newlines()

        self.expect(TokenType.RBRACE)
        return CacheKey(include=include, exclude=exclude)

    def parse_string_list(self) -> List[str]:
        """Parse a list of strings."""
        self.expect(TokenType.LBRACKET)
        items = []
        while not self.match(TokenType.RBRACKET):
            if self.match(TokenType.STRING):
                items.append(self.advance().value)
            elif self.match(TokenType.IDENTIFIER):
                items.append(self.advance().value)
            else:
                self.error("Expected string in list")
            if self.match(TokenType.COMMA):
                self.advance()
        self.expect(TokenType.RBRACKET)
        return items

    def parse_success_block(self) -> Dict[str, SuccessBlock]:
        """Parse success block - mapping of names to expression blocks."""
        self.expect(TokenType.LBRACE)
        self.skip_newlines()

        blocks = {}
        while not self.match(TokenType.RBRACE):
            name = self.expect(TokenType.IDENTIFIER).value
            self.expect(TokenType.COLON)
            self.skip_newlines()

            # Check if it's a block (with braces) or a simple expression
            if self.match(TokenType.LBRACE):
                # Multi-line block
                self.expect(TokenType.LBRACE)
                self.skip_newlines()
                statements = []
                while not self.match(TokenType.RBRACE):
                    stmt = self.parse_statement()
                    statements.append(stmt)
                    self.skip_newlines()
                self.expect(TokenType.RBRACE)
                self.skip_newlines()

                is_multiline = len(statements) > 1 or (len(statements) == 1 and isinstance(statements[0], ReturnStmt))
                blocks[name] = SuccessBlock(statements=statements, is_multiline=is_multiline)
            else:
                # Simple expression
                expr = self.parse_expression()
                if self.match(TokenType.SEMICOLON):
                    self.advance()
                self.skip_newlines()
                blocks[name] = SuccessBlock(
                    statements=[ExprStmt(expr)],
                    is_multiline=False
                )

        self.expect(TokenType.RBRACE)
        return blocks

    def parse_requires_block(self) -> List[Stmt]:
        """Parse requires block - list of statements."""
        self.expect(TokenType.LBRACE)
        self.skip_newlines()

        statements = []
        while not self.match(TokenType.RBRACE):
            stmt = self.parse_requires_statement()
            if stmt:
                statements.append(stmt)
            self.skip_newlines()

        self.expect(TokenType.RBRACE)
        return statements

    def parse_requires_statement(self) -> Optional[Stmt]:
        """Parse a statement in requires block."""
        if self.match(TokenType.FAILS):
            return self.parse_fails_statement()
        elif self.match(TokenType.FAIL):
            return self.parse_fail_statement()
        elif self.match(TokenType.IF):
            return self.parse_if_statement()
        elif self.match(TokenType.FOR):
            return self.parse_for_statement()
        elif self.match(TokenType.WHILE):
            return self.parse_while_statement()
        elif self.match(TokenType.IDENTIFIER):
            # Could be task call or variable declaration
            if self.peek(1).type == TokenType.LPAREN:
                # Task call
                return self.parse_task_call()
            elif self.peek(1).type == TokenType.IDENTIFIER:
                # Variable declaration: type name = value
                return self.parse_variable_declaration()
            else:
                # Expression statement
                expr = self.parse_expression()
                if self.match(TokenType.SEMICOLON):
                    self.advance()
                return ExprStmt(expr)
        else:
            expr = self.parse_expression()
            if self.match(TokenType.SEMICOLON):
                self.advance()
            return ExprStmt(expr)

    def parse_statement(self) -> Stmt:
        """Parse a statement in success block."""
        if self.match(TokenType.RETURN):
            self.advance()
            value = self.parse_expression()
            if self.match(TokenType.SEMICOLON):
                self.advance()
            return ReturnStmt(value)
        elif self.match(TokenType.IF):
            return self.parse_if_statement()
        elif self.match(TokenType.FOR):
            return self.parse_for_statement()
        elif self.match(TokenType.WHILE):
            return self.parse_while_statement()
        elif self.match(TokenType.BREAK):
            self.advance()
            if self.match(TokenType.SEMICOLON):
                self.advance()
            return BreakStmt()
        elif self.match(TokenType.CONTINUE):
            self.advance()
            if self.match(TokenType.SEMICOLON):
                self.advance()
            return ContinueStmt()
        else:
            expr = self.parse_expression()
            if self.match(TokenType.SEMICOLON):
                self.advance()
            return ExprStmt(expr)

    def parse_fails_statement(self) -> FailsStmt:
        self.expect(TokenType.FAILS)
        self.expect(TokenType.LPAREN)
        task_call = self.parse_task_call()
        self.expect(TokenType.RPAREN)
        if self.match(TokenType.SEMICOLON):
            self.advance()
        return FailsStmt(task_call)

    def parse_fail_statement(self) -> FailsStmt:
        self.expect(TokenType.FAIL)
        self.expect(TokenType.LPAREN)
        task_call = self.parse_task_call()
        self.expect(TokenType.RPAREN)
        if self.match(TokenType.SEMICOLON):
            self.advance()
        return FailsStmt(task_call)

    def parse_task_call(self) -> TaskCallStmt:
        name = self.expect(TokenType.IDENTIFIER).value
        self.expect(TokenType.LPAREN)

        positional_args = []
        named_args = {}

        while not self.match(TokenType.RPAREN):
            # Check if it's a named arg
            if self.match(TokenType.IDENTIFIER) and self.peek(1).type == TokenType.EQUALS:
                arg_name = self.advance().value
                self.expect(TokenType.EQUALS)
                arg_value = self.parse_expression()
                named_args[arg_name] = arg_value
            else:
                positional_args.append(self.parse_expression())

            if self.match(TokenType.COMMA):
                self.advance()

        self.expect(TokenType.RPAREN)
        if self.match(TokenType.SEMICOLON):
            self.advance()

        return TaskCallStmt(task_name=name, positional_args=positional_args, named_args=named_args)

    def parse_variable_declaration(self) -> AssignmentExpr:
        """Parse: type name = value;"""
        type_name = self.parse_type_name()
        name = self.expect(TokenType.IDENTIFIER).value
        self.expect(TokenType.EQUALS)
        value = self.parse_expression()
        if self.match(TokenType.SEMICOLON):
            self.advance()
        return AssignmentExpr(type_name=type_name, name=name, value=value)

    def parse_if_statement(self) -> IfStmt:
        self.expect(TokenType.IF)
        self.expect(TokenType.LPAREN)
        condition = self.parse_expression()
        self.expect(TokenType.RPAREN)
        self.skip_newlines()
        then_block = self.parse_block()

        elif_clauses = []
        else_block = None

        while self.match(TokenType.ELIF):
            self.advance()
            self.expect(TokenType.LPAREN)
            elif_cond = self.parse_expression()
            self.expect(TokenType.RPAREN)
            self.skip_newlines()
            elif_block = self.parse_block()
            elif_clauses.append((elif_cond, elif_block))

        if self.match(TokenType.ELSE):
            self.advance()
            self.skip_newlines()
            else_block = self.parse_block()

        return IfStmt(condition=condition, then_block=then_block, elif_clauses=elif_clauses, else_block=else_block)

    def parse_for_statement(self) -> ForStmt:
        self.expect(TokenType.FOR)
        self.expect(TokenType.LPAREN)

        # Parse init
        init = None
        if not self.match(TokenType.SEMICOLON):
            if self.match(TokenType.IDENTIFIER):
                # Check if it's a declaration
                if self.peek(1).type in (TokenType.STRING_TYPE, TokenType.INT_TYPE,
                                          TokenType.FLOAT_TYPE, TokenType.BOOL_TYPE,
                                          TokenType.LIST_TYPE, TokenType.IDENTIFIER):
                    # Could be "int i = 0" pattern - need to look ahead more
                    pass
            # Try to parse as declaration first
            saved_pos = self.pos
            try:
                if self.match(TokenType.INT_TYPE, TokenType.FLOAT_TYPE, TokenType.BOOL_TYPE,
                              TokenType.STRING_TYPE, TokenType.LIST_TYPE):
                    init = self.parse_variable_declaration()
                else:
                    init = self.parse_expression()
                    if self.match(TokenType.SEMICOLON):
                        self.advance()
                    init = ExprStmt(init)
            except:
                self.pos = saved_pos
                init = self.parse_expression()
                if self.match(TokenType.SEMICOLON):
                    self.advance()
                init = ExprStmt(init)
        else:
            self.advance()  # skip ;

        if init is None:
            self.expect(TokenType.SEMICOLON)

        # Parse condition
        condition = None
        if not self.match(TokenType.SEMICOLON):
            condition = self.parse_expression()
        self.expect(TokenType.SEMICOLON)

        # Parse update
        update = None
        if not self.match(TokenType.RPAREN):
            update_expr = self.parse_expression()
            if self.match(TokenType.SEMICOLON):
                self.advance()
            update = ExprStmt(update_expr)

        self.expect(TokenType.RPAREN)
        self.skip_newlines()
        body = self.parse_block()

        return ForStmt(init=init, condition=condition, update=update, body=body)

    def parse_while_statement(self) -> WhileStmt:
        self.expect(TokenType.WHILE)
        self.expect(TokenType.LPAREN)
        condition = self.parse_expression()
        self.expect(TokenType.RPAREN)
        self.skip_newlines()
        body = self.parse_block()
        return WhileStmt(condition=condition, body=body)

    def parse_block(self) -> BlockStmt:
        self.expect(TokenType.LBRACE)
        self.skip_newlines()

        statements = []
        while not self.match(TokenType.RBRACE):
            stmt = self.parse_statement()
            statements.append(stmt)
            self.skip_newlines()

        self.expect(TokenType.RBRACE)
        return BlockStmt(statements=statements)

    def parse_expression(self) -> Expr:
        return self.parse_or_expression()

    def parse_or_expression(self) -> Expr:
        left = self.parse_and_expression()
        while self.match(TokenType.OR):
            op = self.advance().type
            right = self.parse_and_expression()
            left = BinaryExpr(op='||', left=left, right=right)
        return left

    def parse_and_expression(self) -> Expr:
        left = self.parse_equality_expression()
        while self.match(TokenType.AND):
            op = self.advance().type
            right = self.parse_equality_expression()
            left = BinaryExpr(op='&&', left=left, right=right)
        return left

    def parse_equality_expression(self) -> Expr:
        left = self.parse_comparison_expression()
        while self.match(TokenType.EQ, TokenType.NEQ):
            op = '==' if self.advance().type == TokenType.EQ else '!='
            right = self.parse_comparison_expression()
            left = BinaryExpr(op=op, left=left, right=right)
        return left

    def parse_comparison_expression(self) -> Expr:
        left = self.parse_additive_expression()
        while self.match(TokenType.LT, TokenType.GT, TokenType.LTE, TokenType.GTE):
            token = self.advance()
            op_map = {
                TokenType.LT: '<',
                TokenType.GT: '>',
                TokenType.LTE: '<=',
                TokenType.GTE: '>='
            }
            op = op_map[token.type]
            right = self.parse_additive_expression()
            left = BinaryExpr(op=op, left=left, right=right)
        return left

    def parse_additive_expression(self) -> Expr:
        left = self.parse_multiplicative_expression()
        while self.match(TokenType.PLUS, TokenType.MINUS, TokenType.PERCENT):
            token = self.advance()
            op_map = {
                TokenType.PLUS: '+',
                TokenType.MINUS: '-',
                TokenType.PERCENT: '%'
            }
            op = op_map[token.type]
            right = self.parse_multiplicative_expression()
            left = BinaryExpr(op=op, left=left, right=right)
        return left

    def parse_multiplicative_expression(self) -> Expr:
        left = self.parse_unary_expression()
        while self.match(TokenType.STAR, TokenType.SLASH):
            token = self.advance()
            op = '*' if token.type == TokenType.STAR else '/'
            right = self.parse_unary_expression()
            left = BinaryExpr(op=op, left=left, right=right)
        return left

    def parse_unary_expression(self) -> Expr:
        if self.match(TokenType.NOT, TokenType.MINUS):
            token = self.advance()
            op = '!' if token.type == TokenType.NOT else '-'
            operand = self.parse_unary_expression()
            return UnaryExpr(op=op, operand=operand)
        return self.parse_primary_expression()

    def parse_primary_expression(self) -> Expr:
        if self.match(TokenType.INT):
            return IntLiteral(self.advance().value)
        elif self.match(TokenType.FLOAT):
            return FloatLiteral(self.advance().value)
        elif self.match(TokenType.STRING):
            return StringLiteral(self.advance().value)
        elif self.match(TokenType.TRUE):
            self.advance()
            return BoolLiteral(True)
        elif self.match(TokenType.FALSE):
            self.advance()
            return BoolLiteral(False)
        elif self.match(TokenType.DOLLAR):
            return DollarVar(self.advance().value)
        elif self.match(TokenType.LBRACKET):
            return self.parse_list_literal()
        elif self.match(TokenType.LPAREN):
            self.advance()
            expr = self.parse_expression()
            self.expect(TokenType.RPAREN)
            return expr
        elif self.match(TokenType.IDENTIFIER):
            name = self.advance().value

            # Check for function call
            if self.match(TokenType.LPAREN):
                self.advance()
                args = []
                while not self.match(TokenType.RPAREN):
                    args.append(self.parse_expression())
                    if self.match(TokenType.COMMA):
                        self.advance()
                self.expect(TokenType.RPAREN)
                return FunctionCallExpr(name=name, args=args)

            # Check for array index
            while self.match(TokenType.LBRACKET):
                self.advance()
                index = self.parse_expression()
                self.expect(TokenType.RBRACKET)
                name = ArrayIndexExpr(array=IdentifierExpr(name), index=index) if isinstance(name, str) else ArrayIndexExpr(array=name, index=index)

            # Check for assignment
            if self.match(TokenType.EQUALS):
                self.advance()
                value = self.parse_expression()
                return AssignmentExpr(type_name=None, name=name if isinstance(name, str) else name, value=value)

            if isinstance(name, str):
                return IdentifierExpr(name)
            return name
        else:
            self.error(f"Unexpected token in expression: {self.current().type}")

    def parse_list_literal(self) -> ListLiteral:
        self.expect(TokenType.LBRACKET)
        elements = []
        while not self.match(TokenType.RBRACKET):
            elements.append(self.parse_expression())
            if self.match(TokenType.COMMA):
                self.advance()
        self.expect(TokenType.RBRACKET)
        return ListLiteral(elements=elements)