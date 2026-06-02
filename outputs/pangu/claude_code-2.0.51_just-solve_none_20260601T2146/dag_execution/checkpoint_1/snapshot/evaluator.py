"""
Expression evaluator for success criteria and requires blocks.
Supports arithmetic, conditionals, string operations, and builtin functions.
"""
import re
from typing import Dict, Any, List, Optional, Tuple
from pathlib import Path


class EvaluationError(Exception):
    """Error evaluating expression."""
    pass


class ExpressionEvaluator:
    """
    Evaluates expressions in the pipeline language.
    Supports:
    - Variables, literals (int, float, string, bool)
    - Arithmetic (+, -, *, /, %, comparisons)
    - Logical operations (&&, ||, !)
    - String concatenation (%)
    - Arrays/indexing
    - Control flow (if/else, for loops, while loops)
    - Builtin functions (len, contains, equals, exists, fail)
    - Parameter substitution ($ params)
    """

    def __init__(self, workspace: Path, output_dir: Optional[Path] = None):
        self.workspace = workspace
        self.output_dir = output_dir
        self._functions = {
            'len': self._len_func,
            'contains': self._contains_func,
            'equals': self._equals_func,
            'exists': self._exists_func,
            'fail': self._fail_func,
        }

    def evaluate(self, expr: str, context: Dict[str, Any]) -> Any:
        """
        Evaluate an expression string with the given context.
        Returns the result of the evaluation.
        """
        if not expr or not expr.strip():
            return True  # Empty success criteria is considered True

        self._context = context.copy()
        self._result = None
        self._pos = 0
        self._expr = expr.strip()

        result = self._parse_expression()
        return result

    def parse_and_evaluate(self, expr: str, context: Dict[str, Any]) -> Any:
        """Parse and evaluate a block of statements."""
        self._context = context.copy()
        self._result = None
        self._pos = 0
        self._expr = expr.strip()

        return self._parse_block()

    # ============================================================
    # Parsing helpers
    # ============================================================

    def _skip_whitespace(self):
        """Skip whitespace characters."""
        while self._pos < len(self._expr) and self._expr[self._pos].isspace():
            self._pos += 1

    def _peek(self, offset: int = 0) -> str:
        """Look at the character at current position + offset."""
        idx = self._pos + offset
        if idx < len(self._expr):
            return self._expr[idx]
        return '\0'

    def _consume(self, expected: Optional[str] = None) -> str:
        """Consume and return the current character, optionally checking it."""
        self._skip_whitespace()
        if self._pos >= len(self._expr):
            if expected is None:
                return '\0'
            raise EvaluationError(f"Unexpected end of expression, expected '{expected}'")

        ch = self._expr[self._pos]
        if expected is not None and ch != expected:
            raise EvaluationError(f"Expected '{expected}', got '{ch}'")

        self._pos += 1
        return ch

    def _parse_identifier(self) -> str:
        """Parse an identifier (variable name, function name)."""
        self._skip_whitespace()

        if self._pos >= len(self._expr):
            raise EvaluationError("Expected identifier, found end of expression")

        start = self._pos
        while self._pos < len(self._expr):
            ch = self._expr[self._pos]
            if ch.isalnum() or ch == '_':
                self._pos += 1
            else:
                break

        if self._pos == start:
            raise EvaluationError(f"Expected identifier, found '{self._expr[self._pos]}'")

        return self._expr[start:self._pos]

    def _parse_string(self) -> str:
        """Parse a quoted string."""
        self._skip_whitespace()
        if self._pos >= len(self._expr):
            raise EvaluationError("Expected string, found end of expression")

        quote = self._expr[self._pos]
        if quote not in ('"', "'"):
            raise EvaluationError(f"Expected string, found '{quote}'")

        self._pos += 1
        result = []

        while self._pos < len(self._expr):
            ch = self._expr[self._pos]
            if ch == '\\':
                # Handle escape sequences
                self._pos += 1
                if self._pos < len(self._expr):
                    esc = self._expr[self._pos]
                    if esc == 'n':
                        result.append('\n')
                    elif esc == 't':
                        result.append('\t')
                    elif esc == 'r':
                        result.append('\r')
                    elif esc in ('"', "'", '\\'):
                        result.append(esc)
                    else:
                        result.append(esc)
                    self._pos += 1
                else:
                    result.append('\\')
            elif ch == quote:
                self._pos += 1
                break
            else:
                result.append(ch)
                self._pos += 1

        return ''.join(result)

    def _parse_number(self) -> Any:
        """Parse a number (int or float)."""
        self._skip_whitespace()

        start = self._pos
        has_decimal = False

        while self._pos < len(self._expr):
            ch = self._expr[self._pos]
            if ch == '.' and not has_decimal:
                has_decimal = True
                self._pos += 1
            elif ch.isdigit():
                self._pos += 1
            else:
                break

        if self._pos == start:
            raise EvaluationError(f"Expected number, found '{self._expr[self._pos]}'")

        num_str = self._expr[start:self._pos]
        if has_decimal:
            return float(num_str)
        return int(num_str)

    def _parse_list(self) -> List[Any]:
        """Parse a list literal [a, b, c]."""
        self._skip_whitespace()
        if self._peek() != '[':
            raise EvaluationError(f"Expected '[', found '{self._peek()}'")

        self._pos += 1  # consume '['

        result = []

        if self._peek() == ']':
            self._pos += 1
            return result

        while self._pos < len(self._expr):
            self._skip_whitespace()
            value = self._parse_expression()
            result.append(value)

            self._skip_whitespace()
            if self._peek() == ',':
                self._pos += 1
                continue
            elif self._peek() == ']':
                self._pos += 1
                break
            else:
                raise EvaluationError(f"Expected ',' or ']', found '{self._peek()}'")

        return result

    # ============================================================
    # Expression parsing (recursive descent)
    # ============================================================

    def _parse_expression(self) -> Any:
        """Parse an expression."""
        self._skip_whitespace()
        return self._parse_or()

    def _parse_or(self) -> Any:
        """Parse OR expressions (&&, ||)."""
        left = self._parse_and()

        self._skip_whitespace()
        while self._peek() == '&' and self._peek(1) == '&':
            self._pos += 2  # consume '&&'
            right = self._parse_and()
            left = left and self._to_bool(right)
            self._skip_whitespace()

        while self._peek() == '|' and self._peek(1) == '|':
            self._pos += 2  # consume '||'
            right = self._parse_and()
            left = left or self._to_bool(right)
            self._skip_whitespace()

        return left

    def _parse_and(self) -> Any:
        """Parse AND expressions."""
        left = self._parse_equality()

        self._skip_whitespace()
        while self._peek() == '&' and self._peek(1) == '&':
            self._pos += 2
            right = self._parse_equality()
            left = left and self._to_bool(right)
            self._skip_whitespace()

        return left

    def _parse_equality(self) -> Any:
        """Parse equality expressions (==, !=)."""
        left = self._parse_comparison()

        self._skip_whitespace()
        while self._peek() == '=' and self._peek(1) == '=':
            self._pos += 2
            right = self._parse_comparison()
            left = left == right
            self._skip_whitespace()

        while self._peek() == '!' and self._peek(1) == '=':
            self._pos += 2
            right = self._parse_comparison()
            left = left != right
            self._skip_whitespace()

        return left

    def _parse_comparison(self) -> Any:
        """Parse comparison expressions (<, >, <=, >=)."""
        left = self._parse_additive()

        self._skip_whitespace()
        while True:
            if self._peek() == '<' and self._peek(1) == '=':
                self._pos += 2
                right = self._parse_additive()
                left = left <= right
            elif self._peek() == '>':
                if self._peek(1) == '=':
                    self._pos += 2
                    right = self._parse_additive()
                    left = left >= right
                else:
                    self._pos += 1
                    right = self._parse_additive()
                    left = left > right
            elif self._peek() == '<':
                self._pos += 1
                right = self._parse_additive()
                left = left < right
            else:
                break

            self._skip_whitespace()

        return left

    def _parse_additive(self) -> Any:
        """Parse addition and subtraction."""
        left = self._parse_term()

        self._skip_whitespace()
        while self._peek() in ('+', '-'):
            op = self._peek()
            self._pos += 1
            right = self._parse_term()

            if op == '+':
                left = left + right
            else:
                left = left - right

            self._skip_whitespace()

        return left

    def _parse_term(self) -> Any:
        """Parse multiplication, division, and modulo."""
        left = self._parse_factor()

        self._skip_whitespace()
        while self._peek() in ('*', '/', '%'):
            op = self._peek()
            self._pos += 1
            right = self._parse_factor()

            if op == '*':
                left = left * right
            elif op == '/':
                left = left / right
            elif op == '%':
                left = left % right

            self._skip_whitespace()

        return left

    def _parse_factor(self) -> Any:
        """Parse unary operators and primary expressions."""
        self._skip_whitespace()

        # Handle unary + and -
        if self._peek() == '+':
            self._pos += 1
            return self._parse_factor()
        elif self._peek() == '-':
            self._pos += 1
            return -self._parse_number() if self._expr[self._pos].isdigit() else -self._parse_factor()
        elif self._peek() == '!':
            self._pos += 1
            value = self._parse_factor()
            return not self._to_bool(value)

        return self._parse_primary()

    def _parse_primary(self) -> Any:
        """Parse primary expressions (literals, variables, function calls, etc.)."""
        self._skip_whitespace()

        # Handle parameter substitution ${...}
        if self._peek() == '$' and self._peek(1) == '{':
            return self._parse_param_substitution()

        # String literal
        if self._peek() in ('"', "'"):
            return self._parse_string()

        # List literal
        if self._peek() == '[':
            return self._parse_list()

        # Number
        if self._peek().isdigit() or self._peek() == '.':
            return self._parse_number()

        # True/False
        if self._expr.startswith('TRUE', self._pos):
            self._pos += 4
            return True
        if self._expr.startswith('FALSE', self._pos):
            self._pos += 5
            return False

        # Function call
        identifier = self._parse_identifier()
        self._skip_whitespace()
        if self._peek() == '(':
            return self._parse_function_call(identifier)

        # Check if it's a variable
        if identifier in self._context:
            return self._context[identifier]

        # It's an identifier (string value)
        return identifier

    def _parse_param_substitution(self) -> str:
        """Parse ${params.x} or ${workspace}."""
        if not (self._peek() == '$' and self._peek(1) == '{'):
            raise EvaluationError("Expected ${...}")

        self._pos += 2  # consume ${'
        start = self._pos

        depth = 0
        while self._pos < len(self._expr):
            ch = self._expr[self._pos]
            if ch == '{':
                depth += 1
            elif ch == '}':
                if depth == 0:
                    break
                depth -= 1
            self._pos += 1

        inner = self._expr[start:self._pos]

        if inner.startswith('params.'):
            param_name = inner[7:]
            if param_name in self._context.get('params', {}):
                return str(self._context['params'][param_name])
            else:
                return f"${{params.{param_name}}}"
        elif inner == 'workspace':
            return str(self.workspace.resolve()) if self.workspace else ""
        elif inner == 'output':
            return str(self.output_dir) if self.output_dir else ""
        else:
            # Try to resolve as variable
            if inner in self._context:
                return str(self._context[inner])
            return f"${{{inner}}}"

    def _parse_function_call(self, func_name: str) -> Any:
        """Parse a function call: func_name(arg1, arg2, ...)."""
        if self._peek() != '(':
            raise EvaluationError(f"Expected '(', found '{self._peek()}'")

        self._pos += 1  # consume '('
        self._skip_whitespace()

        args = []

        if self._peek() == ')':
            self._pos += 1
        else:
            while True:
                self._skip_whitespace()
                arg = self._parse_expression()
                args.append(arg)

                self._skip_whitespace()
                if self._peek() == ',':
                    self._pos += 1
                    continue
                elif self._peek() == ')':
                    self._pos += 1
                    break
                else:
                    raise EvaluationError(f"Expected ',' or ')', found '{self._peek()}'")

        # Execute the function
        if func_name not in self._functions:
            raise EvaluationError(f"Unknown function: {func_name}")

        return self._functions[func_name](args)

    def _parse_block(self) -> Any:
        """Parse a block of statements."""
        self._result = None

        while self._pos < len(self._expr):
            self._skip_whitespace()

            if self._pos >= len(self._expr):
                break

            # Check for variable declaration
            ident = self._parse_identifier()
            self._skip_whitespace()
            if self._peek() == ':':
                # This is a varDecl
                self._pos += 1
                self._skip_whitespace()
                type_str = self._parse_identifier()
                self._skip_whitespace()
                self._consume('=')
                value = self._parse_expression()
                self._context[ident] = value
                self._skip_whitespace()
                if self._peek() == ';':
                    self._pos += 1
                continue

            # Check for for loop
            if ident == 'for':
                self._skip_whitespace()
                self._consume('(')
                # Parse forInit
                loop_var_type = self._parse_identifier()
                loop_var = self._parse_identifier()
                self._skip_whitespace()
                self._consume('=')
                start_val = self._parse_expression()
                self._skip_whitespace()
                self._consume(';')
                cond = self._parse_expression()
                self._skip_whitespace()
                self._consume(';')
                update = self._parse_identifier()
                self._skip_whitespace()
                self._consume('=')
                update_expr = self._parse_expression()
                self._skip_whitespace()
                self._consume(')')
                self._skip_whitespace()
                self._consume('{')
                loop_body = self._collect_block()

                # Execute loop
                self._context[loop_var] = start_val
                while self._evaluate_condition(cond):
                    # Execute body
                    body_context = self._context.copy()
                    evaluator = ExpressionEvaluator(self.workspace, self.output_dir)
                    evaluator._context = body_context
                    evaluator.parse_and_evaluate(loop_body, body_context)
                    self._context.update(evaluator._context)

                    # Update loop variable
                    if update == loop_var:
                        if isinstance(update_expr, int):
                            self._context[loop_var] += 1
                        else:
                            self._context[loop_var] = update_expr
                    else:
                        # Handle increment
                        if self._peek() in ('++', '--'):
                            self._pos += 2

                # After loop, read closing brace
                self._skip_whitespace()
                if self._peek() == '}':
                    self._pos += 1
                continue

            # Check for while loop
            if ident == 'while':
                self._skip_whitespace()
                self._consume('(')
                cond = self._parse_expression()
                self._consume(')')
                self._skip_whitespace()
                self._consume('{')
                loop_body = self._collect_block()

                # Execute loop
                while self._evaluate_condition(cond):
                    body_context = self._context.copy()
                    evaluator = ExpressionEvaluator(self.workspace, self.output_dir)
                    evaluator._context = body_context
                    evaluator.parse_and_evaluate(loop_body, body_context)
                    self._context.update(evaluator._context)

                self._skip_whitespace()
                if self._peek() == '}':
                    self._pos += 1
                continue

            # Check for if statement
            if ident == 'if':
                self._skip_whitespace()
                self._consume('(')
                cond = self._parse_expression()
                self._consume(')')
                self._skip_whitespace()
                self._consume('{')
                then_body = self._collect_block()

                if self._to_bool(self.evaluate(cond, self._context)):
                    evaluator = ExpressionEvaluator(self.workspace, self.output_dir)
                    evaluator._context = self._context.copy()
                    evaluator.parse_and_evaluate(then_body, evaluator._context)
                    self._context.update(evaluator._context)

                # Consume closing brace
                self._skip_whitespace()
                if self._peek() == '}':
                    self._pos += 1

                # Check for elif/else
                while True:
                    self._skip_whitespace()
                    if self._expr.startswith('elif', self._pos):
                        self._pos += 4
                        self._skip_whitespace()
                        self._consume('(')
                        elif_cond = self._parse_expression()
                        self._consume(')')
                        self._skip_whitespace()
                        self._consume('{')
                        elif_body = self._collect_block()

                        if not self._result and self._to_bool(self.evaluate(elif_cond, self._context)):
                            evaluator = ExpressionEvaluator(self.workspace, self.output_dir)
                            evaluator._context = self._context.copy()
                            evaluator.parse_and_evaluate(elif_body, evaluator._context)
                            self._context.update(evaluator._context)

                        self._skip_whitespace()
                        if self._peek() == '}':
                            self._pos += 1
                    elif self._expr.startswith('else', self._pos):
                        self._pos += 4
                        self._skip_whitespace()
                        self._consume('{')
                        else_body = self._collect_block()

                        if not self._result:
                            evaluator = ExpressionEvaluator(self.workspace, self.output_dir)
                            evaluator._context = self._context.copy()
                            evaluator.parse_and_evaluate(else_body, evaluator._context)
                            self._context.update(evaluator._context)

                        self._skip_whitespace()
                        if self._peek() == '}':
                            self._pos += 1
                    else:
                        break

                continue

            # Check for return statement
            if ident == 'return':
                value = self._parse_expression()
                self._result = self._to_bool(value)
                self._skip_whitespace()
                if self._peek() == ';':
                    self._pos += 1
                return self._result

            # Check for break
            if ident == 'break':
                self._result = True
                self._skip_whitespace()
                if self._peek() == ';':
                    self._pos += 1
                return self._result

            # Check for continue
            if ident == 'continue':
                self._result = True
                self._skip_whitespace()
                if self._peek() == ';':
                    self._pos += 1
                return self._result

            # Check for function call (top-level statement)
            self._skip_whitespace()
            if self._peek() == '(':
                # This is a function call that has side effects
                func_result = self._parse_function_call(ident)
                if isinstance(func_result, bool) and not func_result:
                    self._result = False
                    return self._result
                self._skip_whitespace()
                if self._peek() == ';':
                    self._pos += 1
                continue

            # Try to parse as expression
            self._pos -= len(ident)
            value = self._parse_expression()
            self._skip_whitespace()
            if self._peek() == ';':
                self._pos += 1

        return self._result if self._result is not None else True

    def _collect_block(self) -> str:
        """Collect a block of text until matching brace."""
        start = self._pos
        depth = 0

        while self._pos < len(self._expr):
            ch = self._expr[self._pos]
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth < 0:
                    break
            self._pos += 1

        return self._expr[start:self._pos]

    def _evaluate_condition(self, expr: Any) -> bool:
        """Evaluate a condition to boolean."""
        return self._to_bool(expr)

    def _to_bool(self, value: Any) -> bool:
        """Convert a value to boolean."""
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return value != 0
        if isinstance(value, str):
            return value.lower() in ('true', 'yes', '1', 't')
        if isinstance(value, list):
            return len(value) > 0
        return value is not None

    def _len_func(self, args: List) -> int:
        """Builtin len function."""
        if len(args) != 1:
            raise EvaluationError(f"len() takes exactly 1 argument, got {len(args)}")
        if isinstance(args[0], list):
            return len(args[0])
        raise EvaluationError(f"len() requires a list, got {type(args[0])}")

    def _contains_func(self, args: List) -> bool:
        """Builtin contains function."""
        if len(args) != 2:
            raise EvaluationError(f"contains() takes exactly 2 arguments, got {len(args)}")

        arg1, arg2 = args

        if isinstance(arg1, str) and isinstance(arg2, str):
            return arg2 in arg1

        if isinstance(arg1, list) and isinstance(arg2, str):
            return arg2 in arg1

        # Try to read file contents
        if isinstance(arg1, str):
            # arg1 is a file path, arg2 is what we're looking for
            path = Path(arg1)
            if path.exists():
                try:
                    with open(path, 'r') as f:
                        content = f.read()
                    return arg2 in content
                except:
                    return False

        return False

    def _equals_func(self, args: List) -> bool:
        """Builtin equals function."""
        if len(args) != 2:
            raise EvaluationError(f"equals() takes exactly 2 arguments, got {len(args)}")

        arg1, arg2 = args

        if isinstance(arg1, str):
            # Could be a special value like "stdout", "stderr", "exit_code"
            path = Path(arg1)
            if path.exists():
                try:
                    with open(path, 'r') as f:
                        return f.read().strip() == str(arg2)
                except:
                    return False

        return str(arg1) == str(arg2)

    def _exists_func(self, args: List) -> bool:
        """Builtin exists function."""
        if len(args) != 1:
            raise EvaluationError(f"exists() takes exactly 1 argument, got {len(args)}")

        path = Path(args[0])
        return path.exists()

    def _fail_func(self, args: List) -> bool:
        """Builtin fail function - checks if a task failed."""
        if len(args) != 1:
            raise EvaluationError(f"fail() takes exactly 1 argument, got {len(args)}")

        # This is a special function - we store it for later processing
        # in the requires block evaluation
        task_name = str(args[0])
        self._context['_failed_tasks'] = self._context.get('_failed_tasks', [])
        self._context['_failed_tasks'].append(task_name)
        # For now, assume it's checking if a task failed
        # In the requires block context, we'll use this differently
        return task_name in self._context.get('_completed_tasks', [])
