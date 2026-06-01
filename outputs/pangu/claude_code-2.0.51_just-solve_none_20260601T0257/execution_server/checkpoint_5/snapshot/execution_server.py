#!/usr/bin/env python3
"""Local runner web API for executing commands in throwaway working directories."""

import argparse
import csv
import gzip
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from http.server import HTTPServer, BaseHTTPRequestHandler
from glob import glob
from io import StringIO
from pathlib import Path
from statistics import mean, median, stdev
from typing import Any, Union

try:
    import bz2
except ImportError:
    bz2 = None

try:
    import yaml
except ImportError:
    yaml = None

# Type for structured data values in files
StructuredData = Union[dict, list, str, int, float, bool, None]


stats_lock = threading.Lock()
run_durations: list[float] = []
total_runs = 0

command_total_count = 0  # Total commands specified across all requests
command_ran_count = 0    # Total commands that actually executed
command_durations: list[float] = []


@dataclass
class CommandResult:
    cmd: str
    stdout: str
    stderr: str
    exit_code: int
    duration: float
    timed_out: bool
    required: bool = False


@dataclass
class ExecutionResult:
    id: str
    exit_code: int
    duration: float
    timed_out: bool
    commands: list[CommandResult] | None = None
    stdout: str | None = None
    stderr: str | None = None
    files: dict[str, str] | None = None


# Cache for execution results
cache_lock = threading.Lock()
execution_cache: dict[str, ExecutionResult] = {}
cache_hits = 0
cache_misses = 0


