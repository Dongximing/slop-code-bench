#!/usr/bin/env python3
"""
Local runner web API for executing commands in throwaway working directories.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Any, Optional
from statistics import mean, median, stdev
from pathlib import Path


# In-memory stats storage (thread-safe with lock)
stats_lock = threading.Lock()
run_durations: list[float] = []
total_runs = 0


@dataclass
class ExecutionResult:
    """Result of a command execution."""
    id: str
    stdout: str
    stderr: str
    exit_code: int
    duration: float
    timed_out: bool


class ExecutionHandler(BaseHTTPRequestHandler):
    """HTTP request handler for execution API."""

    def _send_json_response(self, status_code: int, data: dict[str, Any]) -> None:
        """Send a JSON response with proper headers."""
        body = json.dumps(data).encode('utf-8')
        self.send_response(status_code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', len(body))
        self.end_headers()
        self.wfile.write(body)

    def _send_error_response(self, status_code: int, message: str, code: str) -> None:
        """Send an error JSON response."""
        self._send_json_response(status_code, {
            'error': message,
            'code': code
        })

    def _parse_json_body(self) -> dict[str, Any]:
        """Parse JSON request body."""
        content_length = int(self.headers.get('Content-Length', 0))
        if content_length == 0:
            return {}

        body = self.rfile.read(content_length)
        try:
            return json.loads(body.decode('utf-8'))
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON: {e}")

    def _validate_execute_request(self, data: dict[str, Any]) -> dict[str, Any]:
        """Validate and normalize the execute request."""
        if not isinstance(data, dict):
            raise ValueError("Request body must be a JSON object")

        # Required: command
        if 'command' not in data:
            raise ValueError("Missing required field: command")

        command = data['command']
        if not isinstance(command, str) or not command.strip():
            raise ValueError("command must be a non-empty string")

        # Optional: env (default {})
        env = data.get('env', {})
        if not isinstance(env, dict):
            raise ValueError("env must be an object")
        # Validate env values are strings
        for k, v in env.items():
            if not isinstance(v, str):
                raise ValueError(f"env value for key '{k}' must be a string")

        # Optional: files (default {})
        files = data.get('files', {})
        if not isinstance(files, dict):
            raise ValueError("files must be an object")
        # Validate files values are strings
        for k, v in files.items():
            if not isinstance(v, str):
                raise ValueError(f"files value for key '{k}' must be a string")

        # Optional: stdin (default "")
        stdin = data.get('stdin', "")
        if isinstance(stdin, list):
            stdin = "".join(stdin)
        elif not isinstance(stdin, str):
            raise ValueError("stdin must be a string or array of strings")

        # Optional: timeout (default 10)
        timeout = data.get('timeout', 10)
        if not isinstance(timeout, (int, float)):
            raise ValueError("timeout must be a number")
        if timeout <= 0:
            raise ValueError("timeout must be greater than 0")

        return {
            'command': command.strip(),
            'env': dict(env),
            'files': dict(files),
            'stdin': stdin,
            'timeout': float(timeout)
        }

    def _run_command(self, validated: dict[str, Any]) -> ExecutionResult:
        """Execute the command and capture results."""
        start_time = time.time()
        execution_id = str(uuid.uuid4())

        # Create temporary working directory
        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)

            # Write files
            for filename, content in validated['files'].items():
                # Sanitize filename to prevent directory traversal
                safe_filename = os.path.basename(filename)
                file_path = tmppath / safe_filename
                file_path.write_text(content, encoding='utf-8')

            # Prepare environment
            env = os.environ.copy()
            env.update(validated['env'])

            try:
                # Run the command
                process = subprocess.Popen(
                    validated['command'],
                    shell=True,
                    cwd=tmpdir,
                    stdin=subprocess.PIPE if validated['stdin'] else subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=env,
                    text=True,
                    errors='replace'
                )

                # Communicate with timeout handling
                try:
                    stdout, stderr = process.communicate(
                        input=validated['stdin'] if validated['stdin'] else None,
                        timeout=validated['timeout']
                    )
                    timed_out = False
                    exit_code = process.returncode
                except subprocess.TimeoutExpired:
                    # Terminate the process
                    process.terminate()
                    try:
                        process.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()

                    stdout = ""
                    stderr = ""
                    exit_code = -1
                    timed_out = True

            except Exception as e:
                # Handle unexpected execution errors
                return ExecutionResult(
                    id=execution_id,
                    stdout="",
                    stderr=str(e),
                    exit_code=-1,
                    duration=0.0,
                    timed_out=False
                )

        duration = time.time() - start_time

        return ExecutionResult(
            id=execution_id,
            stdout=stdout or "",
            stderr=stderr or "",
            exit_code=exit_code,
            duration=duration,
            timed_out=timed_out
        )

    def do_POST(self) -> None:
        """Handle POST requests."""
        if self.path != '/v1/execute':
            self._send_error_response(404, "Endpoint not found", "ENDPOINT_NOT_FOUND")
            return

        try:
            # Parse and validate request
            data = self._parse_json_body()
            validated = self._validate_execute_request(data)

            # Execute command
            result = self._run_command(validated)

            # Update stats
            with stats_lock:
                global total_runs, run_durations
                total_runs += 1
                run_durations.append(result.duration)

            # Send response
            response = {
                'id': result.id,
                'stdout': result.stdout,
                'stderr': result.stderr,
                'exit_code': result.exit_code,
                'duration': round(result.duration, 3),
                'timed_out': result.timed_out
            }
            self._send_json_response(201, response)

        except ValueError as e:
            self._send_error_response(400, str(e), "INVALID_REQUEST")
        except Exception as e:
            self._send_error_response(500, f"Internal server error: {e}", "INTERNAL_ERROR")

    def do_GET(self) -> None:
        """Handle GET requests."""
        if self.path != '/v1/stats/execution':
            self._send_error_response(404, "Endpoint not found", "ENDPOINT_NOT_FOUND")
            return

        with stats_lock:
            ran = total_runs
            durations = list(run_durations)

        if ran == 0:
            response = {
                'ran': 0,
                'duration': {
                    'average': None,
                    'median': None,
                    'max': None,
                    'min': None,
                    'stddev': None
                }
            }
        else:
            avg = mean(durations)
            med = median(durations)
            max_d = max(durations)
            min_d = min(durations)

            if len(durations) > 1:
                std = stdev(durations)
            else:
                std = 0.0

            response = {
                'ran': ran,
                'duration': {
                    'average': round(avg, 3),
                    'median': round(med, 3),
                    'max': round(max_d, 3),
                    'min': round(min_d, 3),
                    'stddev': round(std, 3)
                }
            }

        self._send_json_response(200, response)

    def log_message(self, format: str, *args: Any) -> None:
        """Override to customize logging."""
        # Suppress default logging for cleaner output
        pass


def main() -> None:
    """Main entry point."""
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
