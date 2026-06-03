"""
AST node definitions for the pipeline DSL.
"""

from dataclasses import dataclass, field
from typing import List, Optional, Any, Dict


# ============ Parameter Nodes ============

@dataclass
class Parameter:
    name: str
    type_name: str  # 'string', 'int', 'float', 'bool', 'list[T]'
    inner_type: Optional[str] = None  # For list[T], the T
    default_value: Any = None
    has_default: bool = False
    env_var: Optional[str] = None  # If default comes from env var


# ============ Cache Configuration ============

@dataclass
class CacheTTL:
    seconds: Optional[int] = None
    minutes: Optional[int] = None
    hours: Optional[int] = None
    days: Optional[int] = None


@dataclass
class CacheKey:
    include: Optional[List[str]] = None
    exclude: Optional[List[str]] = None


@dataclass
class CacheConfig:
    enabled: bool = False
    strategy: str = "content"  # "content", "stale", "always"
    location: str = ".pipe-cache"
    version: Optional[str] = None
    ttl: Optional[CacheTTL] = None
    key: Optional[CacheKey] = None


# ============ Expression Nodes ============

@dataclass
class Expr:
    pass

@dataclass
class IntLiteral(Expr):
    value: int

@dataclass
class FloatLiteral(Expr):
    value: float

@dataclass
class StringLiteral(Expr):
    value: str

@dataclass
class BoolLiteral(Expr):
    value: bool

@dataclass
class ListLiteral(Expr):
    elements: List[Expr]

@dataclass
class IdentifierExpr(Expr):
    name: str

@dataclass
class ArrayIndexExpr(Expr):
    array: Expr
    index: Expr

@dataclass
class DollarVar(Expr):
    value: str  # e.g. "${params.x}" or "$VAR"

@dataclass
class BinaryExpr(Expr):
    op: str  # '+', '-', '*', '/', '%', '==', '!=', '<', '>', '<=', '>=', '&&', '||'
    left: Expr
    right: Expr

@dataclass
class UnaryExpr(Expr):
    op: str  # '!', '-'
    operand: Expr

@dataclass
class FunctionCallExpr(Expr):
    name: str
    args: List[Expr]

@dataclass
class AssignmentExpr(Expr):
    type_name: Optional[str]  # None for simple assignment, type name for declaration
    name: str
    value: Expr


# ============ Statement Nodes ============

@dataclass
class Stmt:
    pass

@dataclass
class ExprStmt(Stmt):
    expr: Expr

@dataclass
class ReturnStmt(Stmt):
    value: Expr

@dataclass
class BreakStmt(Stmt):
    pass

@dataclass
class ContinueStmt(Stmt):
    pass

@dataclass
class BlockStmt(Stmt):
    statements: List[Stmt]

@dataclass
class IfStmt(Stmt):
    condition: Expr
    then_block: BlockStmt
    elif_clauses: List[tuple]  # List of (condition, block)
    else_block: Optional[BlockStmt]

@dataclass
class ForStmt(Stmt):
    init: Optional[Stmt]
    condition: Optional[Expr]
    update: Optional[Stmt]
    body: BlockStmt

@dataclass
class WhileStmt(Stmt):
    condition: Expr
    body: BlockStmt

@dataclass
class TaskCallStmt(Stmt):
    task_name: str
    positional_args: List[Expr]
    named_args: Dict[str, Expr]

@dataclass
class FailsStmt(Stmt):
    task_call: TaskCallStmt


# ============ Task Definition ============

@dataclass
class TaskDef:
    name: str
    params: List[Parameter]
    run_block: Optional[str]  # Raw command string
    success_block: Optional[Dict[str, 'SuccessBlock']]  # name -> block of statements
    requires_block: Optional[List[Stmt]]
    output: Optional[Expr]
    timeout: Optional[float]
    inputs: Optional[List[Expr]] = None  # List of input file patterns
    cache: Optional[CacheConfig] = None  # Cache configuration


@dataclass
class SuccessBlock:
    statements: List[Stmt]
    is_multiline: bool  # True if block has multiple statements/return


# ============ Pipeline ============

@dataclass
class Pipeline:
    tasks: Dict[str, TaskDef]
