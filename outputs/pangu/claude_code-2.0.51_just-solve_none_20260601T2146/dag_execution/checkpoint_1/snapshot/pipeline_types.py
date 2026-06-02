"""
Data structures for pipeline tasks and parameters.
"""
from dataclasses import dataclass, field
from typing import Optional, Dict, List, Any, Union
from enum import Enum
import json
import os

class ParamType(Enum):
    STRING = "string"
    INT = "int"
    FLOAT = "float"
    BOOL = "bool"
    LIST = "list"

@dataclass
class Parameter:
    name: str
    param_type: ParamType
    default_value: Optional[Any] = None
    has_default: bool = False

    @classmethod
    def from_def(cls, name: str, type_str: str, default_value: Optional[str] = None) -> 'Parameter':
        type_map = {
            "string": ParamType.STRING,
            "int": ParamType.INT,
            "float": ParamType.FLOAT,
            "bool": ParamType.BOOL,
        }

        if type_str.startswith("list["):
            param_type = ParamType.LIST
            inner_type = type_str[5:-1]  # extract type inside []
        else:
            param_type = type_map.get(type_str)
            if param_type is None:
                raise ValueError(f"Unknown parameter type: {type_str}")

        parsed_default = None
        has_default = default_value is not None

        if has_default:
            parsed_default = cls._parse_default(default_value, param_type, type_str)

        return cls(name=name, param_type=param_type, default_value=parsed_default, has_default=has_default)

    @staticmethod
    def _parse_default(value: str, param_type: ParamType, type_str: str) -> Any:
        value = value.strip().strip('"').strip("'")

        if param_type == ParamType.STRING:
            return value
        elif param_type == ParamType.INT:
            return int(value)
        elif param_type == ParamType.FLOAT:
            return float(value)
        elif param_type == ParamType.BOOL:
            return value.upper() == "TRUE"
        elif param_type == ParamType.LIST:
            # Parse list like [value1, value2, value3]
            inner = type_str[5:-1]
            if inner == "string":
                # Remove brackets and split by comma
                items = value.strip("[]").split(",")
                return [item.strip().strip('"').strip("'") for item in items if item.strip()]
            elif inner == "int":
                items = value.strip("[]").split(",")
                return [int(item.strip()) for item in items if item.strip()]
            elif inner == "float":
                items = value.strip("[]").split(",")
                return [float(item.strip()) for item in items if item.strip()]
            elif inner == "bool":
                items = value.strip("[]").split(",")
                return [item.strip().upper() == "TRUE" for item in items if item.strip()]
        return value

@dataclass
class SuccessCriterion:
    name: str
    expression: str

@dataclass
class Task:
    name: str
    params: List[Parameter] = field(default_factory=list)
    run: Optional[str] = None
    success: List[SuccessCriterion] = field(default_factory=list)
    requires: Optional[str] = None
    output: Optional[str] = None
    timeout: Optional[float] = None

    # Resolved fields
    resolved_params: Dict[str, Any] = field(default_factory=dict, repr=False)
    job_id: Optional[int] = None
    parent: Optional[int] = None

@dataclass
class JobResult:
    job_id: int
    task: str
    params: Dict[str, Any]
    stdout: str
    stderr: str
    timed_out: bool
    success: Dict[str, bool]
    output: Optional[str]
    exit_code: int
    parent: Optional[int]
    duration: float

    def to_jsonl(self) -> str:
        # Cleanup: \u2018 → '\n        stdout_clean = self.stdout.replace("\u2018", "'").replace("\u2019", "'")
        stderr_clean = self.stderr.replace("\u2018", "'").replace("\u2019", "'")

        return json.dumps({
            "job_id": self.job_id,
            "task": self.task,
            "params": self.params,
            "stdout": stdout_clean,
            "stderr": stderr_clean,
            "timed_out": self.timed_out,
            "success": self.success,
            "output": self.output,
            "exit_code": self.exit_code,
            "parent": self.parent,
            "duration": self.duration
        })

@dataclass
class Config:
    entry: Optional[str] = None
    clean_cwd: bool = False
    env: Dict[str, str] = field(default_factory=dict)
