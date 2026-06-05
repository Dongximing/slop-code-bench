#!/usr/bin/env python3
"""
HMock - Multi-protocol mock server driven by YAML mock definitions.
HTTP request mocking only.
"""

import asyncio
import json
import logging
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

import yaml
from fastapi import FastAPI, Request, Response
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Scope, Receive, Send


# ============================================================================
# Configuration
# ============================================================================

TEMPLATES_DIR = os.environ.get("HM_TEMPLATES_DIR", "./templates")
HTTP_PORT = int(os.environ.get("HM_HTTP_PORT", "9999"))
HTTP_HOST = os.environ.get("HM_HTTP_HOST", "0.0.0.0")
LOG_LEVEL = os.environ.get("HM_LOG_LEVEL", "info").lower()


# ============================================================================
# Logging
# ============================================================================

import json as json_module
from datetime import datetime

def log_request_response(http_path: str, http_method: str, http_host: str, http_req: dict, http_res: dict):
    """Log HTTP request/response pair as structured JSON."""
    entry = {
        'http_path': http_path,
        'http_method': http_method,
        'http_host': http_host,
        'http_req': http_req,
        'http_res': http_res,
        'timestamp': datetime.utcnow().isoformat() + 'Z'
    }
    print(json_module.dumps(entry), flush=True)

def setup_logging():
    handler = logging.StreamHandler(sys.stdout)
    # Use a simple format for other logs
    handler.setFormatter(logging.Formatter('%(message)s'))
    handler.level = getattr(logging, LOG_LEVEL.upper(), logging.INFO)

    root_logger = logging.getLogger()
    root_logger.handlers = []
    root_logger.addHandler(handler)
    root_logger.setLevel(getattr(logging, LOG_LEVEL.upper(), logging.INFO))

    return logging.getLogger("hmock")

log = setup_logging()


# ============================================================================
# Template Engine (Go-style)
# ============================================================================

class TemplateContext:
    """Template context providing request data."""

    def __init__(
        self,
        headers: Headers,
        body: str,
        path: str,
        query_string: str,
        path_params: Dict[str, str] = None
    ):
        self._headers = headers
        self._body = body
        self._path = path
        self._query_string = query_string
        self._path_params = path_params or {}

    @property
    def HTTPHeader(self) -> 'HeaderMap':
        return HeaderMap(self._headers)

    @property
    def HTTPBody(self) -> str:
        return self._body

    @property
    def HTTPPath(self) -> str:
        return self._path

    @property
    def HTTPQueryString(self) -> str:
        return self._query_string

    @property
    def HTTPPathParams(self) -> Dict[str, str]:
        return self._path_params


class HeaderMap:
    """Wrapper for headers to provide .Get method."""

    def __init__(self, headers: Headers):
        self._headers = headers

    def Get(self, key: str) -> str:
        return self._headers.get(key, "")

    def __repr__(self):
        return dict(self._headers).__repr__()


