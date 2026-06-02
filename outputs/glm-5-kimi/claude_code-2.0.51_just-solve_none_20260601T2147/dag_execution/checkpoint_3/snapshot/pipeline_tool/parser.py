"""Pipeline file parser with run block preprocessing."""

from typing import Any, Dict, List, Optional, Tuple

from lexer import Token, TokenType, Lexer
from ast_nodes import (
    ASTNode, Literal, Identifier, BinaryOp, UnaryOp, ArrayAccess,
    FunctionCall, TaskCall, CachedTaskCall, TemplateString, ListLiteral,
    ParameterAccess, WorkspaceAccess, VarDecl, IfStatement, ForStatement,
    WhileStatement, ReturnStatement, BreakStatement, ContinueStatement,
    ExpressionStatement, FailsTask, ParamDef, TaskDef, Pipeline, CacheConfig,
)


class ParseError(Exception):
    def __init__(self, message: str, line: int = None, column: int = None):
        self.message = message
        self.line = line
        self.column = column
        super().__init__(f"SYNTAX_ERROR: {message}")


class Parser:
    def __init__(self, tokens: List[Token], run_blocks: Dict[str, str] = None):
        self.tokens = tokens
        self.pos = 0
        self.run_blocks = run_blocks or {}

    def error(self, message: str) -> ParseError:
        token = self.current()
        return ParseError(message, token.line, token.column)

    def current(self) -> Token:
        if self.pos < len(self.tokens):
            return self.tokens[self.pos]
        return self.tokens[-1]

    def peek(self, offset: int = 0) -> Token:
        pos = self.pos + offset
        if pos < len(self.tokens):
            return self.tokens[pos]
        return self.tokens[-1]

    def advance(self) -> Token:
        token = self.current()
        self.pos += 1
        return token

    def match(self, *types: TokenType) -> bool:
        return self.current().type in types

    def expect(self, token_type: TokenType, message: str = None) -> Token:
        if self.current().type != token_type:
            msg = message or f"Expected {token_type.name}, got {self.current().type.name}"
            raise self.error(msg)
        return self.advance()

    def skip_newlines(self):
        while self.match(TokenType.NEWLINE):
            self.advance()

    def _skip_optional_colon(self):
        if self.match(TokenType.COLON):
            self.advance()

    def _skip_optional_semicolon(self):
        if self.match(TokenType.SEMICOLON):
            self.advance()

    def parse(self) -> Pipeline:
        tasks = {}
        self.skip_newlines()

        while not self.match(TokenType.EOF):
            if self.match(TokenType.TASK):
                task = self.parse_task()
                tasks[task.name] = task
            else:
                raise self.error(f"Expected task definition, got {self.current().type.name}")
            self.skip_newlines()

        return Pipeline(tasks=tasks)

    def parse_task(self) -> TaskDef:
        self.expect(TokenType.TASK)
        name = self.expect(TokenType.IDENTIFIER).value
        self.expect(TokenType.LBRACE)
        self.skip_newlines()

        params, inputs, run, success, requires, output, timeout, cache = \
            [], None, None, {}, [], None, None, None

        while not self.match(TokenType.RBRACE, TokenType.EOF):
            if self.match(TokenType.PARAMS):
                params = self.parse_params()
            elif self.match(TokenType.INPUTS):
                inputs = self.parse_inputs()
            elif self.match(TokenType.RUN):
                run = self.parse_run()
            elif self.match(TokenType.SUCCESS):
                success = self.parse_success()
            elif self.match(TokenType.REQUIRES):
                requires = self.parse_requires()
            elif self.match(TokenType.OUTPUT):
                output = self.parse_output()
            elif self.match(TokenType.TIMEOUT):
                timeout = self.parse_timeout()
            elif self.match(TokenType.CACHE):
                cache = self.parse_cache()
            else:
                raise self.error(f"Unexpected token in task: {self.current().type.name}")
            self.skip_newlines()

        self.expect(TokenType.RBRACE)
        return TaskDef(name=name, params=params, inputs=inputs, run=run,
                       success=success, requires=requires, output=output,
                       timeout=timeout, cache=cache)

    def parse_params(self) -> List[ParamDef]:
        self.expect(TokenType.PARAMS)
        self._skip_optional_colon()
        self.expect(TokenType.LBRACE)
        self.skip_newlines()

        params = []
        has_default = False

        while not self.match(TokenType.RBRACE, TokenType.EOF):
            param = self.parse_param_def()
            if param.default_value is not None:
                has_default = True
            elif has_default:
                raise self.error(f"Parameter '{param.name}' without default cannot follow one with default")
            params.append(param)
            self.skip_newlines()

        self.expect(TokenType.RBRACE)
        return params

    def parse_param_def(self) -> ParamDef:
        name = self.expect(TokenType.IDENTIFIER).value
        self.expect(TokenType.COLON)

        param_type, is_list = self.parse_type()
        default_value = None
        if self.match(TokenType.ASSIGN):
            self.advance()
            default_value = self.parse_literal_or_env()

        self._skip_optional_semicolon()
        return ParamDef(name=name, param_type=param_type, default_value=default_value, is_list_element_type=is_list)

    def parse_type(self) -> Tuple[str, bool]:
        type_map = {
            TokenType.STRING_TYPE: 'string',
            TokenType.INT_TYPE: 'int',
            TokenType.FLOAT_TYPE: 'float',
            TokenType.BOOL_TYPE: 'bool',
            TokenType.LIST_TYPE: 'list',
        }

        if self.current().type not in type_map:
            raise self.error(f"Expected type, got {self.current().type.name}")

        token = self.advance()
        param_type = type_map[token.type]

        if param_type == 'list' and self.match(TokenType.LBRACKET):
            self.advance()
            inner_type, _ = self.parse_type()
            self.expect(TokenType.RBRACKET)
            return inner_type, True

        return param_type, False

    def parse_literal_or_env(self) -> ASTNode:
        if self.match(TokenType.STRING):
            return Literal(self.advance().value)
        if self.match(TokenType.INT):
            return Literal(self.advance().value)
        if self.match(TokenType.FLOAT):
            return Literal(self.advance().value)
        if self.match(TokenType.TRUE):
            self.advance()
            return Literal(True)
        if self.match(TokenType.FALSE):
            self.advance()
            return Literal(False)
        if self.match(TokenType.LBRACKET):
            return self.parse_list_literal()
        if self.match(TokenType.DOLLAR):
            return self._parse_env_ref()
        raise self.error(f"Expected literal value, got {self.current().type.name}")

    def _parse_env_ref(self) -> TemplateString:
        """Parse ${...} environment variable reference."""
        self.advance()  # $
        if self.match(TokenType.LBRACE):
            self.advance()
            parts = []
            while not self.match(TokenType.RBRACE):
                if self.match(TokenType.IDENTIFIER):
                    parts.append(self.advance().value)
                elif self.match(TokenType.DOT):
                    self.advance()
                    parts.append('.')
                else:
                    raise self.error(f"Unexpected token in env var: {self.current().type.name}")
            self.expect(TokenType.RBRACE)
            return TemplateString(['${' + ''.join(parts) + '}'])
        if self.match(TokenType.IDENTIFIER):
            name = self.advance().value
            return TemplateString(['${' + name + '}'])
        raise self.error("Expected identifier after $")

    def parse_list_literal(self) -> ListLiteral:
        self.expect(TokenType.LBRACKET)
        elements = []
        while not self.match(TokenType.RBRACE, TokenType.EOF):
            elements.append(self.parse_literal_or_env())
            if self.match(TokenType.COMMA):
                self.advance()
        self.expect(TokenType.RBRACKET)
        return ListLiteral(elements)

    def parse_run(self) -> str:
        self.expect(TokenType.RUN)
        self._skip_optional_colon()
        self.expect(TokenType.LBRACE)

        if self.match(TokenType.IDENTIFIER) and self.current().value.startswith('__RUN_BLOCK_'):
            placeholder = self.advance().value
            self.expect(TokenType.RBRACE)
            return self.run_blocks.get(placeholder, '')

        depth = 1
        content = []

        while depth > 0 and not self.match(TokenType.EOF):
            token = self.advance()
            if token.type == TokenType.LBRACE:
                depth += 1
                content.append('{')
            elif token.type == TokenType.RBRACE:
                depth -= 1
                if depth > 0:
                    content.append('}')
            elif token.type == TokenType.NEWLINE:
                content.append('\n')
            elif token.type == TokenType.STRING:
                content.append(f'"{token.value.replace(chr(34), chr(92) + chr(34))}"')
            elif token.type == TokenType.COMMENT:
                content.append(f'//{token.value}')
            else:
                content.append(str(token.value) if token.value else '')

        return ''.join(content).strip()

    def parse_success(self) -> Dict[str, List[ASTNode]]:
        self.expect(TokenType.SUCCESS)
        self._skip_optional_colon()
        self.expect(TokenType.LBRACE)
        self.skip_newlines()

        success = {}
        while not self.match(TokenType.RBRACE, TokenType.EOF):
            name = self.expect(TokenType.IDENTIFIER).value
            self.expect(TokenType.COLON)

            if self.match(TokenType.LBRACE):
                self.advance()
                self.skip_newlines()
                statements = self.parse_block_statements()
                self.expect(TokenType.RBRACE)
                success[name] = statements
            else:
                expr = self.parse_expression()
                self._skip_optional_semicolon()
                success[name] = [ReturnStatement(value=expr)]

            self.skip_newlines()

        self.expect(TokenType.RBRACE)
        return success

    def parse_requires(self) -> List[ASTNode]:
        self.expect(TokenType.REQUIRES)
        self._skip_optional_colon()
        self.expect(TokenType.LBRACE)
        self.skip_newlines()

        statements = self.parse_block_statements()
        self.expect(TokenType.RBRACE)
        return statements

    def parse_output(self) -> ASTNode:
        self.expect(TokenType.OUTPUT)
        self._skip_optional_colon()
        return self.parse_expression()

    def parse_timeout(self) -> float:
        self.expect(TokenType.TIMEOUT)
        self._skip_optional_colon()
        if self.match(TokenType.FLOAT):
            return self.advance().value
        if self.match(TokenType.INT):
            return float(self.advance().value)
        raise self.error("Expected number for timeout")

    def parse_inputs(self) -> List[str]:
        self.expect(TokenType.INPUTS)
        self._skip_optional_colon()
        self.expect(TokenType.LBRACKET)
        self.skip_newlines()

        inputs = []
        while not self.match(TokenType.RBRACKET, TokenType.EOF):
            if self.match(TokenType.STRING):
                inputs.append(self.advance().value)
            elif self.match(TokenType.IDENTIFIER):
                parts = [self.advance().value]
                while self.match(TokenType.DOT, TokenType.SLASH, TokenType.IDENTIFIER):
                    parts.append(str(self.advance().value))
                inputs.append(''.join(parts))
            elif self.match(TokenType.DOLLAR):
                self.advance()
                if self.match(TokenType.LBRACE):
                    self.advance()
                    parts = ['${']
                    while not self.match(TokenType.RBRACE):
                        parts.append(str(self.advance().value))
                    self.expect(TokenType.RBRACE)
                    parts.append('}')
                    inputs.append(''.join(parts))
            else:
                raise self.error(f"Expected input file path, got {self.current().type.name}")
            if self.match(TokenType.COMMA):
                self.advance()
            self.skip_newlines()

        self.expect(TokenType.RBRACKET)
        return inputs

    def parse_cache(self) -> CacheConfig:
        self.expect(TokenType.CACHE)
        self._skip_optional_colon()
        self.expect(TokenType.LBRACE)
        self.skip_newlines()

        config = CacheConfig()

        while not self.match(TokenType.RBRACE, TokenType.EOF):
            key_token = self.current()

            if key_token.type == TokenType.ENABLED:
                self.advance()
                self._skip_optional_colon()
                config.enabled = self._parse_bool_value()
            elif key_token.type == TokenType.STRATEGY:
                self.advance()
                self._skip_optional_colon()
                config.strategy = self._parse_string_or_ident()
            elif key_token.type == TokenType.LOCATION:
                self.advance()
                self._skip_optional_colon()
                config.location = self._parse_string_or_ident()
            elif key_token.type == TokenType.VERSION:
                self.advance()
                self._skip_optional_colon()
                config.version = self._parse_version_value()
            elif key_token.type == TokenType.TTL:
                self.advance()
                self._skip_optional_colon()
                self._parse_ttl_block(config)
            elif key_token.type == TokenType.KEY:
                self.advance()
                self._skip_optional_colon()
                self._parse_key_block(config)
            else:
                raise self.error(f"Unexpected cache config key: {key_token.type.name}")

            self._skip_optional_semicolon()
            self.skip_newlines()

        self.expect(TokenType.RBRACE)
        return config

    def _parse_bool_value(self) -> bool:
        if self.match(TokenType.TRUE):
            self.advance()
            return True
        if self.match(TokenType.FALSE):
            self.advance()
            return False
        raise self.error("Expected TRUE or FALSE")

    def _parse_string_or_ident(self) -> str:
        if self.match(TokenType.STRING):
            return self.advance().value
        if self.match(TokenType.IDENTIFIER):
            return self.advance().value
        raise self.error("Expected string value")

    def _parse_version_value(self) -> str:
        if self.match(TokenType.STRING):
            return self.advance().value
        if self.match(TokenType.IDENTIFIER):
            return self.advance().value
        if self.match(TokenType.INT):
            return str(self.advance().value)
        raise self.error("Expected value for version")

    def _parse_ttl_block(self, config: CacheConfig):
        self.expect(TokenType.LBRACE)
        self.skip_newlines()

        ttl_fields = {
            TokenType.SECONDS: 'ttl_seconds',
            TokenType.MINUTES: 'ttl_minutes',
            TokenType.HOURS: 'ttl_hours',
            TokenType.DAYS: 'ttl_days',
        }

        while not self.match(TokenType.RBRACE, TokenType.EOF):
            key = self.current()
            if key.type in ttl_fields:
                self.advance()
                self._skip_optional_colon()
                setattr(config, ttl_fields[key.type], self.advance().value)
            else:
                raise self.error(f"Unexpected TTL key: {self.current().type.name}")
            self.skip_newlines()

        self.expect(TokenType.RBRACE)

    def _parse_key_block(self, config: CacheConfig):
        self.expect(TokenType.LBRACE)
        self.skip_newlines()

        while not self.match(TokenType.RBRACE, TokenType.EOF):
            key_inner = self.current()
            if key_inner.type == TokenType.INCLUDE:
                self.advance()
                self._skip_optional_colon()
                config.key_include = self._parse_string_list()
            elif key_inner.type == TokenType.EXCLUDE:
                self.advance()
                self._skip_optional_colon()
                config.key_exclude = self._parse_string_list()
            else:
                raise self.error(f"Unexpected key config: {self.current().type.name}")
            self.skip_newlines()

        self.expect(TokenType.RBRACE)

    def _parse_string_list(self) -> List[str]:
        self.expect(TokenType.LBRACKET)
        items = []
        while not self.match(TokenType.RBRACKET, TokenType.EOF):
            if self.match(TokenType.STRING):
                items.append(self.advance().value)
            elif self.match(TokenType.IDENTIFIER):
                items.append(self.advance().value)
            else:
                raise self.error(f"Expected string, got {self.current().type.name}")
            if self.match(TokenType.COMMA):
                self.advance()
        self.expect(TokenType.RBRACKET)
        return items

    def parse_block_statements(self) -> List[ASTNode]:
        statements = []
        while not self.match(TokenType.RBRACE, TokenType.EOF):
            stmt = self.parse_statement()
            if stmt is not None:
                statements.append(stmt)
            self.skip_newlines()
        return statements

    def parse_statement(self) -> Optional[ASTNode]:
        if self.match(TokenType.STRING_TYPE, TokenType.INT_TYPE, TokenType.FLOAT_TYPE,
                      TokenType.BOOL_TYPE, TokenType.LIST_TYPE):
            return self.parse_var_decl()
        if self.match(TokenType.IF):
            return self.parse_if()
        if self.match(TokenType.FOR):
            return self.parse_for()
        if self.match(TokenType.WHILE):
            return self.parse_while()
        if self.match(TokenType.RETURN):
            return self.parse_return()
        if self.match(TokenType.BREAK):
            self.advance()
            self._skip_optional_semicolon()
            return BreakStatement()
        if self.match(TokenType.CONTINUE):
            self.advance()
            self._skip_optional_semicolon()
            return ContinueStatement()
        if self.match(TokenType.FAILS):
            return self.parse_fails()
        if self.match(TokenType.IDENTIFIER, TokenType.NOT, TokenType.LPAREN):
            # Check for CachedTask pattern: <task_name> <var_name> = CachedTask(...)
            if self.match(TokenType.IDENTIFIER):
                saved_pos = self.pos
                task_name = self.advance().value
                if self.match(TokenType.IDENTIFIER):
                    var_name = self.advance().value
                    if self.match(TokenType.ASSIGN):
                        self.advance()
                        if self.match(TokenType.CACHEDTASK):
                            cached_task = self._parse_cached_task(task_name, var_name)
                            self._skip_optional_semicolon()
                            return cached_task
                # Not a CachedTask pattern, restore position and parse as expression
                self.pos = saved_pos
            expr = self.parse_expression()
            self._skip_optional_semicolon()
            return ExpressionStatement(expr)
        return None

    def parse_var_decl(self) -> VarDecl:
        var_type, is_list = self.parse_type()
        name = self.expect(TokenType.IDENTIFIER).value
        value = None
        if self.match(TokenType.ASSIGN):
            self.advance()
            value = self.parse_expression()
        self._skip_optional_semicolon()
        return VarDecl(var_type=var_type, var_name=name, value=value, is_list_element_type=is_list)

    def parse_if(self) -> IfStatement:
        self.expect(TokenType.IF)
        self.expect(TokenType.LPAREN)
        condition = self.parse_expression()
        self.expect(TokenType.RPAREN)
        then_block = self._parse_brace_block()
        self.skip_newlines()

        elif_clauses = []
        while self.match(TokenType.ELIF):
            self.advance()
            self.expect(TokenType.LPAREN)
            elif_cond = self.parse_expression()
            self.expect(TokenType.RPAREN)
            elif_block = self._parse_brace_block()
            self.skip_newlines()
            elif_clauses.append((elif_cond, elif_block))

        else_block = None
        if self.match(TokenType.ELSE):
            self.advance()
            else_block = self._parse_brace_block()
            self.skip_newlines()

        return IfStatement(condition=condition, then_block=then_block,
                           elif_clauses=elif_clauses, else_block=else_block)

    def _parse_brace_block(self) -> List[ASTNode]:
        self.expect(TokenType.LBRACE)
        self.skip_newlines()
        stmts = self.parse_block_statements()
        self.expect(TokenType.RBRACE)
        return stmts

    def parse_for(self) -> ForStatement:
        self.expect(TokenType.FOR)
        self.expect(TokenType.LPAREN)

        saved_pos = self.pos
        if self.match(TokenType.IDENTIFIER):
            first = self.advance()
            if self.match(TokenType.IDENTIFIER) and self.current().value == 'in':
                self.advance()
                iterable = self.parse_expression()
                self.expect(TokenType.RPAREN)
                body = self._parse_brace_block()
                return ForStatement(init=None, condition=None, update=None, body=body,
                                    is_foreach=True, var_name=first.value, iterable=iterable)
            self.pos = saved_pos

        init = None
        if not self.match(TokenType.SEMICOLON):
            if self.match(TokenType.STRING_TYPE, TokenType.INT_TYPE, TokenType.FLOAT_TYPE,
                          TokenType.BOOL_TYPE, TokenType.LIST_TYPE):
                init = self.parse_var_decl()
            else:
                init = self.parse_expression()
        if self.match(TokenType.SEMICOLON):
            self.advance()

        condition = None
        if not self.match(TokenType.SEMICOLON):
            condition = self.parse_expression()
        self.expect(TokenType.SEMICOLON)

        update = None
        if not self.match(TokenType.RPAREN):
            update = self.parse_expression()
        self.expect(TokenType.RPAREN)
        body = self._parse_brace_block()

        return ForStatement(init=init, condition=condition, update=update, body=body, is_foreach=False)

    def parse_while(self) -> WhileStatement:
        self.expect(TokenType.WHILE)
        self.expect(TokenType.LPAREN)
        condition = self.parse_expression()
        self.expect(TokenType.RPAREN)
        body = self._parse_brace_block()
        return WhileStatement(condition=condition, body=body)

    def parse_return(self) -> ReturnStatement:
        self.expect(TokenType.RETURN)
        value = None
        if not self.match(TokenType.SEMICOLON, TokenType.RBRACE, TokenType.NEWLINE):
            value = self.parse_expression()
        self._skip_optional_semicolon()
        return ReturnStatement(value=value)

    def parse_fails(self) -> FailsTask:
        self.expect(TokenType.FAILS)
        self.expect(TokenType.LPAREN)
        task_call = self.parse_task_call()
        self.expect(TokenType.RPAREN)
        self._skip_optional_semicolon()
        return FailsTask(task_call=task_call)

    def _parse_cached_task(self, task_name: str, var_name: str) -> CachedTaskCall:
        """Parse CachedTask(...) with cache config overrides."""
        self.expect(TokenType.CACHEDTASK)
        self.expect(TokenType.LPAREN)

        cache_overrides = {}

        while not self.match(TokenType.RPAREN, TokenType.EOF):
            key, value = self._parse_cached_task_param()
            cache_overrides[key] = value
            if self.match(TokenType.COMMA):
                self.advance()

        self.expect(TokenType.RPAREN)

        task_call = TaskCall(name=task_name, args=[], kwargs={})
        return CachedTaskCall(task_call=task_call, variable_name=var_name,
                              cache_overrides=cache_overrides)

    def _parse_cached_task_param(self) -> Tuple[str, Any]:
        """Parse a single cache parameter override like ttl.hours=1 or ttl={hours:1}."""
        # Read the key - could be dotted like ttl.hours or simple like enabled
        # Keys can be keyword tokens (ttl, key, enabled, etc.) or identifiers
        cache_param_keywords = {
            TokenType.CACHE, TokenType.ENABLED, TokenType.STRATEGY, TokenType.LOCATION,
            TokenType.VERSION, TokenType.TTL, TokenType.KEY, TokenType.INCLUDE,
            TokenType.EXCLUDE, TokenType.SECONDS, TokenType.MINUTES, TokenType.HOURS,
            TokenType.DAYS,
        }
        if self.current().type in cache_param_keywords:
            key_parts = [self.advance().value]
        elif self.match(TokenType.IDENTIFIER):
            key_parts = [self.advance().value]
        else:
            raise self.error(f"Expected cache parameter name, got {self.current().type.name}")

        while self.match(TokenType.DOT):
            self.advance()
            if self.current().type in cache_param_keywords:
                key_parts.append(self.advance().value)
            elif self.match(TokenType.IDENTIFIER):
                key_parts.append(self.advance().value)
            else:
                raise self.error(f"Expected cache parameter name, got {self.current().type.name}")
        key = '.'.join(key_parts)

        self.expect(TokenType.ASSIGN)

        # Parse the value - could be a literal, a dict literal, etc.
        if self.match(TokenType.LBRACE):
            # Dict literal like {hours: 1, seconds: 0}
            value = self._parse_cache_dict_literal()
        elif self.match(TokenType.TRUE):
            self.advance()
            value = True
        elif self.match(TokenType.FALSE):
            self.advance()
            value = False
        elif self.match(TokenType.INT):
            value = self.advance().value
        elif self.match(TokenType.FLOAT):
            value = self.advance().value
        elif self.match(TokenType.STRING):
            value = self.advance().value
        elif self.match(TokenType.LBRACKET):
            value = self._parse_string_list()
        else:
            value = self._parse_string_or_ident()

        return key, value

    def _parse_cache_dict_literal(self) -> Dict[str, Any]:
        """Parse a dict literal like {hours: 1, seconds: 0}."""
        self.expect(TokenType.LBRACE)
        result = {}
        while not self.match(TokenType.RBRACE, TokenType.EOF):
            # key can be a keyword token (seconds, minutes, etc.) or identifier
            if self.current().type in (TokenType.SECONDS, TokenType.MINUTES,
                                       TokenType.HOURS, TokenType.DAYS,
                                       TokenType.INCLUDE, TokenType.EXCLUDE):
                key = self.advance().value
            elif self.match(TokenType.IDENTIFIER):
                key = self.advance().value
            else:
                key = self.advance().value

            self._skip_optional_colon()

            if self.match(TokenType.INT):
                val = self.advance().value
            elif self.match(TokenType.FLOAT):
                val = self.advance().value
            elif self.match(TokenType.STRING):
                val = self.advance().value
            elif self.match(TokenType.TRUE):
                self.advance()
                val = True
            elif self.match(TokenType.FALSE):
                self.advance()
                val = False
            else:
                val = self._parse_string_or_ident()

            result[key] = val
            if self.match(TokenType.COMMA):
                self.advance()

        self.expect(TokenType.RBRACE)
        return result

    # Expression parsing - precedence climbing

    def parse_expression(self) -> ASTNode:
        return self.parse_or()

    def parse_or(self) -> ASTNode:
        left = self.parse_and()
        while self.match(TokenType.OR):
            op = self.advance().value
            left = BinaryOp(op=op, left=left, right=self.parse_and())
        return left

    def parse_and(self) -> ASTNode:
        left = self.parse_equality()
        while self.match(TokenType.AND):
            op = self.advance().value
            left = BinaryOp(op=op, left=left, right=self.parse_equality())
        return left

    def parse_equality(self) -> ASTNode:
        left = self.parse_comparison()
        while self.match(TokenType.EQ, TokenType.NEQ):
            op = self.advance().value
            left = BinaryOp(op=op, left=left, right=self.parse_comparison())
        return left

    def parse_comparison(self) -> ASTNode:
        left = self.parse_additive()
        while self.match(TokenType.LT, TokenType.GT, TokenType.LTE, TokenType.GTE):
            op = self.advance().value
            left = BinaryOp(op=op, left=left, right=self.parse_additive())
        return left

    def parse_additive(self) -> ASTNode:
        left = self.parse_multiplicative()
        while self.match(TokenType.PLUS, TokenType.MINUS, TokenType.PERCENT):
            op = self.advance().value
            left = BinaryOp(op=op, left=left, right=self.parse_multiplicative())
        return left

    def parse_multiplicative(self) -> ASTNode:
        left = self.parse_unary()
        while self.match(TokenType.STAR, TokenType.SLASH):
            op = self.advance().value
            left = BinaryOp(op=op, left=left, right=self.parse_unary())
        return left

    def parse_unary(self) -> ASTNode:
        if self.match(TokenType.NOT, TokenType.MINUS):
            op = self.advance().value
            return UnaryOp(op=op, operand=self.parse_unary())
        return self.parse_postfix()

    def parse_postfix(self) -> ASTNode:
        expr = self.parse_primary()
        while True:
            if self.match(TokenType.LPAREN):
                self.advance()
                args, kwargs = self.parse_arguments()
                self.expect(TokenType.RPAREN)
                if isinstance(expr, Identifier):
                    if expr.name in ('len', 'contains', 'equals', 'exists', 'fail'):
                        expr = FunctionCall(name=expr.name, args=args, kwargs=kwargs)
                    else:
                        expr = TaskCall(name=expr.name, args=args, kwargs=kwargs)
                else:
                    expr = FunctionCall(
                        name=expr.name if hasattr(expr, 'name') else str(expr),
                        args=args, kwargs=kwargs
                    )
            elif self.match(TokenType.LBRACKET):
                self.advance()
                index = self.parse_expression()
                self.expect(TokenType.RBRACKET)
                expr = ArrayAccess(array=expr, index=index)
            elif self.match(TokenType.DOT):
                self.advance()
                member = self.expect(TokenType.IDENTIFIER).value
                if isinstance(expr, Identifier):
                    expr = Identifier(name=f"{expr.name}.{member}")
                else:
                    expr = Identifier(name=f"?.{member}")
            else:
                break
        return expr

    def parse_primary(self) -> ASTNode:
        if self.match(TokenType.INT):
            return Literal(self.advance().value)
        if self.match(TokenType.FLOAT):
            return Literal(self.advance().value)
        if self.match(TokenType.STRING):
            value = self.advance().value
            if '${' in value:
                return self.parse_template_string(value)
            return Literal(value)
        if self.match(TokenType.TRUE):
            self.advance()
            return Literal(True)
        if self.match(TokenType.FALSE):
            self.advance()
            return Literal(False)
        if self.match(TokenType.LBRACKET):
            return self.parse_list_literal_expr()
        if self.match(TokenType.LPAREN):
            self.advance()
            expr = self.parse_expression()
            self.expect(TokenType.RPAREN)
            return expr
        if self.match(TokenType.IDENTIFIER):
            name = self.advance().value
            if name == 'params' and self.match(TokenType.DOT):
                self.advance()
                return ParameterAccess(param_name=self.expect(TokenType.IDENTIFIER).value)
            if name == 'workspace':
                return WorkspaceAccess()
            return Identifier(name=name)

        raise self.error(f"Unexpected token in expression: {self.current().type.name}")

    def parse_template_string(self, value: str) -> TemplateString:
        parts = []
        i = 0
        current = ""

        while i < len(value):
            if i + 1 < len(value) and value[i:i+2] == '${':
                if current:
                    parts.append(current)
                    current = ""
                j = i + 2
                brace_count = 1
                while j < len(value) and brace_count > 0:
                    if value[j] == '{':
                        brace_count += 1
                    elif value[j] == '}':
                        brace_count -= 1
                    j += 1
                parts.append(self._parse_template_expr(value[i+2:j-1]))
                i = j
            else:
                current += value[i]
                i += 1

        if current:
            parts.append(current)
        return TemplateString(parts=parts)

    def _parse_template_expr(self, expr_str: str) -> ASTNode:
        expr_str = expr_str.strip()
        if expr_str == 'workspace':
            return WorkspaceAccess()
        if expr_str.startswith('params.'):
            return ParameterAccess(param_name=expr_str[7:])
        if not any(c in expr_str for c in '(){}[];:=+-*/%<>!&|'):
            return TemplateString(['${' + expr_str + '}'])
        lexer = Lexer(expr_str)
        tokens = lexer.filter_tokens(lexer.tokenize())
        return Parser(tokens, self.run_blocks).parse_expression()

    def parse_list_literal_expr(self) -> ListLiteral:
        self.expect(TokenType.LBRACKET)
        elements = []
        while not self.match(TokenType.RBRACKET, TokenType.EOF):
            elements.append(self.parse_expression())
            if self.match(TokenType.COMMA):
                self.advance()
        self.expect(TokenType.RBRACKET)
        return ListLiteral(elements)

    def parse_arguments(self) -> Tuple[List[ASTNode], Dict[str, ASTNode]]:
        args, kwargs = [], []
        while not self.match(TokenType.RPAREN, TokenType.EOF):
            if self.match(TokenType.IDENTIFIER) and self.peek(1).type == TokenType.ASSIGN:
                name = self.advance().value
                self.advance()  # =
                kwargs.append((name, self.parse_expression()))
            else:
                args.append(self.parse_expression())
            if self.match(TokenType.COMMA):
                self.advance()
        return args, dict(kwargs)

    def parse_task_call(self) -> TaskCall:
        name = self.expect(TokenType.IDENTIFIER).value
        self.expect(TokenType.LPAREN)
        args, kwargs = self.parse_arguments()
        self.expect(TokenType.RPAREN)
        return TaskCall(name=name, args=args, kwargs=kwargs)


