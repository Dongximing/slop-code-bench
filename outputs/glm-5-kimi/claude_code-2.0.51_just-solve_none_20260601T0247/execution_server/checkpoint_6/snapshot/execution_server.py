#!/usr/bin/env python3
"""
Local Runner Web API Server

A tiny "local runner" web API used by internal CI to run quick checks against
a throwaway working directory. Runs are unsandboxed local executions.

Extended with persistent environments and configurable concurrency modes.
"""

import argparse
import csv
import gzip
import hashlib
import io
import json
import os
import shutil
import statistics
import subprocess
import tempfile
import threading
import time
import uuid
import yaml
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Any, Dict, List, Optional, Union


execution_stats = {
    "ran": 0,
    "durations": [],
    "commands_ran": 0,
    "commands_durations": [],
    "cache_hits": 0,
    "cache_misses": 0
}

execution_cache: Dict[str, Dict[str, Any]] = {}

# Environment storage
# Key: environment name
# Value: {
#   "name": str,
#   "concurrency_mode": "never" | "fork" | "base",
#   "base_path": str,  # Path to base snapshot directory
#   "current_path": str,  # Path to current state directory
#   "lock": threading.Lock,  # Lock for concurrency control
#   "in_use": bool,  # Whether environment is currently in use
# }
environments: Dict[str, Dict[str, Any]] = {}
environments_lock = threading.Lock()


def round_to_3_decimals(value: float) -> float:
    """Round a float to 3 decimal places."""
    return round(value, 3)


def generate_cache_key(data: Dict[str, Any]) -> str:
    cache_data = {k: v for k, v in data.items() if k != "force"}
    json_str = json.dumps(cache_data, sort_keys=True)
    return hashlib.sha256(json_str.encode("utf-8")).hexdigest()


def _serialize_file_content(filename: str, content: Any) -> bytes:
    """
    Serialize file content based on filename extension and content type.
    Returns bytes ready to be written to disk.
    """
    # Normalize filename to lowercase for extension checking
    lower_name = filename.lower()

    # Handle structured file formats
    if lower_name.endswith('.json'):
        if isinstance(content, (dict, list)):
            return json.dumps(content, indent=2).encode('utf-8')
        elif isinstance(content, str):
            return content.encode('utf-8')
        else:
            raise ValueError(f"Invalid content type for {filename}: {type(content)}")

    elif lower_name.endswith('.jsonl'):
        if isinstance(content, list):
            lines = []
            for item in content:
                lines.append(json.dumps(item))
            return ('\n'.join(lines) + '\n').encode('utf-8')
        elif isinstance(content, str):
            return content.encode('utf-8')
        else:
            raise ValueError(f"Invalid content type for {filename}: {type(content)}")

    elif lower_name.endswith('.json.gz'):
        # Compressed JSON
        if isinstance(content, (dict, list)):
            json_bytes = json.dumps(content).encode('utf-8')
            return gzip.compress(json_bytes)
        elif isinstance(content, str):
            return gzip.compress(content.encode('utf-8'))
        else:
            raise ValueError(f"Invalid content type for {filename}: {type(content)}")

    elif lower_name.endswith('.csv'):
        if isinstance(content, dict):
            # Dict with array values for columns
            return _dict_to_csv(content).encode('utf-8')
        elif isinstance(content, list):
            # List of dicts (rows)
            return _list_to_csv(content).encode('utf-8')
        elif isinstance(content, str):
            return content.encode('utf-8')
        else:
            raise ValueError(f"Invalid content type for {filename}: {type(content)}")

    elif lower_name.endswith('.csv.gz'):
        if isinstance(content, dict):
            csv_bytes = _dict_to_csv(content).encode('utf-8')
            return gzip.compress(csv_bytes)
        elif isinstance(content, list):
            csv_bytes = _list_to_csv(content).encode('utf-8')
            return gzip.compress(csv_bytes)
        elif isinstance(content, str):
            return gzip.compress(content.encode('utf-8'))
        else:
            raise ValueError(f"Invalid content type for {filename}: {type(content)}")

    elif lower_name.endswith('.yaml') or lower_name.endswith('.yml'):
        if isinstance(content, (dict, list)):
            return yaml.dump(content, default_flow_style=False).encode('utf-8')
        elif isinstance(content, str):
            return content.encode('utf-8')
        else:
            raise ValueError(f"Invalid content type for {filename}: {type(content)}")

    else:
        # Plain text file
        if isinstance(content, str):
            return content.encode('utf-8')
        else:
            raise ValueError(f"Invalid content type for {filename}: expected string for plain text files")


