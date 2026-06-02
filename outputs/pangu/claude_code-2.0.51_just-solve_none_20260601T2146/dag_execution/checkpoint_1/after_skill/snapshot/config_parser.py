"""
TOML config parser.
"""
import toml
from typing import Dict, Tuple, Any
from pipeline_types import Config
from pipeline_parser import ParseError


def parse_config_file(config_path: str) -> Config:
    try:
        with open(config_path, 'r') as f:
            data = toml.load(f)
    except toml.TomlDecodeError as e:
        raise ParseError(f"Invalid TOML syntax: {str(e)}")
    except FileNotFoundError:
        raise ParseError(f"Config file not found: {config_path}")

    config = Config()
    if 'entry' in data:
        config.entry = data['entry']
    if 'clean_cwd' in data:
        config.clean_cwd = data['clean_cwd']
    if 'env' in data:
        if isinstance(data['env'], dict):
            config.env = {k: str(v) for k, v in data['env'].items()}
        else:
            raise ParseError("env must be a table in config")
    return config


def _parse_value(val: str) -> Any:
    """Convert string to appropriate type."""
    val = val.strip()
    if val.isdigit():
        return int(val)
    if val.replace('.', '').isdigit():
        return float(val)
    if val.upper() in ('TRUE', 'FALSE'):
        return val.upper() == 'TRUE'
    if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
        return val[1:-1]
    return val


def parse_entry_task(entry_str: str) -> Tuple[str, Dict[str, Any]]:
    parens_start = entry_str.find('(')
    if parens_start == -1:
        return entry_str.strip(), {}
    if not entry_str.endswith(')'):
        raise ParseError(f"Invalid entry task: unclosed parenthesis in '{entry_str}'")

    task_name = entry_str[:parens_start].strip()
    args_str = entry_str[parens_start + 1:-1]
    if not args_str.strip():
        return task_name, {}

    params = {}
    parts = []
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
            if ch in '([':
                depth += 1
            elif ch in ')]':
                depth -= 1
            elif ch == ',' and depth == 0:
                if current.strip():
                    parts.append(current.strip())
                current = ""
            else:
                current += ch
        else:
            current += ch

    if current.strip():
        parts.append(current.strip())

    for part in parts:
        if '=' in part:
            key, val = part.split('=', 1)
            params[key.strip()] = _parse_value(val)
        else:
            params[len(params)] = _parse_value(part)

    return task_name, params
