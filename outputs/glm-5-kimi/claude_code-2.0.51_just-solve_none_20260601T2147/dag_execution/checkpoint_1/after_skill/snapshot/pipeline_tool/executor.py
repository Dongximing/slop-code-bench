"""
Pipeline executor - runs tasks and manages dependencies.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional, Tuple, Set

try:
    import tomli
except ImportError:
    import toml as tomli

from parser import parse_pipeline
from ast_nodes import ASTNode, TaskCall, FailsTask, ExpressionStatement, TemplateString, Pipeline
from evaluator import ExprEvaluator, EvaluationError, ReturnValue, BreakSignal, ContinueSignal


class PipelineError(Exception):
    """Base exception for pipeline errors."""
    def __init__(self, code: int, message: str):
        self.code = code
        self.message = message
        super().__init__(f"Exit {code}: {message}")


class SyntaxError_(PipelineError):
    def __init__(self, message: str):
        super().__init__(2, f"SYNTAX_ERROR:{message}")


class InvalidPipeError(PipelineError):
    def __init__(self, message: str):
        super().__init__(3, f"INVALID_PIPE:{message}")


@dataclass
class TaskResult:
    """Result of a task execution."""
    job_id: int
    task_name: str
    params: Dict[str, Any]
    stdout: str
    stderr: str
    timed_out: bool
    success: Dict[str, bool]
    output: Optional[str]
    exit_code: int
    parent: Optional[int]
    duration: float

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class PendingTaskCall:
    """Represents a pending task call."""
    name: str
    args: List[Any]
    kwargs: Dict[str, Any]
    parent: Optional[int]
    expect_fail: bool = False


class PipelineExecutor:
    """Executes pipeline tasks."""

    def __init__(self, pipeline_path: str, workspace: str, output_dir: str, config_path: Optional[str] = None):
        self.pipeline_path = pipeline_path
        self.workspace = os.path.abspath(workspace)
        self.output_dir = os.path.abspath(output_dir)
        self.config_path = config_path

        self.pipeline: Optional[Pipeline] = None
        self.config: Dict[str, Any] = {}
        self.env_vars: Dict[str, str] = {}
        self.entry_task: Optional[str] = None
        self.clean_cwd: bool = False

        self.job_counter = 0
        self.results: List[TaskResult] = []
        self.running_tasks: Dict[int, PendingTaskCall] = {}
        self.task_stack: Set[str] = set()  # For cycle detection

        self.jobs_file = None
        self.jobs_path = os.path.join(self.output_dir, "jobs.jsonl")

    def load_pipeline(self):
        """Load and parse the pipeline file."""
        try:
            with open(self.pipeline_path, 'r') as f:
                source = f.read()
            self.pipeline = parse_pipeline(source)
        except FileNotFoundError:
            raise InvalidPipeError(f"Pipeline file not found: {self.pipeline_path}")
        except Exception as e:
            raise SyntaxError_(str(e))

    def load_config(self):
        """Load the config file if specified."""
        if not self.config_path:
            return

        try:
            with open(self.config_path, 'rb') as f:
                self.config = tomli.load(f)
        except FileNotFoundError:
            raise InvalidPipeError(f"Config file not found: {self.config_path}")
        except Exception as e:
            raise SyntaxError_(f"Invalid TOML: {e}")

        # Extract config values
        if 'env' in self.config:
            self.env_vars = {k: str(v) for k, v in self.config['env'].items()}

        if 'entry' in self.config:
            self.entry_task = self.config['entry']
        else:
            self.entry_task = 'main'

        self.clean_cwd = self.config.get('clean_cwd', False)

    def emit_event(self, event: str, job_id: int, **kwargs):
        """Emit a JSON event to stdout."""
        data = {"event": event, "job_id": job_id}
        data.update(kwargs)
        print(json.dumps(data), flush=True)

    def write_job_result(self, result: TaskResult):
        """Write a job result to the jobs.jsonl file."""
        if self.jobs_file is None:
            os.makedirs(self.output_dir, exist_ok=True)
            self.jobs_file = open(self.jobs_path, 'w')

        self.jobs_file.write(json.dumps(result.to_dict()) + '\n')
        self.jobs_file.flush()

    def resolve_params(self, task: TaskDef, call: PendingTaskCall) -> Dict[str, Any]:
        """Resolve parameters for a task call."""
        params = {}
        param_defs = {p.name: p for p in task.params}

        # Process positional arguments
        args = list(call.args)
        for i, param in enumerate(task.params):
            if i < len(args):
                params[param.name] = self._cast_value(args[i], param.param_type, param.is_list_element_type)
            elif param.name in call.kwargs:
                params[param.name] = self._cast_value(call.kwargs[param.name], param.param_type, param.is_list_element_type)
            elif param.default_value is not None:
                params[param.name] = self._evaluate_default(param.default_value, param.param_type, param.is_list_element_type)
            else:
                raise InvalidPipeError(f"Missing required parameter '{param.name}' for task '{task.name}'")

        # Process keyword arguments
        for name, value in call.kwargs.items():
            if name not in param_defs:
                raise InvalidPipeError(f"Unknown parameter '{name}' for task '{task.name}'")
            params[name] = self._cast_value(value, param_defs[name].param_type, param_defs[name].is_list_element_type)

        return params

    def _cast_value(self, value: Any, type_name: str, is_list: bool) -> Any:
        """Cast a value to the specified type."""
        if is_list:
            if not isinstance(value, list):
                raise InvalidPipeError(f"Expected list, got {type(value).__name__}")
            return [self._cast_single(v, type_name) for v in value]
        return self._cast_single(value, type_name)

    def _cast_single(self, value: Any, type_name: str) -> Any:
        """Cast a single value to the specified type."""
        if isinstance(value, bool) and type_name == 'bool':
            return value
        if isinstance(value, int) and type_name in ('int', 'float'):
            return float(value) if type_name == 'float' else value
        if isinstance(value, float) and type_name in ('int', 'float'):
            return int(value) if type_name == 'int' else value
        if isinstance(value, str):
            if type_name == 'string':
                return value
            elif type_name == 'int':
                try:
                    return int(value)
                except ValueError:
                    raise InvalidPipeError(f"Cannot convert '{value}' to int")
            elif type_name == 'float':
                try:
                    return float(value)
                except ValueError:
                    raise InvalidPipeError(f"Cannot convert '{value}' to float")
            elif type_name == 'bool':
                if value.lower() in ('true', '1', 'yes'):
                    return True
                elif value.lower() in ('false', '0', 'no', ''):
                    return False
                raise InvalidPipeError(f"Cannot convert '{value}' to bool")

        return value

    def _evaluate_default(self, default: ASTNode, type_name: str, is_list: bool) -> Any:
        """Evaluate a default value expression."""
        evaluator = ExprEvaluator(env_vars=self.env_vars, workspace=self.workspace)
        value = evaluator.evaluate_expr(default)

        # Handle environment variable substitution
        if isinstance(default, TemplateString):
            for part in default.parts:
                if isinstance(part, str) and part.startswith('${') and part.endswith('}'):
                    var_name = part[2:-1]
                    if var_name in self.env_vars:
                        value = self._cast_single(self.env_vars[var_name], type_name)

        return self._cast_value(value, type_name, is_list)

    def resolve_run_command(self, run: str, params: Dict[str, Any], output_dir: Optional[str]) -> str:
        """Resolve parameter references in a run command."""
        result = run

        # Replace ${params.x} with actual values
        for name, value in params.items():
            pattern = f'${{params.{name}}}'
            if isinstance(value, list):
                # Convert list to space-separated string
                value_str = ' '.join(str(v) for v in value)
            else:
                value_str = str(value)
            result = result.replace(pattern, value_str)

        # Replace ${workspace}
        result = result.replace('${workspace}', self.workspace)

        # Replace output directory reference
        if output_dir:
            result = result.replace('${output}', output_dir)

        return result

    def resolve_output_path(self, output_expr: Optional[ASTNode], params: Dict[str, Any]) -> Optional[str]:
        """Resolve the output directory path."""
        if output_expr is None:
            return None

        evaluator = ExprEvaluator(
            variables={'params': params},
            env_vars=self.env_vars,
            workspace=self.workspace
        )

        result = evaluator.evaluate_expr(output_expr)

        if isinstance(result, str):
            # Check if it uses ${workspace}
            if result.startswith('${workspace}'):
                result = result.replace('${workspace}', self.workspace)

            # Make relative paths relative to workspace
            if not os.path.isabs(result):
                full_path = os.path.join(self.workspace, result)
            else:
                full_path = result

            # Check for path escaping workspace
            abs_path = os.path.abspath(full_path)
            if not abs_path.startswith(self.workspace):
                raise InvalidPipeError(f"Output path escapes workspace: {result}")

            return result

        return None

    def execute_run(self, command: str, cwd: str, timeout: Optional[float]) -> Tuple[str, str, int, bool]:
        """Execute a run command."""
        # Clean special characters
        command = command.replace('\u2018', "'").replace('\u2019', "'")

        # Use bash to execute
        try:
            result = subprocess.run(
                ['bash', '-c', command],
                capture_output=True,
                text=True,
                cwd=cwd,
                timeout=timeout
            )
            return result.stdout, result.stderr, result.returncode, False
        except subprocess.TimeoutExpired:
            return "", "", 124, True
        except Exception as e:
            return "", str(e), 1, False

    def check_circular_dependency(self, task_name: str):
        """Check for circular dependencies."""
        if task_name in self.task_stack:
            raise InvalidPipeError(f"Circular dependency detected: {task_name}")
        self.task_stack.add(task_name)

    def execute_task(self, call: PendingTaskCall) -> TaskResult:
        """Execute a single task."""
        self.job_counter += 1
        job_id = self.job_counter

        # Check task exists
        if call.name not in self.pipeline.tasks:
            raise InvalidPipeError(f"Undefined task: {call.name}")

        task = self.pipeline.tasks[call.name]

        # Check for circular dependency
        self.check_circular_dependency(call.name)

        self.emit_event("TASK_STARTED", job_id, task=call.name, params=call.kwargs if call.kwargs else call.args)

        # Resolve parameters
        params = self.resolve_params(task, call)

        # Resolve output directory
        output_dir = self.resolve_output_path(task.output, params)
        if output_dir:
            # Make output dir relative to workspace
            if not os.path.isabs(output_dir):
                output_dir = os.path.join(self.workspace, output_dir)
            os.makedirs(output_dir, exist_ok=True)

        # Determine working directory
        work_dir = output_dir if output_dir else self.workspace

        # Execute requires first
        success = {}
        stdout = ""
        stderr = ""
        exit_code = 0
        timed_out = False
        duration = 0.0

        if task.requires:
            requires_success = self.execute_requires(task.requires, params, job_id, output_dir)
            if not requires_success:
                self.emit_event("TASK_FAILED", job_id)
                self.task_stack.discard(call.name)
                result = TaskResult(
                    job_id=job_id,
                    task_name=call.name,
                    params=params,
                    stdout="",
                    stderr="",
                    timed_out=False,
                    success={},
                    output=output_dir,
                    exit_code=1,
                    parent=call.parent,
                    duration=0.0
                )
                self.results.append(result)
                self.write_job_result(result)
                return result

        # Execute run if present
        if task.run:
            self.emit_event("TASK_RUNNING", job_id)

            start_time = time.time()
            command = self.resolve_run_command(task.run, params, output_dir)
            stdout, stderr, exit_code, timed_out = self.execute_run(command, work_dir, task.timeout)
            duration = time.time() - start_time

            # Clean stdout/stderr
            stdout = stdout.replace('\u2018', "'").replace('\u2019', "'")
            stderr = stderr.replace('\u2018', "'").replace('\u2019', "'")

        # Evaluate success criteria
        if task.success:
            evaluator = ExprEvaluator(
                variables={'params': params},
                env_vars=self.env_vars,
                stdout=stdout,
                stderr=stderr,
                exit_code=exit_code,
                output_dir=output_dir,
                workspace=self.workspace
            )
            try:
                success = evaluator.evaluate_success(task.success)
            except Exception:
                success = {name: False for name in task.success}
        else:
            success = {}

        # Determine final success
        task_success = exit_code == 0 and all(success.values()) if success else exit_code == 0

        if task_success:
            self.emit_event("TASK_COMPLETED", job_id)
        else:
            self.emit_event("TASK_FAILED", job_id)

        self.task_stack.discard(call.name)

        result = TaskResult(
            job_id=job_id,
            task_name=call.name,
            params=params,
            stdout=stdout,
            stderr=stderr,
            timed_out=timed_out,
            success=success,
            output=output_dir,
            exit_code=exit_code,
            parent=call.parent,
            duration=round(duration, 4)
        )

        self.results.append(result)
        self.write_job_result(result)
        return result

    def execute_requires(self, statements: List[ASTNode], params: Dict[str, Any],
                         parent_id: int, output_dir: Optional[str]) -> bool:
        """Execute requires statements."""
        evaluator = RequiresEvaluator(self, params, parent_id, output_dir)
        try:
            evaluator.evaluate_statements(statements)
            return True
        except Exception as e:
            if isinstance(e, (ReturnValue, BreakSignal, ContinueSignal)):
                return True
            return False

    def run(self) -> int:
        """Run the pipeline and return exit code."""
        try:
            # Load pipeline and config
            self.load_pipeline()
            self.load_config()

            # Set up workspace
            os.makedirs(self.workspace, exist_ok=True)
            os.makedirs(self.output_dir, exist_ok=True)

            # Clean cwd if specified
            if self.clean_cwd:
                for item in os.listdir(self.workspace):
                    path = os.path.join(self.workspace, item)
                    if os.path.isfile(path):
                        os.remove(path)
                    elif os.path.isdir(path):
                        shutil.rmtree(path)

            # Determine entry task
            entry = self.entry_task
            if not entry:
                entry = 'main'

            # Parse entry task call
            entry_call = self.parse_entry_call(entry, parent=None)

            # Execute entry task
            result = self.execute_task(entry_call)

            # Close jobs file
            if self.jobs_file:
                self.jobs_file.close()

            return 0 if result.exit_code == 0 else 1

        except PipelineError as e:
            print(e.message, file=sys.stderr)
            return e.code
        except Exception as e:
            print(f"SYNTAX_ERROR:{str(e)}", file=sys.stderr)
            return 2

    def parse_entry_call(self, entry: str, parent: Optional[int]) -> PendingTaskCall:
        """Parse an entry task specification."""
        # Check if it's a simple task name or a call with params
        entry = entry.strip()

        # Simple name
        if '(' not in entry:
            return PendingTaskCall(name=entry, args=[], kwargs={}, parent=parent)

        # Parse task call
        match = re.match(r'(\w+)\((.*)\)', entry)
        if not match:
            raise InvalidPipeError(f"Invalid entry task specification: {entry}")

        name = match.group(1)
        args_str = match.group(2).strip()

        if not args_str:
            return PendingTaskCall(name=name, args=[], kwargs={}, parent=parent)

        # Parse arguments
        args = []
        kwargs = {}

        # Split by comma, handling nested structures
        parts = self._split_args(args_str)

        for part in parts:
            part = part.strip()
            if '=' in part:
                key, value = part.split('=', 1)
                kwargs[key.strip()] = self._parse_value(value.strip())
            else:
                args.append(self._parse_value(part))

        return PendingTaskCall(name=name, args=args, kwargs=kwargs, parent=parent)

    def _split_args(self, args_str: str) -> List[str]:
        """Split arguments by comma, respecting parentheses and quotes."""
        result = []
        current = []
        depth = 0
        in_string = False
        string_char = None

        for char in args_str:
            if char in ('"', "'") and not in_string:
                in_string = True
                string_char = char
            elif char == string_char and in_string:
                in_string = False
            elif char == '(' and not in_string:
                depth += 1
            elif char == ')' and not in_string:
                depth -= 1
            elif char == ',' and depth == 0 and not in_string:
                result.append(''.join(current))
                current = []
                continue

            current.append(char)

        if current:
            result.append(''.join(current))

        return result

    def _parse_value(self, value: str) -> Any:
        """Parse a value from the config."""
        value = value.strip()

        # Boolean
        if value == 'TRUE':
            return True
        if value == 'FALSE':
            return False

        # String
        if (value.startswith('"') and value.endswith('"')) or \
           (value.startswith("'") and value.endswith("'")):
            return value[1:-1]

        # Integer
        try:
            return int(value)
        except ValueError:
            pass

        # Float
        try:
            return float(value)
        except ValueError:
            pass

        # Default to string
        return value


class RequiresEvaluator(ExprEvaluator):
    """Evaluator for requires blocks that handles task calls."""

    def __init__(self, executor: PipelineExecutor, params: Dict[str, Any],
                 parent_id: int, output_dir: Optional[str]):
        super().__init__(
            variables={'params': params},
            env_vars=executor.env_vars,
            workspace=executor.workspace
        )
        self.executor = executor
        self.parent_id = parent_id

    def evaluate_statement(self, stmt: ASTNode) -> Any:
        """Evaluate a statement, handling task calls."""
        if isinstance(stmt, FailsTask):
            return self._run_fails_task(stmt.task_call)
        if isinstance(stmt, ExpressionStatement):
            stmt = stmt.expression
        if isinstance(stmt, TaskCall):
            return self._run_task_call(stmt, expect_fail=False)
        if isinstance(stmt, FailsTask):
            return self._run_fails_task(stmt.task_call)
        return super().evaluate_statement(stmt)

    def evaluate_expr(self, expr: ASTNode) -> Any:
        """Evaluate an expression, handling task calls."""
        if isinstance(expr, TaskCall):
            return self._run_task_call(expr, expect_fail=False)
        if isinstance(expr, FailsTask):
            return self._run_fails_task(expr.task_call)
        return super().evaluate_expr(expr)

    def _run_task_call(self, call: TaskCall, expect_fail: bool) -> bool:
        """Execute a task call and return True on success."""
        args = [self.evaluate_expr(a) if isinstance(a, ASTNode) else a for a in call.args]
        kwargs = {k: self.evaluate_expr(v) if isinstance(v, ASTNode) else v
                  for k, v in call.kwargs.items()}

        pending = PendingTaskCall(
            name=call.name, args=args, kwargs=kwargs, parent=self.parent_id, expect_fail=expect_fail
        )
        result = self.executor.execute_task(pending)

        if expect_fail:
            if result.exit_code == 0:
                raise EvaluationError(f"Task {call.name} was expected to fail but succeeded")
            return True
        if result.exit_code != 0:
            raise EvaluationError(f"Task {call.name} failed")
        return True

    def _run_fails_task(self, call: TaskCall) -> bool:
        """Execute a task that's expected to fail."""
        return self._run_task_call(call, expect_fail=True)