def _dict_to_csv(data: Dict[str, List]) -> str:
    """Convert dict with array values to CSV format."""
    if not data:
        return ""

    # Get headers (sorted for consistency)
    headers = sorted(data.keys())

    # Find max length
    max_len = 0
    for key in headers:
        if isinstance(data[key], list):
            max_len = max(max_len, len(data[key]))

    # Build CSV
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(headers)

    for i in range(max_len):
        row = []
        for key in headers:
            values = data[key]
            if isinstance(values, list) and i < len(values):
                row.append(values[i])
            else:
                row.append('')
        writer.writerow(row)

    return output.getvalue()


def _list_to_csv(data: List[Dict]) -> str:
    """Convert list of dicts to CSV format."""
    if not data:
        return ""

    # Get all unique headers from all dicts
    headers = []
    for row in data:
        for key in row.keys():
            if key not in headers:
                headers.append(key)
    headers = sorted(headers)

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(headers)

    for row in data:
        csv_row = []
        for key in headers:
            if key in row:
                csv_row.append(row[key])
            else:
                csv_row.append('')
        writer.writerow(csv_row)

    return output.getvalue()


def _write_files_to_directory(directory: str, files: Dict[str, Any]) -> Dict[str, int]:
    """
    Write files to a directory, handling structured file serialization.
    Returns dict of filename -> written_bytes.
    """
    written_bytes = {}

    for filename, content in files.items():
        file_path = os.path.join(directory, filename)
        parent_dir = os.path.dirname(file_path)
        if parent_dir and not os.path.exists(parent_dir):
            os.makedirs(parent_dir)

        # Serialize content
        file_bytes = _serialize_file_content(filename, content)

        # Write file
        with open(file_path, 'wb') as f:
            bytes_written = f.write(file_bytes)

        written_bytes[filename] = bytes_written

    return written_bytes


def _copy_directory(src: str, dst: str) -> None:
    """Copy directory contents from src to dst."""
    if os.path.exists(dst):
        shutil.rmtree(dst)
    shutil.copytree(src, dst)


def _get_snapshot_source(mode: str, in_use: bool) -> str:
    """Determine snapshot source based on mode and usage state."""
    if mode == "never":
        return "exclusive"
    elif mode == "fork":
        return "current" if in_use else "exclusive"
    else:  # base
        return "base"


class RWLock:
    """A read-write lock that allows concurrent readers but exclusive writers."""

    def __init__(self):
        self._readers = 0
        self._writer = False
        self._lock = threading.Lock()
        self._read_ready = threading.Condition(self._lock)

    def acquire_read(self):
        """Acquire read lock. Multiple readers can hold this simultaneously."""
        with self._read_ready:
            while self._writer:
                self._read_ready.wait()
            self._readers += 1

    def release_read(self):
        """Release read lock."""
        with self._read_ready:
            self._readers -= 1
            if self._readers == 0:
                self._read_ready.notify_all()

    def acquire_write(self):
        """Acquire write lock. Only one writer can hold this, no readers."""
        with self._read_ready:
            while self._writer or self._readers > 0:
                self._read_ready.wait()
            self._writer = True

    def release_write(self):
        """Release write lock."""
        with self._read_ready:
            self._writer = False
            self._read_ready.notify_all()

    def try_write(self):
        """Try to acquire write lock. Returns True if successful, False if lock is held."""
        with self._read_ready:
            if self._writer or self._readers > 0:
                return False
            self._writer = True
            return True


