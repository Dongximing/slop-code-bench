"""Pipeline executor - runs tasks and manages dependencies."""

import copy
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, asdict, field
from typing import Any, Dict, List, Optional, Tuple, Set

try:
    import tomli
except ImportError:
    import toml as tomli

from parser import parse_pipeline
from ast_nodes import ASTNode, TaskCall, CachedTaskCall, FailsTask, ExpressionStatement, TemplateString, Pipeline, CacheConfig
from evaluator import ExprEvaluator, EvaluationError, ReturnValue, BreakSignal, ContinueSignal
from cache import CacheManager, CachedResult


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


class CacheError(PipelineError):
    def __init__(self, message: str):
        super().__init__(4, f"CACHE_ERROR:{message}")


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
    inputs: Optional[List[str]] = None
    cache: Optional[Dict[str, Any]] = None
    cache_hit: Optional[bool] = None
    cache_key: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        result = asdict(self)
        if self.cache is None:
            result.pop('cache_hit', None)
            result.pop('cache_key', None)
        return result


@dataclass
class PendingTaskCall:
    """Represents a pending task call."""
    name: str
    args: List[Any]
    kwargs: Dict[str, Any]
    parent: Optional[int]
    expect_fail: bool = False
    cache_overrides: Optional[Dict[str, Any]] = None


@dataclass
class GlobalCacheConfig:
    """Global cache configuration from config file."""
    enabled: bool = True
    location: str = ".pipe-cache"
    force_refresh: List[str] = field(default_factory=list)


