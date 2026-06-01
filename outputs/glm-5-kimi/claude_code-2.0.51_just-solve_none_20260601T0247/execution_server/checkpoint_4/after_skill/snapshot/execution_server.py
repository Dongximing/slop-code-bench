#!/usr/bin/env python3
"""
Local Runner Web API Server

A tiny "local runner" web API used by internal CI to run quick checks against
a throwaway working directory. Runs are unsandboxed local executions.
"""

import argparse
import glob
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
    "durations": []
}


def round_to_3_decimals(value: float) -> float:
    """Round a float to 3 decimal places."""
    return round(value, 3)


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
        if not isinstance(command, str):
            return ("Field 'command' must be a string", "INVALID_COMMAND")

        if not command:
            return ("Field 'command' must be non-empty", "INVALID_COMMAND")

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

    def _execute_command(self, data: Dict[str, Any]) -> Dict[str, Any]:
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

            start_time = time.time()

            try:
                result = subprocess.run(
                    command,
                    shell=True,
                    cwd=work_dir,
                    env=process_env,
                    input=stdin_content,
                    capture_output=True,
                    text=True,
                    timeout=timeout
                )

                end_time = time.time()
                duration = end_time - start_time

                stdout = result.stdout
                stderr = result.stderr
                exit_code = result.returncode
                timed_out = False

            except subprocess.TimeoutExpired as e:
                end_time = time.time()
                duration = end_time - start_time

                stdout = e.stdout if e.stdout else ""
                stderr = e.stderr if e.stderr else ""
                exit_code = -1
                timed_out = True

            tracked_files = {}
            if track_patterns:
                tracked_files = self._resolve_tracked_files(work_dir, track_patterns)

        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

        duration_rounded = round_to_3_decimals(duration)

        response = {
            "id": execution_id,
            "stdout": stdout,
            "stderr": stderr,
            "exit_code": exit_code,
            "duration": duration_rounded,
            "timed_out": timed_out
        }

        if track_patterns:
            response["files"] = tracked_files

        return response

    def do_POST(self) -> None:
        global execution_stats

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

                try:
                    result = self._execute_command(data)
                except Exception as e:
                    self._send_error_response(
                        500,
                        f"Execution failed: {str(e)}",
                        "EXECUTION_ERROR"
                    )
                    return

                execution_stats["ran"] += 1
                execution_stats["durations"].append(result["duration"])

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

                if ran == 0:
                    stats_data = {
                        "ran": 0,
                        "duration": {
                            "average": None,
                            "median": None,
                            "max": None,
                            "min": None,
                            "stddev": None
                        }
                    }
                else:
                    stats_data = {
                        "ran": ran,
                        "duration": {
                            "average": round_to_3_decimals(statistics.mean(durations)),
                            "median": round_to_3_decimals(statistics.median(durations)),
                            "max": round_to_3_decimals(max(durations)),
                            "min": round_to_3_decimals(min(durations)),
                            "stddev": round_to_3_decimals(statistics.stdev(durations)) if len(durations) > 1 else round_to_3_decimals(0.0)
                        }
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