class ThreadingHTTPServer(HTTPServer):
    """HTTPServer that creates a new thread for each request."""

    def process_request(self, request, client_address):
        """Start a new thread to process the request."""
        thread = threading.Thread(target=self.process_request_thread,
                                  args=(request, client_address))
        thread.daemon = True
        thread.start()

    def process_request_thread(self, request, client_address):
        """Process the request in a separate thread."""
        try:
            self.finish_request(request, client_address)
        except Exception:
            self.handle_error(request, client_address)
        finally:
            self.shutdown_request(request)


class ExecutionHandler(BaseHTTPRequestHandler):

    def _send_json_response(self, status_code: int, data: Dict[str, Any]) -> None:
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode("utf-8"))

    def _send_error_response(self, status_code: int, message: str, code: str) -> None:
        error_data = {
            "error": message,
            "code": code
        }
        self._send_json_response(status_code, error_data)

    def _parse_json_body(self) -> Optional[Dict[str, Any]]:
        content_length = self.headers.get("Content-Length")
        if content_length is None:
            return None

        try:
            body_length = int(content_length)
            body = self.rfile.read(body_length)
            return json.loads(body.decode("utf-8"))
        except (ValueError, json.JSONDecodeError):
            return None

    def _validate_execute_request(self, data: Dict[str, Any]) -> Optional[tuple]:
        # Check required environment field
        if "environment" not in data:
            return ("Missing required field: environment", "MISSING_ENVIRONMENT")

        environment = data.get("environment")
        if environment is None or (isinstance(environment, str) and not environment.strip()):
            return ("Missing required field: environment", "MISSING_ENVIRONMENT")

        if not isinstance(environment, str):
            return ("Field 'environment' must be a non-empty string", "INVALID_ENVIRONMENT")

        if "command" not in data:
            return ("Missing required field: command", "MISSING_COMMAND")

        command = data["command"]

        if isinstance(command, str):
            if not command:
                return ("Field 'command' must be non-empty", "INVALID_COMMAND")
        elif isinstance(command, list):
            if len(command) == 0:
                return ("Field 'command' must be non-empty", "INVALID_COMMAND")
            for i, cmd_item in enumerate(command):
                if not isinstance(cmd_item, dict):
                    return (f"Command chain item {i} must be an object", "INVALID_COMMAND")
                if "cmd" not in cmd_item:
                    return (f"Command chain item {i} missing required field: cmd", "INVALID_COMMAND")
                if not isinstance(cmd_item["cmd"], str) or not cmd_item["cmd"]:
                    return (f"Command chain item {i} 'cmd' must be a non-empty string", "INVALID_COMMAND")
                for key in cmd_item:
                    if key not in ["cmd", "env", "files", "stdin", "timeout"]:
                        return (f"Unknown field '{key}' in command chain item {i}", "INVALID_COMMAND")
                if "env" in cmd_item:
                    env = cmd_item["env"]
                    if not isinstance(env, dict):
                        return (f"Field 'env' in command chain item {i} must be an object", "INVALID_ENV")
                    for key, value in env.items():
                        if not isinstance(key, str) or not isinstance(value, str):
                            return (f"Field 'env' in command chain item {i} must be an object<string,string>", "INVALID_ENV")
                if "files" in cmd_item:
                    files = cmd_item["files"]
                    if not isinstance(files, dict):
                        return (f"Field 'files' in command chain item {i} must be an object", "INVALID_FILES")
                if "stdin" in cmd_item:
                    stdin = cmd_item["stdin"]
                    if not isinstance(stdin, (str, list)):
                        return (f"Field 'stdin' in command chain item {i} must be a string or array of strings", "INVALID_STDIN")
                    if isinstance(stdin, list):
                        for item in stdin:
                            if not isinstance(item, str):
                                return (f"Field 'stdin' in command chain item {i} must be a string or array of strings", "INVALID_STDIN")
                if "timeout" in cmd_item:
                    timeout = cmd_item["timeout"]
                    if not isinstance(timeout, (int, float)):
                        return (f"Field 'timeout' in command chain item {i} must be a number", "INVALID_TIMEOUT")
                    if timeout <= 0:
                        return (f"Field 'timeout' in command chain item {i} must be greater than 0", "INVALID_TIMEOUT")
        else:
            return ("Field 'command' must be a string or array", "INVALID_COMMAND")

        if "env" in data:
            env = data["env"]
            if not isinstance(env, dict):
                return ("Field 'env' must be an object", "INVALID_ENV")

            for key, value in env.items():
                if not isinstance(key, str) or not isinstance(value, str):
                    return ("Field 'env' must be an object<string,string>", "INVALID_ENV")

        if "files" in data:
            files = data["files"]
            if not isinstance(files, dict):
                return ("Field 'files' must be an object", "INVALID_FILES")

        if "stdin" in data:
            stdin = data["stdin"]
            if not isinstance(stdin, (str, list)):
                return ("Field 'stdin' must be a string or array of strings", "INVALID_STDIN")

            if isinstance(stdin, list):
                for item in stdin:
                    if not isinstance(item, str):
                        return ("Field 'stdin' must be a string or array of strings", "INVALID_STDIN")

        if "timeout" in data:
            timeout = data["timeout"]
            if not isinstance(timeout, (int, float)):
                return ("Field 'timeout' must be a number", "INVALID_TIMEOUT")

            if timeout <= 0:
                return ("Field 'timeout' must be greater than 0", "INVALID_TIMEOUT")

        if "track" in data:
            track = data["track"]
            if not isinstance(track, list):
                return ("Field 'track' must be an array", "INVALID_TRACK")

            for item in track:
                if not isinstance(item, str):
                    return ("Field 'track' must be an array of strings", "INVALID_TRACK")

        if "continue_on_error" in data:
            continue_on_error = data["continue_on_error"]
            if not isinstance(continue_on_error, bool):
                return ("Field 'continue_on_error' must be a boolean", "INVALID_CONTINUE_ON_ERROR")

        if "force" in data:
            force = data["force"]
            if not isinstance(force, bool):
                return ("Field 'force' must be a boolean", "INVALID_FORCE")

        if "max_memory_mb" in data:
            max_memory_mb = data["max_memory_mb"]
            if not isinstance(max_memory_mb, (int, float)):
                return ("Field 'max_memory_mb' must be a number", "INVALID_MAX_MEMORY_MB")
            if max_memory_mb < 0:
                return ("Field 'max_memory_mb' must be non-negative", "INVALID_MAX_MEMORY_MB")

        return None

    def _process_stdin(self, stdin_data: Union[str, List[str], None]) -> str:
        if isinstance(stdin_data, str):
            return stdin_data
        if isinstance(stdin_data, list):
            return "".join(stdin_data)
        return ""

    def _resolve_tracked_files(self, work_dir: str, track_patterns: List[str]) -> Dict[str, str]:
        tracked_files = {}

        for pattern in track_patterns:
            import glob as glob_module
            matches = glob_module.glob(os.path.join(work_dir, pattern), recursive=True)

            for file_path in matches:
                if not os.path.isfile(file_path):
                    continue

                rel_path = os.path.relpath(file_path, work_dir)

                if rel_path in tracked_files:
                    continue

                try:
                    # Check if it's a compressed file
                    lower_path = file_path.lower()
                    if lower_path.endswith('.gz'):
                        with gzip.open(file_path, 'rt', encoding='utf-8') as f:
                            tracked_files[rel_path] = f.read()
                    else:
                        with open(file_path, "r", encoding="utf-8") as f:
                            tracked_files[rel_path] = f.read()
                except Exception:
                    pass

        return tracked_files

    def _run_single_command(
        self,
        command: str,
        work_dir: str,
        base_env: Dict[str, str],
        stdin_content: str,
        timeout: float
    ) -> Dict[str, Any]:
        start_time = time.time()

        try:
            result = subprocess.run(
                command,
                shell=True,
                cwd=work_dir,
                env=base_env,
                input=stdin_content,
                capture_output=True,
                text=True,
                timeout=timeout
            )

            end_time = time.time()
            duration = end_time - start_time

            return {
                "cmd": command,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "exit_code": result.returncode,
                "duration": round_to_3_decimals(duration),
                "timed_out": False
            }

        except subprocess.TimeoutExpired as e:
            end_time = time.time()
            duration = end_time - start_time

            return {
                "cmd": command,
                "stdout": e.stdout if e.stdout else "",
                "stderr": e.stderr if e.stderr else "",
                "exit_code": -1,
                "duration": round_to_3_decimals(duration),
                "timed_out": True
            }

    def _execute_single_command(self, data: Dict[str, Any], work_dir: str, env_name: str) -> Dict[str, Any]:
        command = data["command"]
        env = data.get("env", {})
        files = data.get("files", {})
        stdin_data = data.get("stdin", [])
        timeout = data.get("timeout", 10)
        track_patterns = data.get("track", [])

        execution_id = str(uuid.uuid4())

        stdin_content = self._process_stdin(stdin_data)

        # Write overlay files
        for filename, content in files.items():
            file_path = os.path.join(work_dir, filename)
            parent_dir = os.path.dirname(file_path)
            if parent_dir and not os.path.exists(parent_dir):
                os.makedirs(parent_dir)

            # Handle structured files
            file_bytes = _serialize_file_content(filename, content)
            with open(file_path, 'wb') as f:
                f.write(file_bytes)

        process_env = os.environ.copy()
        process_env.update(env)

        result = self._run_single_command(
            command=command,
            work_dir=work_dir,
            base_env=process_env,
            stdin_content=stdin_content,
            timeout=timeout
        )

        tracked_files = {}
        if track_patterns:
            tracked_files = self._resolve_tracked_files(work_dir, track_patterns)

        response = {
            "id": execution_id,
            "stdout": result["stdout"],
            "stderr": result["stderr"],
            "exit_code": result["exit_code"],
            "duration": result["duration"],
            "timed_out": result["timed_out"]
        }

        if track_patterns:
            response["files"] = tracked_files

        return response

    def _execute_command_chain(self, data: Dict[str, Any], work_dir: str, env_name: str) -> Dict[str, Any]:
        commands = data["command"]
        top_env = data.get("env", {})
        top_files = data.get("files", {})
        top_stdin = data.get("stdin", [])
        top_timeout = data.get("timeout", 10)
        track_patterns = data.get("track", [])
        continue_on_error = data.get("continue_on_error", False)

        execution_id = str(uuid.uuid4())

        # Write overlay files
        for filename, content in top_files.items():
            file_path = os.path.join(work_dir, filename)
            parent_dir = os.path.dirname(file_path)
            if parent_dir and not os.path.exists(parent_dir):
                os.makedirs(parent_dir)

            file_bytes = _serialize_file_content(filename, content)
            with open(file_path, 'wb') as f:
                f.write(file_bytes)

        process_env = os.environ.copy()
        process_env.update(top_env)

        command_results = []
        total_duration = 0.0
        overall_exit_code = 0
        overall_timed_out = False

        for i, cmd_item in enumerate(commands):
            cmd = cmd_item["cmd"]
            cmd_env = cmd_item.get("env", {})
            cmd_files = cmd_item.get("files", {})
            cmd_stdin = cmd_item.get("stdin", [])
            cmd_timeout = cmd_item.get("timeout", top_timeout)

            for filename, content in cmd_files.items():
                file_path = os.path.join(work_dir, filename)
                parent_dir = os.path.dirname(file_path)
                if parent_dir and not os.path.exists(parent_dir):
                    os.makedirs(parent_dir)

                file_bytes = _serialize_file_content(filename, content)
                with open(file_path, 'wb') as f:
                    f.write(file_bytes)

            cmd_process_env = process_env.copy()
            cmd_process_env.update(cmd_env)

            stdin_content = self._process_stdin(cmd_stdin)

            result = self._run_single_command(
                command=cmd,
                work_dir=work_dir,
                base_env=cmd_process_env,
                stdin_content=stdin_content,
                timeout=cmd_timeout
            )

            command_results.append(result)
            total_duration += result["duration"]

            global execution_stats
            execution_stats["commands_ran"] += 1
            execution_stats["commands_durations"].append(result["duration"])

            if result["exit_code"] != 0:
                overall_exit_code = result["exit_code"]
                if result["timed_out"]:
                    overall_timed_out = True
                if not continue_on_error:
                    break

        tracked_files = {}
        if track_patterns:
            tracked_files = self._resolve_tracked_files(work_dir, track_patterns)

        response = {
            "id": execution_id,
            "commands": command_results,
            "exit_code": overall_exit_code,
            "duration": round_to_3_decimals(total_duration),
            "timed_out": overall_timed_out
        }

        if track_patterns:
            response["files"] = tracked_files

        return response

    def _execute_command(self, data: Dict[str, Any], work_dir: str, env_name: str) -> Dict[str, Any]:
        command = data["command"]

        if isinstance(command, list):
            return self._execute_command_chain(data, work_dir, env_name)
        return self._execute_single_command(data, work_dir, env_name)

    def _validate_environment_request(self, data: Dict[str, Any]) -> Optional[tuple]:
        if "name" not in data:
            return ("Missing required field: name", "MISSING_NAME")

        name = data.get("name")
        if not isinstance(name, str) or not name.strip():
            return ("Field 'name' must be a non-empty string", "INVALID_NAME")

        # Validate name format (alphanumeric, hyphens, underscores)
        name = name.strip()
        for char in name:
            if not (char.isalnum() or char in '-_'):
                return ("Field 'name' contains invalid characters", "INVALID_NAME")

        concurrency_mode = data.get("concurrency_mode", "never")
        if concurrency_mode not in ("never", "fork", "base"):
            return ("Field 'concurrency_mode' must be 'never', 'fork', or 'base'", "INVALID_CONCURRENCY_MODE")

        if "files" in data:
            files = data["files"]
            if not isinstance(files, dict):
                return ("Field 'files' must be an object", "INVALID_FILES")

        return None

    def _handle_create_environment(self, data: Dict[str, Any]) -> None:
        global environments

        validation_error = self._validate_environment_request(data)
        if validation_error:
            self._send_error_response(400, validation_error[0], validation_error[1])
            return

        name = data["name"].strip()
        concurrency_mode = data.get("concurrency_mode", "never")
        files = data.get("files", {})

        with environments_lock:
            if name in environments:
                self._send_error_response(400, f"Environment '{name}' already exists", "ENVIRONMENT_EXISTS")
                return

            # Create environment directories
            env_base_dir = tempfile.mkdtemp(prefix=f"env_{name}_base_")
            env_current_dir = tempfile.mkdtemp(prefix=f"env_{name}_current_")

            try:
                # Write files to base directory
                written_bytes = _write_files_to_directory(env_base_dir, files)
                # Copy to current directory
                _copy_directory(env_base_dir, env_current_dir)

                # Create environment record
                environments[name] = {
                    "name": name,
                    "concurrency_mode": concurrency_mode,
                    "base_path": env_base_dir,
                    "current_path": env_current_dir,
                    "lock": RWLock(),
                    "in_use": False
                }

                # Build response
                files_response = {k: {"written_bytes": v} for k, v in written_bytes.items()}

                response = {
                    "name": name,
                    "concurrency_mode": concurrency_mode,
                    "files": files_response
                }

                self._send_json_response(201, response)

            except Exception as e:
                # Clean up on error
                shutil.rmtree(env_base_dir, ignore_errors=True)
                shutil.rmtree(env_current_dir, ignore_errors=True)
                self._send_error_response(500, f"Failed to create environment: {str(e)}", "INTERNAL_ERROR")

    def _handle_execute_with_environment(self, data: Dict[str, Any]) -> None:
        global environments, execution_stats, execution_cache

        env_name = data.get("environment")

        with environments_lock:
            if env_name not in environments:
                self._send_error_response(404, "Environment not found", "ENVIRONMENT_NOT_FOUND")
                return

            env = environments[env_name]

            # Acquire lock based on concurrency mode
            if env["concurrency_mode"] == "never":
                # Exclusive mode - acquire write lock, reject if not available
                acquired = env["lock"].try_write()
                if not acquired:
                    self._send_error_response(423, "Environment is in use", "ENVIRONMENT_LOCKED")
                    return
                env["in_use"] = True
                snapshot_source = "exclusive"
            elif env["concurrency_mode"] == "fork":
                # Fork mode - acquire read lock to read current state
                env["lock"].acquire_read()
                env["in_use"] = True
                snapshot_source = "current"
            else:  # base
                # Base mode - acquire read lock to read base state
                env["lock"].acquire_read()
                env["in_use"] = True
                snapshot_source = "base"

        try:
            # Create work directory from appropriate snapshot
            work_dir = tempfile.mkdtemp()

            try:
                # Copy from appropriate source
                if env["concurrency_mode"] == "base":
                    source_dir = env["base_path"]
                else:
                    # fork and never both use current state
                    source_dir = env["current_path"]

                # Copy snapshot to work directory
                if os.path.exists(source_dir):
                    for item in os.listdir(source_dir):
                        src_path = os.path.join(source_dir, item)
                        dst_path = os.path.join(work_dir, item)
                        if os.path.isdir(src_path):
                            shutil.copytree(src_path, dst_path)
                        else:
                            shutil.copy2(src_path, dst_path)

                # Execute command
                result = self._execute_command(data, work_dir, env_name)

                # Determine committed status and update state if needed
                if env["concurrency_mode"] == "never":
                    committed = True
                    # Update current state
                    _copy_directory(work_dir, env["current_path"])
                elif env["concurrency_mode"] == "fork":
                    committed = True
                    # Update current state - need to release read lock and acquire write lock
                    env["lock"].release_read()
                    env["lock"].acquire_write()
                    _copy_directory(work_dir, env["current_path"])
                    env["lock"].release_write()
                    env["lock"].acquire_read()  # Re-acquire for the finally block
                else:  # base
                    committed = False
                    # Don't update state for base mode

                # Add environment metadata to response
                result["environment"] = {
                    "name": env_name,
                    "snapshot_source": snapshot_source,
                    "committed": committed
                }

                self._send_json_response(201, result)

            finally:
                shutil.rmtree(work_dir, ignore_errors=True)

        finally:
            with environments_lock:
                env["in_use"] = False
            if env["concurrency_mode"] == "never":
                env["lock"].release_write()
            else:
                env["lock"].release_read()

    def do_POST(self) -> None:
        global execution_stats, execution_cache

        try:
            if self.path == "/v1/environment":
                data = self._parse_json_body()

                if data is None:
                    self._send_error_response(
                        400,
                        "Invalid JSON body",
                        "INVALID_JSON"
                    )
                    return

                self._handle_create_environment(data)
                return

            elif self.path == "/v1/execute":
                data = self._parse_json_body()

                if data is None:
                    self._send_error_response(
                        400,
                        "Invalid JSON body",
                        "INVALID_JSON"
                    )
                    return

                validation_error = self._validate_execute_request(data)
                if validation_error:
                    self._send_error_response(400, validation_error[0], validation_error[1])
                    return

                self._handle_execute_with_environment(data)
                return

            else:
                self._send_error_response(
                    404,
                    "Endpoint not found",
                    "NOT_FOUND"
                )

        except Exception as e:
            self._send_error_response(
                500,
                f"Internal server error: {str(e)}",
                "INTERNAL_ERROR"
            )

    def do_GET(self) -> None:
        global execution_stats

        try:
            if self.path == "/v1/stats/execution":
                ran = execution_stats["ran"]
                durations = execution_stats["durations"]
                commands_ran = execution_stats["commands_ran"]
                commands_durations = execution_stats["commands_durations"]
                cache_hits = execution_stats["cache_hits"]
                cache_misses = execution_stats["cache_misses"]

                total_commands = commands_ran

                if ran == 0:
                    duration_stats = {
                        "average": None,
                        "median": None,
                        "max": None,
                        "min": None,
                        "stddev": None
                    }
                else:
                    duration_stats = {
                        "average": round_to_3_decimals(statistics.mean(durations)),
                        "median": round_to_3_decimals(statistics.median(durations)),
                        "max": round_to_3_decimals(max(durations)),
                        "min": round_to_3_decimals(min(durations)),
                        "stddev": round_to_3_decimals(statistics.stdev(durations)) if len(durations) > 1 else round_to_3_decimals(0.0)
                    }

                if commands_ran == 0:
                    commands_duration_stats = {
                        "average": None,
                        "median": None,
                        "max": None,
                        "min": None,
                        "stddev": None
                    }
                else:
                    commands_duration_stats = {
                        "average": round_to_3_decimals(statistics.mean(commands_durations)),
                        "median": round_to_3_decimals(statistics.median(commands_durations)),
                        "max": round_to_3_decimals(max(commands_durations)),
                        "min": round_to_3_decimals(min(commands_durations)),
                        "stddev": round_to_3_decimals(statistics.stdev(commands_durations)) if len(commands_durations) > 1 else round_to_3_decimals(0.0)
                    }

                total_requests = cache_hits + cache_misses
                if total_requests == 0:
                    cache_stats = {
                        "hits": None,
                        "misses": None,
                        "hit_rate": None
                    }
                else:
                    cache_stats = {
                        "hits": cache_hits,
                        "misses": cache_misses,
                        "hit_rate": round_to_3_decimals(cache_hits / total_requests)
                    }

                if ran == 0:
                    commands_stats = {
                        "total": 0,
                        "ran": 0,
                        "average": None,
                        "average_ran": None,
                        "duration": {
                            "average": None,
                            "median": None,
                            "max": None,
                            "min": None,
                            "stddev": None
                        }
                    }
                else:
                    commands_stats = {
                        "total": total_commands,
                        "ran": commands_ran,
                        "average": round_to_3_decimals(total_commands / ran) if ran > 0 else None,
                        "average_ran": round_to_3_decimals(commands_ran / ran) if ran > 0 else None,
                        "duration": commands_duration_stats
                    }

                stats_data = {
                    "ran": ran,
                    "duration": duration_stats,
                    "commands": commands_stats,
                    "cache": cache_stats
                }

                self._send_json_response(200, stats_data)

            else:
                self._send_error_response(
                    404,
                    "Endpoint not found",
                    "NOT_FOUND"
                )

        except Exception as e:
            self._send_error_response(
                500,
                f"Internal server error: {str(e)}",
                "INTERNAL_ERROR"
            )

    def log_message(self, format: str, *args) -> None:
        pass


def main():
    parser = argparse.ArgumentParser(description="Local Runner Web API Server")
    parser.add_argument(
        "--port",
        type=int,
        default=8080,
        help="Port to listen on (default: 8080)"
    )
    parser.add_argument(
        "--address",
        type=str,
        default="0.0.0.0",
        help="Address to bind to (default: 0.0.0.0)"
    )

    args = parser.parse_args()

    server_address = (args.address, args.port)
    httpd = ThreadingHTTPServer(server_address, ExecutionHandler)

    print(f"Starting server on {args.address}:{args.port}")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
