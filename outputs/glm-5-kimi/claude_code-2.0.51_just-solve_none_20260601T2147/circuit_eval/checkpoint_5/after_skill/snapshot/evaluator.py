"""Circuit evaluator using 3-valued logic."""

from ast_nodes import Circuit, Literal, Identifier, Index, Slice, Concat, Call, ExprVisitor, accept
from trivalue import TriValue
from operators import apply_operator, _WIDTH_PROPAGATE, _SELECT, _WIDTH_1_RESULT


def _all_signal_widths(circuit: Circuit) -> dict:
    return {sig.name: sig.width for sig in circuit.inputs + circuit.outputs + circuit.wires}


def expr_width(expr, signal_widths: dict) -> int:
    return accept(expr, _WidthVisitor(signal_widths))


class _WidthVisitor(ExprVisitor):
    def __init__(self, signal_widths: dict):
        self.signal_widths = signal_widths

    def visit_literal(self, expr: Literal):
        return expr.width

    def visit_identifier(self, expr: Identifier):
        return self.signal_widths[expr.name]

    def visit_index(self, expr: Index):
        return 1

    def visit_slice(self, expr: Slice):
        return expr.hi - expr.lo + 1

    def visit_concat(self, expr: Concat):
        return sum(accept(p, self) for p in expr.parts)

    def visit_call(self, expr: Call):
        arg_widths = [accept(arg, self) for arg in expr.args]
        from operators import operator_width
        return operator_width(expr.operator, arg_widths)


class Evaluator:
    def __init__(self, circuit: Circuit):
        self.circuit = circuit
        self._signal_widths = _all_signal_widths(circuit)

    def evaluate(self, inputs: dict) -> dict:
        self.values = dict(inputs)
        visitor = _EvalVisitor(self.values, self._signal_widths)
        for asn in self.circuit.assignments:
            self.values[asn.lhs] = accept(asn.rhs, visitor)
        return {sig.name: self.values[sig.name] for sig in self.circuit.outputs}


class _EvalVisitor(ExprVisitor):
    def __init__(self, values: dict, signal_widths: dict):
        self.values = values
        self.signal_widths = signal_widths

    def visit_literal(self, expr: Literal):
        return TriValue.from_int(expr.value, expr.width)

    def visit_identifier(self, expr: Identifier):
        return self.values[expr.name]

    def visit_index(self, expr: Index):
        val = accept(expr.expr, self)
        return TriValue(
            (val.value_mask >> expr.index) & 1,
            (val.known_mask >> expr.index) & 1,
            1
        )

    def visit_slice(self, expr: Slice):
        val = accept(expr.expr, self)
        width = expr.hi - expr.lo + 1
        mask = (1 << width) - 1
        return TriValue(
            (val.value_mask >> expr.lo) & mask,
            (val.known_mask >> expr.lo) & mask,
            width
        )

    def visit_concat(self, expr: Concat):
        result_value = 0
        result_known = 0
        total_width = 0
        for part in expr.parts:
            w = expr_width(part, self.signal_widths)
            part_val = accept(part, self)
            result_value = (result_value << w) | (part_val.value_mask & ((1 << w) - 1))
            result_known = (result_known << w) | (part_val.known_mask & ((1 << w) - 1))
            total_width += w
        return TriValue(result_value, result_known, total_width)

    def visit_call(self, expr: Call):
        args = [accept(arg, self) for arg in expr.args]
        return apply_operator(expr.operator, args)
