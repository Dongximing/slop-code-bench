"""AST nodes and visitor pattern for circuit expressions."""

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass
class Signal:
    name: str
    msb: int = 0
    lsb: int = 0

    @property
    def width(self) -> int:
        return self.msb - self.lsb + 1


@dataclass
class Identifier:
    name: str


@dataclass
class Literal:
    value: int
    width: int


@dataclass
class Index:
    expr: 'Expr'
    index: int


@dataclass
class Slice:
    expr: 'Expr'
    hi: int
    lo: int


@dataclass
class Concat:
    parts: list


@dataclass
class Call:
    operator: str
    args: list


Expr = (Identifier, Literal, Index, Slice, Concat, Call)


@dataclass
class Assignment:
    lhs: str
    rhs: object
    line: int
    col: int


@dataclass
class Circuit:
    inputs: list = field(default_factory=list)
    outputs: list = field(default_factory=list)
    wires: list = field(default_factory=list)
    assignments: list = field(default_factory=list)

    def get_all_names(self) -> set:
        return {sig.name for sig in self.inputs + self.outputs + self.wires}

    def get_signal(self, name: str):
        for sig in self.inputs + self.outputs + self.wires:
            if sig.name == name:
                return sig
        return None


@runtime_checkable
class ExprVisitor(Protocol):
    def visit_literal(self, expr: Literal): ...
    def visit_identifier(self, expr: Identifier): ...
    def visit_index(self, expr: Index): ...
    def visit_slice(self, expr: Slice): ...
    def visit_concat(self, expr: Concat): ...
    def visit_call(self, expr: Call): ...


def accept(expr, visitor: ExprVisitor):
    if isinstance(expr, Literal):
        return visitor.visit_literal(expr)
    if isinstance(expr, Identifier):
        return visitor.visit_identifier(expr)
    if isinstance(expr, Index):
        return visitor.visit_index(expr)
    if isinstance(expr, Slice):
        return visitor.visit_slice(expr)
    if isinstance(expr, Concat):
        return visitor.visit_concat(expr)
    if isinstance(expr, Call):
        return visitor.visit_call(expr)
    raise TypeError(f"Unknown expression type: {type(expr)}")


def collect_dependencies(expr) -> set:
    if isinstance(expr, Identifier):
        return {expr.name}
    if isinstance(expr, (Index, Slice)):
        return collect_dependencies(expr.expr)
    if isinstance(expr, Concat):
        deps = set()
        for part in expr.parts:
            deps.update(collect_dependencies(part))
        return deps
    if isinstance(expr, Call):
        deps = set()
        for arg in expr.args:
            deps.update(collect_dependencies(arg))
        return deps
    return set()
