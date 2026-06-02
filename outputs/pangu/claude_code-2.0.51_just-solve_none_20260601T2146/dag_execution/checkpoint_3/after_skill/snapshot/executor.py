"""
Task execution engine.
"""
import subprocess
import time
import json
from pathlib import Path
from typing import Dict, Any, Optional, List
from pipeline_types import Task, JobResult, CacheConfig, Config
from evaluator import ExpressionEvaluator
import cache


class TaskExecutor:
    """Executes pipeline tasks, manages workspace, evaluates success criteria."""

    def __init__(self, workspace: Path, output_dir: Path, env: Optional[Dict[str, str]] = None):
        self.workspace = workspace
        self.output_dir = output_dir
        self.env = env or {}
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.job_results: list[JobResult] = []
        self.job_counter = 0
        self.completed_tasks: dict[str, JobResult] = {}
        self.jobs_file = self.output_dir / "jobs.jsonl"
        # Cleanup fancy quotes
        self._cleanup_chars = {'\u2018': "'", '\u2019': "'"}
        # Cache storage (initialized when needed)
        self._cache_storage: Optional[cache.CacheStorage] = None
        # Track tasks that should bypass cache (force_refresh)
        self._force_refresh_tasks: set = set()

    def _cleanup_output(self, text: str) -> str:
        for fancy, plain in self._cleanup_chars.items():
            text = text.replace(fancy, plain)
        return text

    def _resolve_params(self, task: Task, params: dict[str, Any]) -> dict[str, Any]:
        resolved = {}
        for key, value in params.items():
            if isinstance(value, str) and '${' in value:
                evaluator = ExpressionEvaluator(self.workspace, self.output_dir)
                value = evaluator.evaluate(value, {'params': self._get_param_values(task)})
            resolved[key] = value
        return resolved

    def _get_param_values(self, task: Task) -> dict[str, Any]:
        params = dict(task.resolved_params)
        for param_def in task.params:
            if param_def.name not in params and param_def.has_default:
                params[param_def.name] = param_def.default_value
        return params

    def _run_commands(self, task: Task, params: dict[str, Any]) -> tuple:
        if not task.run:
            return "", "", 0, False

        context = {'params': params, 'workspace': str(self.workspace), 'output': str(self.output_dir)}
        evaluator = ExpressionEvaluator(self.workspace, self.output_dir)
        script = str(evaluator.evaluate(task.run, context)).strip()
        start_time = time.time()
        timeout = task.timeout if task.timeout else 3600

        try:
            result = subprocess.run(
                script, shell=True, cwd=self.workspace,
                capture_output=True, text=True, timeout=timeout, env=self.env
            )
            return (
                self._cleanup_output(result.stdout),
                self._cleanup_output(result.stderr),
                result.returncode,
                False
            )
        except subprocess.TimeoutExpired:
            return "", "Command timed out", 124, True

    def _evaluate_success_criteria(self, task: Task, params: dict[str, Any],
                                    stdout: str, stderr: str,
                                    output_dir: Optional[Path]) -> dict[str, bool]:
        context = {'params': params, 'workspace': str(self.workspace), 'output': str(self.output_dir)}
        evaluator = ExpressionEvaluator(self.workspace, self.output_dir)
        results = {}

        for criterion in task.success:
            try:
                result = evaluator.parse_and_evaluate(criterion.expression, context.copy())
                results[criterion.name] = self._to_bool(result)
            except Exception as e:
                print(f"Warning: Failed to evaluate success criterion '{criterion.name}': {e}")
                results[criterion.name] = False

        if not task.success:
            results['_exit_code'] = True  # Placeholder, will be updated after run
        return results

    def _to_bool(self, value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return value != 0
        if isinstance(value, str):
            return value.lower() not in ('false', '', 'none', 'null')
        return value is not None

    def _init_cache(self, global_enabled: bool, global_location: Optional[str],
                    force_refresh: List[str]) -> None:
        """Initialize cache with global configuration."""
        cache_dir = global_location if global_location else ".pipe-cache"
        self._cache_storage = cache.CacheStorage(cache_dir)
        self._global_cache_enabled = global_enabled
        if force_refresh:
            self._force_refresh_tasks = set(force_refresh)

    def _get_task_inputs(self, task: Task) -> Optional[List[str]]:
        return None

    def _get_cache_storage(self, cache_config: Optional[CacheConfig]) -> cache.CacheStorage:
        """Get appropriate cache storage for a task."""
        if cache_config and cache_config.location and cache_config.location != ".pipe-cache":
            return cache.CacheStorage(cache_config.location)
        return self._cache_storage

    def _check_cache(self, task: Task, params: dict[str, Any]) -> tuple:
        if not self._global_cache_enabled:
            return False, None, None
        if task.name in self._force_refresh_tasks:
            return False, None, None
        if not task.cache or not task.cache.enabled:
            return False, None, None

        task_cache_storage = self._get_cache_storage(task.cache)
        inputs = self._get_task_inputs(task)
        cache_key = cache.generate_cache_key(
            task.name, params, task.cache, self.workspace, inputs
        )
        cached = task_cache_storage.retrieve(cache_key)
        return (True, cached, cache_key) if cached else (False, None, cache_key)

    def _store_result(self, task: Task, params: dict[str, Any],
                      stdout: str, stderr: str, exit_code: int,
                      success: dict[str, bool], task_output_dir: Optional[Path],
                      cache_key: Optional[str], duration: float) -> None:
        if not cache_key or not self._global_cache_enabled:
            return
        if task.name in self._force_refresh_tasks:
            return
        if not task.cache or not task.cache.enabled:
            return

        self._get_cache_storage(task.cache).store(
            cache_key, stdout, stderr, exit_code, success,
            task_output_dir, duration
        )

    def execute_task(self, task: Task, params: dict[str, Any],
                     parent_job_id: Optional[int] = None,
                     global_cache_enabled: bool = False,
                     global_cache_location: Optional[str] = None,
                     force_refresh: Optional[List[str]] = None) -> JobResult:
        # Initialize cache on first call with cache config
        if self._cache_storage is None:
            self._init_cache(global_cache_enabled, global_cache_location, force_refresh or [])

        job_id = self.job_counter + 1
        self.job_counter = job_id

        # Check cache first
        cache_hit, cached_result, cache_key = self._check_cache(task, params)

        if cache_hit:
            stdout, stderr, exit_code, success, cached_output_dir, duration = cached_result

            print(json.dumps({"event": "CACHE_HIT", "job_id": job_id}))

            # Use cached output dir if available
            task_output_dir = None
            if cached_output_dir:
                task_output_dir = Path(cached_output_dir)
            elif task.output:
                resolved_path = ExpressionEvaluator(self.workspace, self.output_dir).evaluate(
                    task.output, {'params': params}
                )
                task_output_dir = Path(resolved_path)
                task_output_dir.mkdir(parents=True, exist_ok=True)

            result = JobResult(
                job_id=job_id, task=task.name, params=params,
                stdout=stdout, stderr=stderr, timed_out=False,
                success=success,
                output=str(task_output_dir) if task_output_dir else None,
                exit_code=exit_code, parent=parent_job_id, duration=duration,
                inputs=self._get_task_inputs(task),
                cache={"enabled": task.cache.enabled if task.cache else False,
                       "strategy": task.cache.strategy if task.cache else "content",
                       "location": task.cache.location if task.cache else ".pipe-cache"},
                cache_hit=True,
                cache_key=cache_key
            )
            self.job_results.append(result)

            print(json.dumps({"event": "TASK_RUNNING", "job_id": job_id}))

            if exit_code == 0 and all(success.values()):
                print(json.dumps({"event": "TASK_COMPLETED", "job_id": job_id}))
                self.completed_tasks[task.name] = result
            else:
                print(json.dumps({"event": "TASK_FAILED", "job_id": job_id}))

            with open(self.jobs_file, 'a') as f:
                f.write(result.to_jsonl() + '\n')

            return result

        # Cache miss - print CACHE_CHECK event
        print(json.dumps({"event": "CACHE_CHECK", "job_id": job_id}))

        # Resolve output directory
        task_output_dir = None
        if task.output:
            resolved_path = ExpressionEvaluator(self.workspace, self.output_dir).evaluate(
                task.output, {'params': params}
            )
            task_output_dir = Path(resolved_path)
            task_output_dir.mkdir(parents=True, exist_ok=True)

        # Run task
        start_time = time.time()
        stdout, stderr, exit_code, timed_out = self._run_commands(task, params)
        if timed_out:
            exit_code = 124
        duration = time.time() - start_time

        # Evaluate success criteria
        success_criteria = {}
        if exit_code == 0 or not task.run:
            success_criteria = self._evaluate_success_criteria(task, params, stdout, stderr, task_output_dir)
        else:
            if task.success:
                for criterion in task.success:
                    success_criteria[criterion.name] = False
            else:
                success_criteria['_exit_code'] = False

        # Store result in cache
        if cache_key:
            self._store_result(task, params, stdout, stderr, exit_code,
                              success_criteria, task_output_dir, cache_key, duration)

        # Build result
        result = JobResult(
            job_id=job_id, task=task.name, params=params,
            stdout=stdout, stderr=stderr, timed_out=timed_out,
            success=success_criteria,
            output=str(task_output_dir) if task_output_dir else None,
            exit_code=exit_code, parent=parent_job_id, duration=duration,
            inputs=self._get_task_inputs(task),
            cache={"enabled": task.cache.enabled if task.cache else False,
                   "strategy": task.cache.strategy if task.cache else "content",
                   "location": task.cache.location if task.cache else ".pipe-cache"},
            cache_hit=False,
            cache_key=cache_key
        )
        self.job_results.append(result)

        print(json.dumps({"event": "TASK_RUNNING", "job_id": job_id}))

        if exit_code == 0 and all(success_criteria.values()):
            print(json.dumps({"event": "TASK_COMPLETED", "job_id": job_id}))
            self.completed_tasks[task.name] = result
        else:
            print(json.dumps({"event": "TASK_FAILED", "job_id": job_id}))

        # Write regardless
        with open(self.jobs_file, 'a') as f:
            f.write(result.to_jsonl() + '\n')

        return result
