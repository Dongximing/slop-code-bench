"""
Expression evaluator for the pipeline DSL.
"""

import os
import re
import subprocess
from typing import Any, Dict, List, Optional, Tuple
from .ast_nodes import (
    Expr, Stmt, IntLiteral, FloatLiteral, StringLiteral, BoolLiteral,
    ListLiteral, IdentifierExpr, ArrayIndexExpr, DollarVar, BinaryExpr,
    UnaryExpr, FunctionCallExpr, AssignmentExpr, ExprStmt, ReturnStmt,
    BreakStmt, ContinueStmt, BlockStmt, IfStmt, ForStmt, WhileStmt,
    TaskCallStmt, FailsStmt, SuccessBlock
)


class ReturnValue(Exception):
    def __init__(self, value):
        self.value = value


class BreakException(Exception):
    pass


class ContinueException(Exception):
    pass


class Evaluator:
    def __init__(self, variables: Dict[str, Any] = None, env_vars: Dict[str, str] = None,
                 workspace: str = None, output_dir: str = None, stdout: str = "",
                 stderr: str = "", exit_code: int = 0, task_executor=None):
        self.variables = variables or {}
        self.env_vars = env_vars or {}
        self.workspace = workspace
        self.output_dir = output_dir
        self.stdout = stdout
        self.stderr = stderr
        self.exit_code = exit_code
        self.task_executor = task_executor  # For executing task calls in requires

    def evaluate_expr(self, expr: Expr) -> Any:
        if isinstance(expr, IntLiteral):
            return expr.value
        elif isinstance(expr, FloatLiteral):
            return expr.value
        elif isinstance(expr, StringLiteral):
            return self.resolve_string_interpolation(expr.value)
        elif isinstance(expr, BoolLiteral):
            return expr.value
        elif isinstance(expr, ListLiteral):
            return [self.evaluate_expr(e) for e in expr.elements]
        elif isinstance(expr, IdentifierExpr):
            name = expr.name
            if name in self.variables:
                return self.variables[name]
            # Handle built-in variables
            elif name == 'stdout':
                return self.stdout
            elif name == 'stderr':
                return self.stderr
            elif name == 'exit_code':
                return self.exit_code
            else:
                raise ValueError(f"INVALID_PIPE: Undefined variable '{name}'")
        elif isinstance(expr, ArrayIndexExpr):
            array = self.evaluate_expr(expr.array)
            index = self.evaluate_expr(expr.index)
            if not isinstance(array, (list, str)):
                raise ValueError(f"INVALID_PIPE: Cannot index non-array type")
            return array[int(index)]
        elif isinstance(expr, DollarVar):
            return self.resolve_dollar_var(expr.value)
        elif isinstance(expr, BinaryExpr):
            return self.evaluate_binary(expr)
        elif isinstance(expr, UnaryExpr):
            return self.evaluate_unary(expr)
        elif isinstance(expr, FunctionCallExpr):
            return self.evaluate_function(expr)
        elif isinstance(expr, AssignmentExpr):
            return self.evaluate_assignment(expr)
        else:
            raise ValueError(f"INVALID_PIPE: Unknown expression type: {type(expr)}")

    def resolve_string_interpolation(self, value: str) -> str:
        """Resolve ${...} patterns within string literals."""
        import re
        # Find all ${...} patterns and replace them
        pattern = r'\$\{[^}]+\}'

        def replace_match(match):
            matched = match.group(0)
            return str(self.resolve_dollar_var(matched))

        return re.sub(pattern, replace_match, value)

    def resolve_dollar_var(self, value: str) -> Any:
        """Resolve ${params.x} or $VAR patterns."""
        if value.startswith('${'):
            # Extract the variable path
            inner = value[2:-1]  # Remove ${ and }
            parts = inner.split('.')

            if parts[0] == 'params':
                if len(parts) < 2:
                    raise ValueError(f"INVALID_PIPE: Invalid parameter reference: {value}")
                param_name = parts[1]
                if param_name not in self.variables.get('params', {}):
                    raise ValueError(f"INVALID_PIPE: Undefined parameter '{param_name}'")
                result = self.variables['params'][param_name]
                # Handle nested access like params.x.field
                for part in parts[2:]:
                    if isinstance(result, dict):
                        result = result.get(part)
                    else:
                        raise ValueError(f"INVALID_PIPE: Cannot access '{part}' on non-dict type")
                return result
            elif parts[0] == 'workspace':
                return self.workspace
            elif parts[0] == 'output':
                return self.output_dir
            else:
                # Try to find in variables
                obj = self.variables.get(parts[0])
                if obj is None:
                    raise ValueError(f"INVALID_PIPE: Undefined variable '{parts[0]}'")
                for part in parts[1:]:
                    if isinstance(obj, dict):
                        obj = obj.get(part)
                    else:
                        raise ValueError(f"INVALID_PIPE: Cannot access '{part}' on non-dict type")
                return obj
        elif value.startswith('$'):
            # Simple environment variable
            var_name = value[1:]
            if var_name in self.env_vars:
                return self.env_vars[var_name]
            return os.environ.get(var_name, "")
        return value

    def evaluate_binary(self, expr: BinaryExpr) -> Any:
        left = self.evaluate_expr(expr.left)
        right = self.evaluate_expr(expr.right)

        op = expr.op

        if op == '+':
            return left + right
        elif op == '-':
            return left - right
        elif op == '*':
            return left * right
        elif op == '/':
            if isinstance(left, int) and isinstance(right, int):
                return left // right  # Integer division
            return left / right
        elif op == '%':
            if isinstance(left, str) or isinstance(right, str):
                # String concatenation
                return str(left) + str(right)
            return left % right
        elif op == '==':
            return left == right
        elif op == '!=':
            return left != right
        elif op == '<':
            return left < right
        elif op == '>':
            return left > right
        elif op == '<=':
            return left <= right
        elif op == '>=':
            return left >= right
        elif op == '&&':
            return bool(left) and bool(right)
        elif op == '||':
            return bool(left) or bool(right)
        else:
            raise ValueError(f"INVALID_PIPE: Unknown operator: {op}")

    def evaluate_unary(self, expr: UnaryExpr) -> Any:
        operand = self.evaluate_expr(expr.operand)

        if expr.op == '!':
            return not bool(operand)
        elif expr.op == '-':
            return -operand
        else:
            raise ValueError(f"INVALID_PIPE: Unknown unary operator: {expr.op}")

    def evaluate_function(self, expr: FunctionCallExpr) -> Any:
        name = expr.name
        args = [self.evaluate_expr(arg) for arg in expr.args]

        if name == 'len':
            if len(args) != 1:
                raise ValueError(f"INVALID_PIPE: len() takes exactly 1 argument")
            return len(args[0])
        elif name == 'contains':
            if len(args) != 2:
                raise ValueError(f"INVALID_PIPE: contains() takes exactly 2 arguments")
            source, pattern = args
            if source == 'stdout':
                source = self.stdout
            elif source == 'stderr':
                source = self.stderr
            # Check if it's a regex pattern
            try:
                if pattern.startswith('/') and pattern.endswith('/'):
                    # Regex pattern
                    regex_pattern = pattern[1:-1]
                    return bool(re.search(regex_pattern, source))
                else:
                    # Exact string match
                    return pattern in source
            except re.error:
                return pattern in source
        elif name == 'equals':
            if len(args) != 2:
                raise ValueError(f"INVALID_PIPE: equals() takes exactly 2 arguments")
            source, value = args
            if source == 'stdout':
                source = self.stdout
            elif source == 'stderr':
                source = self.stderr
            elif source == 'exit_code':
                source = str(self.exit_code)
            else:
                # Check if source is a file path
                potential_path = source
                if not os.path.isabs(potential_path):
                    potential_path = os.path.join(self.workspace, potential_path)
                if os.path.isfile(potential_path):
                    with open(potential_path, 'r') as f:
                        source = f.read()
            return str(source) == str(value)
        elif name == 'exists':
            if len(args) != 1:
                raise ValueError(f"INVALID_PIPE: exists() takes exactly 1 argument")
            path = args[0]
            # Make path absolute if relative
            if not os.path.isabs(path):
                path = os.path.join(self.workspace, path)
            return os.path.exists(path)
        else:
            raise ValueError(f"INVALID_PIPE: Unknown function: {name}")

    def evaluate_assignment(self, expr: AssignmentExpr) -> Any:
        value = self.evaluate_expr(expr.value)
        name = expr.name if isinstance(expr.name, str) else str(expr.name)
        self.variables[name] = value
        return value

    def evaluate_stmt(self, stmt: Stmt) -> Any:
        if isinstance(stmt, ExprStmt):
            return self.evaluate_expr(stmt.expr)
        elif isinstance(stmt, ReturnStmt):
            value = self.evaluate_expr(stmt.value)
            raise ReturnValue(value)
        elif isinstance(stmt, BreakStmt):
            raise BreakException()
        elif isinstance(stmt, ContinueStmt):
            raise ContinueException()
        elif isinstance(stmt, BlockStmt):
            result = None
            for s in stmt.statements:
                result = self.evaluate_stmt(s)
            return result
        elif isinstance(stmt, IfStmt):
            condition = self.evaluate_expr(stmt.condition)
            if condition:
                return self.evaluate_stmt(stmt.then_block)
            else:
                for elif_cond, elif_block in stmt.elif_clauses:
                    if self.evaluate_expr(elif_cond):
                        return self.evaluate_stmt(elif_block)
                if stmt.else_block:
                    return self.evaluate_stmt(stmt.else_block)
        elif isinstance(stmt, ForStmt):
            if stmt.init:
                self.evaluate_stmt(stmt.init)
            while stmt.condition is None or self.evaluate_expr(stmt.condition):
                try:
                    self.evaluate_stmt(stmt.body)
                except BreakException:
                    break
                except ContinueException:
                    pass
                if stmt.update:
                    self.evaluate_stmt(stmt.update)
            return None
        elif isinstance(stmt, WhileStmt):
            while self.evaluate_expr(stmt.condition):
                try:
                    self.evaluate_stmt(stmt.body)
                except BreakException:
                    break
                except ContinueException:
                    pass
            return None
        elif isinstance(stmt, TaskCallStmt):
            if self.task_executor:
                result = self.task_executor(stmt, expect_fail=False)
                if not result:
                    raise ValueError("Task call failed")
                return result
            raise ValueError("INVALID_PIPE: Task calls not supported in this context")
        elif isinstance(stmt, FailsStmt):
            if self.task_executor:
                result = self.task_executor(stmt.task_call, expect_fail=True)
                if not result:
                    raise ValueError("Expected task to fail but it succeeded")
                return result
            raise ValueError("INVALID_PIPE: Task calls not supported in this context")
        else:
            raise ValueError(f"INVALID_PIPE: Unknown statement type: {type(stmt)}")

    def evaluate_success_block(self, block: SuccessBlock) -> bool:
        """Evaluate a success block and return True/False."""
        try:
            for stmt in block.statements:
                result = self.evaluate_stmt(stmt)
            # If we get here without a return, the result is the last expression
            # For single-expression blocks, return the boolean value
            if len(block.statements) == 1 and isinstance(block.statements[0], ExprStmt):
                return bool(result)
            # For multi-line blocks without return, it's an error
            if block.is_multiline:
                raise ValueError("SYNTAX_ERROR: Multi-line success block must have a return statement")
            return True
        except ReturnValue as rv:
            return bool(rv.value)

    def evaluate_requires_block(self, statements: List[Stmt]) -> bool:
        """Evaluate requires block statements and return success."""
        try:
            for stmt in statements:
                self.evaluate_stmt(stmt)
            return True
        except ReturnValue:
            return False


class SuccessEvaluator(Evaluator):
    """Evaluator specifically for success blocks."""

    def __init__(self, variables: Dict[str, Any] = None, env_vars: Dict[str, str] = None,
                 workspace: str = None, output_dir: str = None, stdout: str = "",
                 stderr: str = "", exit_code: int = 0):
        super().__init__(
            variables=variables,
            env_vars=env_vars,
            workspace=workspace,
            output_dir=output_dir,
            stdout=stdout,
            stderr=stderr,
            exit_code=exit_code,
            task_executor=None
        )


class RequiresEvaluator(Evaluator):
    """Evaluator for requires blocks with task execution support."""

    def __init__(self, variables: Dict[str, Any] = None, env_vars: Dict[str, str] = None,
                 workspace: str = None, output_dir: str = None, task_executor=None):
        super().__init__(
            variables=variables,
            env_vars=env_vars,
            workspace=workspace,
            output_dir=output_dir,
            task_executor=task_executor
        )