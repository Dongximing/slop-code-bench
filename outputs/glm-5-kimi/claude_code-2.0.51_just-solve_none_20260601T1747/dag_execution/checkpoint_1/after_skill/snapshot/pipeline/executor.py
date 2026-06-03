"""
Task executor for running pipeline tasks.
"""

import os
import sys
import json
import time
import subprocess
import shutil
from typing import Dict, List, Any, Optional, Tuple
from dataclasses import dataclass, field, asdict
from .ast_nodes import (
    TaskDef, Parameter, TaskCallStmt, FailsStmt, DollarVar
)
from .evaluator import Evaluator, RequiresEvaluator, SuccessEvaluator


@dataclass
class JobResult:
    job_id: int
    task: str
    params: Dict[str, Any]
    stdout: str
    stderr: str
    timed_out: bool
    success: Dict[str, bool]
    output: Optional[str]
    exit_code: int
    parent: Optional[int]
    duration: float

    def to_dict(self) -> dict:
        return {
            "job_id": self.job_id,
            "task": self.task,
            "params": self.params,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "timed_out": self.timed_out,
            "success": self.success,
            "output": self.output,
            "exit_code": self.exit_code,
            "parent": self.parent,
            "duration": self.duration
        }


class TaskExecutor:
    def __init__(self, pipeline, workspace: str, output_dir: str, env_vars: Dict[str, str],
                 clean_cwd: bool = False):
        self.pipeline = pipeline
        self.workspace = os.path.abspath(workspace)
        self.output_dir = os.path.abspath(output_dir)
        self.env_vars = env_vars
        self.clean_cwd = clean_cwd
        self.job_counter = 0
        self.job_results: List[JobResult] = []
        self.call_stack: set = set()  # Track task calls to detect recursion

    def log_event(self, event: str, job_id: int, **kwargs):
        """Log event to stdout as JSON."""
        data = {"event": event, "job_id": job_id, **kwargs}
        print(json.dumps(data))
        sys.stdout.flush()

    def validate_params(self, task: TaskDef, positional_args: List[Any],
                        named_args: Dict[str, Any]) -> Dict[str, Any]:
        """Validate and resolve parameters for a task call."""
        params = {}

        # Check for parameters with defaults first
        has_default_seen = False
        for i, param in enumerate(task.params):
            if param.has_default:
                has_default_seen = True
            elif has_default_seen and not param.has_default:
                raise ValueError(f"SYNTAX_ERROR: Parameter '{param.name}' without default follows one with default")

        # Process positional args
        if len(positional_args) > len(task.params):
            raise ValueError(f"INVALID_PIPE: Too many arguments for task '{task.name}'")

        for i, value in enumerate(positional_args):
            param = task.params[i]
            params[param.name] = self.validate_param_type(param, value)

        # Process named args
        for name, value in named_args.items():
            param_def = None
            for p in task.params:
                if p.name == name:
                    param_def = p
                    break
            if param_def is None:
                raise ValueError(f"INVALID_PIPE: Unknown parameter '{name}' for task '{task.name}'")
            if name in params:
                raise ValueError(f"INVALID_PIPE: Duplicate parameter '{name}'")
            params[name] = self.validate_param_type(param_def, value)

        # Fill in defaults for missing parameters
        for param in task.params:
            if param.name not in params:
                if param.has_default:
                    if param.env_var:
                        # Resolve from environment
                        env_value = self.env_vars.get(param.env_var.lstrip('${').rstrip('}'))
                        if env_value is None:
                            env_value = os.environ.get(param.env_var.lstrip('$'))
                        if env_value is None:
                            raise ValueError(f"INVALID_PIPE: Environment variable '{param.env_var}' not set")
                        params[param.name] = self.cast_env_value(env_value, param.type_name)
                    else:
                        params[param.name] = param.default_value
                else:
                    raise ValueError(f"INVALID_PIPE: Missing required parameter '{param.name}'")

        return params

    def validate_param_type(self, param: Parameter, value: Any) -> Any:
        """Validate and cast a parameter value to the expected type."""
        type_name = param.type_name

        if type_name == 'string':
            return str(value)
        elif type_name == 'int':
            if isinstance(value, bool):
                return int(value)
            if isinstance(value, (int, float)):
                return int(value)
            raise ValueError(f"INVALID_PIPE: Cannot convert {type(value).__name__} to int")
        elif type_name == 'float':
            if isinstance(value, (int, float)):
                return float(value)
            raise ValueError(f"INVALID_PIPE: Cannot convert {type(value).__name__} to float")
        elif type_name == 'bool':
            if isinstance(value, bool):
                return value
            if isinstance(value, (int, float)):
                return bool(value)
            if isinstance(value, str):
                if value.lower() == 'true':
                    return True
                elif value.lower() == 'false':
                    return False
                return bool(value)
            raise ValueError(f"INVALID_PIPE: Cannot convert {type(value).__name__} to bool")
        elif type_name.startswith('list['):
            if isinstance(value, list):
                inner_type = param.inner_type
                return [self.cast_value(v, inner_type) for v in value]
            raise ValueError(f"INVALID_PIPE: Expected list, got {type(value).__name__}")

        return value

    def cast_value(self, value: Any, type_name: str) -> Any:
        """Cast a value to the specified type."""
        if type_name == 'string':
            return str(value)
        elif type_name == 'int':
            return int(value)
        elif type_name == 'float':
            return float(value)
        elif type_name == 'bool':
            if isinstance(value, str):
                return value.lower() == 'true'
            return bool(value)
        return value

    def cast_env_value(self, value: str, type_name: str) -> Any:
        """Cast an environment variable value to the expected type."""
        if type_name == 'string':
            return value
        elif type_name == 'int':
            return int(value)
        elif type_name == 'float':
            return float(value)
        elif type_name == 'bool':
            if value.lower() in ('true', '1', 'yes'):
                return True
            return False
        elif type_name.startswith('list['):
            # Parse comma-separated list
            return [v.strip() for v in value.split(',')]
        return value

    def resolve_run_command(self, run_block: str, params: Dict[str, Any],
                            output_dir: Optional[str]) -> str:
        """Resolve parameters in run command."""
        command = run_block

        # Replace ${params.x} with parameter values
        for name, value in params.items():
            command = command.replace(f'${{params.{name}}}', str(value))

        # Replace ${workspace}
        command = command.replace('${workspace}', self.workspace)

        # Replace ${output}
        if output_dir:
            command = command.replace('${output}', output_dir)

        return command

    def run_command(self, command: str, cwd: str, timeout: Optional[float] = None) -> Tuple[str, str, int, bool]:
        """Run a shell command and return stdout, stderr, exit_code, timed_out."""
        try:
            result = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                cwd=cwd,
                timeout=timeout
            )
            stdout = result.stdout
            stderr = result.stderr
            exit_code = result.returncode
            timed_out = False
        except subprocess.TimeoutExpired:
            stdout = ""
            stderr = ""
            exit_code = 124
            timed_out = True

        # Clean up stdout/stderr
        stdout = self.clean_output(stdout)
        stderr = self.clean_output(stderr)

        return stdout, stderr, exit_code, timed_out

    def clean_output(self, text: str) -> str:
        """Clean output text - replace special characters."""
        text = text.replace('\u2018', "'")
        text = text.replace('\u2019', "'")
        return text

    def resolve_output_path(self, output_expr, params: Dict[str, Any]) -> Optional[str]:
        """Resolve output directory path."""
        if output_expr is None:
            return None

        evaluator = Evaluator(
            variables={'params': params},
            env_vars=self.env_vars,
            workspace=self.workspace
        )

        output_str = evaluator.evaluate_expr(output_expr)

        if output_str is None:
            return None

        # Handle ${workspace} prefix
        if isinstance(output_str, str) and '${workspace}' in output_str:
            output_str = output_str.replace('${workspace}', self.workspace)
            return output_str

        # Make absolute path
        if os.path.isabs(output_str):
            return output_str

        return os.path.join(self.workspace, output_str)

    def validate_path_in_workspace(self, path: str) -> bool:
        """Check that a path is within the workspace."""
        abs_path = os.path.abspath(path)
        return abs_path.startswith(self.workspace)

    def execute_task_call(self, task_call: TaskCallStmt, expect_fail: bool = False,
                          parent_job_id: Optional[int] = None) -> bool:
        """Execute a task call from within a requires block."""
        task_name = task_call.task_name

        # Check for circular dependency
        if task_name in self.call_stack:
            raise ValueError(f"INVALID_PIPE: Circular dependency detected for task '{task_name}'")

        # Get task definition
        if task_name not in self.pipeline.tasks:
            raise ValueError(f"INVALID_PIPE: Undefined task '{task_name}'")

        task_def = self.pipeline.tasks[task_name]

        # Evaluate positional and named arguments
        evaluator = Evaluator(
            variables={},
            env_vars=self.env_vars,
            workspace=self.workspace
        )

        positional_args = [evaluator.evaluate_expr(arg) for arg in task_call.positional_args]
        named_args = {name: evaluator.evaluate_expr(value) for name, value in task_call.named_args.items()}

        # Execute the task
        result = self.execute_task(task_def, positional_args, named_args, parent_job_id)

        # Check if result matches expectation
        success = result.exit_code == 0 and all(result.success.values()) if result.success else result.exit_code == 0

        if expect_fail:
            return not success
        return success

    def execute_task(self, task: TaskDef, positional_args: List[Any],
                     named_args: Dict[str, Any], parent_job_id: Optional[int] = None) -> JobResult:
        """Execute a task and return the result."""
        self.job_counter += 1
        job_id = self.job_counter

        start_time = time.time()

        # Validate and get parameters
        params = self.validate_params(task, positional_args, named_args)

        # Log task started
        self.log_event("TASK_STARTED", job_id, task=task.name, params=params)

        stdout = ""
        stderr = ""
        exit_code = 0
        timed_out = False
        success_results = {}
        output_dir = None

        try:
            # Check for recursion
            if task.name in self.call_stack:
                raise ValueError(f"INVALID_PIPE: Recursive call to task '{task.name}'")
            self.call_stack.add(task.name)

            # Resolve output directory
            output_dir = self.resolve_output_path(task.output, params)
            if output_dir and not self.validate_path_in_workspace(output_dir):
                raise ValueError(f"INVALID_PIPE: Output path '{output_dir}' escapes workspace")

            # Execute requires block first
            if task.requires_block:
                requires_evaluator = RequiresEvaluator(
                    variables={'params': params},
                    env_vars=self.env_vars,
                    workspace=self.workspace,
                    output_dir=output_dir,
                    task_executor=lambda tc, expect_fail=False: self.execute_task_call(tc, expect_fail, job_id)
                )

                for stmt in task.requires_block:
                    try:
                        requires_evaluator.evaluate_stmt(stmt)
                    except Exception as e:
                        if "Task call failed" in str(e) or "Expected task to fail" in str(e):
                            raise
                        # For other errors, we continue (the requires might have failed() wrapper)

            # Log task running
            self.log_event("TASK_RUNNING", job_id, task=task.name, params=params)

            # Execute run block
            if task.run_block:
                # Setup working directory
                if output_dir:
                    os.makedirs(output_dir, exist_ok=True)
                    cwd = output_dir
                else:
                    cwd = self.workspace

                # Clean cwd if needed
                if self.clean_cwd and output_dir:
                    for item in os.listdir(output_dir):
                        item_path = os.path.join(output_dir, item)
                        if os.path.isfile(item_path):
                            os.remove(item_path)
                        else:
                            shutil.rmtree(item_path)

                # Resolve and run command
                command = self.resolve_run_command(task.run_block, params, output_dir)
                stdout, stderr, exit_code, timed_out = self.run_command(
                    command, cwd, task.timeout
                )

            # Evaluate success block
            if task.success_block:
                success_evaluator = SuccessEvaluator(
                    variables={'params': params},
                    env_vars=self.env_vars,
                    workspace=self.workspace,
                    output_dir=output_dir,
                    stdout=stdout,
                    stderr=stderr,
                    exit_code=exit_code
                )

                for name, block in task.success_block.items():
                    try:
                        success_results[name] = success_evaluator.evaluate_success_block(block)
                    except Exception as e:
                        success_results[name] = False

            self.call_stack.discard(task.name)

        except Exception as e:
            self.call_stack.discard(task.name)
            stderr = str(e)
            exit_code = 1

        duration = time.time() - start_time

        # Determine overall success
        overall_success = exit_code == 0 and all(success_results.values()) if success_results else exit_code == 0

        result = JobResult(
            job_id=job_id,
            task=task.name,
            params=params,
            stdout=stdout,
            stderr=stderr,
            timed_out=timed_out,
            success=success_results,
            output=os.path.relpath(output_dir, self.workspace) if output_dir else None,
            exit_code=exit_code,
            parent=parent_job_id,
            duration=round(duration, 4)
        )

        self.job_results.append(result)

        # Log completion or failure
        if overall_success:
            self.log_event("TASK_COMPLETED", job_id, task=task.name, params=params)
        else:
            self.log_event("TASK_FAILED", job_id, task=task.name, params=params)

        return result

    def write_results(self):
        """Write job results to jobs.jsonl."""
        os.makedirs(self.output_dir, exist_ok=True)
        results_path = os.path.join(self.output_dir, "jobs.jsonl")

        with open(results_path, 'w') as f:
            for result in self.job_results:
                f.write(json.dumps(result.to_dict()) + '\n')