class TemplateEngine:
    """Go-style template engine with {{ }} delimiters."""

    # Built-in functions from the spec
    BUILTIN_FUNCTIONS = {
        # Comparison
        'eq': lambda args: args[0] == args[1] if len(args) == 2 else args[0] == args[1] == args[2],
        'ne': lambda args: args[0] != args[1],
        'lt': lambda args: args[0] < args[1],
        'gt': lambda args: args[0] > args[1],
        'le': lambda args: args[0] <= args[1],
        'ge': lambda args: args[0] >= args[1],
        # Logic
        'and': lambda args: all(args),
        'or': lambda args: any(args),
        'not': lambda args: not args[0],
        # Output
        'print': lambda args: str(args[0]) if args else "",
        'printf': lambda args: args[0] % args[1:] if len(args) > 1 else str(args[0]),
        'println': lambda args: str(args[0]) if args else "",
        # Collections
        'len': lambda args: len(args[0]),
        'index': lambda args: args[0][args[1]] if len(args) > 1 else None,
        'call': lambda args: args[0](*args[1:]) if args else None,
        # Extended String
        'contains': lambda args: args[1] in args[0],
        'hasPrefix': lambda args: args[0].startswith(args[1]),
        'hasSuffix': lambda args: args[0].endswith(args[1]),
        'replace': lambda args: args[0].replace(args[1], args[2], args[3] if len(args) > 3 else -1),
        'trim': lambda args: args[0].strip(),
        'upper': lambda args: args[0].upper(),
        'lower': lambda args: args[0].lower(),
        'title': lambda args: args[0].title(),
        'split': lambda args: args[0].split(args[1]),
        'splitList': lambda args: args[0].split(args[1]),
        'join': lambda args: args[1].join(args[0]) if isinstance(args[0], list) else "",
        'repeat': lambda args: args[0] * args[1],
        'nospace': lambda args: ''.join(args[0].split()),
        'toString': lambda args: str(args[0]),
        # Extended Comparison
        'default': lambda args: args[0] if args[0] not in (None, "") else args[1],
        'empty': lambda args: not args[0],
        'coalesce': lambda args: next((a for a in args if a not in (None, "")), None),
        'ternary': lambda args: args[1] if args[0] else args[2],
        # Extended Encoding
        'b64enc': lambda args: __import__('base64').b64encode(str(args[0]).encode()).decode(),
        'b64dec': lambda args: __import__('base64').b64decode(args[0]).decode(),
        # Extended Environment
        'env': lambda args: os.environ.get(args[0], ""),
        # Extended Math
        'add': lambda args: args[0] + args[1],
        'sub': lambda args: args[0] - args[1],
        'mul': lambda args: args[0] * args[1],
        'div': lambda args: args[0] // args[1],
        'mod': lambda args: args[0] % args[1],
        'max': lambda args: max(args),
        'min': lambda args: min(args),
        # UUID
        'uuidv4': lambda args: __import__('uuid').uuid4().hex,
    }

    # Extended functions for output
    OUTPUT_FUNCTIONS = {
        'html': lambda x: __import__('html').escape(str(x)),
        'js': lambda x: json.dumps(str(x)),
        'urlquery': lambda x: __import__('urllib.parse').quote(str(x)),
    }

    def __init__(self):
        self._pattern = re.compile(r'{{[\s\S]*?}}')

    def render(self, template: str, context: TemplateContext) -> str:
        """Render a template with context."""
        if not template or '{' not in template:
            return template

        # Replace line breaks and tabs with spaces before parsing
        template = template.replace('\r\n', ' ').replace('\n', ' ').replace('\t', ' ')

        result = []
        last_end = 0

        for match in self._pattern.finditer(template):
            # Add literal text before this template expression
            if match.start() > last_end:
                result.append(template[last_end:match.start()])

            expr = match.group()[2:-2].strip()  # Remove {{ and }}
            try:
                output = self._evaluate(expr, context)
                result.append(str(output))
            except Exception as e:
                raise ValueError(f"Template render error: {e}, expression: {expr}")

            last_end = match.end()

        # Add remaining literal text
        if last_end < len(template):
            result.append(template[last_end:])

        return ''.join(result)

    def _evaluate(self, expr: str, context: TemplateContext) -> Any:
        """Evaluate a template expression."""
        expr = expr.strip()

        # Handle conditional (if/else/end) - simplified
        if expr.startswith('if '):
            condition = expr[3:].strip()
            try:
                result = self._evaluate(condition, context)
                return str(result).lower() == 'true'
            except:
                return False

        # Handle raw string literals {{ `text` }}
        if expr.startswith('`'):
            end = expr.find('`', 1)
            if end != -1:
                return expr[1:end]

        # Handle pipeline (e.g., .Value | functionName "arg")
        if '|' in expr:
            return self._eval_pipeline(expr, context)

        # Handle variable or method call
        return self._eval_expression(expr, context)

    def _eval_expression(self, expr: str, context: TemplateContext) -> Any:
        """Evaluate a simple expression (no pipeline)."""
        expr = expr.strip()

        # Try quoted string
        if (expr.startswith('"') and expr.endswith('"')) or (expr.startswith('`') and expr.endswith('`')):
            return expr[1:-1]

        # Try number
        try:
            if '.' in expr:
                return float(expr)
            return int(expr)
        except ValueError:
            pass

        # Boolean
        if expr == 'true':
            return True
        if expr == 'false':
            return False

        # Handle method call with argument: .Method "arg"
        # This is handled by _eval_expression recursively

        # Handle variable access
        if expr.startswith('.'):
            return self._eval_access(expr[1:], context)

        return expr

    def _eval_access(self, access: str, context: TemplateContext) -> Any:
        """Evaluate a variable/method access chain."""
        access = access.strip()

        # Split by dot for chain: .HTTPHeader.Get "X-Token"
        parts = access.split()
        if len(parts) >= 2 and parts[1]:
            # This could be a method call: .HTTPHeader.Get "X-Token"
            # or a property chain: .HTTPHeader.Get
            first_part = parts[0]
            rest = ' '.join(parts[1:])

            if first_part.startswith('.'):
                # Continue with dot-separated property chain
                obj = self._eval_access(first_part, context)
                # Now handle the rest which could be a method call
                return self._apply_chain(obj, rest, context)
            else:
                # It's a method call from a built-in function or value
                obj = self._eval_expression(first_part, context)
                return self._apply_chain(obj, rest, context)

        # Simple property access: .HTTPHeader
        parts = access.split('.')
        obj = getattr(context, parts[0], None)
        if obj is None:
            raise ValueError(f"Undefined variable: .{parts[0]}")

        for part in parts[1:]:
            if hasattr(obj, part):
                attr = getattr(obj, part)
                if callable(attr):
                    # It's a method, return it for later call
                    return attr
                obj = attr
            elif isinstance(obj, dict) and part in obj:
                obj = obj[part]
            else:
                raise ValueError(f"Undefined variable: .{access}")

        return obj

    def _apply_chain(self, obj, chain: str, context: TemplateContext) -> Any:
        """Apply a chain of property/method accesses."""
        chain = chain.strip()

        # Parse method call: Method "arg" or Method
        method_match = re.match(r'^([a-zA-Z_][a-zA-Z0-9_]*)\s*(.*)', chain)
        if method_match:
            method_name = method_match.group(1)
            args_str = method_match.group(2).strip()

            if hasattr(obj, method_name):
                method = getattr(obj, method_name)
                if callable(method):
                    # Parse arguments
                    args = self._parse_args(args_str, context)
                    return method(*args)

        # Regular property access
        if chain:
            chain_parts = chain.split('.')
            current_obj = obj
            for part in chain_parts:
                if hasattr(current_obj, part):
                    attr = getattr(current_obj, part)
                    if callable(attr):
                        return attr
                    current_obj = attr
                elif isinstance(current_obj, dict) and part in current_obj:
                    current_obj = current_obj[part]
                else:
                    raise ValueError(f"Undefined property: {chain}")
            return current_obj

        return obj

    def _eval_pipeline(self, expr: str, context: TemplateContext) -> Any:
        """Evaluate a pipeline expression."""
        parts = [p.strip() for p in expr.split('|')]

        # First part is the value
        value = self._evaluate(parts[0], context)

        # Apply each function in the pipeline
        for part in parts[1:]:
            # Parse function call: functionName "arg" or functionName
            func_match = re.match(r'^([a-zA-Z_][a-zA-Z0-9_]*)\s*(.*)', part)
            if func_match:
                func_name = func_match.group(1)
                args_str = func_match.group(2).strip()

                args = self._parse_args(args_str, context)

                if func_name in self.BUILTIN_FUNCTIONS:
                    value = self.BUILTIN_FUNCTIONS[func_name]([value] + args)
                elif func_name in self.OUTPUT_FUNCTIONS:
                    value = self.OUTPUT_FUNCTIONS[func_name](value)
                else:
                    raise ValueError(f"Unknown function: {func_name}")

        return value

    def _parse_args(self, args_str: str, context: TemplateContext) -> List[Any]:
        """Parse function arguments."""
        if not args_str:
            return []

        args = []
        in_quotes = False
        quote_char = None
        current = ""
        i = 0

        while i < len(args_str):
            c = args_str[i]

            if c in '"\'':
                if not in_quotes:
                    in_quotes = True
                    quote_char = c
                elif c == quote_char:
                    in_quotes = False
                    quote_char = None
                else:
                    current += c
            elif c == ' ' and not in_quotes:
                if current:
                    args.append(self._evaluate_arg(current, context))
                    current = ""
            else:
                current += c

            i += 1

        if current:
            args.append(self._evaluate_arg(current, context))

        return args

    def _evaluate_arg(self, arg: str, context: TemplateContext) -> Any:
        """Evaluate a single argument."""
        arg = arg.strip()

        # Try literal string
        if (arg.startswith('"') and arg.endswith('"')) or (arg.startswith('`') and arg.endswith('`')):
            return arg[1:-1]

        # Try number
        try:
            if '.' in arg:
                return float(arg)
            return int(arg)
        except ValueError:
            pass

        # Boolean
        if arg == 'true':
            return True
        if arg == 'false':
            return False

        # Variable reference
        if arg.startswith('.'):
            return self._eval_access(arg[1:], context)

        return arg


