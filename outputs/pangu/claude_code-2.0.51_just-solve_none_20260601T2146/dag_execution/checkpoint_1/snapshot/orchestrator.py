"""
Main orchestrator for pipeline execution.
Handles task scheduling, dependency resolution, and execution flow.
"""
import time
from pathlib import Path
from typing import Dict, List, Optional, Set
from pipeline_types import Task, TaskDict, JobResult, Config
from pipeline_parser import parse_pipeline_file, ParseError
from config_parser import parse_config_file, parse_entry_task
from executor import TaskExecutor
from evaluator import ExpressionEvaluator, EvaluationError


class Orchestrator:
    """
    Orchestrates pipeline execution: resolves dependencies and schedules tasks.
    """

    def __init__(self, workspace: Path, output_dir: Path,
                 config: Optional[Config] = None, env: Optional[Dict[str, str]] = None):
        self.workspace = workspace
        self.output_dir = output_dir
        self.config = config or Config()
        self.env = env or {}

        # Update environment with config
        self.env.update(self.config.env)

        self.executor = TaskExecutor(workspace, output_dir, self.env)

        # Task registry - name -> Task object
        self.tasks: Dict[str, Task] = {}

        # Track which tasks have been executed with which params
        self.executed_tasks: Dict[str, List[JobResult]] = {}

        # Track tasks that are currently running
        self.running_tasks: Set[str] = set()

        # Job results indexed by task name
        self.job_results: Dict[str, JobResult] = {}

    def register_tasks(self, parsed_tasks: List[Task]):
        """Register tasks from parsed pipeline."""
        for task in parsed_tasks:
            if task.name in self.tasks:
                raise ValueError(f"Duplicate task definition: {task.name}")
            self.tasks[task.name] = task

    def resolve_entry_task(self, entry_spec: Optional[str] = None) -> tuple:
        """
        Determine which task is the entry point and with what parameters.
        Returns (task_name, params_dict).
        """
        if entry_spec:
            return parse_entry_task(entry_spec)

        # Look for 'main' task as default entry point
        if 'main' in self.tasks:
            return 'main', {}

        # If there's only one task, use that
        if len(self.tasks) == 1:
            task_name = list(self.tasks.keys())[0]
            return task_name, {}

        raise ValueError("No entry task specified and no default 'main' or single task found")

    def _resolve_params_from_call(self, task: Task, call_params: Dict[str, Any]) -> Dict[str, Any]:
        """
        Resolve parameters from a task call, applying defaults and type checking.
        """
        resolved = {}

        # Map call params to parameter definitions
        param_by_position = {}  # index -> param name
        param_by_name = {}  # name -> param def

        for i, param_def in enumerate(task.params):
            if param_def.has_default:
                resolved[param_def.name] = param_def.default_value
            param_by_name[param_def.name] = param_def

        # Apply call params
        for key, value in call_params.items():
            if isinstance(key, int):
                # Positional param
                if key < len(task.params):
                    param_name = task.params[key].name
                    param_def = task.params[key]
                    resolved[param_name] = self._cast_value(value, param_def)
            else:
                # Named param
                if key in param_by_name:
                    param_def = param_by_name[key]
                    resolved[key] = self._cast_value(value, param_def)
                else:
                    raise ValueError(f"Unknown parameter: {key}")

        return resolved

    def _cast_value(self, value: str, param_def) -> Any:
        """Cast a value from the task call to the parameter's expected type."""
        if param_def.param_type.name == 'STRING':
            return str(value)
        elif param_def.param_type.name == 'INT':
            try:
                return int(value)
            except ValueError:
                raise ValueError(f"Invalid integer value: {value}")
        elif param_def.param_type.name == 'FLOAT':
            try:
                return float(value)
            except ValueError:
                raise ValueError(f"Invalid float value: {value}")
        elif param_def.param_type.name == 'BOOL':
            return str(value).upper() in ('TRUE', '1', 'YES', 'T')
        elif param_def.param_type.name == 'LIST':
            # Lists from task calls are tricky - assume string representation
            return [v.strip().strip('"').strip("'") for v in value.split(',')] if value else []

        return value

    def resolve_dependencies(self, task: Task, params: Dict[str, Any]) -> List[JobResult]:
        """
        Resolve task dependencies based on the requires block.
        Returns a list of job results from required tasks.
        """
        if not task.requires:
            return []

        # We need to parse the requires expression and execute sub-tasks
        # For now, use a simple parser for requires
        return self._parse_and_execute_requires(task, params)

    def _parse_and_execute_requires(self, task: Task, params: Dict[str, Any]) -> List[JobResult]:
        """
        Parse and execute requires block expressions.
        Returns a list of job results from required tasks.
        """
        requires_text = task.requires

        if not requires_text:
            return []

        # Simple parser for requires block
        # We'll handle:
        # - task(name, params)
        # - fails(task(name, params))
        # - for loops
        # - if/else conditions

        results = []
        pos = 0
        text = requires_text.strip()

        # Skip opening brace if present
        if text.startswith('{'):
            text = text[1:].strip()

        context = {
            '_completed_tasks': set(self.executor.completed_tasks.keys()),
            '_failed_tasks': [],
        }

        while pos < len(text):
            # Skip whitespace and newlines
            while pos < len(text) and text[pos].isspace():
                pos += 1

            if pos >= len(text):
                break

            # Check for 'fails(' function
            if text.startswith('fails(', pos):
                # Parse inside fails
                paren_idx = text.find('(', pos)
                brace_depth = 1
                paren_idx += 1
                start = paren_idx

                while brace_depth > 0:
                    if text[paren_idx] == '(':
                        brace_depth += 1
                    elif text[paren_idx] == ')':
                        brace_depth -= 1
                    paren_idx += 1

                task_call = text[start:paren_idx-1].strip()

                # Execute the task even if it fails
                try:
                    sub_result = self._execute_task_from_call(task_call, params)
                    # Don't stop the pipeline even if this task fails
                    results.append(sub_result)
                except Exception as e:
                    # Task execution error
                    pass

                pos = paren_idx
                continue

            # Check for task call
            identifier = ''
            idx = pos
            while idx < len(text) and (text[idx].isalnum() or text[idx] == '_'):
                idx += 1

            identifier = text[pos:idx]

            if identifier and identifier in self.tasks:
                # This is a task call
                self._pos_in_requires = idx
                task_call, end_pos = self._extract_task_call(text, idx)

                sub_result = self._execute_task_from_call(task_call, params)
                results.append(sub_result)

                # Update context
                context['_completed_tasks'] = set(self.executor.completed_tasks.keys())

                if not sub_result or sub_result.exit_code != 0:
                    # Task failed - we should stop executing
                    return results

                pos = end_pos
                continue

            # Check for 'for' loop
            if identifier == 'for':
                # Parse for loop
                self._pos_in_requires = idx
                loop_info = self._parse_for_loop(text, idx)

                if loop_info:
                    var_name, start_val, cond, update, loop_body, end_pos = loop_info

                    # Initialize loop variable
                    context[var_name] = start_val

                    while evaluator.evaluate(cond, context.copy()):
                        # Check if it's a task call in the loop body
                        if loop_body.strip().startswith(('task_', 'fail', 'fails')):
                            sub_result = self._execute_task_from_call(loop_body, params)
                            if sub_result and sub_result.exit_code != 0:
                                # Task failed
                                pass
                        else:
                            # Not a task call - could be condition or other expression
                            pass

                        # Update loop variable
                        if update.isnumeric():
                            context[var_name] += 1
                        else:
                            # Try to evaluate the update expression
                            try:
                                context[var_name] = evaluator.evaluate(update, context.copy())
                            except:
                                pass

                    pos = end_pos
                    continue

            # Check for 'while' loop
            if identifier == 'while':
                self._pos_in_requires = idx
                loop_info = self._parse_while_loop(text, idx)
                pos = loop_info.get('end_pos', idx)
                continue

            # Check for 'if' condition
            if identifier == 'if':
                self._pos_in_requires = idx
                if_info = self._parse_if_block(text, idx)
                pos = if_info.get('end_pos', idx)
                continue

            # Skip closing brace if at top level
            if text[pos] == '}' and pos > 0:
                # Check if this is the final closing brace
                break

            pos += 1

        return results

    def _extract_task_call(self, text: str, start: int) -> tuple:
        """Extract a task call from the requires block."""
        # Find the task name
        idx = start
        while idx < len(text) and (text[idx].isalnum() or text[idx] == '_'):
            idx += 1

        task_name = text[start:idx]

        # Skip whitespace
        while idx < len(text) and text[idx].isspace():
            idx += 1

        # Check if it's a function call (fails(task(...)))
        if text[idx:idx+5] == 'fails':
            # Find the closing paren
            depth = 0
            open_paren = text.find('(', idx)
            start_paren = open_paren
            depth = 1
            paren_idx = open_paren + 1

            while depth > 0 and paren_idx < len(text):
                if text[paren_idx] == '(':
                    depth += 1
                elif text[paren_idx] == ')':
                    depth -= 1
                paren_idx += 1

            task_call = text[idx:paren_idx]
            end_pos = paren_idx
            return task_call, end_pos

        # Extract parameters if present
        if idx < len(text) and text[idx] == '(':
            # Parse parameters
            depth = 1
            paren_idx = idx + 1

            while depth > 0 and paren_idx < len(text):
                if text[paren_idx] == '(':
                    depth += 1
                elif text[paren_idx] == ')':
                    depth -= 1
                paren_idx += 1

            task_call = text[idx:paren_idx]
            end_pos = paren_idx
            return task_call, end_pos

        # Just the task name
        task_call = task_name
        end_pos = idx
        return task_call, end_pos

    def _execute_task_from_call(self, call_str: str, parent_params: Dict[str, Any]) -> Optional[JobResult]:
        """Execute a sub-task based on a call string like 'task_name(1, x=2)' or just 'task_name'."""
        # Parse the call string
        paren_idx = call_str.find('(')

        if paren_idx == -1:
            # No parentheses - just a task name
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
                call_params = self._parse_call_args(args_str)

        if task_name not in self.tasks:
            raise ValueError(f"Unknown task: {task_name}")

        task = self.tasks[task_name]

        # Resolve parameters
        resolved_params = self._resolve_params_from_call(task, call_params)

        # Find parent job ID (if there's a currently running task)
        parent_job_id = None
        if hasattr(self, '_current_job_id'):
            parent_job_id = self._current_job_id

        # Execute the task
        return self.executor.execute_task(task, resolved_params, parent_job_id)

    def _parse_call_args(self, args_str: str) -> Dict[str, Any]:
        """Parse arguments from a task call string."""
        args = {}

        current = ""
        in_quotes = False
        quote_char = None
        depth = 0

        for ch in args_str:
            if ch in ('"', "'") and (not in_quotes or quote_char == ch):
                in_quotes = not in_quotes
                if in_quotes:
                    quote_char = ch
                else:
                    quote_char = None
                current += ch
            elif not in_quotes:
                if ch == '(' or ch == '[':
                    depth += 1
                    current += ch
                elif ch == ')' or ch == ']':
                    depth -= 1
                    current += ch
                elif ch == ',' and depth == 0:
                    if current.strip():
                        self._add_arg(current.strip(), args)
                    current = ""
                else:
                    current += ch
            else:
                current += ch

        if current.strip():
            self._add_arg(current.strip(), args)

        return args

    def _add_arg(self, arg: str, args: Dict[str, Any]):
        """Add a parsed argument to the args dict."""
        arg = arg.strip()

        if '=' in arg:
            key, val = arg.split('=', 1)
            key = key.strip()
            val = val.strip()

            # Remove quotes if present
            if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
                val = val[1:-1]

            # Type inference
            if val.isdigit():
                val = int(val)
            elif val.replace('.', '').isdigit():
                val = float(val)
            elif val.upper() in ('TRUE', 'FALSE'):
                val = val.upper() == 'TRUE'

            args[key] = val
        else:
            # Positional argument
            val = arg
            if val.isdigit():
                val = int(val)
            elif val.replace('.', '').isdigit():
                val = float(val)
            elif val.upper() in ('TRUE', 'FALSE'):
                val = val.upper() == 'TRUE'

            args[len(args)] = val

    def _parse_for_loop(self, text: str, start: int) -> Optional[dict]:
        """Parse a for loop statement."""
        # Format: for (type var = start; cond; update) { body }
        idx = start + 3  # Skip 'for'

        # Skip whitespace
        while idx < len(text) and text[idx].isspace():
            idx += 1

        if idx >= len(text) or text[idx] != '(':
            return None

        idx += 1  # Skip '('

        # Find the closing paren
        paren_depth = 1
        loop_start = idx

        while paren_depth > 0 and idx < len(text):
            if text[idx] == '(':
                paren_depth += 1
            elif text[idx] == ')':
                paren_depth -= 1
            idx += 1

        loop_header = text[loop_start:idx-1]
        parts = [p.strip() for p in loop_header.split(';')]

        if len(parts) != 3:
            return None

        # Parse init: type var = start
        init = parts[0].strip()
        init_parts = init.split('=')
        var_name = init_parts[0].split()[-1]  # Last word is the variable name
        start_val = init_parts[1].strip() if len(init_parts) > 1 else '0'

        # Handle start value
        try:
            start_val = int(start_val) if start_val.isdigit() else start_val
        except:
            pass

        # Parse condition
        cond = parts[1].strip()

        # Parse update
        update = parts[2].strip()

        # Find the body (inside braces)
        while idx < len(text) and text[idx].isspace():
            idx += 1

        if idx < len(text) and text[idx] == '{':
            body_idx = idx
            brace_depth = 1
            idx += 1

            while brace_depth > 0 and idx < len(text):
                if text[idx] == '{':
                    brace_depth += 1
                elif text[idx] == '}':
                    brace_depth -= 1
                idx += 1

            body = text[body_idx+1:idx-1].strip()
        else:
            body = ""

        return {
            'var_name': var_name,
            'start_val': start_val,
            'cond': cond,
            'update': update,
            'body': body,
            'end_pos': idx
        }

    def _parse_while_loop(self, text: str, start: int) -> dict:
        """Parse a while loop."""
        idx = start + 5  # Skip 'while'

        # Skip whitespace
        while idx < len(text) and text[idx].isspace():
            idx += 1

        # Find condition
        if idx >= len(text) or text[idx] != '(':
            return {'end_pos': idx}

        idx += 1  # Skip '('

        paren_depth = 1
        cond_start = idx

        while paren_depth > 0 and idx < len(text):
            if text[idx] == '(':
                paren_depth += 1
            elif text[idx] == ')':
                paren_depth -= 1
            idx += 1

        # Find body
        while idx < len(text) and text[idx].isspace():
            idx += 1

        if idx < len(text) and text[idx] == '{':
            body_idx = idx
            brace_depth = 1
            idx += 1

            while brace_depth > 0 and idx < len(text):
                if text[idx] == '{':
                    brace_depth += 1
                elif text[idx] == '}':
                    brace_depth -= 1
                idx += 1

        return {'end_pos': idx}

    def _parse_if_block(self, text: str, start: int) -> dict:
        """Parse an if/else block."""
        idx = start + 2  # Skip 'if'

        # Skip whitespace
        while idx < len(text) and text[idx].isspace():
            idx += 1

        # Find condition
        if idx >= len(text) or text[idx] != '(':
            return {'end_pos': idx}

        idx += 1  # Skip '('

        paren_depth = 1
        cond_start = idx

        while paren_depth > 0 and idx < len(text):
            if text[idx] == '(':
                paren_depth += 1
            elif text[idx] == ')':
                paren_depth -= 1
            idx += 1

        # Find body (then block)
        while idx < len(text) and text[idx].isspace():
            idx += 1

        if idx < len(text) and text[idx] == '{':
            body_idx = idx
            brace_depth = 1
            idx += 1

            while brace_depth > 0 and idx < len(text):
                if text[idx] == '{':
                    brace_depth += 1
                elif text[idx] == '}':
                    brace_depth -= 1
                idx += 1

        # Check for elif/else blocks
        remaining = text[idx:]

        # Parse elif blocks
        while remaining.startswith('elif'):
            idx += 4
            # Parse condition and body similarly to if
            self._skip_to_brace_close(remaining)

        # Parse else block
        if remaining.startswith('else'):
            idx += 4
            self._skip_to_brace_close(remaining)

        return {'end_pos': idx}

    def _skip_to_brace_close(self, text: str) -> int:
        """Skip to the closing brace."""
        brace_depth = 0
        pos = 0

        while pos < len(text):
            if text[pos] == '{':
                brace_depth += 1
            elif text[pos] == '}':
                brace_depth -= 1
                if brace_depth < 0:
                    return pos
            pos += 1

        return pos

    def execute(self, entry_spec: Optional[str] = None) -> int:
        """
        Execute the pipeline starting from the entry task.
        Returns exit code: 0 for success, 1 for entry task failure.
        """
        # Determine entry task
        entry_task_name, entry_params = self.resolve_entry_task(entry_spec)

        # Execute entry task
        result = self._execute_task(entry_task_name, entry_params, None)

        if result.exit_code != 0:
            # Entry task failed
            print(f"Entry task failed with exit code {result.exit_code}")
            return 1

        return 0

    def _execute_task(self, task_name: str, params: Dict[str, Any],
                     parent_job_id: Optional[int] = None) -> JobResult:
        """
        Execute a task, resolving dependencies first.
        """
        if task_name not in self.tasks:
            raise ValueError(f"Unknown task: {task_name}")

        task = self.tasks[task_name]

        # Set current job ID for dependency resolution
        self._current_job_id = parent_job_id

        # Resolve dependencies
        if task.requires:
            # Evaluate requires block
            self.resolve_dependencies(task, params)

        # Execute the task itself
        result = self.executor.execute_task(task, params, parent_job_id)

        return result
