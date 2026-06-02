"""Operator definitions and dispatch tables."""

from trivalue import TriValue

OPERATOR_ARITY = {
    'NOT': (1, 1), 'BUF': (1, 1),
    'AND': (2, None), 'OR': (2, None), 'XOR': (2, None),
    'NAND': (2, None), 'NOR': (2, None), 'XNOR': (2, None),
    'MUX': (3, 3), 'ITE': (3, 3),
    'REDUCE_AND': (1, 1), 'REDUCE_OR': (1, 1), 'REDUCE_XOR': (1, 1),
    'EQ': (2, 2),
}

_UNARY_PROPAGATE = frozenset({'NOT', 'BUF'})
_BINARY_PROPAGATE = frozenset({'AND', 'OR', 'XOR', 'NAND', 'NOR', 'XNOR'})
_SELECT = frozenset({'MUX', 'ITE'})
_REDUCE = frozenset({'REDUCE_AND', 'REDUCE_OR', 'REDUCE_XOR'})

_WIDTH_PROPAGATE = _UNARY_PROPAGATE | _BINARY_PROPAGATE
_SELECT_RESULT = _SELECT
_WIDTH_1_RESULT = _REDUCE | {'EQ'}

_BINARY_OPS = {
    'AND': TriValue.and2, 'NAND': TriValue.and2,
    'OR': TriValue.or2, 'NOR': TriValue.or2,
    'XOR': TriValue.xor2, 'XNOR': TriValue.xor2,
}
_NEGATED = frozenset({'NAND', 'NOR', 'XNOR'})


def operator_width(op: str, arg_widths: list) -> int:
    """Return the result width for an operator given its argument widths."""
    if op in _WIDTH_PROPAGATE:
        return arg_widths[0]
    if op in _SELECT_RESULT:
        return arg_widths[1]
    if op in _WIDTH_1_RESULT:
        return 1
    raise RuntimeError(f"Unknown operator: {op}")


def apply_operator(op: str, args: list) -> TriValue:
    if op == 'NOT':
        return TriValue.not_(args[0])
    if op == 'BUF':
        return args[0].copy()

    if op in _BINARY_OPS:
        result = args[0]
        for a in args[1:]:
            result = _BINARY_OPS[op](result, a)
        if op in _NEGATED:
            result = TriValue.not_(result)
        return result

    if op in _SELECT:
        return TriValue.mux(args[0], args[1], args[2])

    if op == 'REDUCE_AND':
        return TriValue.reduce_and(args[0])
    if op == 'REDUCE_OR':
        return TriValue.reduce_or(args[0])
    if op == 'REDUCE_XOR':
        return TriValue.reduce_xor(args[0])
    if op == 'EQ':
        return TriValue.eq(args[0], args[1])

    raise RuntimeError(f"Unknown operator: {op}")
