#!/usr/bin/env python3
"""
Local Runner Web API Server

A tiny "local runner" web API used by internal CI to run quick checks against
a throwaway working directory. Runs are unsandboxed local executions.
"""

import argparse
import glob
import hashlib
import json
import os
import shutil
import statistics
import subprocess
import tempfile
import time
import uuid
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


def round_to_3_decimals(value: float) -> float:
    """Round a float to 3 decimal places."""
    return round(value, 3)


def generate_cache_key(data: Dict[str, Any]) -> str:
    cache_data = {k: v for k, v in data.items() if k != "force"}
    json_str = json.dumps(cache_data, sort_keys=True)
    return hashlib.sha256(json_str.encode("utf-8")).hexdigest()


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
                    for key, value in files.items():
                        if not isinstance(key, str) or not isinstance(value, str):
                            return (f"Field 'files' in command chain item {i} must be an object<string,string>", "INVALID_FILES")
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

            for key, value in files.items():
                if not isinstance(key, str) or not isinstance(value, str):
                    return ("Field 'files' must be an object<string,string>", "INVALID_FILES")

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
            matches = glob.glob(os.path.join(work_dir, pattern), recursive=True)

            for file_path in matches:
                if not os.path.isfile(file_path):
                    continue

                rel_path = os.path.relpath(file_path, work_dir)

                if rel_path in tracked_files:
                    continue

                try:
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

    def _execute_single_command(self, data: Dict[str, Any]) -> Dict[str, Any]:
        command = data["command"]
        env = data.get("env", {})
        files = data.get("files", {})
        stdin_data = data.get("stdin", [])
        timeout = data.get("timeout", 10)
        track_patterns = data.get("track", [])

        execution_id = str(uuid.uuid4())

        stdin_content = self._process_stdin(stdin_data)

        work_dir = tempfile.mkdtemp()

        try:
            for filename, content in files.items():
                file_path = os.path.join(work_dir, filename)
                parent_dir = os.path.dirname(file_path)
                if parent_dir and not os.path.exists(parent_dir):
                    os.makedirs(parent_dir)

                with open(file_path, "w", encoding="utf-8") as f:
                    f.write(content)

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

        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

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

    def _execute_command_chain(self, data: Dict[str, Any]) -> Dict[str, Any]:
        commands = data["command"]
        top_env = data.get("env", {})
        top_files = data.get("files", {})
        top_stdin = data.get("stdin", [])
        top_timeout = data.get("timeout", 10)
        track_patterns = data.get("track", [])
        continue_on_error = data.get("continue_on_error", False)

        execution_id = str(uuid.uuid4())
        work_dir = tempfile.mkdtemp()

        try:
            for filename, content in top_files.items():
                file_path = os.path.join(work_dir, filename)
                parent_dir = os.path.dirname(file_path)
                if parent_dir and not os.path.exists(parent_dir):
                    os.makedirs(parent_dir)

                with open(file_path, "w", encoding="utf-8") as f:
                    f.write(content)

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

                    with open(file_path, "w", encoding="utf-8") as f:
                        f.write(content)

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

        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

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

    def _execute_command(self, data: Dict[str, Any]) -> Dict[str, Any]:
        command = data["command"]

        if isinstance(command, list):
            return self._execute_command_chain(data)
        return self._execute_single_command(data)

    def do_POST(self) -> None:
        global execution_stats, execution_cache

        try:
            if self.path == "/v1/execute":
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

                force = data.get("force", False)
                cache_key = generate_cache_key(data)

                if not force and cache_key in execution_cache:
                    cached_result = execution_cache[cache_key]
                    result = cached_result.copy()
                    result["id"] = str(uuid.uuid4())
                    result["cached"] = True

                    execution_stats["cache_hits"] += 1

                    self._send_json_response(201, result)
                    return

                execution_stats["cache_misses"] += 1

                try:
                    result = self._execute_command(data)
                except Exception as e:
                    self._send_error_response(
                        500,
                        f"Execution failed: {str(e)}",
                        "EXECUTION_ERROR"
                    )
                    return

                result["cached"] = False
                execution_cache[cache_key] = result.copy()

                execution_stats["ran"] += 1
                execution_stats["durations"].append(result["duration"])

                if not isinstance(data["command"], list):
                    execution_stats["commands_ran"] += 1
                    execution_stats["commands_durations"].append(result["duration"])

                self._send_json_response(201, result)

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
    httpd = HTTPServer(server_address, ExecutionHandler)

    print(f"Starting server on {args.address}:{args.port}")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
