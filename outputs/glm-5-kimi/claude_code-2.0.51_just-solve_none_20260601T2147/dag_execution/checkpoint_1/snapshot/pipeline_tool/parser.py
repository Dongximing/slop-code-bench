"""
Pipeline file parser with run block preprocessing.
"""

import re
from typing import Any, Dict, List, Optional, Tuple, Union
from lexer import Token, TokenType, Lexer, LexerError
from ast_nodes import *


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
        return self.tokens[-1]  # EOF

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

    def parse(self) -> Pipeline:
        """Parse the entire pipeline file."""
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
        """Parse a task definition."""
        self.expect(TokenType.TASK)
        name = self.expect(TokenType.IDENTIFIER).value
        self.expect(TokenType.LBRACE)
        self.skip_newlines()

        params = []
        run = None
        success = {}
        requires = []
        output = None
        timeout = None

        while not self.match(TokenType.RBRACE, TokenType.EOF):
            if self.match(TokenType.PARAMS):
                params = self.parse_params()
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
            else:
                raise self.error(f"Unexpected token in task: {self.current().type.name}")
            self.skip_newlines()

        self.expect(TokenType.RBRACE)
        return TaskDef(
            name=name,
            params=params,
            run=run,
            success=success,
            requires=requires,
            output=output,
            timeout=timeout
        )

    def parse_params(self) -> List[ParamDef]:
        """Parse parameter definitions."""
        self.expect(TokenType.PARAMS)
        if self.match(TokenType.COLON):
            self.advance()
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
        """Parse a single parameter definition."""
        name = self.expect(TokenType.IDENTIFIER).value
        self.expect(TokenType.COLON)

        # Parse type
        param_type, is_list = self.parse_type()

        # Parse optional default value
        default_value = None
        if self.match(TokenType.ASSIGN):
            self.advance()
            default_value = self.parse_literal_or_env()

        # Optional semicolon
        if self.match(TokenType.SEMICOLON):
            self.advance()

        return ParamDef(
            name=name,
            param_type=param_type,
            default_value=default_value,
            is_list_element_type=is_list
        )

    def parse_type(self) -> Tuple[str, bool]:
        """Parse a type, returns (type_name, is_list)."""
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
        is_list = False

        # Check for list[T]
        if param_type == 'list':
            if self.match(TokenType.LBRACKET):
                self.advance()
                inner_type, _ = self.parse_type()
                self.expect(TokenType.RBRACKET)
                is_list = True
                param_type = inner_type

        return param_type, is_list

    def parse_literal_or_env(self) -> ASTNode:
        """Parse a literal value or environment variable reference."""
        if self.match(TokenType.STRING):
            return Literal(self.advance().value)
        elif self.match(TokenType.INT):
            return Literal(self.advance().value)
        elif self.match(TokenType.FLOAT):
            return Literal(self.advance().value)
        elif self.match(TokenType.TRUE):
            self.advance()
            return Literal(True)
        elif self.match(TokenType.FALSE):
            self.advance()
            return Literal(False)
        elif self.match(TokenType.LBRACKET):
            return self.parse_list_literal()
        elif self.match(TokenType.DOLLAR):
            # Environment variable
            self.advance()
            if self.match(TokenType.LBRACE):
                self.advance()
                # Read until closing brace
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
            elif self.match(TokenType.IDENTIFIER):
                name = self.advance().value
                return TemplateString(['${' + name + '}'])
            else:
                raise self.error("Expected identifier after $")
        else:
            raise self.error(f"Expected literal value, got {self.current().type.name}")

    def parse_list_literal(self) -> ListLiteral:
        """Parse a list literal."""
        self.expect(TokenType.LBRACKET)
        elements = []

        while not self.match(TokenType.RBRACE, TokenType.EOF):
            elements.append(self.parse_literal_or_env())
            if self.match(TokenType.COMMA):
                self.advance()

        self.expect(TokenType.RBRACKET)
        return ListLiteral(elements)

    def parse_run(self) -> str:
        """Parse a run block - returns raw string content."""
        self.expect(TokenType.RUN)

        # Colon is optional
        if self.match(TokenType.COLON):
            self.advance()

        self.expect(TokenType.LBRACE)

        # Check if this is a preprocessed run block placeholder
        if self.match(TokenType.IDENTIFIER) and self.current().value.startswith('__RUN_BLOCK_'):
            placeholder = self.advance().value
            self.expect(TokenType.RBRACE)
            return self.run_blocks.get(placeholder, '')

        # Collect everything until matching closing brace
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
                # Preserve quotes for strings
                escaped = token.value.replace('"', '\\"')
                content.append(f'"{escaped}"')
            elif token.type == TokenType.IDENTIFIER:
                content.append(token.value)
            elif token.type == TokenType.DOLLAR:
                content.append('$')
            elif token.type == TokenType.INT:
                content.append(str(token.value))
            elif token.type == TokenType.FLOAT:
                content.append(str(token.value))
            elif token.type == TokenType.COMMENT:
                content.append(f'//{token.value}')
            else:
                content.append(str(token.value) if token.value else '')

        return ''.join(content).strip()

    def parse_success(self) -> Dict[str, List[ASTNode]]:
        """Parse a success block."""
        self.expect(TokenType.SUCCESS)
        if self.match(TokenType.COLON):
            self.advance()
        self.expect(TokenType.LBRACE)
        self.skip_newlines()

        success = {}

        while not self.match(TokenType.RBRACE, TokenType.EOF):
            name = self.expect(TokenType.IDENTIFIER).value
            self.expect(TokenType.COLON)

            if self.match(TokenType.LBRACE):
                # Block-style success criteria
                self.advance()
                self.skip_newlines()
                statements = self.parse_block_statements()
                self.expect(TokenType.RBRACE)
                success[name] = statements
            else:
                # Expression-style success criteria
                expr = self.parse_expression()
                if self.match(TokenType.SEMICOLON):
                    self.advance()
                success[name] = [ReturnStatement(value=expr)]

            self.skip_newlines()

        self.expect(TokenType.RBRACE)
        return success

    def parse_requires(self) -> List[ASTNode]:
        """Parse a requires block."""
        self.expect(TokenType.REQUIRES)
        if self.match(TokenType.COLON):
            self.advance()
        self.expect(TokenType.LBRACE)
        self.skip_newlines()

        statements = self.parse_block_statements()

        self.expect(TokenType.RBRACE)
        return statements

    def parse_output(self) -> ASTNode:
        """Parse output directory."""
        self.expect(TokenType.OUTPUT)
        if self.match(TokenType.COLON):
            self.advance()
        return self.parse_expression()

    def parse_timeout(self) -> float:
        """Parse timeout value."""
        self.expect(TokenType.TIMEOUT)
        if self.match(TokenType.COLON):
            self.advance()
        if self.match(TokenType.FLOAT):
            return self.advance().value
        elif self.match(TokenType.INT):
            return float(self.advance().value)
        else:
            raise self.error("Expected number for timeout")

    def parse_block_statements(self) -> List[ASTNode]:
        """Parse statements inside a block."""
        statements = []

        while not self.match(TokenType.RBRACE, TokenType.EOF):
            stmt = self.parse_statement()
            if stmt is not None:
                statements.append(stmt)
            self.skip_newlines()

        return statements

    def parse_statement(self) -> Optional[ASTNode]:
        """Parse a single statement."""
        # Variable declaration: type name = value;
        if self.match(TokenType.STRING_TYPE, TokenType.INT_TYPE, TokenType.FLOAT_TYPE,
                      TokenType.BOOL_TYPE, TokenType.LIST_TYPE):
            return self.parse_var_decl()

        # If statement
        if self.match(TokenType.IF):
            return self.parse_if()

        # For statement
        if self.match(TokenType.FOR):
            return self.parse_for()

        # While statement
        if self.match(TokenType.WHILE):
            return self.parse_while()

        # Return statement
        if self.match(TokenType.RETURN):
            return self.parse_return()

        # Break
        if self.match(TokenType.BREAK):
            self.advance()
            if self.match(TokenType.SEMICOLON):
                self.advance()
            return BreakStatement()

        # Continue
        if self.match(TokenType.CONTINUE):
            self.advance()
            if self.match(TokenType.SEMICOLON):
                self.advance()
            return ContinueStatement()

        # Fails task call
        if self.match(TokenType.FAILS):
            return self.parse_fails()

        # Expression statement (including task calls)
        if self.match(TokenType.IDENTIFIER, TokenType.NOT, TokenType.LPAREN):
            expr = self.parse_expression()
            if self.match(TokenType.SEMICOLON):
                self.advance()
            return ExpressionStatement(expr)

        return None

    def parse_var_decl(self) -> VarDecl:
        """Parse variable declaration."""
        var_type, is_list = self.parse_type()
        name = self.expect(TokenType.IDENTIFIER).value

        value = None
        if self.match(TokenType.ASSIGN):
            self.advance()
            value = self.parse_expression()

        if self.match(TokenType.SEMICOLON):
            self.advance()

        return VarDecl(
            var_type=var_type,
            var_name=name,
            value=value,
            is_list_element_type=is_list
        )

    def parse_if(self) -> IfStatement:
        """Parse if/elif/else statement."""
        self.expect(TokenType.IF)
        self.expect(TokenType.LPAREN)
        condition = self.parse_expression()
        self.expect(TokenType.RPAREN)
        self.expect(TokenType.LBRACE)
        self.skip_newlines()

        then_block = self.parse_block_statements()
        self.expect(TokenType.RBRACE)
        self.skip_newlines()

        elif_clauses = []
        while self.match(TokenType.ELIF):
            self.advance()
            self.expect(TokenType.LPAREN)
            elif_cond = self.parse_expression()
            self.expect(TokenType.RPAREN)
            self.expect(TokenType.LBRACE)
            self.skip_newlines()
            elif_block = self.parse_block_statements()
            self.expect(TokenType.RBRACE)
            self.skip_newlines()
            elif_clauses.append((elif_cond, elif_block))

        else_block = None
        if self.match(TokenType.ELSE):
            self.advance()
            self.expect(TokenType.LBRACE)
            self.skip_newlines()
            else_block = self.parse_block_statements()
            self.expect(TokenType.RBRACE)
            self.skip_newlines()

        return IfStatement(
            condition=condition,
            then_block=then_block,
            elif_clauses=elif_clauses,
            else_block=else_block
        )

    def parse_for(self) -> ForStatement:
        """Parse for loop."""
        self.expect(TokenType.FOR)
        self.expect(TokenType.LPAREN)

        # Check for foreach: for (x in arr)
        saved_pos = self.pos
        if self.match(TokenType.IDENTIFIER):
            first = self.advance()
            if self.match(TokenType.IDENTIFIER) and self.current().value == 'in':
                # This is a foreach loop
                self.advance()  # 'in'
                iterable = self.parse_expression()
                self.expect(TokenType.RPAREN)
                self.expect(TokenType.LBRACE)
                self.skip_newlines()
                body = self.parse_block_statements()
                self.expect(TokenType.RBRACE)
                return ForStatement(
                    init=None,
                    condition=None,
                    update=None,
                    body=body,
                    is_foreach=True,
                    var_name=first.value,
                    iterable=iterable
                )
            else:
                # Not foreach, restore position
                self.pos = saved_pos

        # Regular for loop: for (init; cond; update)
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
        self.expect(TokenType.LBRACE)
        self.skip_newlines()

        body = self.parse_block_statements()
        self.expect(TokenType.RBRACE)

        return ForStatement(
            init=init,
            condition=condition,
            update=update,
            body=body,
            is_foreach=False
        )

    def parse_while(self) -> WhileStatement:
        """Parse while loop."""
        self.expect(TokenType.WHILE)
        self.expect(TokenType.LPAREN)
        condition = self.parse_expression()
        self.expect(TokenType.RPAREN)
        self.expect(TokenType.LBRACE)
        self.skip_newlines()

        body = self.parse_block_statements()
        self.expect(TokenType.RBRACE)

        return WhileStatement(condition=condition, body=body)

    def parse_return(self) -> ReturnStatement:
        """Parse return statement."""
        self.expect(TokenType.RETURN)
        value = None
        if not self.match(TokenType.SEMICOLON, TokenType.RBRACE, TokenType.NEWLINE):
            value = self.parse_expression()
        if self.match(TokenType.SEMICOLON):
            self.advance()
        return ReturnStatement(value=value)

    def parse_fails(self) -> FailsTask:
        """Parse fails(task_call)."""
        self.expect(TokenType.FAILS)
        self.expect(TokenType.LPAREN)
        task_call = self.parse_task_call()
        self.expect(TokenType.RPAREN)
        if self.match(TokenType.SEMICOLON):
            self.advance()
        return FailsTask(task_call=task_call)

    def parse_expression(self) -> ASTNode:
        """Parse an expression."""
        return self.parse_or()

    def parse_or(self) -> ASTNode:
        """Parse OR expression."""
        left = self.parse_and()

        while self.match(TokenType.OR):
            op = self.advance().value
            right = self.parse_and()
            left = BinaryOp(op=op, left=left, right=right)

        return left

    def parse_and(self) -> ASTNode:
        """Parse AND expression."""
        left = self.parse_equality()

        while self.match(TokenType.AND):
            op = self.advance().value
            right = self.parse_equality()
            left = BinaryOp(op=op, left=left, right=right)

        return left

    def parse_equality(self) -> ASTNode:
        """Parse equality expression."""
        left = self.parse_comparison()

        while self.match(TokenType.EQ, TokenType.NEQ):
            op = self.advance().value
            right = self.parse_comparison()
            left = BinaryOp(op=op, left=left, right=right)

        return left

    def parse_comparison(self) -> ASTNode:
        """Parse comparison expression."""
        left = self.parse_additive()

        while self.match(TokenType.LT, TokenType.GT, TokenType.LTE, TokenType.GTE):
            op = self.advance().value
            right = self.parse_additive()
            left = BinaryOp(op=op, left=left, right=right)

        return left

    def parse_additive(self) -> ASTNode:
        """Parse additive expression (+ - %)."""
        left = self.parse_multiplicative()

        while self.match(TokenType.PLUS, TokenType.MINUS, TokenType.PERCENT):
            op = self.advance().value
            right = self.parse_multiplicative()
            left = BinaryOp(op=op, left=left, right=right)

        return left

    def parse_multiplicative(self) -> ASTNode:
        """Parse multiplicative expression (* /)."""
        left = self.parse_unary()

        while self.match(TokenType.STAR, TokenType.SLASH):
            op = self.advance().value
            right = self.parse_unary()
            left = BinaryOp(op=op, left=left, right=right)

        return left

    def parse_unary(self) -> ASTNode:
        """Parse unary expression."""
        if self.match(TokenType.NOT):
            op = self.advance().value
            operand = self.parse_unary()
            return UnaryOp(op=op, operand=operand)
        if self.match(TokenType.MINUS):
            op = self.advance().value
            operand = self.parse_unary()
            return UnaryOp(op=op, operand=operand)

        return self.parse_postfix()

    def parse_postfix(self) -> ASTNode:
        """Parse postfix expression (function calls, array access)."""
        expr = self.parse_primary()

        while True:
            if self.match(TokenType.LPAREN):
                # Function or task call
                self.advance()
                args, kwargs = self.parse_arguments()
                self.expect(TokenType.RPAREN)
                if isinstance(expr, Identifier):
                    # Built-in functions are FunctionCalls, task names are TaskCalls
                    if expr.name in ('len', 'contains', 'equals', 'exists', 'fail'):
                        expr = FunctionCall(name=expr.name, args=args, kwargs=kwargs)
                    else:
                        expr = TaskCall(name=expr.name, args=args, kwargs=kwargs)
                else:
                    expr = FunctionCall(name=expr.name if hasattr(expr, 'name') else str(expr),
                                       args=args, kwargs=kwargs)
            elif self.match(TokenType.LBRACKET):
                # Array access
                self.advance()
                index = self.parse_expression()
                self.expect(TokenType.RBRACKET)
                expr = ArrayAccess(array=expr, index=index)
            elif self.match(TokenType.DOT):
                # Member access
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
        """Parse primary expression."""
        # Literals
        if self.match(TokenType.INT):
            return Literal(self.advance().value)
        if self.match(TokenType.FLOAT):
            return Literal(self.advance().value)
        if self.match(TokenType.STRING):
            value = self.advance().value
            # Check if it's a template string
            if '${' in value:
                return self.parse_template_string(value)
            return Literal(value)
        if self.match(TokenType.TRUE):
            self.advance()
            return Literal(True)
        if self.match(TokenType.FALSE):
            self.advance()
            return Literal(False)

        # List literal
        if self.match(TokenType.LBRACKET):
            return self.parse_list_literal_expr()

        # Parenthesized expression
        if self.match(TokenType.LPAREN):
            self.advance()
            expr = self.parse_expression()
            self.expect(TokenType.RPAREN)
            return expr

        # Identifier
        if self.match(TokenType.IDENTIFIER):
            name = self.advance().value

            # Check for params.workspace or workspace
            if name == 'params':
                if self.match(TokenType.DOT):
                    self.advance()
                    param_name = self.expect(TokenType.IDENTIFIER).value
                    return ParameterAccess(param_name=param_name)
                return Identifier(name=name)

            if name == 'workspace':
                return WorkspaceAccess()

            # Built-in functions
            if name in ('len', 'contains', 'equals', 'exists', 'fail'):
                return Identifier(name=name)

            return Identifier(name=name)

        raise self.error(f"Unexpected token in expression: {self.current().type.name}")

    def parse_template_string(self, value: str) -> TemplateString:
        """Parse a template string with ${...} placeholders."""
        parts = []
        i = 0
        current = ""

        while i < len(value):
            if i + 1 < len(value) and value[i:i+2] == '${':
                if current:
                    parts.append(current)
                    current = ""
                # Find the closing brace
                j = i + 2
                brace_count = 1
                while j < len(value) and brace_count > 0:
                    if value[j] == '{':
                        brace_count += 1
                    elif value[j] == '}':
                        brace_count -= 1
                    j += 1
                expr_str = value[i+2:j-1]
                # Parse the expression inside
                parts.append(self.parse_template_expr(expr_str))
                i = j
            else:
                current += value[i]
                i += 1

        if current:
            parts.append(current)

        return TemplateString(parts=parts)

    def parse_template_expr(self, expr_str: str) -> ASTNode:
        """Parse an expression inside ${...}."""
        expr_str = expr_str.strip()

        # Handle simple param access like params.x
        if expr_str == 'workspace':
            return WorkspaceAccess()
        if expr_str.startswith('params.'):
            param_name = expr_str[len('params.'):]
            return ParameterAccess(param_name=param_name)

        # Handle env variables
        if not any(c in expr_str for c in '(){}[];:=+-*/%<>!&|'):
            return TemplateString(['${' + expr_str + '}'])

        # Tokenize the expression for complex cases
        lexer = Lexer(expr_str)
        tokens = lexer.tokenize()
        tokens = lexer.filter_tokens(tokens)
        parser = Parser(tokens, self.run_blocks)
        return parser.parse_expression()

    def parse_list_literal_expr(self) -> ListLiteral:
        """Parse a list literal expression."""
        self.expect(TokenType.LBRACKET)
        elements = []

        while not self.match(TokenType.RBRACKET, TokenType.EOF):
            elements.append(self.parse_expression())
            if self.match(TokenType.COMMA):
                self.advance()

        self.expect(TokenType.RBRACKET)
        return ListLiteral(elements)

    def parse_arguments(self) -> Tuple[List[ASTNode], Dict[str, ASTNode]]:
        """Parse function/task call arguments."""
        args = []
        kwargs = {}

        while not self.match(TokenType.RPAREN, TokenType.EOF):
            # Check for keyword argument
            if self.match(TokenType.IDENTIFIER) and self.peek(1).type == TokenType.ASSIGN:
                name = self.advance().value
                self.advance()  # =
                value = self.parse_expression()
                kwargs[name] = value
            else:
                args.append(self.parse_expression())

            if self.match(TokenType.COMMA):
                self.advance()

        return args, kwargs

    def parse_task_call(self) -> TaskCall:
        """Parse a task call."""
        name = self.expect(TokenType.IDENTIFIER).value
        self.expect(TokenType.LPAREN)
        args, kwargs = self.parse_arguments()
        self.expect(TokenType.RPAREN)
        return TaskCall(name=name, args=args, kwargs=kwargs)


