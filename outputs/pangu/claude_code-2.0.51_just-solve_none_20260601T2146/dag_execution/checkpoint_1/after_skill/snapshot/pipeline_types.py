"""
Data structures for pipeline tasks and parameters.
"""
import json
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any
from enum import Enum


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

    # Type parser dispatcher - eliminates long if-elif chains
    _PARSERS = {
        "string": lambda v: v.strip().strip('"').strip("'"),
        "int": lambda v: int(v.strip()),
        "float": lambda v: float(v.strip()),
        "bool": lambda v: v.strip().upper() == "TRUE",
    }

    @classmethod
    def from_def(cls, name: str, type_str: str, default_value: Optional[str] = None) -> 'Parameter':
        if type_str.startswith("list["):
            param_type = ParamType.LIST
            inner_type = type_str[5:-1]
        else:
            param_type = {"string": ParamType.STRING, "int": ParamType.INT,
                         "float": ParamType.FLOAT, "bool": ParamType.BOOL}.get(type_str)
            if param_type is None:
                raise ValueError(f"Unknown parameter type: {type_str}")

        parsed = None
        if default_value is not None:
            parsed = cls._parse_default(default_value.strip().strip('"').strip("'"),
                                       param_type, type_str)

        return cls(name=name, param_type=param_type, default_value=parsed,
                   has_default=default_value is not None)

    @staticmethod
    def _parse_list(value: str, inner_type: str) -> List[Any]:
        """Parse list values with dispatcher pattern."""
        items = [item.strip() for item in value.strip("[]").split(",") if item.strip()]
        parser = {"string": str, "int": int, "float": float,
                  "bool": lambda v: v.upper() == "TRUE"}.get(inner_type)
        return [parser(item) for item in items] if parser else items

    @staticmethod
    def _parse_default(value: str, param_type: ParamType, type_str: str) -> Any:
        if param_type == ParamType.LIST:
            return Parameter._parse_list(value, type_str[5:-1])
        parser = Parameter._PARSERS.get(param_type.value)
        return parser(value) if parser else value

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
        clean = lambda s: s.replace("\u2018", "'").replace("\u2019", "'")
        return json.dumps(dict(
            job_id=self.job_id, task=self.task, params=self.params,
            stdout=clean(self.stdout), stderr=clean(self.stderr),
            timed_out=self.timed_out, success=self.success,
            output=self.output, exit_code=self.exit_code,
            parent=self.parent, duration=self.duration
        ))

@dataclass
class Config:
    entry: Optional[str] = None
    clean_cwd: bool = False
    env: Dict[str, str] = field(default_factory=dict)