def preprocess_pipeline(source: str) -> Tuple[str, Dict[str, str]]:
    """Extract run block contents from source, replacing each with a placeholder."""
    run_blocks: Dict[str, str] = {}
    result = []
    i = 0

    while i < len(source):
        if source[i:i+3] != 'run':
            result.append(source[i])
            i += 1
            continue

        brace_start = _find_run_brace_start(source, i + 3)
        if brace_start is None:
            result.append(source[i])
            i += 1
            continue

        end, content = _extract_brace_content(source, brace_start)
        placeholder = f'__RUN_BLOCK_{len(run_blocks)}__'
        run_blocks[placeholder] = content
        result.append(source[i:brace_start + 1])
        result.append(placeholder + ' }')
        i = end

    return ''.join(result), run_blocks


def _find_run_brace_start(source: str, j: int) -> Optional[int]:
    while j < len(source) and source[j] in ' \t\n':
        j += 1
    if j < len(source) and source[j] == '{':
        return j
    if j < len(source) and source[j] == ':':
        j += 1
        while j < len(source) and source[j] in ' \t\n':
            j += 1
        if j < len(source) and source[j] == '{':
            return j
    return None


def _extract_brace_content(source: str, brace_start: int) -> Tuple[int, str]:
    k = brace_start + 1
    depth = 1
    while k < len(source) and depth > 0:
        if source[k] == '{':
            depth += 1
        elif source[k] == '}':
            depth -= 1
        elif source[k] in ('"', "'"):
            quote = source[k]
            k += 1
            while k < len(source) and source[k] != quote:
                if source[k] == '\\':
                    k += 1
                k += 1
        elif source[k:k+2] == '//':
            while k < len(source) and source[k] != '\n':
                k += 1
        k += 1

    return k, source[brace_start + 1:k - 1].strip()


def parse_pipeline(source: str) -> Pipeline:
    preprocessed, run_blocks = preprocess_pipeline(source)
    lexer = Lexer(preprocessed)
    tokens = lexer.filter_tokens(lexer.tokenize())
    return Parser(tokens, run_blocks).parse()
