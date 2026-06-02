"""
Pipeline file parser.
"""
import re
from typing import List, Dict, Optional, Any
from pipeline_types import Task, Parameter, SuccessCriterion, ParamType
from config_parser import _parse_value


class ParseError(Exception):
    """Syntax error in pipeline file."""
    def __init__(self, message: str, position: int = -1, line: int = -1):
        msg = f"SYNTAX_ERROR:{message}"
        if position != -1:
            msg += f" at position {position}"
        if line != -1:
            msg += f" (line {line})"
        super().__init__(msg)
        self.message = message


def _find_matching_brace(text: str, start: int) -> int:
    if text[start] != '{':
        raise ParseError("Expected '{'")
    depth, pos = 1, start + 1
    while pos < len(text) and depth > 0:
        if text[pos] == '{':
            depth += 1
        elif text[pos] == '}':
            depth -= 1
        pos += 1
    if depth != 0:
        raise ParseError("Unclosed block")
    return pos - 1


def _strip_value(s: str) -> str:
    s = s.strip()
    if s and ((s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'"))):
        return s[1:-1]
    return s


def _split_comma_separated(text: str) -> List[str]:
    """Split by commas outside of quotes."""
    parts = []
    current = ""
    in_quotes = False
    quote_char = None

    for ch in text:
        if ch in ('"', "'") and (not in_quotes or quote_char == ch):
            in_quotes = not in_quotes
            quote_char = ch if in_quotes else None
            current += ch
        elif ch == ',' and not in_quotes:
            if current.strip():
                parts.append(current.strip())
            current = ""
        else:
            current += ch
    if current.strip():
        parts.append(current.strip())
    return parts


def parse_pipeline_file(content: str) -> List[Task]:
    # Remove comments
    content = ''.join(line[:line.index('//')] if '//' in line else line for line in content.split('\n'))

    tasks = []
    task_pattern = r'\btask\s+([a-zA-Z_][a-zA-Z0-9_]*)\s*\{'
    pos = 0

    while match := re.search(task_pattern, content[pos:]):
        task_start = pos + match.start()
        task_name = match.group(1)
        brace_start = task_start + match.end() - 1  # position of '{'
        brace_end = _find_matching_brace(content, brace_start)
        task_text = content[brace_start + 1:brace_end].strip()

        try:
            tasks.append(parse_task_body(task_name, task_text))
        except ParseError as e:
            raise ParseError(f"Error parsing task '{task_name}': {e.message}")
        pos = brace_end + 1

    return tasks


def parse_task_body(name: str, body: str) -> Task:
    task = Task(name=name)
    blocks = _extract_blocks(body)

    if 'params' in blocks:
        task.params = parse_params(blocks['params'])
    if 'run' in blocks:
        task.run = blocks['run'].strip()
    if 'success' in blocks:
        task.success = parse_success(blocks['success'])
    if 'requires' in blocks:
        task.requires = blocks['requires'].strip()
    if 'output' in blocks:
        task.output = _strip_value(blocks['output'])
    if 'timeout' in blocks:
        task.timeout = float(blocks['timeout'].strip())

    return task


def _extract_blocks(body: str) -> Dict[str, str]:
    """Extract named blocks from body text."""
    blocks = {}
    keywords = ['params:', 'run:', 'success:', 'requires:', 'output:', 'timeout:']
    pos = 0

    while pos < len(body):
        while pos < len(body) and body[pos].isspace():
            pos += 1
        if pos >= len(body):
            break

        matched = False
        for kw in keywords:
            if body.startswith(kw, pos):
                block_name = kw.rstrip(':')
                pos += len(kw)
                while pos < len(body) and body[pos].isspace():
                    pos += 1

                if block_name in ('params', 'run', 'success', 'requires'):
                    if pos < len(body) and body[pos] == '{':
                        end = _find_matching_brace(body, pos)
                        blocks[block_name] = body[pos + 1:end].strip()
                        pos = end + 1
                else:
                    end = pos
                    while end < len(body) and body[end] not in ('\n', ';'):
                        end += 1
                    blocks[block_name] = body[pos:end].strip()
                    pos = end
                matched = True
                break
        if not matched:
            pos += 1

    return blocks


def parse_params(text: str) -> List[Parameter]:
    params = []
    for segment in _split_comma_separated(text):
        if ':' not in segment:
            raise ParseError(f"Invalid parameter definition: {segment}")

        name, type_and_default = segment.split(':', 1)
        name = name.strip()
        type_str = type_and_default.strip()

        default_value = None
        has_default = False
        if '=' in type_str:
            parts = type_str.split('=', 1)
            type_str = parts[0].strip()
            default_str = parts[1].strip().rstrip(';')
            default_value = _parse_value(default_str)
            has_default = True

        type_str = type_str.rstrip(';').strip()

        if type_str == 'list':
            match = re.search(r'list\s*\[([a-zA-Z_][a-zA-Z0-9_]*)\]', segment)
            if match:
                params.append(Parameter(name=name, param_type=ParamType.LIST,
                                       default_value=default_value, has_default=has_default))
                continue

        type_map = {'string': ParamType.STRING, 'int': ParamType.INT,
                   'float': ParamType.FLOAT, 'bool': ParamType.BOOL}
        if type_str not in type_map:
            raise ParseError(f"Unknown parameter type: {type_str}")
        params.append(Parameter(name=name, param_type=type_map[type_str],
                               default_value=default_value, has_default=has_default))

    return params


def parse_success(text: str) -> List[SuccessCriterion]:
    """Parse success criteria block."""
    criteria = []
    pos = 0

    while pos < len(text):
        while pos < len(text) and text[pos].isspace():
            pos += 1
        if pos >= len(text):
            break

        name_match = re.match(r'([a-zA-Z_][a-zA-Z0-9_]*)\s*:', text[pos:])
        if name_match:
            name = name_match.group(1)
            colon_pos = pos + name_match.end()
            while colon_pos < len(text) and text[colon_pos].isspace():
                colon_pos += 1
            if colon_pos < len(text) and text[colon_pos] == '{':
                brace_end = _find_matching_brace(text, colon_pos)
                criteria.append(SuccessCriterion(name, text[colon_pos + 1:brace_end].strip()))
                pos = brace_end + 1
            else:
                pos += 1
        else:
            pos += 1

    return criteria


def parse_call_args(args_str: str) -> Dict[str, Any]:
    """Parse arguments from a task call."""
    args = {}
    if not args_str.strip():
        return args

    current = ""
    in_quotes = False
    quote_char = None
    depth = 0

    for ch in args_str:
        if ch in ('"', "'") and (not in_quotes or quote_char == ch):
            in_quotes = not in_quotes
            quote_char = ch if in_quotes else None
            current += ch
        elif not in_quotes:
            if ch == '(':
                depth += 1
            elif ch == ')':
                depth -= 1
            elif ch == ',' and depth == 0:
                if current.strip():
                    _add_arg(current.strip(), args)
                current = ""
            else:
                current += ch
        else:
            current += ch

    if current.strip():
        _add_arg(current.strip(), args)
    return args


def _add_arg(arg: str, args: Dict):
    """Add a parsed argument to the args dict."""
    arg = arg.strip()
    if '=' in arg:
        key, val = arg.split('=', 1)
        key, val = key.strip(), val.strip()
        if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
            val = val[1:-1]
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
