"""
Pipeline file parser - simplified robust approach.
"""
import re
from typing import List, Dict, Optional, Tuple, Any
from pipeline_types import Task, Parameter, SuccessCriterion, ParamType


class ParseError(Exception):
    """Syntax error in pipeline file."""

    def __init__(self, message: str, position: int = -1, line: int = -1):
        full_msg = f"SYNTAX_ERROR:{message}"
        if position != -1:
            full_msg += f" at position {position}"
        if line != -1:
            full_msg += f" (line {line})"
        super().__init__(full_msg)
        self.message = message


def find_matching_brace(text: str, start: int) -> int:
    """Find the matching closing brace for the opening brace at start position."""
    if text[start] != '{':
        raise ParseError("Expected '{'")

    depth = 1
    pos = start + 1
    while pos < len(text) and depth > 0:
        if text[pos] == '{':
            depth += 1
        elif text[pos] == '}':
            depth -= 1
        pos += 1

    if depth != 0:
        raise ParseError("Unclosed block")

    return pos - 1


def strip_value(s: str) -> str:
    """Strip and unquote a string value."""
    s = s.strip()
    if s and ((s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'"))):
        s = s[1:-1]
    return s


def parse_pipeline_file(content: str) -> List[Task]:
    """Parse a pipeline file and return the list of tasks."""
    # Remove comments
    lines = content.split('\n')
    cleaned_lines = []
    for line in lines:
        if '//' in line:
            line = line[:line.index('//')]
        cleaned_lines.append(line)
    content = '\n'.join(cleaned_lines)

    tasks = []

    # Pattern: task <name> { content }
    task_pattern = r'\btask\s+([a-zA-Z_][a-zA-Z0-9_]*)\s*\{'

    pos = 0
    while pos < len(content):
        match = re.search(task_pattern, content[pos:])
        if not match:
            break

        task_start = pos + match.start()
        task_name = match.group(1)
        brace_start = task_start + match.end() - 1
        brace_end = find_matching_brace(content, brace_start)

        task_text = content[brace_start + 1:brace_end].strip()

        try:
            task = parse_task_body(task_name, task_text)
            tasks.append(task)
        except ParseError as e:
            raise ParseError(f"Error parsing task '{task_name}': {e.message}")

        pos = brace_end + 1

    return tasks


def parse_task_body(name: str, body: str) -> Task:
    """Parse a task body."""
    task = Task(name=name)

    # Split body into blocks using braces tracking
    blocks = extract_blocks(body)

    for block_name, block_content in blocks.items():
        if block_name == 'params':
            task.params = parse_params(block_content)
        elif block_name == 'run':
            task.run = block_content.strip()
        elif block_name == 'success':
            task.success = parse_success(block_content)
        elif block_name == 'requires':
            task.requires = block_content.strip()
        elif block_name == 'output':
            task.output = strip_value(block_content)
        elif block_name == 'timeout':
            task.timeout = float(block_content.strip())

    return task


def extract_blocks(body: str) -> Dict[str, str]:
    """Extract named blocks from a body text."""
    blocks = {}
    pos = 0

    while pos < len(body):
        # Skip whitespace
        while pos < len(body) and body[pos].isspace():
            pos += 1

        if pos >= len(body):
            break

        # Look for a block name followed by colon
        for keyword in ['params:', 'run:', 'success:', 'requires:', 'output:', 'timeout:']:
            if body.startswith(keyword, pos):
                block_name = keyword.rstrip(':')
                pos += len(keyword)

                # Skip whitespace
                while pos < len(body) and body[pos].isspace():
                    pos += 1

                # Extract content based on syntax
                if block_name in ['params', 'run', 'success', 'requires']:
                    # These have braces
                    if pos < len(body) and body[pos] == '{':
                        content_end = find_matching_brace(body, pos)
                        content = body[pos + 1:content_end].strip()
                        blocks[block_name] = content
                        pos = content_end + 1
                    else:
                        # Check for shorthand run without braces: run { ... }
                        while pos < len(body) and body[pos].isspace():
                            pos += 1
                        if pos < len(body) and body[pos] == '{':
                            content_end = find_matching_brace(body, pos)
                            content = body[pos + 1:content_end].strip()
                            blocks[block_name] = content
                            pos = content_end + 1
                elif block_name == 'output':
                    # output: "value"
                    end_pos = pos
                    while end_pos < len(body) and body[end_pos] not in ('\n', ';'):
                        end_pos += 1
                    content = body[pos:end_pos].strip()
                    blocks[block_name] = content
                    pos = end_pos
                elif block_name == 'timeout':
                    # timeout: 60.5
                    end_pos = pos
                    while end_pos < len(body) and body[end_pos] not in ('\n', ';'):
                        end_pos += 1
                    content = body[pos:end_pos].strip()
                    blocks[block_name] = content
                    pos = end_pos

                break
        else:
            pos += 1

    return blocks


def parse_params(text: str) -> List[Parameter]:
    """Parse parameter definitions."""
    params = []

    # Split by semicolons or newlines, respecting quoted strings
    segments = []
    current = ""
    in_quotes = False
    quote_char = None

    for ch in text:
        if ch in ('"', "'") and (not in_quotes or quote_char == ch):
            in_quotes = not in_quotes
            if in_quotes:
                quote_char = ch
            else:
                quote_char = None
            current += ch
        elif ch == ';' and not in_quotes:
            if current.strip():
                segments.append(current.strip())
            current = ""
        else:
            current += ch

    if current.strip():
        segments.append(current.strip())

    # Actually, let's use comma splitting since it looks like: a: int, b: string = "default"
    segments = []
    current = ""

    for ch in text:
        if ch == ',' and not any([in_quotes]):
            if current.strip():
                segments.append(current.strip())
            current = ""
        else:
            current += ch
    if current.strip():
        segments.append(current.strip())

    # Let's re-implement with simple comma splitting
    # Pattern: <name>: <type> [= <default-value>];
    segments = []
    current = ""
    in_quotes = False
    quote_char = None

    for ch in text:
        if ch in ('"', "'") and (not in_quotes or quote_char == ch):
            in_quotes = not in_quotes
            if in_quotes:
                quote_char = ch
            else:
                quote_char = None
            current += ch
        elif ch == ',' and not in_quotes:
            if current.strip():
                segments.append(current.strip())
            current = ""
        else:
            current += ch

    if current.strip():
        segments.append(current.strip())

    for segment in segments:
        if not segment.strip():
            continue

        # Parse: param_name: type [= default]
        # Split at the first ':'
        if ':' not in segment:
            raise ParseError(f"Invalid parameter definition: {segment}")

        name_part, type_and_default = segment.split(':', 1)
        name = name_part.strip()

        # Split type and default
        type_str = type_and_default.strip()
        default_value = None
        has_default = False

        if '=' in type_str:
            parts = type_str.split('=', 1)
            type_str = parts[0].strip()
            default_str = parts[1].strip().rstrip(';')
            default_value = parse_default_value(default_str)
            has_default = True

        # Remove trailing semicolon if present
        type_str = type_str.rstrip(';').strip()

        # Handle list types
        if type_str == 'list':
            # Check for [...] after
            match = re.search(r'list\s*\[([a-zA-Z_][a-zA-Z0-9_]*)\]', segment)
            if match:
                inner_type = match.group(1)
                param_type = ParamType.LIST
                param = Parameter(name=name, param_type=param_type, default_value=default_value, has_default=has_default)
                params.append(param)
                continue

        # Regular types
        type_map = {
            'string': ParamType.STRING,
            'int': ParamType.INT,
            'float': ParamType.FLOAT,
            'bool': ParamType.BOOL,
        }

        if type_str not in type_map:
            raise ParseError(f"Unknown parameter type: {type_str}")

        param = Parameter(name=name, param_type=type_map[type_str], default_value=default_value, has_default=has_default)
        params.append(param)

    return params


def parse_default_value(value_str: str) -> Any:
    """Parse a default value string into its Python representation."""
    value_str = value_str.strip()

    # Try boolean
    if value_str.upper() == 'TRUE':
        return True
    if value_str.upper() == 'FALSE':
        return False

    # Try quoted strings
    if value_str.startswith('"') and value_str.endswith('"'):
        return value_str[1:-1]
    if value_str.startswith("'") and value_str.endswith("'"):
        return value_str[1:-1]

    # Try integer
    if value_str.isdigit() or (value_str.startswith('-') and value_str[1:].isdigit()):
        return int(value_str)

    # Try float
    try:
        f = float(value_str)
        if f == float('inf') or f == float('-inf') or value_str == 'nan':
            return f
        return f
    except ValueError:
        pass

    return value_str


def parse_success(text: str) -> List[SuccessCriterion]:
    """Parse success criteria block."""
    criteria = []

    pos = 0
    text_len = len(text)

    while pos < text_len:
        # Skip whitespace
        while pos < text_len and text[pos].isspace():
            pos += 1

        if pos >= text_len:
            break

        # Look for: <name>: { <expression> }
        # Find the name
        name_match = re.match(r'([a-zA-Z_][a-zA-Z0-9_]*)\s*:', text[pos:])
        if name_match:
            name = name_match.group(1)
            colon_pos = pos + name_match.end()

            # Skip whitespace after colon
            while colon_pos < text_len and text[colon_pos].isspace():
                colon_pos += 1

            if colon_pos < text_len and text[colon_pos] == '{':
                brace_end = find_matching_brace(text, colon_pos)
                expr = text[colon_pos + 1:brace_end].strip()
                criteria.append(SuccessCriterion(name, expr))
                pos = brace_end + 1
            else:
                pos += 1
        else:
            pos += 1

    return criteria


def parse_call_args(args_str: str) -> Dict[str, Any]:
    """Parse arguments from a task call like `1, x=2` or `name=value`."""
    args = {}

    if not args_str.strip():
        return args

    # Simple comma splitting
    current = ""
    in_quotes = False
    quote_char = None
    depth = 0  # For nested ()

    for ch in args_str:
        if ch in ('"', "'") and (not in_quotes or quote_char == ch):
            in_quotes = not in_quotes
            if in_quotes:
                quote_char = ch
            else:
                quote_char = None
            current += ch
        elif not in_quotes:
            if ch == '(':
                depth += 1
                current += ch
            elif ch == ')':
                depth -= 1
                current += ch
            elif ch == ',' and depth == 0:
                if current.strip():
                    add_arg(current.strip(), args)
                current = ""
            else:
                current += ch
        else:
            current += ch

    if current.strip():
        add_arg(current.strip(), args)

    return args


def add_arg(arg: str, args: Dict):
    """Add a parsed argument to the args dict."""
    arg = arg.strip()

    if '=' in arg:
        key, val = arg.split('=', 1)
        key = key.strip()
        val = val.strip()

        # Remove quotes
        if val.startswith('"') and val.endswith('"'):
            val = val[1:-1]
        elif val.startswith("'") and val.endswith("'"):
            val = val[1:-1]

        # Type inference
        if val.isdigit():
            val = int(val)
        elif val.replace('.', '').isdigit():
            try:
                val = float(val)
            except ValueError:
                pass
        elif val.upper() in ('TRUE', 'FALSE'):
            val = val.upper() == 'TRUE'

        args[key] = val
    else:
        # Positional argument
        val = arg
        if val.isdigit():
            val = int(val)
        elif val.replace('.', '').isdigit():
            try:
                val = float(val)
            except ValueError:
                pass
        elif val.upper() in ('TRUE', 'FALSE'):
            val = val.upper() == 'TRUE'

        args[len(args)] = val