class PipelineExecutor:
    """Executes pipeline tasks with caching support."""

    BUILTIN_FUNCTIONS = frozenset(['len', 'contains', 'equals', 'exists', 'fail'])

    @staticmethod
    def _merge_cache_config(base: CacheConfig, overrides: Dict[str, Any]) -> CacheConfig:
        """Merge cache overrides into a base CacheConfig, returning a new CacheConfig."""
        merged = copy.deepcopy(base)

        # Handle dict-style overrides that replace entire sub-sections
        if 'ttl' in overrides and isinstance(overrides['ttl'], dict):
            merged.ttl_seconds = merged.ttl_minutes = merged.ttl_hours = merged.ttl_days = None
            for k, v in overrides['ttl'].items():
                setattr(merged, f'ttl_{k}', v)
        if 'key' in overrides and isinstance(overrides['key'], dict):
            merged.key_include = merged.key_exclude = None
            for k, v in overrides['key'].items():
                setattr(merged, f'key_{k}', v)

        # Handle specific/dotted overrides
        for key, value in overrides.items():
            if key in ('enabled', 'strategy', 'location'):
                setattr(merged, key, value)
            elif key == 'version':
                merged.version = str(value)
            elif '.' in key:
                setattr(merged, key.replace('.', '_'), value)

        return merged

    @staticmethod
    def _validate_cache_config(config: CacheConfig) -> Optional[str]:
        if config.strategy not in ('content', 'stale', 'always'):
            return f"Invalid cache strategy: {config.strategy}"
        if not isinstance(config.enabled, bool):
            return f"Cache enabled must be a boolean, got {type(config.enabled).__name__}"
        for field_name in ('ttl_seconds', 'ttl_minutes', 'ttl_hours', 'ttl_days'):
            val = getattr(config, field_name)
            if val is not None and not isinstance(val, (int, float)):
                return f"Cache {field_name} must be a number, got {type(val).__name__}"
        if config.version is not None and not isinstance(config.version, str):
            return f"Cache version must be a string, got {type(config.version).__name__}"
        for field_name in ('key_include', 'key_exclude'):
            val = getattr(config, field_name)
            if val is not None and not isinstance(val, list):
                return f"Cache {field_name} must be a list, got {type(val).__name__}"
        return None

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
        self.global_cache_config = GlobalCacheConfig()

        self.job_counter = 0
        self.results: List[TaskResult] = []
        self.task_stack: Set[str] = set()
        self.run_cache: Dict[str, Tuple[int, TaskResult]] = {}

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

        self.env_vars = {k: str(v) for k, v in self.config.get('env', {}).items()}
        self.entry_task = self.config.get('entry', 'main')
        self.clean_cwd = self.config.get('clean_cwd', False)

        if 'cache' in self.config:
            cache_cfg = self.config['cache']
            self.global_cache_config.enabled = cache_cfg.get('enabled', True)
            if 'location' in cache_cfg:
                self.global_cache_config.location = cache_cfg['location']
            if 'force_refresh' in cache_cfg:
                self.global_cache_config.force_refresh = cache_cfg['force_refresh']

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

    def resolve_params(self, task, call: PendingTaskCall) -> Dict[str, Any]:
        """Resolve parameters for a task call."""
        params = {}
        param_defs = {p.name: p for p in task.params}

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

        for name, value in call.kwargs.items():
            if name not in param_defs:
                raise InvalidPipeError(f"Unknown parameter '{name}' for task '{task.name}'")
            params[name] = self._cast_value(value, param_defs[name].param_type, param_defs[name].is_list_element_type)

        return params

    def _cast_value(self, value: Any, type_name: str, is_list: bool) -> Any:
        if is_list:
            if not isinstance(value, list):
                raise InvalidPipeError(f"Expected list, got {type(value).__name__}")
            return [self._cast_single(v, type_name) for v in value]
        return self._cast_single(value, type_name)

    def _cast_single(self, value: Any, type_name: str) -> Any:
        if isinstance(value, bool) and type_name == 'bool':
            return value
        if isinstance(value, int) and type_name in ('int', 'float'):
            return float(value) if type_name == 'float' else value
        if isinstance(value, float) and type_name in ('int', 'float'):
            return int(value) if type_name == 'int' else value
        if isinstance(value, str):
            if type_name == 'string':
                return value
            try:
                if type_name == 'int':
                    return int(value)
                if type_name == 'float':
                    return float(value)
            except ValueError:
                raise InvalidPipeError(f"Cannot convert '{value}' to {type_name}")
            if type_name == 'bool':
                if value.lower() in ('true', '1', 'yes'):
                    return True
                if value.lower() in ('false', '0', 'no', ''):
                    return False
                raise InvalidPipeError(f"Cannot convert '{value}' to bool")
        return value

    def _evaluate_default(self, default: ASTNode, type_name: str, is_list: bool) -> Any:
        evaluator = ExprEvaluator(env_vars=self.env_vars, workspace=self.workspace)
        value = evaluator.evaluate_expr(default)

        if isinstance(default, TemplateString):
            for part in default.parts:
                if isinstance(part, str) and part.startswith('${') and part.endswith('}'):
                    var_name = part[2:-1]
                    if var_name in self.env_vars:
                        value = self._cast_single(self.env_vars[var_name], type_name)

        return self._cast_value(value, type_name, is_list)

    def resolve_run_command(self, run: str, params: Dict[str, Any], output_dir: Optional[str]) -> str:
        result = run
        for name, value in params.items():
            value_str = ' '.join(str(v) for v in value) if isinstance(value, list) else str(value)
            result = result.replace(f'${{params.{name}}}', value_str)
        result = result.replace('${workspace}', self.workspace)
        if output_dir:
            result = result.replace('${output}', output_dir)
        return result

    def resolve_output_path(self, output_expr: Optional[ASTNode], params: Dict[str, Any]) -> Optional[str]:
        if output_expr is None:
            return None

        evaluator = ExprEvaluator(
            variables={'params': params},
            env_vars=self.env_vars,
            workspace=self.workspace
        )
        result = evaluator.evaluate_expr(output_expr)

        if isinstance(result, str):
            result = result.replace('${workspace}', self.workspace)
            if not os.path.isabs(result):
                full_path = os.path.join(self.workspace, result)
            else:
                full_path = result
            if not os.path.abspath(full_path).startswith(self.workspace):
                raise InvalidPipeError(f"Output path escapes workspace: {result}")
            return result
        return None

    def execute_run(self, command: str, cwd: str, timeout: Optional[float]) -> Tuple[str, str, int, bool]:
        command = command.replace('\u2018', "'").replace('\u2019', "'")
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
        if task_name in self.task_stack:
            raise InvalidPipeError(f"Circular dependency detected: {task_name}")
        self.task_stack.add(task_name)

    def resolve_inputs(self, inputs: Optional[List[str]], params: Dict[str, Any]) -> List[str]:
        if not inputs:
            return []

        resolved = []
        for pattern in inputs:
            for name, value in params.items():
                pattern = pattern.replace(f'${{params.{name}}}', str(value))
            pattern = pattern.replace('${workspace}', self.workspace)

            if '*' in pattern or '?' in pattern:
                resolved.extend(sorted(glob.glob(pattern, recursive=True)))
            else:
                resolved.append(pattern)
        return resolved

    def _is_effectively_cacheable(self, task_name: str, effective_config: Optional[CacheConfig]) -> bool:
        if not effective_config or not effective_config.enabled:
            return False
        if not self.global_cache_config.enabled:
            return False
        if task_name in self.global_cache_config.force_refresh:
            return False
        return True

    def _get_cache_manager(self, cache_config: CacheConfig) -> CacheManager:
        location = cache_config.location
        if not os.path.isabs(location):
            location = os.path.join(self.workspace, location)
        return CacheManager(location)

    def execute_task(self, call: PendingTaskCall) -> TaskResult:
        self.job_counter += 1
        job_id = self.job_counter

        if call.name not in self.pipeline.tasks:
            raise InvalidPipeError(f"Undefined task: {call.name}")

        task = self.pipeline.tasks[call.name]
        self.check_circular_dependency(call.name)
        self.emit_event("TASK_STARTED", job_id, task=call.name, params=call.kwargs if call.kwargs else call.args)

        params = self.resolve_params(task, call)
        output_dir = self._prepare_output_dir(task.output, params)
        work_dir = output_dir if output_dir else self.workspace
        resolved_inputs = self.resolve_inputs(task.inputs, params)

        cache_hit = False
        cache_key = None
        cache_config_dict = None
        cache_manager = None
        effective_cache_config = task.cache

        if call.cache_overrides:
            if not task.cache:
                raise CacheError(f"Task '{call.name}' does not have caching configured")
            effective_cache_config = self._merge_cache_config(task.cache, call.cache_overrides)
            validation_error = self._validate_cache_config(effective_cache_config)
            if validation_error:
                raise CacheError(validation_error)

        if self._is_effectively_cacheable(call.name, effective_cache_config):
            cache_manager = self._get_cache_manager(effective_cache_config)
            cache_key = cache_manager.compute_key(call.name, params, resolved_inputs, effective_cache_config)
            cache_config_dict = {
                'enabled': effective_cache_config.enabled,
                'strategy': effective_cache_config.strategy,
                'location': effective_cache_config.location,
                **({'version': effective_cache_config.version} if effective_cache_config.version else {}),
            }
            self.emit_event("CACHE_CHECK", job_id, cache_key=cache_key, task=call.name)

            result = self._try_cache(job_id, call, params, output_dir, resolved_inputs,
                                     cache_config_dict, cache_key, cache_manager, effective_cache_config)
            if result:
                return result

        stdout, stderr, exit_code, timed_out, duration, success = self._execute_task_body(
            task, params, output_dir, work_dir, job_id, resolved_inputs
        )

        task_success = exit_code == 0 and all(success.values()) if success else exit_code == 0
        self.emit_event("TASK_COMPLETED" if task_success else "TASK_FAILED", job_id)
        self.task_stack.discard(call.name)

        result = TaskResult(
            job_id=job_id, task_name=call.name, params=params, stdout=stdout, stderr=stderr,
            timed_out=timed_out, success=success, output=output_dir, exit_code=exit_code,
            parent=call.parent, duration=round(duration, 4), inputs=resolved_inputs or None,
            cache=cache_config_dict, cache_hit=cache_hit, cache_key=cache_key
        )

        if cache_manager and cache_key and task_success:
            cache_manager.save(cache_key, stdout, stderr, exit_code, success, output_dir)
            self.run_cache[cache_key] = (job_id, result)

        self.results.append(result)
        self.write_job_result(result)
        return result

    def _prepare_output_dir(self, output_expr: Optional[ASTNode], params: Dict[str, Any]) -> Optional[str]:
        output_dir = self.resolve_output_path(output_expr, params)
        if output_dir:
            if not os.path.isabs(output_dir):
                output_dir = os.path.join(self.workspace, output_dir)
            os.makedirs(output_dir, exist_ok=True)
        return output_dir

    def _try_cache(self, job_id: int, call: PendingTaskCall, params: Dict[str, Any],
                   output_dir: Optional[str], resolved_inputs: List[str],
                   cache_config_dict: Dict[str, Any], cache_key: str,
                   cache_manager: CacheManager, cache_config: CacheConfig) -> Optional[TaskResult]:
        if cache_key in self.run_cache:
            cached_job_id, cached_result = self.run_cache[cache_key]
            self.emit_event("CACHE_HIT", job_id, cache_key=cache_key, task=call.name, cached_job_id=cached_job_id)
            result = self._make_cached_result(job_id, call, params, output_dir, resolved_inputs,
                                              cache_config_dict, cache_key, cached_result.stdout,
                                              cached_result.stderr, cached_result.exit_code, cached_result.success)
            self._finalize_task(result, call.name)
            return result

        cached = cache_manager.check(cache_key, cache_config)
        if cached:
            self.emit_event("CACHE_HIT", job_id, cache_key=cache_key, task=call.name)
            cache_manager.restore_files(cache_key, output_dir, cached.output_files)
            result = self._make_cached_result(job_id, call, params, output_dir, resolved_inputs,
                                              cache_config_dict, cache_key, cached.stdout,
                                              cached.stderr, cached.exit_code, cached.success)
            self.run_cache[cache_key] = (job_id, result)
            self._finalize_task(result, call.name)
            return result

        self.emit_event("CACHE_MISS", job_id, cache_key=cache_key, task=call.name)
        return None

    def _make_cached_result(self, job_id: int, call: PendingTaskCall, params: Dict[str, Any],
                            output_dir: Optional[str], resolved_inputs: List[str],
                            cache_config_dict: Dict[str, Any], cache_key: str,
                            stdout: str, stderr: str, exit_code: int, success: Dict[str, bool]) -> TaskResult:
        return TaskResult(
            job_id=job_id, task_name=call.name, params=params, stdout=stdout,
            stderr=stderr, timed_out=False, success=success,
            output=output_dir, exit_code=exit_code, parent=call.parent,
            duration=0.0, inputs=resolved_inputs or None, cache=cache_config_dict,
            cache_hit=True, cache_key=cache_key
        )

    def _finalize_task(self, result: TaskResult, task_name: str):
        self.emit_event("TASK_COMPLETED", result.job_id)
        self.task_stack.discard(task_name)
        self.results.append(result)
        self.write_job_result(result)

    def _execute_task_body(self, task, params: Dict[str, Any], output_dir: Optional[str],
                           work_dir: str, job_id: int, resolved_inputs: List[str]
                           ) -> Tuple[str, str, int, bool, float, Dict[str, bool]]:
        if task.requires:
            if not self._execute_requires(task.requires, params, job_id, output_dir):
                return "", "", 1, False, 0.0, {}

        stdout, stderr, exit_code, timed_out, duration = "", "", 0, False, 0.0

        if task.run:
            self.emit_event("TASK_RUNNING", job_id)
            start_time = time.time()
            command = self.resolve_run_command(task.run, params, output_dir)
            stdout, stderr, exit_code, timed_out = self.execute_run(command, work_dir, task.timeout)
            duration = time.time() - start_time
            stdout = stdout.replace('\u2018', "'").replace('\u2019', "'")
            stderr = stderr.replace('\u2018', "'").replace('\u2019', "'")

        success = self._evaluate_success(task.success, params, stdout, stderr, exit_code, output_dir)
        return stdout, stderr, exit_code, timed_out, duration, success

    def _evaluate_success(self, success_block: Optional[Dict[str, List[ASTNode]]],
                          params: Dict[str, Any], stdout: str, stderr: str,
                          exit_code: int, output_dir: Optional[str]) -> Dict[str, bool]:
        if not success_block:
            return {}

        evaluator = ExprEvaluator(
            variables={'params': params}, env_vars=self.env_vars, stdout=stdout,
            stderr=stderr, exit_code=exit_code, output_dir=output_dir, workspace=self.workspace
        )
        try:
            return evaluator.evaluate_success(success_block)
        except Exception:
            return {name: False for name in success_block}

    def _execute_requires(self, statements: List[ASTNode], params: Dict[str, Any],
                          parent_id: int, output_dir: Optional[str]) -> bool:
        evaluator = RequiresEvaluator(self, params, parent_id, output_dir)
        try:
            evaluator.evaluate_statements(statements)
            return True
        except PipelineError:
            raise
        except (ReturnValue, BreakSignal, ContinueSignal):
            return False
        except Exception:
            return False

    def run(self) -> int:
        try:
            self.load_pipeline()
            self.load_config()

            os.makedirs(self.workspace, exist_ok=True)
            os.makedirs(self.output_dir, exist_ok=True)

            if self.clean_cwd:
                for item in os.listdir(self.workspace):
                    path = os.path.join(self.workspace, item)
                    if os.path.isfile(path):
                        os.remove(path)
                    elif os.path.isdir(path):
                        shutil.rmtree(path)

            entry = self.entry_task or 'main'
            entry_call = self._parse_entry_call(entry, parent=None)
            result = self.execute_task(entry_call)

            if self.jobs_file:
                self.jobs_file.close()

            return 0 if result.exit_code == 0 else 1

        except PipelineError as e:
            print(e.message, file=sys.stderr)
            return e.code
        except Exception as e:
            print(f"SYNTAX_ERROR:{str(e)}", file=sys.stderr)
            return 2

    def _parse_entry_call(self, entry: str, parent: Optional[int]) -> PendingTaskCall:
        entry = entry.strip()
        if '(' not in entry:
            return PendingTaskCall(name=entry, args=[], kwargs={}, parent=parent)

        match = re.match(r'(\w+)\((.*)\)', entry)
        if not match:
            raise InvalidPipeError(f"Invalid entry task specification: {entry}")

        name = match.group(1)
        args_str = match.group(2).strip()

        if not args_str:
            return PendingTaskCall(name=name, args=[], kwargs={}, parent=parent)

        args, kwargs = [], {}
        for part in self._split_args(args_str):
            part = part.strip()
            if '=' in part:
                key, value = part.split('=', 1)
                kwargs[key.strip()] = self._parse_value(value.strip())
            else:
                args.append(self._parse_value(part))

        return PendingTaskCall(name=name, args=args, kwargs=kwargs, parent=parent)

    def _split_args(self, args_str: str) -> List[str]:
        result, current, depth, in_string, string_char = [], [], 0, False, None

        for char in args_str:
            if char in ('"', "'") and not in_string:
                in_string, string_char = True, char
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
        value = value.strip()
        if value == 'TRUE':
            return True
        if value == 'FALSE':
            return False
        if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
            return value[1:-1]
        try:
            return int(value)
        except ValueError:
            pass
        try:
            return float(value)
        except ValueError:
            pass
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
        if isinstance(stmt, FailsTask):
            return self._run_task(stmt.task_call, expect_fail=True)
        if isinstance(stmt, CachedTaskCall):
            return self._run_cached_task(stmt)
        if isinstance(stmt, ExpressionStatement):
            stmt = stmt.expression
        if isinstance(stmt, TaskCall):
            return self._run_task(stmt, expect_fail=False)
        return super().evaluate_statement(stmt)

    def evaluate_expr(self, expr: ASTNode) -> Any:
        if isinstance(expr, TaskCall):
            return self._run_task(expr, expect_fail=False)
        if isinstance(expr, FailsTask):
            return self._run_task(expr.task_call, expect_fail=True)
        if isinstance(expr, CachedTaskCall):
            return self._run_cached_task(expr)
        return super().evaluate_expr(expr)

    def _run_task(self, call: TaskCall, expect_fail: bool, cache_overrides: Optional[Dict[str, Any]] = None) -> bool:
        args = [self.evaluate_expr(a) if isinstance(a, ASTNode) else a for a in call.args]
        kwargs = {k: self.evaluate_expr(v) if isinstance(v, ASTNode) else v for k, v in call.kwargs.items()}

        pending = PendingTaskCall(name=call.name, args=args, kwargs=kwargs, parent=self.parent_id,
                                  expect_fail=expect_fail, cache_overrides=cache_overrides)
        result = self.executor.execute_task(pending)

        if expect_fail:
            if result.exit_code == 0:
                raise EvaluationError(f"Task {call.name} was expected to fail but succeeded")
            return True
        if result.exit_code != 0:
            raise EvaluationError(f"Task {call.name} failed")
        return True

    def _run_cached_task(self, cached_call: CachedTaskCall) -> bool:
        return self._run_task(cached_call.task_call, expect_fail=False,
                              cache_overrides=cached_call.cache_overrides)
