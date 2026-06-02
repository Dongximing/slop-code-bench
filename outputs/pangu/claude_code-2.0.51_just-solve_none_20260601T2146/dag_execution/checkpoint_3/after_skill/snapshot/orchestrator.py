"""
Main orchestrator for pipeline execution.
"""
import time
from pathlib import Path
from typing import Dict, List, Optional, Set, Any

from pipeline_types import Task, JobResult, Config
from pipeline_parser import parse_pipeline_file, ParseError, parse_call_args
from config_parser import parse_config_file, parse_entry_task
from executor import TaskExecutor


class Orchestrator:
    """
    Orchestrates pipeline execution: resolves dependencies and schedules tasks.
    """

    def __init__(
        self, workspace: Path, output_dir: Path,
        config: Optional[Config] = None, env: Optional[Dict[str, str]] = None
    ):
        self.workspace = workspace
        self.output_dir = output_dir
        self.config = config or Config()
        self.env = env or {}

        self.env.update(self.config.env)
        self.executor = TaskExecutor(workspace, output_dir, self.env)

        self.tasks: Dict[str, Task] = {}
        self.executed_tasks: Dict[str, List[JobResult]] = {}
        self.running_tasks: Set[str] = set()
        self.job_results: Dict[str, JobResult] = {}

    def register_tasks(self, parsed_tasks: List[Task]):
        """Register tasks from parsed pipeline."""
        for task in parsed_tasks:
            if task.name in self.tasks:
                raise ValueError(f"Duplicate task definition: {task.name}")
            self.tasks[task.name] = task

    def resolve_entry_task(self, entry_spec: Optional[str] = None) -> tuple:
        """Determine which task is the entry point and with what parameters."""
        if entry_spec:
            return parse_entry_task(entry_spec)

        if 'main' in self.tasks:
            return 'main', {}

        if len(self.tasks) == 1:
            return list(self.tasks.keys())[0], {}

        raise ValueError("No entry task specified and no default 'main' or single task found")

    def _resolve_params_from_call(self, task: Task, call_params: Dict[str, Any]) -> Dict[str, Any]:
        """Resolve parameters from a task call, applying defaults and type checking."""
        resolved = {}
        param_by_name = {}

        for i, param_def in enumerate(task.params):
            if param_def.has_default:
                resolved[param_def.name] = param_def.default_value
            param_by_name[param_def.name] = param_def

        for key, value in call_params.items():
            if isinstance(key, int):
                if key < len(task.params):
                    param_def = task.params[key]
                    resolved[param_def.name] = self._cast_value(value, param_def)
            else:
                if key in param_by_name:
                    resolved[key] = self._cast_value(value, param_by_name[key])
                else:
                    raise ValueError(f"Unknown parameter: {key}")

        return resolved

    def _cast_value(self, value: str, param_def) -> Any:
        """Cast a value from the task call to the parameter's expected type."""
        type_name = param_def.param_type.name
        if type_name == 'STRING':
            return str(value)
        if type_name == 'INT':
            try:
                return int(value)
            except ValueError:
                raise ValueError(f"Invalid integer value: {value}")
        if type_name == 'FLOAT':
            try:
                return float(value)
            except ValueError:
                raise ValueError(f"Invalid float value: {value}")
        if type_name == 'BOOL':
            return str(value).upper() in ('TRUE', '1', 'YES', 'T')
        if type_name == 'LIST':
            return [v.strip().strip('"').strip("'") for v in value.split(',')] if value else []
        return value

    def resolve_dependencies(self, task: Task, params: Dict[str, Any]) -> List[JobResult]:
        """Execute tasks in the requires block."""
        if not task.requires:
            return []

        results = []
        pos = 0
        text = task.requires.strip()

        while pos < len(text):
            while pos < len(text) and text[pos].isspace():
                pos += 1

            if pos >= len(text) or text[pos] == '}':
                break

            start = pos
            while pos < len(text) and (text[pos].isalnum() or text[pos] == '_'):
                pos += 1
            ident = text[start:pos]

            if ident == 'fails':
                if pos >= len(text) or text[pos] != '(':
                    continue
                depth = 1
                start = pos + 1
                while pos < len(text) and depth > 0:
                    if text[pos] == '(':
                        depth += 1
                    elif text[pos] == ')':
                        depth -= 1
                    pos += 1
                task_call = text[start:pos-1]
                try:
                    sub_result = self._execute_task_from_call(task_call, params)
                    if sub_result:
                        results.append(sub_result)
                except Exception:
                    pass
            elif ident in self.tasks:
                if pos < len(text) and text[pos] == '(':
                    depth = 1
                    start = pos + 1
                    while pos < len(text) and depth > 0:
                        if text[pos] == '(':
                            depth += 1
                        elif text[pos] == ')':
                            depth -= 1
                        pos += 1
                    task_call = text[start:pos-1]
                else:
                    task_call = ident

                sub_result = self._execute_task_from_call(task_call, params)
                results.append(sub_result)
                if sub_result and sub_result.exit_code != 0:
                    return results
            else:
                pos += 1

        return results

    def _execute_task_from_call(self, call_str: str, parent_params: Dict[str, Any]) -> Optional[JobResult]:
        """Execute a sub-task based on a call string like 'task_name(1, x=2)' or just 'task_name'."""
        paren_idx = call_str.find('(')

        if paren_idx == -1:
            task_name = call_str.strip()
            call_params = {}
        else:
            task_name = call_str[:paren_idx].strip()
            if not call_str.endswith(')'):
                raise ValueError(f"Unclosed parenthesis in task call: {call_str}")

            args_str = call_str[paren_idx + 1:-1]
            if not args_str.strip():
                call_params = {}
            else:
                call_params = parse_call_args(args_str)

        if task_name not in self.tasks:
            raise ValueError(f"Unknown task: {task_name}")

        task = self.tasks[task_name]
        resolved_params = self._resolve_params_from_call(task, call_params)

        parent_job_id = None
        if hasattr(self, '_current_job_id'):
            parent_job_id = self._current_job_id

        return self.executor.execute_task(
            task, resolved_params, parent_job_id,
            global_cache_enabled=self.config.cache_enabled,
            global_cache_location=self.config.cache_location,
            force_refresh=self.config.force_refresh
        )

    def _execute_task(self, task_name: str, params: Dict[str, Any],
                     parent_job_id: Optional[int] = None) -> JobResult:
        """Execute a task, resolving dependencies first."""
        task = self.tasks[task_name]

        if task.requires:
            self.resolve_dependencies(task, params)

        return self.executor.execute_task(
            task, params, parent_job_id,
            global_cache_enabled=self.config.cache_enabled,
            global_cache_location=self.config.cache_location,
            force_refresh=self.config.force_refresh
        )

    def execute(self, entry_spec: Optional[str] = None) -> int:
        """Execute the pipeline starting from the entry task. Returns exit code: 0 for success, 1 for failure."""
        entry_task_name, entry_params = self.resolve_entry_task(entry_spec)

        result = self._execute_task(entry_task_name, entry_params, None)

        if result.exit_code != 0:
            print(f"Entry task failed with exit code {result.exit_code}")
            return 1

        return 0
