"""
Expression evaluator for pipeline success/requires blocks.
"""

import os
import re
from typing import Any, Dict, List, Optional, Tuple, Union
from ast_nodes import *


class ReturnValue(Exception):
    """Exception used to implement return statements."""
    def __init__(self, value: Any):
        self.value = value


class BreakSignal(Exception):
    pass


class ContinueSignal(Exception):
    pass


class EvaluationError(Exception):
    def __init__(self, message: str):
        self.message = message
        super().__init__(f"INVALID_PIPE: {message}")


class ExprEvaluator:
    """Evaluates expressions in the pipeline context."""

    def __init__(self, variables: Dict[str, Any] = None, env_vars: Dict[str, str] = None,
                 stdout: str = "", stderr: str = "", exit_code: int = 0,
                 output_dir: str = None, workspace: str = None):
        self.variables = variables or {}
        self.env_vars = env_vars or {}
        self.stdout = stdout
        self.stderr = stderr
        self.exit_code = exit_code
        self.output_dir = output_dir
        self.workspace = workspace

    def evaluate_statements(self, statements: List[ASTNode]) -> Any:
        """Evaluate a list of statements, returning the last value or return value."""
        result = None
        for stmt in statements:
            result = self.evaluate_statement(stmt)
        return result

    def evaluate_statement(self, stmt: ASTNode) -> Any:
        """Evaluate a single statement."""
        if isinstance(stmt, ReturnStatement):
            value = self.evaluate_expr(stmt.value) if stmt.value else None
            raise ReturnValue(value)
        elif isinstance(stmt, BreakStatement):
            raise BreakSignal()
        elif isinstance(stmt, ContinueStatement):
            raise ContinueSignal()
        elif isinstance(stmt, VarDecl):
            value = self.evaluate_expr(stmt.value) if stmt.value else self.default_for_type(stmt.var_type)
            self.variables[stmt.var_name] = value
            return value
        elif isinstance(stmt, Assignment):
            value = self.evaluate_expr(stmt.value) if hasattr(stmt, 'value') and stmt.value else None
            if isinstance(stmt.name, Identifier):
                self.variables[stmt.name.name] = value
            elif isinstance(stmt.name, ArrayAccess):
                arr = self.evaluate_expr(stmt.name.array)
                idx = self.evaluate_expr(stmt.name.index)
                arr[int(idx)] = value
            return value
        elif isinstance(stmt, IfStatement):
            return self.evaluate_if(stmt)
        elif isinstance(stmt, ForStatement):
            return self.evaluate_for(stmt)
        elif isinstance(stmt, WhileStatement):
            return self.evaluate_while(stmt)
        elif isinstance(stmt, ExpressionStatement):
            return self.evaluate_expr(stmt.expression)
        elif isinstance(stmt, FailsTask):
            # This is handled specially in requires evaluation
            return stmt
        else:
            return self.evaluate_expr(stmt)

    def evaluate_if(self, stmt: IfStatement) -> Any:
        """Evaluate an if statement."""
        condition = self.evaluate_expr(stmt.condition)
        if self.is_truthy(condition):
            return self.evaluate_statements(stmt.then_block)

        for elif_cond, elif_block in stmt.elif_clauses:
            if self.is_truthy(self.evaluate_expr(elif_cond)):
                return self.evaluate_statements(elif_block)

        if stmt.else_block:
            return self.evaluate_statements(stmt.else_block)

        return None

    def evaluate_for(self, stmt: ForStatement) -> Any:
        """Evaluate a for loop."""
        if stmt.is_foreach:
            iterable = self.evaluate_expr(stmt.iterable)
            for item in iterable:
                self.variables[stmt.var_name] = item
                try:
                    self.evaluate_statements(stmt.body)
                except BreakSignal:
                    break
                except ContinueSignal:
                    continue
            return None

        # Regular for loop
        if stmt.init:
            self.evaluate_statement(stmt.init)

        while True:
            if stmt.condition:
                cond = self.evaluate_expr(stmt.condition)
                if not self.is_truthy(cond):
                    break

            try:
                self.evaluate_statements(stmt.body)
            except BreakSignal:
                break
            except ContinueSignal:
                pass

            if stmt.update:
                self.evaluate_expr(stmt.update)

        return None

    def evaluate_while(self, stmt: WhileStatement) -> Any:
        """Evaluate a while loop."""
        while self.is_truthy(self.evaluate_expr(stmt.condition)):
            try:
                self.evaluate_statements(stmt.body)
            except BreakSignal:
                break
            except ContinueSignal:
                continue
        return None

    def evaluate_expr(self, expr: ASTNode) -> Any:
        """Evaluate an expression."""
        if isinstance(expr, Literal):
            return expr.value

        elif isinstance(expr, Identifier):
            name = expr.name
            # Special identifiers
            if name == 'stdout':
                return self.stdout
            elif name == 'stderr':
                return self.stderr
            elif name == 'exit_code':
                return self.exit_code
            if name in self.variables:
                return self.variables[name]
            # Check env vars
            if name in self.env_vars:
                return self.env_vars[name]
            raise EvaluationError(f"Undefined variable: {name}")

        elif isinstance(expr, ParameterAccess):
            if 'params' in self.variables and expr.param_name in self.variables['params']:
                return self.variables['params'][expr.param_name]
            raise EvaluationError(f"Undefined parameter: {expr.param_name}")

        elif isinstance(expr, WorkspaceAccess):
            return self.workspace or os.getcwd()

        elif isinstance(expr, BinaryOp):
            return self.evaluate_binary_op(expr)

        elif isinstance(expr, UnaryOp):
            return self.evaluate_unary_op(expr)

        elif isinstance(expr, ArrayAccess):
            arr = self.evaluate_expr(expr.array)
            idx = self.evaluate_expr(expr.index)
            if isinstance(arr, (list, str)):
                return arr[int(idx)]
            raise EvaluationError(f"Cannot index into {type(arr)}")

        elif isinstance(expr, FunctionCall):
            return self.evaluate_function(expr)

        elif isinstance(expr, TaskCall):
            # Task calls in requires are handled specially
            return expr

        elif isinstance(expr, TemplateString):
            return self.evaluate_template_string(expr)

        elif isinstance(expr, ListLiteral):
            return [self.evaluate_expr(e) for e in expr.elements]

        elif isinstance(expr, FailsTask):
            return expr

        else:
            raise EvaluationError(f"Cannot evaluate expression: {type(expr).__name__}")

    def evaluate_binary_op(self, expr: BinaryOp) -> Any:
        """Evaluate a binary operation."""
        left = self.evaluate_expr(expr.left)

        # Short-circuit for logical operators
        if expr.op == '&&':
            if not self.is_truthy(left):
                return False
            return self.is_truthy(self.evaluate_expr(expr.right))
        if expr.op == '||':
            if self.is_truthy(left):
                return True
            return self.is_truthy(self.evaluate_expr(expr.right))

        right = self.evaluate_expr(expr.right)

        if expr.op == '+':
            return left + right
        elif expr.op == '-':
            return left - right
        elif expr.op == '*':
            return left * right
        elif expr.op == '/':
            if right == 0:
                raise EvaluationError("Division by zero")
            return left / right
        elif expr.op == '%':
            # String concatenation operator
            if isinstance(left, str) and isinstance(right, str):
                return left + right
            elif isinstance(left, str):
                if isinstance(right, list):
                    raise EvaluationError("Cannot convert list to string")
                return left + str(right)
            elif isinstance(right, str):
                if isinstance(left, list):
                    raise EvaluationError("Cannot convert list to string")
                return str(left) + right
            else:
                return left % right
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
        else:
            raise EvaluationError(f"Unknown operator: {expr.op}")

    def evaluate_unary_op(self, expr: UnaryOp) -> Any:
        """Evaluate a unary operation."""
        operand = self.evaluate_expr(expr.operand)

        if expr.op == '!':
            return not self.is_truthy(operand)
        elif expr.op == '-':
            return -operand
        else:
            raise EvaluationError(f"Unknown unary operator: {expr.op}")

    def evaluate_function(self, expr: FunctionCall) -> Any:
        """Evaluate a built-in function call."""
        name = expr.name
        args = [self.evaluate_expr(a) for a in expr.args]

        if name == 'len':
            if len(args) != 1:
                raise EvaluationError("len() takes exactly 1 argument")
            return len(args[0])

        elif name == 'contains':
            if len(args) != 2:
                raise EvaluationError("contains() takes exactly 2 arguments")
            target, pattern = args

            if target == 'stdout':
                text = self.stdout
            elif target == 'stderr':
                text = self.stderr
            elif isinstance(target, str) and os.path.isfile(self._resolve_path(target)):
                with open(self._resolve_path(target), 'r') as f:
                    text = f.read()
            else:
                text = str(target)

            # Check if pattern is regex
            try:
                if re.search(pattern, text):
                    return True
            except re.error:
                pass

            return pattern in text

        elif name == 'equals':
            if len(args) != 2:
                raise EvaluationError("equals() takes exactly 2 arguments")
            target, value = args

            if target == 'stdout':
                actual = self.stdout
            elif target == 'stderr':
                actual = self.stderr
            elif target == 'exit code' or target == 'exit_code':
                actual = str(self.exit_code)
            elif isinstance(target, str) and os.path.isfile(self._resolve_path(target)):
                with open(self._resolve_path(target), 'r') as f:
                    actual = f.read()
            else:
                actual = str(target)

            if isinstance(value, int) and target in ('exit code', 'exit_code'):
                return self.exit_code == value

            return actual == str(value)

        elif name == 'exists':
            if len(args) != 1:
                raise EvaluationError("exists() takes exactly 1 argument")
            path = args[0]
            resolved = self._resolve_path(path)
            return os.path.exists(resolved)

        elif name == 'fail':
            # fail() in requires context
            return ('fail', args[0] if args else None)

        else:
            raise EvaluationError(f"Unknown function: {name}")

    def evaluate_template_string(self, expr: TemplateString) -> str:
        """Evaluate a template string."""
        result = []
        for part in expr.parts:
            if isinstance(part, str):
                # Check for env var references
                result.append(part)
            elif isinstance(part, ASTNode):
                value = self.evaluate_expr(part)
                if isinstance(value, list):
                    raise EvaluationError("Cannot convert list to string")
                result.append(str(value))
            else:
                result.append(str(part))
        return ''.join(result)

    def _resolve_path(self, path: str) -> str:
        """Resolve a path relative to workspace or output dir."""
        if os.path.isabs(path):
            return path
        if self.output_dir:
            return os.path.join(self.output_dir, path)
        if self.workspace:
            return os.path.join(self.workspace, path)
        return path

    @staticmethod
    def is_truthy(value: Any) -> bool:
        """Check if a value is truthy."""
        if isinstance(value, bool):
            return value
        if isinstance(value, int):
            return value != 0
        if isinstance(value, float):
            return value != 0.0
        if isinstance(value, str):
            return len(value) > 0
        if isinstance(value, list):
            return len(value) > 0
        return bool(value)

    @staticmethod
    def default_for_type(type_name: str) -> Any:
        """Get the default value for a type."""
        defaults = {
            'string': '',
            'int': 0,
            'float': 0.0,
            'bool': False,
        }
        return defaults.get(type_name, None)

    def evaluate_success(self, success_block: Dict[str, List[ASTNode]]) -> Dict[str, bool]:
        """Evaluate success criteria."""
        results = {}
        for name, statements in success_block.items():
            try:
                self.evaluate_statements(statements)
                results[name] = True  # If no return statement, it passes
            except ReturnValue as rv:
                results[name] = self.is_truthy(rv.value)
            except Exception:
                results[name] = False
        return results