# Global template engine instance
template_engine = TemplateEngine()


# ============================================================================
# Mock Data Models
# ============================================================================

@dataclass
class HTTPExpect:
    method: str
    path: str
    condition: Optional[str] = None


@dataclass
class ReplyHTTPAction:
    status_code: int
    headers: Dict[str, str] = field(default_factory=dict)
    body: str = ""


@dataclass
class SleepAction:
    duration: float


@dataclass
class Behavior:
    key: str
    kind: str = "Behavior"
    expect: Optional[HTTPExpect] = None
    actions: List[Any] = field(default_factory=list)


@dataclass
class ParsedBehavior:
    """Parsed behavior with compiled regex for path matching."""
    key: str
    kind: str
    expect: HTTPExpect
    actions: List[Any]
    path_pattern: re.Pattern
    param_names: List[str]


# ============================================================================
# Mock Loader
# ============================================================================

class MockLoader:
    """Loads and parses mock definitions from YAML files."""

    def __init__(self, templates_dir: str):
        self.templates_dir = Path(templates_dir)
        self.behaviors: List[ParsedBehavior] = []
        self._seen_keys: Dict[str, Path] = {}

    def load_all(self) -> List[ParsedBehavior]:
        """Load all YAML files from templates directory."""
        if not self.templates_dir.exists():
            log.warning(f"Templates directory does not exist: {self.templates_dir}")
            return []

        yaml_files = list(self.templates_dir.rglob("*.yaml")) + list(self.templates_dir.rglob("*.yml"))

        all_behaviors = []

        for yaml_file in yaml_files:
            try:
                behaviors = self._load_file(yaml_file)
                all_behaviors.extend(behaviors)
            except Exception as e:
                log.warning(f"Failed to load {yaml_file}: {e}")

        # Merge and deduplicate by key (last wins)
        key_to_behavior = {}
        for b in all_behaviors:
            if b.key in self._seen_keys:
                log.warning(f"Duplicate key '{b.key}': overriding previous definition from {self._seen_keys[b.key]}")
            key_to_behavior[b.key] = b
            self._seen_keys[b.key] = yaml_file

        self.behaviors = list(key_to_behavior.values())

        # Sort by load order (maintain insertion order from keys)
        self.behaviors.sort(key=lambda b: list(key_to_behavior.keys()).index(b.key))

        log.info(f"Loaded {len(self.behaviors)} behaviors from {len(yaml_files)} files")
        return self.behaviors

    def _load_file(self, yaml_file: Path) -> List[ParsedBehavior]:
        """Load behaviors from a single YAML file."""
        with open(yaml_file, 'r') as f:
            data = yaml.safe_load(f)

        if not data:
            return []

        behaviors = []

        for item in data:
            behavior = self._parse_behavior(item, yaml_file)
            if behavior:
                behaviors.append(behavior)

        return behaviors

    def _parse_behavior(self, item: Dict, yaml_file: Path) -> Optional[ParsedBehavior]:
        """Parse a single behavior definition."""
        # Validate key
        key = item.get('key')
        if not key or not isinstance(key, str) or not key.strip():
            raise ValueError("Behavior 'key' is required and must be a non-empty string")

        # Default kind is Behavior
        kind = item.get('kind', 'Behavior')

        # Parse expect
        expect_data = item.get('expect', {})
        if not expect_data:
            raise ValueError(f"Behavior '{key}' requires 'expect' field")

        http_data = expect_data.get('http', {})
        method = http_data.get('method', '')
        path = http_data.get('path', '')
        condition = expect_data.get('condition')

        if not method or not path:
            raise ValueError(f"Behavior '{key}' requires 'expect.http.method' and 'expect.http.path'")

        expect = HTTPExpect(method=method.upper(), path=path, condition=condition)

        # Parse actions
        actions = []
        reply_http_count = 0

        for action in item.get('actions', []):
            if 'reply_http' in action:
                reply_http_count += 1
                if reply_http_count > 1:
                    raise ValueError(f"Behavior '{key}' cannot have more than one 'reply_http' action")

                rh_data = action['reply_http']
                status_code = rh_data.get('status_code')
                if status_code is None:
                    raise ValueError(f"Behavior '{key}': 'reply_http' requires 'status_code'")

                headers = rh_data.get('headers', {})
                body = rh_data.get('body', '')

                actions.append(ReplyHTTPAction(
                    status_code=status_code,
                    headers=headers,
                    body=body
                ))

            elif 'sleep' in action:
                sleep_data = action['sleep']
                duration = sleep_data.get('duration')
                if duration is None:
                    raise ValueError(f"Behavior '{key}': 'sleep' requires 'duration'")

                # Parse duration
                duration_seconds = self._parse_duration(duration)
                actions.append(SleepAction(duration=duration_seconds))

        # Compile path pattern
        path_pattern, param_names = self._compile_path_pattern(path)

        return ParsedBehavior(
            key=key,
            kind=kind,
            expect=expect,
            actions=actions,
            path_pattern=path_pattern,
            param_names=param_names
        )

    def _parse_duration(self, duration) -> float:
        """Parse duration string to seconds."""
        if isinstance(duration, (int, float)):
            return float(duration)

        duration = str(duration)
        units = {
            'ns': 1e-9,
            'us': 1e-6,
            'ms': 1e-3,
            's': 1,
            'm': 60,
            'h': 3600,
        }

        for unit, multiplier in units.items():
            if duration.endswith(unit):
                try:
                    return float(duration[:-len(unit)]) * multiplier
                except ValueError:
                    pass

        # Try to parse as raw number
        try:
            return float(duration)
        except ValueError:
            raise ValueError(f"Invalid duration format: {duration}")

    def _compile_path_pattern(self, path: str) -> Tuple[re.Pattern, List[str]]:
        """Compile a path pattern with :param syntax into regex."""
        param_names = []
        pattern_parts = []

        i = 0
        while i < len(path):
            if path[i] == ':' and i + 1 < len(path):
                # Start of parameter
                j = i + 1
                while j < len(path) and path[j] not in '/?':
                    j += 1

                param_name = path[i+1:j]
                param_names.append(param_name)
                pattern_parts.append(f"(?P<{param_name}>[^/?]+)")
                i = j
            else:
                # Escape special regex chars but keep literal match
                if path[i] in '.^$*+?{}[]|\\':
                    pattern_parts.append(re.escape(path[i]))
                else:
                    pattern_parts.append(path[i])
                i += 1

        pattern = '^' + ''.join(pattern_parts) + '$'
        return re.compile(pattern), param_names


