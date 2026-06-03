"""Evaluate expressions in success/requires blocks."""
import os
import re
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any
import subprocess
import sys

from models.task import ExpressionAST, ParameterType, Value


@dataclass
class EvaluationContext:
    """Context for expression evaluation."""
    variables: Dict[str, Any] = field(default_factory=dict)
    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0
    output_dir: str = ""
    workspace: str = ""
    params: Dict[str, Any] = field(default_factory=dict)
    task_results: Dict[str, bool] = field(default_factory=dict)  # Task name -> success
    failed_tasks: Dict[str, bool] = field(default_factory=dict)  # Tasks that failed
    return_value: Optional[Any] = None
    break_flag: bool = False
    continue_flag: bool = False


class ExpressionEvaluator:
    """Evaluate expressions in success/requires blocks."""

    def __init__(self, workspace: str, output_dir: str):
        self.workspace = workspace
        self.output_dir = output_dir

    def evaluate(self, ast: ExpressionAST, context: Optional[EvaluationContext] = None) -> Any:
        """Evaluate an AST and return the result."""
        if context is None:
            context = EvaluationContext(workspace=self.workspace, output_dir=self.output_dir)
        return self._eval(ast, context)

    def _eval(self, ast: ExpressionAST, context: EvaluationContext) -> Any:
        if ast.type == 'block':
            for stmt in ast.children:
                if context.break_flag or context.continue_flag:
                    break
                result = self._eval(stmt, context)
                if context.return_value is not None:
                    return context.return_value
            return None

        elif ast.type == 'identifier':
            if ast.value in context.variables:
                return context.variables[ast.value]
            if ast.value in context.params:
                return context.params[ast.value]
            # Assume it's a variable
            return context.variables.get(ast.value, None)

        elif ast.type == 'literal':
            return ast.value

        elif ast.type == 'operator':
            return self._evaluate_operator(ast, context)

        elif ast.type == 'function_call':
            return self._evaluate_function_call(ast, context)

        elif ast.type == 'fails':
            # fails(task_call) - returns True if the task failed
            # task_call is stored as the first child
            task_call = ast.children[0]
            task_name = task_call.value
            # Get the result of the task from task_results
            return not context.task_results.get(task_name, False)

        elif ast.type == 'if':
            return self._evaluate_if(ast, context)

        elif ast.type == 'for':
            return self._evaluate_for(ast, context)

        elif ast.type == 'while':
            return self._evaluate_while(ast, context)

        elif ast.type == 'assign':
            value = self._eval(ast.children[0], context)
            context.variables[ast.value] = value
            return value

        elif ast.type == 'return':
            if ast.children:
                context.return_value = self._eval(ast.children[0], context)
            else:
                context.return_value = None
            return context.return_value

        elif ast.type == 'break':
            context.break_flag = True
            return None

        elif ast.type == 'continue':
            context.continue_flag = True
            return None

        elif ast.type == 'list':
            return [self._eval(child, context) for child in ast.children]

        return None

    def _evaluate_operator(self, ast: ExpressionAST, context: EvaluationContext) -> Any:
        op = ast.operator

        if op == 'not':
            val = self._eval(ast.children[0], context)
            return not self._to_bool(val)

        if op in ('+', '-', '*', '/', '%'):
            left = self._eval(ast.children[0], context)
            right = self._eval(ast.children[1], context)
            return self._arithmetic(op, left, right)

        if op in ('==', '!=', '>', '<', '>=', '<='):
            left = self._eval(ast.children[0], context)
            right = self._eval(ast.children[1], context)
            return self._compare(op, left, right)

        if op == 'and':
            left = self._eval(ast.children[0], context)
            if not self._to_bool(left):
                return False
            return self._to_bool(self._eval(ast.children[1], context))

        if op == 'or':
            left = self._eval(ast.children[0], context)
            if self._to_bool(left):
                return True
            return self._to_bool(self._eval(ast.children[1], context))

        return None

    def _to_bool(self, val: Any) -> bool:
        """Convert a value to boolean."""
        if isinstance(val, bool):
            return val
        if isinstance(val, (int, float)):
            return val != 0
        if isinstance(val, str):
            return val.lower() == 'true' and val != ''
        if val is None:
            return False
        if isinstance(val, list):
            return len(val) > 0
        return bool(val)

    def _arithmetic(self, op: str, left: Any, right: Any) -> Any:
        """Perform arithmetic operation."""
        # Handle lists specially for % (concatenation)
        if op == '%' and isinstance(left, str):
            # String concatenation
            return left + str(right)
        if isinstance(left, (int, float)) and isinstance(right, (int, float)):
            if op == '+':
                return left + right
            elif op == '-':
                return left - right
            elif op == '*':
                return left * right
            elif op == '/':
                return left / right
            elif op == '%':
                return left % right
        raise TypeError(f"Cannot perform {op} on {type(left)} and {type(right)}")

    def _compare(self, op: str, left: Any, right: Any) -> bool:
        """Perform comparison operation."""
        if op == '==':
            return left == right
        elif op == '!=':
            return left != right
        elif op == '>':
            return left > right
        elif op == '<':
            return left < right
        elif op == '>=':
            return left >= right
        elif op == '<=':
            return left <= right
        return False

    def _evaluate_function_call(self, ast: ExpressionAST, context: EvaluationContext) -> Any:
        func_name = ast.value

        if func_name == 'len':
            arg = self._eval(ast.children[0], context) if ast.children else None
            if isinstance(arg, list):
                return len(arg)
            elif isinstance(arg, str):
                return len(arg)
            elif isinstance(arg, dict):
                return len(arg)
            raise TypeError(f"len() expected list/str, got {type(arg)}")

        elif func_name == 'contains':
            arg1 = self._eval(ast.children[0], context)
            arg2 = self._eval(ast.children[1], context)
            return self._contains(arg1, arg2)

        elif func_name == 'equals':
            arg1 = self._eval(ast.children[0], context)
            arg2 = self._eval(ast.children[1], context)
            return str(arg1) == str(arg2)

        elif func_name == 'exists':
            arg = self._eval(ast.children[0], context)
            path = str(arg)
            # Check for workspace substitution
            if '${workspace}' in path:
                path = path.replace('${workspace}', self.workspace)
            # Check for output substitution
            if '${output}' in path and self.output_dir:
                path = path.replace('${output}', self.output_dir)
            return os.path.exists(path)

        raise ValueError(f"Unknown function: {func_name}")

    def _contains(self, arg1: Any, arg2: Any) -> bool:
        """Check if arg2 contains arg1 or arg1 contains arg2 pattern."""
        if isinstance(arg1, str) and isinstance(arg2, str):
            # arg2 is the text to search in, arg1 is the pattern
            # But based on the spec, it's contains(file, pattern)
            # So arg1 is file/stdout/stderr, arg2 is pattern
            # Since we evaluate children first, we need to swap interpretation
            # Actually looking at the spec: contains("results.txt", "Yes!")
            # So first arg is file path or stdout/stderr, second is pattern
            filepath = arg1
            pattern = arg2

            if filepath == 'stdout':
                return pattern in self.stdout
            elif filepath == 'stderr':
                return pattern in self.stderr
            else:
                # Check for workspace substitution
                actual_path = filepath
                if '${workspace}' in actual_path:
                    actual_path = actual_path.replace('${workspace}', self.workspace)
                if not os.path.exists(actual_path):
                    return False
                try:
                    with open(actual_path, 'r') as f:
                        return pattern in f.read()
                except:
                    return False
        elif isinstance(arg1, str) and isinstance(arg2, list):
            return arg1 in arg2
        return False

    def _evaluate_if(self, ast: ExpressionAST, context: EvaluationContext) -> Any:
        """Evaluate if/elif/else statement."""
        condition = self._eval(ast.condition, context)
        if self._to_bool(condition):
            return self._eval(ast.true_branch, context)

        # Check elif
        if hasattr(ast, 'children') and ast.children and ast.children[0]:
            return self._eval(ast.children[0], context)

        # Else
        if ast.false_branch:
            return self._eval(ast.false_branch, context)

        return None

    def _evaluate_for(self, ast: ExpressionAST, context: EvaluationContext) -> Any:
        """Evaluate for loop."""
        # Parse init: var = value
        init = ast.init
        if init.type == 'assign':
            var_name = init.value
            init_value = self._eval(init.children[0], context)
            context.variables[var_name] = init_value

        while True:
            # Check condition
            condition = self._eval(ast.condition, context)
            if not self._to_bool(condition):
                break

            context.continue_flag = False
            result = self._eval(ast.body, context)

            if context.break_flag:
                context.break_flag = False
                break

            # Execute update
            if ast.update:
                self._eval(ast.update, context)

        return None

    def _evaluate_while(self, ast: ExpressionAST, context: EvaluationContext) -> Any:
        """Evaluate while loop."""
        while True:
            condition = self._eval(ast.condition, context)
            if not self._to_bool(condition):
                break

            context.continue_flag = False
            result = self._eval(ast.body, context)

            if context.break_flag:
                context.break_flag = False
                break

        return None

    def set_output(self, context: EvaluationContext, stdout: str, stderr: str, exit_code: int):
        """Set output for context (used after task execution)."""
        context.stdout = stdout
        context.stderr = stderr
        context.exit_code = exit_code


def evaluate_success(success: dict, context: EvaluationContext) -> dict:
    """Evaluate all success criteria and return results."""
    evaluator = ExpressionEvaluator(context.workspace, context.output_dir)
    results = {}

    for name, ast in success.items():
        # Create fresh context for each success criterion
        ctx = EvaluationContext(
            variables=dict(context.variables),
            stdout=context.stdout,
            stderr=context.stderr,
            exit_code=context.exit_code,
            output_dir=context.output_dir,
            workspace=context.workspace,
            params=context.params,
            task_results=dict(context.task_results),
            failed_tasks=dict(context.failed_tasks)
        )
        results[name] = evaluator.evaluate(ast, ctx) is True

    return results
