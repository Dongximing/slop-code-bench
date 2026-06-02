"""
AST nodes for the pipeline parser.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Union


@dataclass
class ASTNode:
    pass


# Expression nodes
@dataclass
class Literal(ASTNode):
    value: Any


@dataclass
class Identifier(ASTNode):
    name: str


@dataclass
class BinaryOp(ASTNode):
    op: str
    left: ASTNode
    right: ASTNode


@dataclass
class UnaryOp(ASTNode):
    op: str
    operand: ASTNode


@dataclass
class ArrayAccess(ASTNode):
    array: ASTNode
    index: ASTNode


@dataclass
class FunctionCall(ASTNode):
    name: str
    args: List[ASTNode]
    kwargs: Dict[str, ASTNode] = field(default_factory=dict)


@dataclass
class TaskCall(ASTNode):
    name: str
    args: List[ASTNode]
    kwargs: Dict[str, ASTNode] = field(default_factory=dict)


@dataclass
class CachedTaskCall(ASTNode):
    """A task call with dynamic cache configuration overrides."""
    task_call: TaskCall
    variable_name: str
    cache_overrides: Dict[str, Any] = field(default_factory=dict)


@dataclass
class TemplateString(ASTNode):
    parts: List[Union[str, ASTNode]]


@dataclass
class ListLiteral(ASTNode):
    elements: List[ASTNode]


@dataclass
class ParameterAccess(ASTNode):
    param_name: str


@dataclass
class WorkspaceAccess(ASTNode):
    pass


# Statement nodes
@dataclass
class VarDecl(ASTNode):
    var_type: str
    var_name: str
    value: ASTNode
    is_list_element_type: bool = False


@dataclass
class Assignment(ASTNode):
    name: ASTNode
    value: ASTNode


@dataclass
class IfStatement(ASTNode):
    condition: ASTNode
    then_block: List[ASTNode]
    elif_clauses: List[tuple]  # List of (condition, block)
    else_block: Optional[List[ASTNode]]


@dataclass
class ForStatement(ASTNode):
    init: Optional[ASTNode]
    condition: Optional[ASTNode]
    update: Optional[ASTNode]
    body: List[ASTNode]
    is_foreach: bool = False
    var_name: str = ""
    iterable: ASTNode = None


@dataclass
class WhileStatement(ASTNode):
    condition: ASTNode
    body: List[ASTNode]


@dataclass
class ReturnStatement(ASTNode):
    value: Optional[ASTNode]


@dataclass
class BreakStatement(ASTNode):
    pass


@dataclass
class ContinueStatement(ASTNode):
    pass


@dataclass
class ExpressionStatement(ASTNode):
    expression: ASTNode


@dataclass
class FailsTask(ASTNode):
    task_call: TaskCall


# Parameter definition
@dataclass
class ParamDef(ASTNode):
    name: str
    param_type: str
    default_value: Optional[ASTNode]
    is_list_element_type: bool = False


# Cache configuration
@dataclass
class CacheConfig(ASTNode):
    enabled: bool = False
    strategy: str = "content"  # "content", "stale", "always"
    location: str = ".pipe-cache"
    version: Optional[str] = None
    ttl_seconds: Optional[int] = None
    ttl_minutes: Optional[int] = None
    ttl_hours: Optional[int] = None
    ttl_days: Optional[int] = None
    key_include: Optional[List[str]] = None
    key_exclude: Optional[List[str]] = None


# Task definition
@dataclass
class TaskDef(ASTNode):
    name: str
    params: List[ParamDef]
    inputs: Optional[List[str]]
    run: Optional[str]
    success: Dict[str, List[ASTNode]]
    requires: List[ASTNode]
    output: Optional[ASTNode]
    timeout: Optional[float]
    cache: Optional[CacheConfig] = None


# Pipeline
@dataclass
class Pipeline(ASTNode):
    tasks: Dict[str, TaskDef]
