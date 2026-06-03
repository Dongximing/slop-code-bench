"""Models for pipeline definitions."""
from dataclasses import dataclass, field
from typing import Optional
from enum import Enum


class ParameterType(Enum):
    STRING = "string"
    INT = "int"
    FLOAT = "float"
    BOOL = "bool"
    LIST = "list"

    @classmethod
    def from_str(cls, s: str) -> Optional['ParameterType']:
        mapping = {
            'string': cls.STRING,
            'int': cls.INT,
            'float': cls.FLOAT,
            'bool': cls.BOOL,
        }
        if s in mapping:
            return mapping[s]
        if s.startswith('list'):
            return cls.LIST
        return None


@dataclass
class Parameter:
    name: str
    type: ParameterType
    default: Optional['Value'] = None
    has_default: bool = False


@dataclass
class Value:
    """Represents a typed value."""
    value: any
    value_type: ParameterType

    @staticmethod
    def from_string(s: str) -> 'Value':
        """Try to parse a string into a typed value."""
        # Try bool
        if s.upper() == 'TRUE':
            return Value(True, ParameterType.BOOL)
        if s.upper() == 'FALSE':
            return Value(False, ParameterType.BOOL)
        # Try int
        try:
            return Value(int(s), ParameterType.INT)
        except ValueError:
            pass
        # Try float
        try:
            return Value(float(s), ParameterType.FLOAT)
        except ValueError:
            pass
        # Default to string
        return Value(s, ParameterType.STRING)

    def to_python_value(self) -> any:
        return self.value


@dataclass
class ExpressionAST:
    """AST node for expressions."""
    type: str  # 'literal', 'identifier', 'operator', 'function', 'control'
    value: any = None
    children: list = field(default_factory=list)
    operator: Optional[str] = None
    true_branch: Optional['ExpressionAST'] = None
    false_branch: Optional['ExpressionAST'] = None
    body: Optional['ExpressionAST'] = None
    init: Optional['ExpressionAST'] = None
    condition: Optional['ExpressionAST'] = None
    update: Optional['ExpressionAST'] = None


@dataclass
class Task:
    name: str
    params: list[Parameter] = field(default_factory=list)
    run: Optional[str] = None
    success: Optional[dict[str, ExpressionAST]] = None
    requires: Optional[ExpressionAST] = None
    output: Optional[str] = None
    timeout: Optional[float] = None
