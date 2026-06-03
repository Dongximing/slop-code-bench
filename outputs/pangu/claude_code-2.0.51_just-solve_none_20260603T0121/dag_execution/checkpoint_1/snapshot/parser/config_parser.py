"""Parse TOML configuration files."""
import os
import tomli
from dataclasses import dataclass
from typing import Optional
import sys


@dataclass
class Config:
    entry: Optional[str] = None
    clean_cwd: bool = False
    env: dict = None

    def __post_init__(self):
        if self.env is None:
            self.env = {}


class ConfigParser:
    def parse(self, config_path: str) -> Config:
        try:
            with open(config_path, 'r') as f:
                data = tomli.load(f)
        except tomli.TOMLDecodeError as e:
            print(f"SYNTAX_ERROR: Invalid TOML: {e}", file=sys.stderr)
            sys.exit(2)
        except OSError as e:
            print(f"SYNTAX_ERROR: Cannot read config file: {e}", file=sys.stderr)
            sys.exit(2)

        config = Config()

        # Parse entry task
        if 'entry' in data:
            entry = data['entry']
            if isinstance(entry, str):
                config.entry = entry
            else:
                print(f"SYNTAX_ERROR: entry must be a string", file=sys.stderr)
                sys.exit(2)

        # Parse clean_cwd
        if 'clean_cwd' in data:
            if isinstance(data['clean_cwd'], bool):
                config.clean_cwd = data['clean_cwd']
            else:
                print(f"SYNTAX_ERROR: clean_cwd must be a boolean", file=sys.stderr)
                sys.exit(2)

        # Parse env
        if 'env' in data:
            if isinstance(data['env'], dict):
                config.env = dict(data['env'])
            else:
                print(f"SYNTAX_ERROR: env must be a table", file=sys.stderr)
                sys.exit(2)

        return config
