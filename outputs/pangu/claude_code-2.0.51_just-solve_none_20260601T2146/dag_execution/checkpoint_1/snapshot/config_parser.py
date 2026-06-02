"""
TOML config parser.
"""
import toml
from typing import Dict, Optional, Tuple, Any, List
from pipeline_types import Config
from pipeline_parser import ParseError

def parse_config_file(config_path: str) -> Config:
    """Parse a TOML config file."""
    try:
        with open(config_path, 'r') as f:
            data = toml.load(f)
    except toml.TomlDecodeError as e:
        raise ParseError(f"Invalid TOML syntax: {str(e)}")
    except FileNotFoundError:
        raise ParseError(f"Config file not found: {config_path}")

    config = Config()

    # Parse entry task
    if 'entry' in data:
        entry = data['entry']
        config.entry = entry

    # Parse clean_cwd
    if 'clean_cwd' in data:
        config.clean_cwd = data['clean_cwd']

    # Parse environment variables
    if 'env' in data:
        if isinstance(data['env'], dict):
            config.env = {k: str(v) for k, v in data['env'].items()}
        else:
            raise ParseError("env must be a table in config")

    return config


def parse_entry_task(entry_str: str) -> Tuple[str, Dict[str, Any]]:
    """
    Parse an entry task string like 'main' or 'main(1, x=2)'.
    Returns (task_name, params_dict).
    """
    # Check if task call has parameters
    parens_start = entry_str.find('(')
    if parens_start == -1:
        return entry_str.strip(), {}

    if not entry_str.endswith(')'):
        raise ParseError(f"Invalid entry task: unclosed parenthesis in '{entry_str}'")

    task_name = entry_str[:parens_start].strip()
    args_str = entry_str[parens_start + 1:-1]

    if not args_str.strip():
        return task_name, {}

    # Parse arguments
    params = {}
    parts = []
    current = ""
    in_quotes = False
    quote_char = None
    depth = 0

    for ch in args_str:
        if ch in ('"', "'") and (not in_quotes or quote_char == ch):
            in_quotes = not in_quotes
            if in_quotes:
                quote_char = ch
            else:
                quote_char = None
            current += ch
        elif not in_quotes:
            if ch == '(' or ch == '[':
                depth += 1
                current += ch
            elif ch == ')' or ch == ']':
                depth -= 1
                current += ch
            elif ch == ',' and depth == 0:
                parts.append(current.strip())
                current = ""
            else:
                current += ch
        else:
            current += ch

    if current.strip():
        parts.append(current.strip())

    for part in parts:
        part = part.strip()
        if not part:
            continue

        if '=' in part:
            # Named argument
            key, val = part.split('=', 1)
            key = key.strip()
            val = val.strip()
            # Remove quotes if present
            if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
                val = val[1:-1]
            # Try to parse value
            if val.isdigit():
                val = int(val)
            elif val.replace('.', '').isdigit():
                val = float(val)
            elif val.upper() in ('TRUE', 'FALSE'):
                val = val.upper() == 'TRUE'
            params[key] = val
        else:
            # Positional argument
            val = part
            if val.isdigit():
                val = int(val)
            elif val.replace('.', '').isdigit():
                val = float(val)
            elif val.upper() in ('TRUE', 'FALSE'):
                val = val.upper() == 'TRUE'
            params[len(params)] = val

    return task_name, params