def preprocess_pipeline(source: str) -> Tuple[str, Dict[str, str]]:
    """
    Extract run block contents from pipeline source and replace with placeholders.
    Returns (modified_source, run_blocks_map).
    """
    run_blocks = {}
    counter = [0]

    result = _extract_blocks(source, run_blocks, counter)

    return result, run_blocks


def _extract_blocks(source: str, run_blocks: Dict[str, str], counter: list) -> str:
    """Extract run blocks from source."""
    result = []
    i = 0

    while i < len(source):
        # Look for run: or run {
        if source[i:i+3] == 'run':
            # Check if followed by : or {
            j = i + 3
            while j < len(source) and source[j] in ' \t\n':
                j += 1

            is_run_block = False
            if j < len(source) and source[j] == '{':
                # run { ... }
                is_run_block = True
                brace_start = j
            elif j < len(source) and source[j] == ':':
                # run: { ... }
                j += 1
                while j < len(source) and source[j] in ' \t\n':
                    j += 1
                if j < len(source) and source[j] == '{':
                    is_run_block = True
                    brace_start = j

            if not is_run_block:
                result.append(source[i:j])
                i = j
                continue

            # Find matching closing brace
            depth = 1
            k = brace_start + 1
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
                    # Skip comments
                    while k < len(source) and source[k] != '\n':
                        k += 1
                k += 1

            content = source[brace_start+1:k-1].strip()
            placeholder = f'__RUN_BLOCK_{counter[0]}__'
            run_blocks[placeholder] = content
            counter[0] += 1

            result.append(source[i:brace_start+1])
            result.append(placeholder + ' }')
            i = k
        else:
            result.append(source[i])
            i += 1

    return ''.join(result)


def parse_pipeline(source: str) -> Pipeline:
    """Parse a pipeline file from source string."""
    # Preprocess to extract run blocks
    preprocessed, run_blocks = preprocess_pipeline(source)

    lexer = Lexer(preprocessed)
    tokens = lexer.tokenize()
    tokens = lexer.filter_tokens(tokens)
    parser = Parser(tokens, run_blocks)
    return parser.parse()