class ExecutionHandler(BaseHTTPRequestHandler):

    def _send_json_response(self, status_code: int, data: dict[str, Any]) -> None:
        body = json.dumps(data).encode('utf-8')
        self.send_response(status_code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', len(body))
        self.end_headers()
        self.wfile.write(body)

    def _send_error_response(self, status_code: int, message: str, code: str) -> None:
        self._send_json_response(status_code, {
            'error': message,
            'code': code
        })

    def _parse_json_body(self) -> dict[str, Any]:
        content_length = int(self.headers.get('Content-Length', 0))
        if content_length == 0:
            return {}

        body = self.rfile.read(content_length)
        try:
            return json.loads(body.decode('utf-8'))
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON: {e}")

    def _generate_cache_key(self, validated: dict[str, Any]) -> str:
        """Generate a cache key based on all relevant execution parameters."""
        # Create a serialized representation of all cache-relevant data
        cache_data = {
            'commands': validated['commands'],
            'env': validated['env'],
            'files': validated['files'],
            'stdin': validated['stdin'],
            'timeout': validated['timeout'],
            'track': validated['track'],
            'continue_on_error': validated['continue_on_error']
        }
        # Use JSON serialization for consistent hashing
        serialized = json.dumps(cache_data, sort_keys=True, ensure_ascii=False)
        import hashlib
        return hashlib.sha256(serialized.encode('utf-8')).hexdigest()

    def _validate_execute_request(self, data: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(data, dict):
            raise ValueError("Request body must be a JSON object")

        if 'command' not in data:
            raise ValueError("Missing required field: command")

        command = data['command']

        # Handle command as string (backward compatible)
        if isinstance(command, str):
            if not command.strip():
                raise ValueError("command must be a non-empty string")
            commands = [{'cmd': command.strip()}]
        # Handle command as array of command objects
        elif isinstance(command, list):
            if len(command) == 0:
                raise ValueError("command array must not be empty")
            commands = []
            for i, cmd_obj in enumerate(command):
                if not isinstance(cmd_obj, dict):
                    raise ValueError(f"command[{i}] must be an object")
                if 'cmd' not in cmd_obj:
                    raise ValueError(f"command[{i}] missing required field 'cmd'")
                if not isinstance(cmd_obj['cmd'], str) or not cmd_obj['cmd'].strip():
                    raise ValueError(f"command[{i}].cmd must be a non-empty string")

                cmd_entry = {'cmd': cmd_obj['cmd'].strip()}

                # Optional timeout override
                if 'timeout' in cmd_obj:
                    if not isinstance(cmd_obj['timeout'], (int, float)):
                        raise ValueError(f"command[{i}].timeout must be a number")
                    if cmd_obj['timeout'] <= 0:
                        raise ValueError(f"command[{i}].timeout must be greater than 0")
                    cmd_entry['timeout'] = float(cmd_obj['timeout'])

                # Optional required flag
                if 'required' in cmd_obj:
                    if not isinstance(cmd_obj['required'], bool):
                        raise ValueError(f"command[{i}].required must be a boolean")
                    cmd_entry['required'] = cmd_obj['required']

                commands.append(cmd_entry)
        else:
            raise ValueError("command must be a string or an array of command objects")

        env = data.get('env', {})
        if not isinstance(env, dict):
            raise ValueError("env must be an object")
        for k, v in env.items():
            if not isinstance(v, str):
                raise ValueError(f"env value for key '{k}' must be a string")

        files = data.get('files', {})
        if not isinstance(files, dict):
            raise ValueError("files must be an object")
        if not all(isinstance(k, str) for k in files.keys()):
            raise ValueError("files keys must be strings")

        stdin = data.get('stdin', "")
        if isinstance(stdin, list):
            stdin = "".join(stdin)
        elif not isinstance(stdin, str):
            raise ValueError("stdin must be a string or array of strings")

        timeout = data.get('timeout', 10)
        if not isinstance(timeout, (int, float)):
            raise ValueError("timeout must be a number")
        if timeout <= 0:
            raise ValueError("timeout must be greater than 0")

        track = data.get('track')
        if track is not None:
            if not isinstance(track, list):
                raise ValueError("track must be an array")
            for pattern in track:
                if not isinstance(pattern, str):
                    raise ValueError("track patterns must be strings")

        # Continue on error flag
        continue_on_error = data.get('continue_on_error', False)
        if not isinstance(continue_on_error, bool):
            raise ValueError("continue_on_error must be a boolean")

        # Force bypass cache flag
        force = data.get('force', False)
        if not isinstance(force, bool):
            raise ValueError("force must be a boolean")

        return {
            'commands': commands,
            'env': dict(env),
            'files': dict(files),
            'stdin': stdin,
            'timeout': float(timeout),
            'track': list(track) if track is not None else None,
            'continue_on_error': continue_on_error,
            'force': force
        }

    def _serialize_files(self, files: dict[str, StructuredData]) -> dict[str, tuple]:
        result = {}

        for filename, content in files.items():
            if isinstance(content, str):
                if content == '' and Path(filename).suffix.lower() in ['.csv', '.tsv', '.jsonl', '.ndjson']:
                    result[filename] = ('\n', None)
                else:
                    result[filename] = (content, None)
            else:
                serialized, compression = self._serialize_content(content, filename)
                result[filename] = (serialized, compression)

        return result

    def _serialize_content(self, content: StructuredData, filename: str) -> tuple[str, str]:
        path = Path(filename)
        ext = path.suffix.lower()
        stem = path.stem

        compression_exts = ['.gz', '.bz2']
        compression = None
        for comp_ext in compression_exts:
            if path.suffixes and path.suffixes[-1].lower() == comp_ext:
                compression = comp_ext
                break

        base_ext = None
        remaining_suffixes = path.suffixes
        if remaining_suffixes and remaining_suffixes[-1].lower() in compression_exts:
            remaining_suffixes = remaining_suffixes[:-1]
        if remaining_suffixes:
            base_ext = remaining_suffixes[-1].lower()

        if compression and len([s for s in path.suffixes if s.lower() in compression_exts]) > 1:
            raise ValueError(f"Multiple compression extensions not allowed: {filename}")

        format_type = None

        if base_ext in ['.json', '.yaml', '.yml']:
            format_type = 'structured'
        elif base_ext in ['.jsonl', '.ndjson']:
            format_type = 'jsonl'
        elif base_ext in ['.csv', '.tsv']:
            format_type = 'tabular'
        elif base_ext is None and compression:
            format_type = 'text'
        elif base_ext:
            format_type = 'text'
        else:
            format_type = 'text'

        if format_type == 'text':
            output = str(content) if not isinstance(content, str) else content
            return output, compression

        elif format_type == 'structured':
            if base_ext == '.json':
                output = json.dumps(content, ensure_ascii=False) + '\n'
            elif base_ext in ['.yaml', '.yml'] and yaml is not None:
                output = yaml.dump(content, allow_unicode=True, default_flow_style=False)
                if not output.endswith('\n'):
                    output += '\n'
            else:
                output = json.dumps(content, ensure_ascii=False) + '\n'
            return output, compression

        elif format_type == 'jsonl':
            if isinstance(content, str):
                lines = content.strip().split('\n')
                output = '\n'.join(lines) + '\n'
            elif isinstance(content, list):
                lines = [json.dumps(item, ensure_ascii=False) for item in content]
                output = '\n'.join(lines) + '\n'
            else:
                output = json.dumps(content, ensure_ascii=False) + '\n'
            return output, compression

        elif format_type == 'tabular':
            if isinstance(content, str):
                output = content + '\n' if content else '\n'
            elif isinstance(content, dict):
                columns = sorted(content.keys())
                rows_data = []
                num_rows = 0
                for col in columns:
                    col_data = content[col]
                    if isinstance(col_data, list):
                        rows_data.append(col_data)
                        num_rows = max(num_rows, len(col_data))
                    else:
                        rows_data.append([col_data])
                        num_rows = max(num_rows, 1)

                output_io = StringIO()
                writer = csv.writer(output_io)
                writer.writerow(columns)
                for row_idx in range(num_rows):
                    row = []
                    for col_idx, col in enumerate(columns):
                        if row_idx < len(rows_data[col_idx]):
                            row.append(rows_data[col_idx][row_idx])
                        else:
                            row.append('')
                    writer.writerow(row)
                output = output_io.getvalue()

            elif isinstance(content, list):
                if not content:
                    output = '\n'
                else:
                    if isinstance(content[0], dict):
                        all_keys = set()
                        for item in content:
                            if isinstance(item, dict):
                                all_keys.update(item.keys())
                        columns = sorted(all_keys)

                        output_io = StringIO()
                        writer = csv.writer(output_io)
                        writer.writerow(columns)
                        for item in content:
                            if isinstance(item, dict):
                                row = [item.get(col, '') for col in columns]
                                writer.writerow(row)
                            elif isinstance(item, list):
                                writer.writerow(item)
                            else:
                                writer.writerow([item])
                        output = output_io.getvalue()
                    else:
                        output_io = StringIO()
                        writer = csv.writer(output_io)
                        for row in content:
                            writer.writerow([row] if not isinstance(row, (list, tuple)) else row)
                        output = output_io.getvalue()
            else:
                output = str(content)
            return output, compression

        else:
            output = str(content) if not isinstance(content, str) else content
            return output, compression

    def _run_command(self, validated: dict[str, Any]) -> ExecutionResult:
        start_time = time.time()
        execution_id = str(uuid.uuid4())

        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)

            serialized_files = self._serialize_files(validated['files'])

            for filename, file_data in serialized_files.items():
                safe_filename = os.path.basename(filename)
                file_path = tmppath / safe_filename

                if isinstance(file_data, str):
                    content = file_data
                    compression = None
                else:
                    content, compression = file_data

                if compression == '.gz':
                    file_path.write_bytes(gzip.compress(content.encode('utf-8')))
                elif compression == '.bz2' and bz2 is not None:
                    file_path.write_bytes(bz2.compress(content.encode('utf-8')))
                else:
                    file_path.write_text(content, encoding='utf-8')

            env = os.environ.copy()
            env.update(validated['env'])

            commands = validated['commands']
            default_timeout = validated['timeout']
            continue_on_error = validated.get('continue_on_error', False)
            stdin = validated['stdin']

            command_results: list[CommandResult] = []
            overall_exit_code = 0
            any_timed_out = False

            # Determine if we're running a single string command or command chain
            is_single_command = len(commands) == 1 and 'cmd' in commands[0] and 'timeout' not in validated and 'continue_on_error' not in validated

            for i, cmd_spec in enumerate(commands):
                cmd = cmd_spec['cmd']
                timeout = cmd_spec.get('timeout', default_timeout)
                required = cmd_spec.get('required', False)

                # For backward compatibility: if it's a single string command, use old format
                if is_single_command and i == 0 and not cmd_spec.get('timeout') and not cmd_spec.get('required'):
                    # Use old single-command execution path
                    cmd_start = time.time()
                    try:
                        process = subprocess.Popen(
                            cmd,
                            shell=True,
                            cwd=tmpdir,
                            stdin=subprocess.PIPE if stdin else subprocess.DEVNULL,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            env=env,
                            text=True,
                            errors='replace'
                        )

                        try:
                            cmd_stdout, cmd_stderr = process.communicate(
                                input=stdin if stdin else None,
                                timeout=timeout
                            )
                            cmd_timed_out = False
                            cmd_exit_code = process.returncode
                        except subprocess.TimeoutExpired:
                            process.terminate()
                            try:
                                process.wait(timeout=1)
                            except subprocess.TimeoutExpired:
                                process.kill()
                                process.wait()

                            cmd_stdout = ""
                            cmd_stderr = ""
                            cmd_exit_code = -1
                            cmd_timed_out = True

                    except Exception as e:
                        cmd_result = CommandResult(
                            cmd=cmd,
                            stdout="",
                            stderr=str(e),
                            exit_code=-1,
                            duration=0.0,
                            timed_out=False,
                            required=required
                        )
                        command_results.append(cmd_result)
                        overall_exit_code = -1
                        any_timed_out = True
                        break

                    cmd_duration = time.time() - cmd_start

                    cmd_result = CommandResult(
                        cmd=cmd,
                        stdout=cmd_stdout or "",
                        stderr=cmd_stderr or "",
                        exit_code=cmd_exit_code,
                        duration=cmd_duration,
                        timed_out=cmd_timed_out,
                        required=required
                    )
                    command_results.append(cmd_result)

                    if cmd_exit_code != 0:
                        overall_exit_code = cmd_exit_code
                    if cmd_timed_out:
                        any_timed_out = True
                else:
                    # Command chain execution
                    cmd_start = time.time()
                    try:
                        process = subprocess.Popen(
                            cmd,
                            shell=True,
                            cwd=tmpdir,
                            stdin=subprocess.PIPE if (i == 0 and stdin) else subprocess.DEVNULL,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            env=env,
                            text=True,
                            errors='replace'
                        )

                        try:
                            input_data = stdin if (i == 0 and stdin) else None
                            cmd_stdout, cmd_stderr = process.communicate(
                                input=input_data,
                                timeout=timeout
                            )
                            cmd_timed_out = False
                            cmd_exit_code = process.returncode
                        except subprocess.TimeoutExpired:
                            process.terminate()
                            try:
                                process.wait(timeout=1)
                            except subprocess.TimeoutExpired:
                                process.kill()
                                process.wait()

                            cmd_stdout = ""
                            cmd_stderr = ""
                            cmd_exit_code = -1
                            cmd_timed_out = True

                    except Exception as e:
                        cmd_result = CommandResult(
                            cmd=cmd,
                            stdout="",
                            stderr=str(e),
                            exit_code=-1,
                            duration=0.0,
                            timed_out=False,
                            required=required
                        )
                        command_results.append(cmd_result)
                        overall_exit_code = -1
                        any_timed_out = True
                        # For required commands, continue execution
                        if not required:
                            break
                        continue

                    cmd_duration = time.time() - cmd_start

                    cmd_result = CommandResult(
                        cmd=cmd,
                        stdout=cmd_stdout or "",
                        stderr=cmd_stderr or "",
                        exit_code=cmd_exit_code,
                        duration=cmd_duration,
                        timed_out=cmd_timed_out,
                        required=required
                    )
                    command_results.append(cmd_result)

                    # Track overall exit code (first non-zero, but required commands with failure don't stop)
                    if cmd_exit_code != 0:
                        if overall_exit_code == 0:
                            overall_exit_code = cmd_exit_code
                    if cmd_timed_out:
                        any_timed_out = True

                    # Stop on non-required command failure unless continue_on_error is True
                    # OR there are still required commands remaining
                    if cmd_exit_code != 0 and not required and not continue_on_error:
                        # Check if there are any remaining required commands
                        remaining_required = any(
                            cmd_spec.get('required', False)
                            for j, cmd_spec in enumerate(commands)
                            if j > i
                        )
                        if not remaining_required:
                            break

        duration = time.time() - start_time

        # For backward compatibility: single string command uses old format
        if (len(command_results) == 1 and
            command_results[0].cmd == validated['commands'][0]['cmd'] and
            not validated['commands'][0].get('timeout') and
            not validated['commands'][0].get('required')):
            # Track files if requested
            tracked_files: dict[str, str] | None = None
            if validated.get('track'):
                tracked_files = {}
                for pattern in validated['track']:
                    for matched_path in glob(str(tmppath / pattern), recursive=True):
                        relative_path = Path(matched_path).relative_to(tmppath)
                        if str(relative_path) not in tracked_files:
                            try:
                                if matched_path.endswith('.gz'):
                                    with gzip.open(matched_path, 'rt', encoding='utf-8') as f:
                                        content = f.read()
                                elif matched_path.endswith('.bz2') and bz2 is not None:
                                    with bz2.open(matched_path, 'rt', encoding='utf-8') as f:
                                        content = f.read()
                                else:
                                    with open(matched_path, 'r', encoding='utf-8') as f:
                                        content = f.read()
                                tracked_files[str(relative_path)] = content
                            except Exception:
                                pass
            return ExecutionResult(
                id=execution_id,
                exit_code=command_results[0].exit_code,
                duration=duration,
                timed_out=command_results[0].timed_out,
                commands=None,
                stdout=command_results[0].stdout,
                stderr=command_results[0].stderr,
                files=tracked_files
            )

        # Track files if requested
        tracked_files: dict[str, str] | None = None
        if validated.get('track'):
            tracked_files = {}
            for pattern in validated['track']:
                for matched_path in glob(str(tmppath / pattern), recursive=True):
                    relative_path = Path(matched_path).relative_to(tmppath)
                    if str(relative_path) not in tracked_files:
                        try:
                            if matched_path.endswith('.gz'):
                                with gzip.open(matched_path, 'rt', encoding='utf-8') as f:
                                    content = f.read()
                            elif matched_path.endswith('.bz2') and bz2 is not None:
                                with bz2.open(matched_path, 'rt', encoding='utf-8') as f:
                                    content = f.read()
                            else:
                                with open(matched_path, 'r', encoding='utf-8') as f:
                                    content = f.read()
                            tracked_files[str(relative_path)] = content
                        except Exception:
                            pass

        return ExecutionResult(
            id=execution_id,
            exit_code=overall_exit_code,
            duration=duration,
            timed_out=any_timed_out,
            commands=command_results if command_results else None,
            stdout=None,
            stderr=None,
            files=tracked_files
        )

    def do_POST(self) -> None:
        if self.path != '/v1/execute':
            self._send_error_response(404, "Endpoint not found", "ENDPOINT_NOT_FOUND")
            return

        try:
            data = self._parse_json_body()
            validated = self._validate_execute_request(data)

            # Check if we should use cache (unless force is true)
            cache_key = None
            cached_result = None
            force = validated.get('force', False)

            if not force:
                cache_key = self._generate_cache_key(validated)
                with cache_lock:
                    if cache_key in execution_cache:
                        cached_result = execution_cache[cache_key]

            if cached_result:
                # Return cached result - always generate new UUID but use cached data
                import uuid as uuid_module
                execution_id = str(uuid_module.uuid4())

                response = {
                    'id': execution_id,
                    'exit_code': cached_result.exit_code,
                    'duration': round(cached_result.duration, 3),
                    'timed_out': cached_result.timed_out,
                    'cached': True
                }

                if cached_result.commands:
                    response['commands'] = [
                        {
                            'cmd': cmd_result.cmd,
                            'stdout': cmd_result.stdout,
                            'stderr': cmd_result.stderr,
                            'exit_code': cmd_result.exit_code,
                            'duration': round(cmd_result.duration, 3),
                            'timed_out': cmd_result.timed_out,
                            **({'required': True} if cmd_result.required else {})
                        }
                        for cmd_result in cached_result.commands
                    ]
                else:
                    response['stdout'] = cached_result.stdout
                    response['stderr'] = cached_result.stderr

                if cached_result.files:
                    response['files'] = cached_result.files

                self._send_json_response(201, response)
                # Increment cache hit stat after response is sent
                with cache_lock:
                    global cache_hits
                    cache_hits += 1
                return

            # Cache miss - execute the command
            result = self._run_command(validated)

            # Update stats (cached executions are NOT included)
            with stats_lock:
                global total_runs, run_durations, command_total_count, command_ran_count, command_durations
                total_runs += 1
                run_durations.append(result.duration)

                # Track command-level stats
                cmd_count = len(validated['commands'])
                command_total_count += cmd_count
                command_ran_count += len(result.commands) if result.commands else 0

                for cmd_result in (result.commands or []):
                    command_durations.append(cmd_result.duration)

            # Store in cache if we have a cache key
            if cache_key:
                with cache_lock:
                    execution_cache[cache_key] = result
                    global cache_misses
                    cache_misses += 1

            response = {
                'id': result.id,
                'exit_code': result.exit_code,
                'duration': round(result.duration, 3),
                'timed_out': result.timed_out,
                'cached': False
            }

            # For backward compatibility with string commands, don't include commands array
            # but still include files (will include them for backward compatibility)
            if result.commands:
                # Chain of commands - return full format with commands array
                response['commands'] = [
                    {
                        'cmd': cmd_result.cmd,
                        'stdout': cmd_result.stdout,
                        'stderr': cmd_result.stderr,
                        'exit_code': cmd_result.exit_code,
                        'duration': round(cmd_result.duration, 3),
                        'timed_out': cmd_result.timed_out,
                        **({'required': True} if cmd_result.required else {})
                    }
                    for cmd_result in result.commands
                ]
            else:
                # Single string command - backward compatible format
                response['stdout'] = result.stdout
                response['stderr'] = result.stderr

            if result.files:
                response['files'] = result.files
            self._send_json_response(201, response)

        except ValueError as e:
            self._send_error_response(400, str(e), "INVALID_REQUEST")
        except Exception as e:
            self._send_error_response(500, f"Internal server error: {e}", "INTERNAL_ERROR")

    def _calculate_stats(self, durations: list[float]) -> dict[str, float | None]:
        if len(durations) == 0:
            return {
                'average': None,
                'median': None,
                'max': None,
                'min': None,
                'stddev': None
            }

        avg = mean(durations)
        med = median(durations)
        max_d = max(durations)
        min_d = min(durations)

        if len(durations) > 1:
            std = stdev(durations)
        else:
            std = 0.0

        return {
            'average': round(avg, 3),
            'median': round(med, 3),
            'max': round(max_d, 3),
            'min': round(min_d, 3),
            'stddev': round(std, 3)
        }

    def do_GET(self) -> None:
        if self.path != '/v1/stats/execution':
            self._send_error_response(404, "Endpoint not found", "ENDPOINT_NOT_FOUND")
            return

        with stats_lock:
            ran = total_runs
            durations = list(run_durations)
            cmd_total = command_total_count
            cmd_ran = command_ran_count
            cmd_durations = list(command_durations)

        with cache_lock:
            hits = cache_hits
            misses = cache_misses

        response = {
            'ran': ran,
            'commands': {
                'total': cmd_total,
                'ran': cmd_ran,
                'average': None,
                'average_ran': None,
                'duration': self._calculate_stats(cmd_durations)
            },
            'duration': self._calculate_stats(durations),
            'cache': {
                'hits': hits,
                'misses': misses,
                'hit_rate': None if (hits + misses) == 0 else round(hits / (hits + misses), 3)
            }
        }

        if ran > 0:
            response['commands']['average'] = round(cmd_total / ran, 3) if ran > 0 else None
            response['commands']['average_ran'] = round(cmd_ran / ran, 3) if ran > 0 else None

        self._send_json_response(200, response)

    def log_message(self, format: str, *args: Any) -> None:
        pass


def main() -> None:
    parser = argparse.ArgumentParser(
        description='Local runner web API for executing commands'
    )
    parser.add_argument(
        '--port',
        type=int,
        default=8080,
        help='Port to listen on (default: 8080)'
    )
    parser.add_argument(
        '--address',
        type=str,
        default='0.0.0.0',
        help='Address to bind to (default: 0.0.0.0)'
    )

    args = parser.parse_args()

    server = HTTPServer((args.address, args.port), ExecutionHandler)

    print(f"Starting execution server on {args.address}:{args.port}")
    print(f"Endpoints:")
    print(f"  POST /v1/execute - Execute a command")
    print(f"  GET  /v1/stats/execution - Get execution statistics")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down server...")
        server.shutdown()


if __name__ == '__main__':
    main()
