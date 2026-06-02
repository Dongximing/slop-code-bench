"""Circuit validation using visitor pattern."""

from ast_nodes import Circuit, Literal, Identifier, Index, Slice, Concat, Call, ExprVisitor, accept, collect_dependencies
from errors import CircError
from operators import OPERATOR_ARITY, operator_width, _WIDTH_PROPAGATE, _SELECT, _REDUCE


class Validator:
    def __init__(self, circuit: Circuit, filename: str):
        self.circuit = circuit
        self.filename = filename
        self.signal_types = {sig.name: 'input' for sig in circuit.inputs}
        self.signal_types.update({sig.name: 'output' for sig in circuit.outputs})
        self.signal_types.update({sig.name: 'wire' for sig in circuit.wires})
        self.signal_info = {sig.name: sig for sig in circuit.inputs + circuit.outputs + circuit.wires}
        self._all_names = circuit.get_all_names()
        self._signal_widths = {name: sig.width for name, sig in self.signal_info.items()}
        self._input_names = {sig.name for sig in circuit.inputs}

    def validate(self):
        self._check_assignments()
        self._check_cycles()

    def _check_assignments(self):
        assigned = set()
        for asn in self.circuit.assignments:
            if asn.lhs not in self._all_names:
                raise CircError("UndefinedNameError", f"Undefined signal: {asn.lhs}", self.filename, asn.line, asn.col)
            if self.signal_types.get(asn.lhs) == 'input':
                raise CircError("InputAssignmentError", f"Cannot assign to input: {asn.lhs}", self.filename, asn.line, asn.col)
            if asn.lhs in assigned:
                raise CircError("MultipleAssignmentError", f"Signal assigned multiple times: {asn.lhs}", self.filename, asn.line, asn.col)
            assigned.add(asn.lhs)
            self._validate_expr(asn.rhs, asn.line)

        for sig in self.circuit.outputs + self.circuit.wires:
            if sig.name not in assigned:
                if sig.name in self._input_names and self.signal_types.get(sig.name) == 'output':
                    continue
                kind = "Output" if self.signal_types.get(sig.name) == 'output' else "Wire"
                raise CircError("UnassignedSignalError", f"{kind} not assigned: {sig.name}", self.filename)

    def _validate_expr(self, expr, line_num: int) -> int:
        return accept(expr, _ValidateVisitor(self, line_num))

    def _check_cycles(self):
        dependencies = {sig.name: set() for sig in self.circuit.inputs}
        for asn in self.circuit.assignments:
            dependencies[asn.lhs] = collect_dependencies(asn.rhs)
        WHITE, GRAY, BLACK = 0, 1, 2
        color = {name: WHITE for name in self._all_names}

        def dfs(node: str, path: list):
            color[node] = GRAY
            path.append(node)
            for dep in dependencies.get(node, set()):
                if color[dep] == GRAY:
                    cycle_start = path.index(dep)
                    return path[cycle_start:] + [dep]
                if color[dep] == WHITE:
                    result = dfs(dep, path)
                    if result:
                        return result
            path.pop()
            color[node] = BLACK
            return None

        for node in self._all_names:
            if color[node] == WHITE:
                cycle = dfs(node, [])
                if cycle:
                    raise CircError("CycleError", f"Cycle detected: {' -> '.join(cycle)}", self.filename)


