"""Expression evaluator for pipeline success/requires blocks."""

import os
import re
from typing import Any, Dict, List

from ast_nodes import (
    ASTNode, Literal, Identifier, BinaryOp, UnaryOp, ArrayAccess,
    FunctionCall, TaskCall, CachedTaskCall, TemplateString, ListLiteral, ParameterAccess,
    WorkspaceAccess, VarDecl, Assignment, IfStatement, ForStatement,
    WhileStatement, ReturnStatement, BreakStatement, ContinueStatement,
    ExpressionStatement, FailsTask,
)


class ReturnValue(Exception):
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
        result = None
        for stmt in statements:
            result = self.evaluate_statement(stmt)
        return result

    def evaluate_statement(self, stmt: ASTNode) -> Any:
        if isinstance(stmt, ReturnStatement):
            raise ReturnValue(self.evaluate_expr(stmt.value) if stmt.value else None)
        if isinstance(stmt, BreakStatement):
            raise BreakSignal()
        if isinstance(stmt, ContinueStatement):
            raise ContinueSignal()
        if isinstance(stmt, VarDecl):
            value = self.evaluate_expr(stmt.value) if stmt.value else self.default_for_type(stmt.var_type)
            self.variables[stmt.var_name] = value
            return value
        if isinstance(stmt, Assignment):
            return self._do_assignment(stmt)
        if isinstance(stmt, IfStatement):
            return self.evaluate_if(stmt)
        if isinstance(stmt, ForStatement):
            return self.evaluate_for(stmt)
        if isinstance(stmt, WhileStatement):
            return self.evaluate_while(stmt)
        if isinstance(stmt, ExpressionStatement):
            return self.evaluate_expr(stmt.expression)
        if isinstance(stmt, (FailsTask, CachedTaskCall)):
            return stmt
        return self.evaluate_expr(stmt)

    def _do_assignment(self, stmt: Assignment) -> Any:
        value = self.evaluate_expr(stmt.value) if hasattr(stmt, 'value') and stmt.value else None
        if isinstance(stmt.name, Identifier):
            self.variables[stmt.name.name] = value
        elif isinstance(stmt.name, ArrayAccess):
            arr = self.evaluate_expr(stmt.name.array)
            idx = self.evaluate_expr(stmt.name.index)
            arr[int(idx)] = value
        return value

    def evaluate_if(self, stmt: IfStatement) -> Any:
        if self.is_truthy(self.evaluate_expr(stmt.condition)):
            return self.evaluate_statements(stmt.then_block)

        for elif_cond, elif_block in stmt.elif_clauses:
            if self.is_truthy(self.evaluate_expr(elif_cond)):
                return self.evaluate_statements(elif_block)

        if stmt.else_block:
            return self.evaluate_statements(stmt.else_block)
        return None

    def evaluate_for(self, stmt: ForStatement) -> Any:
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

        if stmt.init:
            self.evaluate_statement(stmt.init)

        while True:
            if stmt.condition and not self.is_truthy(self.evaluate_expr(stmt.condition)):
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
        while self.is_truthy(self.evaluate_expr(stmt.condition)):
            try:
                self.evaluate_statements(stmt.body)
            except BreakSignal:
                break
            except ContinueSignal:
                continue
        return None

    def evaluate_expr(self, expr: ASTNode) -> Any:
        if isinstance(expr, Literal):
            return expr.value
        if isinstance(expr, Identifier):
            return self._resolve_identifier(expr.name)
        if isinstance(expr, ParameterAccess):
            if 'params' in self.variables and expr.param_name in self.variables['params']:
                return self.variables['params'][expr.param_name]
            raise EvaluationError(f"Undefined parameter: {expr.param_name}")
        if isinstance(expr, WorkspaceAccess):
            return self.workspace or os.getcwd()
        if isinstance(expr, BinaryOp):
            return self.evaluate_binary_op(expr)
        if isinstance(expr, UnaryOp):
            return self.evaluate_unary_op(expr)
        if isinstance(expr, ArrayAccess):
            arr = self.evaluate_expr(expr.array)
            idx = self.evaluate_expr(expr.index)
            if isinstance(arr, (list, str)):
                return arr[int(idx)]
            raise EvaluationError(f"Cannot index into {type(arr)}")
        if isinstance(expr, FunctionCall):
            return self.evaluate_function(expr)
        if isinstance(expr, (TaskCall, FailsTask, CachedTaskCall)):
            return expr
        if isinstance(expr, TemplateString):
            return self.evaluate_template_string(expr)
        if isinstance(expr, ListLiteral):
            return [self.evaluate_expr(e) for e in expr.elements]
        raise EvaluationError(f"Cannot evaluate expression: {type(expr).__name__}")

    def _resolve_identifier(self, name: str) -> Any:
        special = {'stdout': self.stdout, 'stderr': self.stderr, 'exit_code': self.exit_code}
        if name in special:
            return special[name]
        if name in self.variables:
            return self.variables[name]
        if name in self.env_vars:
            return self.env_vars[name]
        raise EvaluationError(f"Undefined variable: {name}")

    def evaluate_binary_op(self, expr: BinaryOp) -> Any:
        left = self.evaluate_expr(expr.left)

        # Short-circuit logical operators
        if expr.op == '&&':
            return self.is_truthy(left) and self.is_truthy(self.evaluate_expr(expr.right))
        if expr.op == '||':
            return self.is_truthy(left) or self.is_truthy(self.evaluate_expr(expr.right))

        right = self.evaluate_expr(expr.right)

        if expr.op == '+':
            return left + right
        if expr.op == '-':
            return left - right
        if expr.op == '*':
            return left * right
        if expr.op == '/':
            if right == 0:
                raise EvaluationError("Division by zero")
            return left / right
        if expr.op == '%':
            return self._concat_or_modulo(left, right)

        comparisons = {
            '==': lambda a, b: a == b, '!=': lambda a, b: a != b,
            '<': lambda a, b: a < b, '>': lambda a, b: a > b,
            '<=': lambda a, b: a <= b, '>=': lambda a, b: a >= b,
        }
        if expr.op in comparisons:
            return comparisons[expr.op](left, right)

        raise EvaluationError(f"Unknown operator: {expr.op}")

    @staticmethod
    def _concat_or_modulo(left: Any, right: Any) -> Any:
        if isinstance(left, (str, list)) or isinstance(right, (str, list)):
            if isinstance(left, list) or isinstance(right, list):
                raise EvaluationError("Cannot convert list to string")
            return str(left) + str(right)
        return left % right

    def evaluate_unary_op(self, expr: UnaryOp) -> Any:
        operand = self.evaluate_expr(expr.operand)
        if expr.op == '!':
            return not self.is_truthy(operand)
        if expr.op == '-':
            return -operand
        raise EvaluationError(f"Unknown unary operator: {expr.op}")

    def evaluate_function(self, expr: FunctionCall) -> Any:
        args = [self.evaluate_expr(a) for a in expr.args]
        name = expr.name

        if name == 'len':
            self._check_arg_count(args, 1, 'len')
            return len(args[0])

        if name == 'contains':
            self._check_arg_count(args, 2, 'contains')
            text = self._resolve_text(args[0])
            try:
                if re.search(args[1], text):
                    return True
            except re.error:
                pass
            return args[1] in text

        if name == 'equals':
            self._check_arg_count(args, 2, 'equals')
            target, value = args
            if isinstance(value, int) and target in ('exit code', 'exit_code'):
                return self.exit_code == value
            return self._resolve_text(target) == str(value)

        if name == 'exists':
            self._check_arg_count(args, 1, 'exists')
            return os.path.exists(self._resolve_path(args[0]))

        if name == 'fail':
            return ('fail', args[0] if args else None)

        raise EvaluationError(f"Unknown function: {name}")

    @staticmethod
    def _check_arg_count(args: List[Any], expected: int, fn: str):
        if len(args) != expected:
            raise EvaluationError(f"{fn}() takes exactly {expected} arguments")

    def _resolve_text(self, target: Any) -> str:
        if target == 'stdout':
            return self.stdout
        if target == 'stderr':
            return self.stderr
        if target in ('exit code', 'exit_code'):
            return str(self.exit_code)
        if isinstance(target, str) and os.path.isfile(self._resolve_path(target)):
            with open(self._resolve_path(target), 'r') as f:
                return f.read()
        return str(target)

    def evaluate_template_string(self, expr: TemplateString) -> str:
        result = []
        for part in expr.parts:
            if isinstance(part, str):
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
        if os.path.isabs(path):
            return path
        if self.output_dir:
            return os.path.join(self.output_dir, path)
        if self.workspace:
            return os.path.join(self.workspace, path)
        return path

    @staticmethod
    def is_truthy(value: Any) -> bool:
        if isinstance(value, (bool, int, float)):
            return bool(value)
        if isinstance(value, (str, list)):
            return len(value) > 0
        return bool(value)

    @staticmethod
    def default_for_type(type_name: str) -> Any:
        return {'string': '', 'int': 0, 'float': 0.0, 'bool': False}.get(type_name)

    def evaluate_success(self, success_block: Dict[str, List[ASTNode]]) -> Dict[str, bool]:
        results = {}
        for name, statements in success_block.items():
            try:
                self.evaluate_statements(statements)
                results[name] = True
            except ReturnValue as rv:
                results[name] = self.is_truthy(rv.value)
            except Exception:
                results[name] = False
        return results
