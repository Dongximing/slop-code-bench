"""
Task execution engine - runs commands and evaluates success criteria.
"""
import subprocess
import time
import json
from pathlib import Path
from typing import Dict, Any, List, Optional
from pipeline_types import Task, JobResult, SuccessCriterion
from evaluator import ExpressionEvaluator, EvaluationError


class TaskExecutor:
    """
    Executes pipeline tasks, manages workspace, and evaluates success criteria.
    """

    def __init__(self, workspace: Path, output_dir: Path, env: Optional[Dict[str, str]] = None):
        self.workspace = workspace
        self.output_dir = output_dir
        self.env = env or {}

        # Create output directory
        self.output_dir.mkdir(parents=True, exist_ok=True)

        # Store job results
        self.job_results: List[JobResult] = []
        self.job_counter = 0

        # Track completed tasks
        self.completed_tasks: Dict[str, JobResult] = {}

        # Jobs.jsonl file
        self.jobs_file = self.output_dir / "jobs.jsonl"

        # Cleanup for newline chars in output
        self._cleanup_chars = {
            '\u2018': "'",
            '\u2019': "'",
        }

    def _cleanup_output(self, text: str) -> str:
        """Replace fancy quotes with regular quotes."""
        for fancy, plain in self._cleanup_chars.items():
            text = text.replace(fancy, plain)
        return text

    def _resolve_path(self, path: str) -> Path:
        """Resolve a path relative to workspace or output directory."""
        path = path.strip()
        if path.startswith('/'):
            # Absolute path
            return Path(path)
        elif '${workspace}' in path:
            # Use workspace variable
            path = path.replace('${workspace}', str(self.workspace))
            return Path(path)
        elif '${output}' in path:
            # Use output variable
            path = path.replace('${output}', str(self.output_dir))
            return Path(path)
        else:
            # Relative to working directory
            return Path(path)

    def _resolve_params(self, task: Task, params: Dict[str, Any]) -> Dict[str, Any]:
        """Resolve parameter references in the params dict."""
        # This is a simple resolver - recursively substitute values
        resolved = {}
        for key, value in params.items():
            if isinstance(value, str):
                # Check if it contains ${params.x} references
                if '${' in value:
                    # Build evaluation context
                    context = {
                        'params': self._get_param_values(task),
                    }
                    evaluator = ExpressionEvaluator(self.workspace, self.output_dir)
                    # Resolve the string
                    value = evaluator.evaluate(value, context)
            resolved[key] = value
        return resolved

    def _get_param_values(self, task: Task) -> Dict[str, Any]:
        """Get actual parameter values for a task."""
        # Use resolved params from the task, or defaults
        params = {}

        # Add resolved params
        params.update(task.resolved_params)

        # Add defaults for missing params
        for param_def in task.params:
            if param_def.name not in params and param_def.has_default:
                params[param_def.name] = param_def.default_value

        return params

    def _run_commands(self, task: Task, params: Dict[str, Any]) -> tuple:
        """
        Run the commands for a task.
        Returns (stdout, stderr, exit_code, timed_out).
        """
        if not task.run:
            return "", "", 0, False

        # Resolve parameters in the run commands
        context = {
            'params': params,
            'workspace': str(self.workspace),
            'output': str(self.output_dir),
        }
        evaluator = ExpressionEvaluator(self.workspace, self.output_dir)

        # Substitute ${params.x} and ${workspace} in commands
        commands = evaluator.evaluate(task.run, context)

        # Write commands to a temp script for execution
        # Actually, we should execute each command directly
        # For simplicity, we'll just run the commands as a shell script

        start_time = time.time()
        timeout = task.timeout if task.timeout else 3600  # default 1 hour

        # Build the shell script
        script_lines = []
        for line in str(commands).strip().split('\n'):
            line = line.strip()
            if line:
                # Check if this is a ${params.x} reference
                if '${' in line:
                    # Evaluate the line as an expression if needed
                    context = {
                        'params': params,
                        'workspace': str(self.workspace),
                        'output': str(self.output_dir),
                    }
                    line = evaluator.evaluate(line, context)
                    line = str(line).strip()
                script_lines.append(line)

        script = '\n'.join(script_lines)

        try:
            result = subprocess.run(
                script,
                shell=True,
                cwd=self.workspace,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=self.env
            )

            stdout = self._cleanup_output(result.stdout)
            stderr = self._cleanup_output(result.stderr)
            exit_code = result.returncode
            timed_out = False

            return stdout, stderr, exit_code, timed_out

        except subprocess.TimeoutExpired:
            return "", "Command timed out", 124, True

    def _evaluate_success_criteria(self, task: Task, params: Dict[str, Any],
                                    stdout: str, stderr: str, output_dir: Optional[Path]) -> Dict[str, bool]:
        """
        Evaluate all success criteria for a task.
        Returns a dict of criterion name -> boolean result.
        """
        results = {}

        # By default, check exit code
        exit_code = 0

        # Build context for evaluation
        context = {
            'params': params,
            'workspace': str(self.workspace),
            'output': str(self.output_dir),
        }
        evaluator = ExpressionEvaluator(self.workspace, self.output_dir)

        for criterion in task.success:
            try:
                # Evaluate the expression
                result = evaluator.parse_and_evaluate(criterion.expression, context.copy())
                results[criterion.name] = self._to_bool(result)
            except (EvaluationError, Exception) as e:
                # If evaluation fails, mark as failed
                print(f"Warning: Failed to evaluate success criterion '{criterion.name}': {e}")
                results[criterion.name] = False

        # Add exit code check if no explicit success criteria
        if not task.success:
            results['_exit_code'] = (exit_code == 0)

        return results

    def _to_bool(self, value: Any) -> bool:
        """Convert value to boolean."""
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return value != 0
        if isinstance(value, str):
            return value.lower() not in ('false', '', 'none', 'null')
        if value is None:
            return False
        return True

    def execute_task(self, task: Task, params: Dict[str, Any],
                     parent_job_id: Optional[int] = None) -> JobResult:
        """
        Execute a task and return the job result.
        """
        job_id = self.job_counter + 1
        self.job_counter = job_id

        # Emit TASK_STARTED event
        event = json.dumps({
            "event": "TASK_STARTED",
            "job_id": job_id,
        })
        print(event)

        # Resolve output directory
        task_output_dir = None
        if task.output:
            resolved_path = evaluator.ExpressionEvaluator(self.workspace, self.output_dir).evaluate(
                task.output,
                {'params': params}
            )
            task_output_dir = Path(resolved_path)
            task_output_dir.mkdir(parents=True, exist_ok=True)

        # Run the task
        start_time = time.time()
        stdout, stderr, exit_code, timed_out = self._run_commands(task, params)

        # If task timed out, set exit code to 124
        if timed_out:
            exit_code = 124

        duration = time.time() - start_time

        # Evaluate success criteria (only if task completed)
        success_criteria = {}
        if exit_code == 0 or not task.run:
            success_criteria = self._evaluate_success_criteria(
                task, params, stdout, stderr, task_output_dir
            )
        else:
            # Task failed - set success criteria to false
            if task.success:
                for criterion in task.success:
                    success_criteria[criterion.name] = False
            else:
                success_criteria['_exit_code'] = False

        # Create job result
        result = JobResult(
            job_id=job_id,
            task=task.name,
            params=params,
            stdout=stdout,
            stderr=stderr,
            timed_out=timed_out,
            success=success_criteria,
            output=str(task_output_dir) if task_output_dir else None,
            exit_code=exit_code,
            parent=parent_job_id,
            duration=duration
        )

        self.job_results.append(result)

        # Emit TASK_RUNNING then TASK_COMPLETED/TASK_FAILED
        event = json.dumps({
            "event": "TASK_RUNNING",
            "job_id": job_id,
        })
        print(event)

        if exit_code == 0 and all(success_criteria.values()):
            event = json.dumps({
                "event": "TASK_COMPLETED",
                "job_id": job_id,
            })
            print(event)

            # Store completed task
            self.completed_tasks[task.name] = result

            # Write to jobs.jsonl
            with open(self.jobs_file, 'a') as f:
                f.write(result.to_jsonl() + '\n')
        else:
            event = json.dumps({
                "event": "TASK_FAILED",
                "job_id": job_id,
            })
            print(event)

            # Write to jobs.jsonl even for failed tasks
            with open(self.jobs_file, 'a') as f:
                f.write(result.to_jsonl() + '\n')

        return result