class _ValidateVisitor(ExprVisitor):
    def __init__(self, validator: Validator, line_num: int):
        self.validator = validator
        self.line_num = line_num

    def visit_literal(self, expr: Literal):
        return expr.width

    def visit_identifier(self, expr: Identifier):
        if expr.name not in self.validator._all_names:
            raise CircError("UndefinedNameError", f"Undefined signal: {expr.name}", self.validator.filename, self.line_num)
        return self.validator._signal_widths[expr.name]

    def visit_index(self, expr: Index):
        base_width = self.validator._validate_expr(expr.expr, self.line_num)
        self._check_index_bounds(expr, base_width)
        return 1

    def visit_slice(self, expr: Slice):
        base_width = self.validator._validate_expr(expr.expr, self.line_num)
        if expr.hi < expr.lo:
            raise CircError("CircParseError", f"Invalid slice: hi ({expr.hi}) < lo ({expr.lo})",
                           self.validator.filename, self.line_num)
        self._check_slice_bounds(expr, base_width)
        return expr.hi - expr.lo + 1

    def visit_concat(self, expr: Concat):
        return sum(self.validator._validate_expr(p, self.line_num) for p in expr.parts)

    def visit_call(self, expr: Call):
        self._check_arity(expr)
        arg_widths = [self.validator._validate_expr(arg, self.line_num) for arg in expr.args]
        self._check_widths(expr, arg_widths)
        return operator_width(expr.operator, arg_widths)

    def _check_index_bounds(self, expr: Index, base_width: int):
        sig = self.validator.signal_info.get(expr.expr.name) if isinstance(expr.expr, Identifier) else None
        if sig:
            if expr.index < sig.lsb or expr.index > sig.msb:
                raise CircError("IndexOutOfBoundsError",
                               f"Index {expr.index} out of bounds for signal {sig.name}[{sig.msb}:{sig.lsb}]",
                               self.validator.filename, self.line_num)
        elif expr.index < 0 or expr.index >= base_width:
            raise CircError("IndexOutOfBoundsError",
                           f"Index {expr.index} out of bounds for expression of width {base_width}",
                           self.validator.filename, self.line_num)

    def _check_slice_bounds(self, expr: Slice, base_width: int):
        sig = self.validator.signal_info.get(expr.expr.name) if isinstance(expr.expr, Identifier) else None
        if sig:
            if expr.lo < sig.lsb or expr.hi > sig.msb:
                raise CircError("IndexOutOfBoundsError",
                               f"Slice [{expr.hi}:{expr.lo}] out of bounds for signal {sig.name}[{sig.msb}:{sig.lsb}]",
                               self.validator.filename, self.line_num)
        elif expr.lo < 0 or expr.hi >= base_width:
            raise CircError("IndexOutOfBoundsError",
                           f"Slice [{expr.hi}:{expr.lo}] out of bounds for expression of width {base_width}",
                           self.validator.filename, self.line_num)

    def _check_arity(self, call: Call):
        op = call.operator
        num_args = len(call.args)
        if op not in OPERATOR_ARITY:
            raise CircError("UndefinedNameError", f"Unknown operator: {op}", self.validator.filename, self.line_num)
        min_args, max_args = OPERATOR_ARITY[op]
        if num_args < min_args or (max_args and num_args > max_args):
            if max_args == min_args:
                req = f"exactly {min_args}"
            elif max_args is None:
                req = f"at least {min_args}"
            else:
                req = f"between {min_args} and {max_args}"
            plural = "s" if min_args > 1 or (max_args and max_args > 1) else ""
            raise CircError("ArityError", f"Operator {op} requires {req} argument{plural}, got {num_args}",
                           self.validator.filename, self.line_num)

    def _check_widths(self, call: Call, arg_widths: list):
        op = call.operator
        if op in _WIDTH_PROPAGATE:
            for i, w in enumerate(arg_widths[1:], 1):
                if w != arg_widths[0]:
                    raise CircError("WidthMismatchError",
                                   f"Width mismatch in {op}: operand 0 has width {arg_widths[0]}, operand {i} has width {w}",
                                   self.validator.filename, self.line_num)
        elif op in _SELECT:
            if arg_widths[0] != 1:
                raise CircError("WidthMismatchError",
                               f"Selector in {op} must have width 1, got width {arg_widths[0]}",
                               self.validator.filename, self.line_num)
            if arg_widths[1] != arg_widths[2]:
                raise CircError("WidthMismatchError",
                               f"Width mismatch in {op}: operand 1 has width {arg_widths[1]}, operand 2 has width {arg_widths[2]}",
                               self.validator.filename, self.line_num)
        elif op in _REDUCE:
            pass
        elif op == 'EQ':
            if arg_widths[0] != arg_widths[1]:
                raise CircError("WidthMismatchError",
                               f"Width mismatch in EQ: operand 0 has width {arg_widths[0]}, operand 1 has width {arg_widths[1]}",
                               self.validator.filename, self.line_num)