# ============================================================================
# HTTP Server
# ============================================================================

class HMockServer:
    """Main mock server."""

    def __init__(self):
        self.loader = MockLoader(TEMPLATES_DIR)
        self.behaviors = self.loader.load_all()
        self.app = FastAPI()
        self._setup_routes()

    def _setup_routes(self):
        """Set up HTTP routes."""
        @self.app.api_route("{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
        async def handle_request(request: Request, path: str):
            return await self._handle_request(request, path)

    async def _handle_request(self, request: Request, path: str) -> Response:
        """Handle incoming HTTP request."""
        method = request.method
        full_path = f"{request.scope.get('root_path', '')}{path}"
        query_string = request.query_params.__dict__.get('_string', str(request.query_params)) or str(request.query_params)

        # Get raw body
        body = await request.body()
        body_str = body.decode('utf-8', errors='replace')

        # Create context for template rendering
        context = TemplateContext(
            headers=request.headers,
            body=body_str,
            path=full_path,
            query_string=query_string
        )

        # Try to find matching behavior
        matched_behavior = None
        path_params = {}

        for behavior in self.behaviors:
            if behavior.expect.method != method:
                continue

            match = behavior.path_pattern.match(full_path)
            if not match:
                continue

            # Extract path parameters
            path_params = match.groupdict()

            # Evaluate condition if present
            if behavior.expect.condition:
                try:
                    condition_result = template_engine.render(behavior.expect.condition, context)
                    if condition_result.strip().lower() != 'true':
                        continue
                except Exception:
                    # Condition rendering failed, skip this behavior
                    continue

            matched_behavior = behavior
            break

        # Log request/response
        if matched_behavior:
            log.info(json.dumps({
                'http_path': full_path,
                'http_method': method,
                'http_host': request.client.host if request.client else 'unknown',
                'http_req': {'method': method, 'path': full_path, 'headers': dict(request.headers)},
                'http_res': {'behavior': matched_behavior.key, 'status': 'pending'}
            }))

            # Execute actions
            response = await self._execute_actions(matched_behavior, context, path_params)

            # Update log with actual response
            log.info(json.dumps({
                'http_path': full_path,
                'http_method': method,
                'http_host': request.client.host if request.client else 'unknown',
                'http_req': {'method': method, 'path': full_path},
                'http_res': {'status_code': response.status_code, 'headers': dict(response.headers.media)}
            }))

            return response
        else:
            # No matching behavior - return 404
            log.info(json.dumps({
                'http_path': full_path,
                'http_method': method,
                'http_host': request.client.host if request.client else 'unknown',
                'http_req': {'method': method, 'path': full_path},
                'http_res': {'status_code': 404}
            }))

            return Response(
                content="not found",
                status_code=404,
                media_type="text/plain"
            )

    async def _execute_actions(
        self,
        behavior: ParsedBehavior,
        context: TemplateContext,
        path_params: Dict[str, str]
    ) -> Response:
        """Execute behavior actions and return response."""
        response_body = ""
        response_headers = {}
        status_code = 200

        # Extend context with path params for template rendering
        class ExtendedContext(TemplateContext):
            def __init__(self, base: TemplateContext, path_params: Dict[str, str]):
                super().__init__(
                    headers=base._headers,
                    body=base._body,
                    path=base._path,
                    query_string=base._query_string
                )
                self._path_params = path_params

            @property
            def HTTPPathParams(self) -> Dict[str, str]:
                return self._path_params

        ext_context = ExtendedContext(context, path_params)

        for action in behavior.actions:
            if isinstance(action, SleepAction):
                await asyncio.sleep(action.duration)

            elif isinstance(action, ReplyHTTPAction):
                # Render body
                try:
                    response_body = template_engine.render(action.body, ext_context)
                except Exception as e:
                    log.warning(f"Failed to render body for behavior {behavior.key}: {e}")
                    response_body = action.body or ""

                # Render headers
                for header_name, header_value in action.headers.items():
                    try:
                        rendered_value = template_engine.render(header_value, ext_context)
                        response_headers[header_name] = rendered_value
                    except Exception as e:
                        log.warning(f"Failed to render header {header_name} for behavior {behavior.key}: {e}")
                        response_headers[header_name] = header_value

                # Set default Content-Type if not provided
                if 'Content-Type' not in response_headers:
                    response_headers['Content-Type'] = 'application/json'

                # Set Content-Length
                response_headers['Content-Length'] = str(len(response_body.encode('utf-8')))

                status_code = action.status_code

        return Response(
            content=response_body,
            status_code=status_code,
            headers=response_headers,
            media_type=response_headers.get('Content-Type', 'application/json')
        )

    def run(self):
        """Start the server."""
        import uvicorn
        log.info(f"Starting HMock server on {HTTP_HOST}:{HTTP_PORT}")
        uvicorn.run(
            self.app,
            host=HTTP_HOST,
            port=HTTP_PORT,
            log_level="warning"  # Use our own logging
        )


# ============================================================================
# Main Entry Point
# ============================================================================

def main():
    """Main entry point."""
    server = HMockServer()
    server.run()


if __name__ == "__main__":
    main()
