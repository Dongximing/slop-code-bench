#!/usr/bin/env python3
"""
Local runner web API for executing commands in throwaway working directories.
"""

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

# Type for structured data values in files
StructuredData = Union[dict, list, str, int, float, bool, None]


stats_lock = threading.Lock()
run_durations: list[float] = []
total_runs = 0


@dataclass
class ExecutionResult:
    id: str
    stdout: str
    stderr: str
    exit_code: int
    duration: float
    timed_out: bool
    files: dict[str, str] | None = None


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

    def _validate_execute_request(self, data: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(data, dict):
            raise ValueError("Request body must be a JSON object")

        if 'command' not in data:
            raise ValueError("Missing required field: command")

        command = data['command']
        if not isinstance(command, str) or not command.strip():
            raise ValueError("command must be a non-empty string")

        env = data.get('env', {})
        if not isinstance(env, dict):
            raise ValueError("env must be an object")
        for k, v in env.items():
            if not isinstance(v, str):
                raise ValueError(f"env value for key '{k}' must be a string")

        files = data.get('files', {})
        if not isinstance(files, dict):
            raise ValueError("files must be an object")
        # Allow structured data (not just strings) in files
        # Validation happens in _serialize_files based on file extension
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

        return {
            'command': command.strip(),
            'env': dict(env),
            'files': dict(files),
            'stdin': stdin,
            'timeout': float(timeout),
            'track': list(track) if track is not None else None
        }

    def _serialize_files(self, files: dict[str, StructuredData]) -> dict[str, tuple]:
        """Serialize files based on their extensions."""
        result = {}

        for filename, content in files.items():
            if isinstance(content, str):
                # Raw text - write as-is
                # For tabular files with empty string, add newline
                if content == '' and Path(filename).suffix.lower() in ['.csv', '.tsv', '.jsonl', '.ndjson']:
                    result[filename] = ('\n', None)
                else:
                    result[filename] = (content, None)
            else:
                # Structured data - serialize based on extension
                serialized, compression = self._serialize_content(content, filename)
                result[filename] = (serialized, compression)

        return result

    def _serialize_content(self, content: StructuredData, filename: str) -> tuple[str, str]:
        """Serialize content based on file extension.

        Returns: (serialized_content, compression)
        - compression: None, '.gz', or '.bz2'
        """
        path = Path(filename)
        ext = path.suffix.lower()
        stem = path.stem

        # Check for compression extensions
        compression_exts = ['.gz', '.bz2']
        compression = None
        for comp_ext in compression_exts:
            if path.suffixes and path.suffixes[-1].lower() == comp_ext:
                compression = comp_ext
                break

        # Get the base extension (without compression)
        base_ext = None
        remaining_suffixes = path.suffixes
        if remaining_suffixes and remaining_suffixes[-1].lower() in compression_exts:
            remaining_suffixes = remaining_suffixes[:-1]
        if remaining_suffixes:
            base_ext = remaining_suffixes[-1].lower()

        # Handle multiple compression extensions (error)
        if compression and len([s for s in path.suffixes if s.lower() in compression_exts]) > 1:
            raise ValueError(f"Multiple compression extensions not allowed: {filename}")

        # Determine format based on extension
        format_type = None

        if base_ext in ['.json', '.yaml', '.yml']:
            format_type = 'structured'
        elif base_ext in ['.jsonl', '.ndjson']:
            format_type = 'jsonl'
        elif base_ext in ['.csv', '.tsv']:
            format_type = 'tabular'
        elif base_ext is None and compression:
            # Compressed without base extension - treat as raw text
            format_type = 'text'
        elif base_ext:
            # Unknown extension - treat as raw text
            format_type = 'text'
        else:
            # No extension - treat as raw text
            format_type = 'text'

        if format_type == 'text':
            # Treat as raw text - stringify if needed
            output = str(content) if not isinstance(content, str) else content
            return output, compression

        elif format_type == 'structured':
            if base_ext == '.json':
                output = json.dumps(content, ensure_ascii=False)
            elif base_ext in ['.yaml', '.yml']:
                try:
                    import yaml
                    output = yaml.dump(content, allow_unicode=True, default_flow_style=False)
                except ImportError:
                    # Fallback to JSON if PyYAML not available
                    output = json.dumps(content, ensure_ascii=False)
            else:
                output = json.dumps(content, ensure_ascii=False)
            return output, compression

        elif format_type == 'jsonl':
            # JSON Lines format - one JSON object per line
            if isinstance(content, str):
                # String content - each line is a JSON object
                lines = content.strip().split('\n')
                output = '\n'.join(lines) + '\n'
            elif isinstance(content, list):
                # List of objects - each object on a separate line
                lines = [json.dumps(item, ensure_ascii=False) for item in content]
                output = '\n'.join(lines) + '\n'
            else:
                # Single object
                output = json.dumps(content, ensure_ascii=False) + '\n'
            return output, compression

        elif format_type == 'tabular':
            if isinstance(content, str):
                # Empty string like examples show
                output = content + '\n' if content else '\n'
            elif isinstance(content, dict):
                # Dictionary of columns - columns sorted lexicographically
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

                # Build CSV
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
                # List of rows
                if not content:
                    output = '\n'
                else:
                    # Check if first element is a dict (list of objects) or list
                    if isinstance(content[0], dict):
                        # List of dicts - extract columns from all dicts, sorted
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
                        # List of lists / simple values
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

            # Serialize files based on their extensions
            serialized_files = self._serialize_files(validated['files'])

            for filename, file_data in serialized_files.items():
                safe_filename = os.path.basename(filename)
                file_path = tmppath / safe_filename

                # Handle both old format (string) and new format (tuple)
                if isinstance(file_data, str):
                    # Old format - raw text
                    content = file_data
                    compression = None
                else:
                    # New format - (content, compression)
                    content, compression = file_data

                # Write the file
                if compression == '.gz':
                    file_path.write_bytes(gzip.compress(content.encode('utf-8')))
                elif compression == '.bz2':
                    import bz2
                    file_path.write_bytes(bz2.compress(content.encode('utf-8')))
                else:
                    file_path.write_text(content, encoding='utf-8')

            env = os.environ.copy()
            env.update(validated['env'])

            try:
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

                try:
                    stdout, stderr = process.communicate(
                        input=validated['stdin'] if validated['stdin'] else None,
                        timeout=validated['timeout']
                    )
                    timed_out = False
                    exit_code = process.returncode
                except subprocess.TimeoutExpired:
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
                return ExecutionResult(
                    id=execution_id,
                    stdout="",
                    stderr=str(e),
                    exit_code=-1,
                    duration=0.0,
                    timed_out=False
                )

            tracked_files: dict[str, str] | None = None
            if validated.get('track'):
                tracked_files = {}
                for pattern in validated['track']:
                    for matched_path in glob(str(tmppath / pattern), recursive=True):
                        relative_path = Path(matched_path).relative_to(tmppath)
                        if str(relative_path) not in tracked_files:
                            # Read file content (handle compressed files)
                            try:
                                if matched_path.endswith('.gz'):
                                    with gzip.open(matched_path, 'rt', encoding='utf-8') as f:
                                        content = f.read()
                                elif matched_path.endswith('.bz2'):
                                    import bz2
                                    with bz2.open(matched_path, 'rt', encoding='utf-8') as f:
                                        content = f.read()
                                else:
                                    with open(matched_path, 'r', encoding='utf-8') as f:
                                        content = f.read()
                                tracked_files[str(relative_path)] = content
                            except Exception:
                                # Skip files that can't be read
                                pass

        duration = time.time() - start_time

        return ExecutionResult(
            id=execution_id,
            stdout=stdout or "",
            stderr=stderr or "",
            exit_code=exit_code,
            duration=duration,
            timed_out=timed_out,
            files=tracked_files
        )

    def do_POST(self) -> None:
        if self.path != '/v1/execute':
            self._send_error_response(404, "Endpoint not found", "ENDPOINT_NOT_FOUND")
            return

        try:
            data = self._parse_json_body()
            validated = self._validate_execute_request(data)
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
            if result.files:
                response['files'] = result.files
            self._send_json_response(201, response)

        except ValueError as e:
            self._send_error_response(400, str(e), "INVALID_REQUEST")
        except Exception as e:
            self._send_error_response(500, f"Internal server error: {e}", "INTERNAL_ERROR")

    def do_GET(self) -> None:
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
