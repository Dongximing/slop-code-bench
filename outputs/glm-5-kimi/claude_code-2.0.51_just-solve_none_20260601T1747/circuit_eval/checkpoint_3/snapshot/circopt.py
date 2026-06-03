#!/usr/bin/env python3
"""circopt - Circuit optimizer and equivalence checker."""

import sys
import json
import os
import re
from collections import defaultdict

VERSION = "1.0.0"

# ─── Exit codes ───────────────────────────────────────────────────────────────

EXIT_SUCCESS = 0
EXIT_CLI_USAGE = 1
EXIT_PARSE_ERROR = 2
EXIT_VALIDATION_ERROR = 3
EXIT_INTERNAL_ERROR = 4
EXIT_NON_EQUIVALENCE = 10

# ─── Operator definitions ────────────────────────────────────────────────────

# Operators with (min_arity, max_arity, result_width_rule)
# result_width_rule:
#   'same' - result width = operand width (all operands same width)
#   'sum' - result width = sum of operand widths (concatenation)
#   1 - result width is 1 (reductions, equality)
#   'mux' - special handling for MUX/ITE
OPERATORS = {
    "NOT": (1, 1, 'same'),
    "BUF": (1, 1, 'same'),
    "AND": (2, None, 'same'),
    "OR": (2, None, 'same'),
    "XOR": (2, None, 'same'),
    "NAND": (2, None, 'same'),
    "NOR": (2, None, 'same'),
    "XNOR": (2, None, 'same'),
    "MUX": (3, 3, 'mux'),
    "ITE": (3, 3, 'mux'),
    "REDUCE_AND": (1, 1, 1),
    "REDUCE_OR": (1, 1, 1),
    "REDUCE_XOR": (1, 1, 1),
    "EQ": (2, 2, 1),
}

# ─── Error classes ────────────────────────────────────────────────────────────

class CircError(Exception):
    """Base error for circuit operations."""
    exit_code = EXIT_INTERNAL_ERROR

    def __init__(self, message, file=None, line=None, col=None):
        self.message = message
        self.file = file
        self.line = line
        self.col = col
        super().__init__(message)

    def to_dict(self):
        return {
            "type": self.__class__.__name__,
            "message": self.message,
            "file": self.file,
            "line": self.line,
            "col": self.col,
        }


class CliUsageError(CircError):
    exit_code = EXIT_CLI_USAGE


class FileNotFoundError_(CircError):
    exit_code = EXIT_CLI_USAGE


class CircParseError(CircError):
    exit_code = EXIT_PARSE_ERROR


class DeclarationAfterAssignmentError(CircError):
    exit_code = EXIT_VALIDATION_ERROR


class DuplicateNameError(CircError):
    exit_code = EXIT_VALIDATION_ERROR


class UndefinedNameError(CircError):
    exit_code = EXIT_VALIDATION_ERROR


class UnassignedSignalError(CircError):
    exit_code = EXIT_VALIDATION_ERROR


class InputAssignmentError(CircError):
    exit_code = EXIT_VALIDATION_ERROR


class MultipleAssignmentError(CircError):
    exit_code = EXIT_VALIDATION_ERROR


class ArityError(CircError):
    exit_code = EXIT_VALIDATION_ERROR


class CycleError(CircError):
    exit_code = EXIT_VALIDATION_ERROR


class MissingInputError(CircError):
    exit_code = EXIT_CLI_USAGE


class UnknownInputError(CircError):
    exit_code = EXIT_CLI_USAGE


class InputValueParseError(CircError):
    exit_code = EXIT_PARSE_ERROR


class WidthMismatchError(CircError):
    exit_code = EXIT_VALIDATION_ERROR


class IndexOutOfBoundsError(CircError):
    exit_code = EXIT_VALIDATION_ERROR


class InputWidthMismatchError(CircError):
    exit_code = EXIT_VALIDATION_ERROR


# ─── Width and value utilities ───────────────────────────────────────────────

def parse_literal_value(lit_str, filename=None, line=None, col=None):
    """Parse a literal string and return (width, integer_value).
    width is None for unsized literals (except 0/1 which have width 1).
    Raises CircParseError on invalid format.
    """
    orig_str = lit_str
    lit_str = lit_str.replace('_', '')  # Remove underscores

    # Check for X/x literal (not allowed)
    if lit_str.upper() == 'X':
        raise CircParseError(
            f"Literal 'X' (unknown/don't-care) is not allowed",
            file=filename, line=line, col=col,
        )

    # Scalar literals 0, 1
    if lit_str in ('0', '1'):
        return (1, int(lit_str))

    # Sized literals: N'b..., N'h..., N'd... (note the apostrophe)
    sized_match = re.match(r"^(\d+)'([bhd])(.+)$", lit_str, re.IGNORECASE)
    if sized_match:
        width = int(sized_match.group(1))
        base = sized_match.group(2).lower()
        value_str = sized_match.group(3)

        if width <= 0:
            raise CircParseError(
                f"Invalid literal width: {width}",
                file=filename, line=line, col=col,
            )

        if base == 'b':
            if not re.match(r'^[01]+$', value_str):
                raise CircParseError(
                    f"Invalid binary literal: {orig_str}",
                    file=filename, line=line, col=col,
                )
            value = int(value_str, 2)
        elif base == 'h':
            if not re.match(r'^[0-9a-fA-F]+$', value_str):
                raise CircParseError(
                    f"Invalid hex literal: {orig_str}",
                    file=filename, line=line, col=col,
                )
            value = int(value_str, 16)
        elif base == 'd':
            if not re.match(r'^[0-9]+$', value_str):
                raise CircParseError(
                    f"Invalid decimal literal: {orig_str}",
                    file=filename, line=line, col=col,
                )
            value = int(value_str, 10)
        else:
            raise CircParseError(
                f"Invalid literal base: {base}",
                file=filename, line=line, col=col,
            )

        # Check value fits in width
        if value >= (1 << width):
            raise CircParseError(
                f"Value {value} does not fit in {width} bits",
                file=filename, line=line, col=col,
            )

        return (width, value)

    # Unsized binary: 0b...
    if lit_str.lower().startswith('0b'):
        value_str = lit_str[2:]
        if not value_str:
            raise CircParseError(
                f"Invalid binary literal: {orig_str}",
                file=filename, line=line, col=col,
            )
        if not re.match(r'^[01]+$', value_str):
            raise CircParseError(
                f"Invalid binary literal: {orig_str}",
                file=filename, line=line, col=col,
            )
        width = len(value_str)
        value = int(value_str, 2)
        return (width, value)

    # Unsized hex: 0x...
    if lit_str.lower().startswith('0x'):
        value_str = lit_str[2:]
        if not value_str:
            raise CircParseError(
                f"Invalid hex literal: {orig_str}",
                file=filename, line=line, col=col,
            )
        if not re.match(r'^[0-9a-fA-F]+$', value_str):
            raise CircParseError(
                f"Invalid hex literal: {orig_str}",
                file=filename, line=line, col=col,
            )
        value = int(value_str, 16)
        # Width is minimum bits needed
        width = max(1, value.bit_length())
        # For hex, width is multiple of 4 and at least 4
        # Actually, looking at the spec: 0x0f has width 8, 0xff has width 8
        # Hex width = number of hex digits * 4
        width = len(value_str) * 4
        return (width, value)

    # Plain decimal (not sized) - treat as sized to fit
    if re.match(r'^[0-9]+$', lit_str):
        value = int(lit_str)
        if value <= 1:
            return (1, value)
        width = value.bit_length()
        return (width, value)

    raise CircParseError(
        f"Invalid literal format: {orig_str}",
        file=filename, line=line, col=col,
    )


def format_value(value, width, radix='bin'):
    """Format a value for output according to radix."""
    if width == 1:
        # Scalar outputs are always just 0 or 1
        return str(value)

    if radix == 'bin':
        return f"0b{value:0{width}b}"
    elif radix == 'hex':
        hex_width = (width + 3) // 4  # ceil(width/4)
        return f"0x{value:0{hex_width}x}"
    elif radix == 'dec':
        return str(value)
    else:
        return f"0b{value:0{width}b}"


# ─── Tokenizer / Parser ──────────────────────────────────────────────────────

IDENT_RE = re.compile(r'[A-Za-z_][A-Za-z0-9_]*')
NUMBER_RE = re.compile(r'\d+')


class Token:
    __slots__ = ('kind', 'value', 'line', 'col')

    def __init__(self, kind, value, line, col):
        self.kind = kind
        self.value = value
        self.line = line
        self.col = col

    def __repr__(self):
        return f"Token({self.kind}, {self.value!r}, L{self.line}:{self.col})"


def tokenize(text, filename="<stdin>"):
    """Tokenize .circ source text into a list of Tokens."""
    tokens = []
    lines = text.split('\n')
    filtered_lines = []  # (line_number, line_text) - skip blanks and comments

    for i, raw_line in enumerate(lines, start=1):
        stripped = raw_line.strip()
        if stripped == '' or stripped.startswith('#'):
            continue
        filtered_lines.append((i, raw_line))

    for line_num, raw_line in filtered_lines:
        line_text = raw_line
        pos = 0
        length = len(line_text)

        while pos < length:
            ch = line_text[pos]

            if ch in ' \t':
                pos += 1
                continue

            col = pos + 1  # 1-based column

            if ch == '=':
                tokens.append(Token('eq', '=', line_num, col))
                pos += 1
            elif ch == '(':
                tokens.append(Token('lparen', '(', line_num, col))
                pos += 1
            elif ch == ')':
                tokens.append(Token('rparen', ')', line_num, col))
                pos += 1
            elif ch == ',':
                tokens.append(Token('comma', ',', line_num, col))
                pos += 1
            elif ch == '[':
                tokens.append(Token('lbracket', '[', line_num, col))
                pos += 1
            elif ch == ']':
                tokens.append(Token('rbracket', ']', line_num, col))
                pos += 1
            elif ch == ':':
                tokens.append(Token('colon', ':', line_num, col))
                pos += 1
            elif ch == '{':
                tokens.append(Token('lbrace', '{', line_num, col))
                pos += 1
            elif ch == '}':
                tokens.append(Token('rbrace', '}', line_num, col))
                pos += 1
            elif ch.isdigit():
                # Could be start of literal (0b, 0x, sized), or just a number
                rest = line_text[pos:]

                # Check for sized literal like 8'b..., 8'h..., 8'd...
                sized_match = re.match(r'^(\d+\'[bBhHdD][0-9a-fA-F_]+)', rest)
                if sized_match:
                    lit_str = sized_match.group(1)
                    tokens.append(Token('lit', lit_str, line_num, col))
                    pos += len(lit_str)
                    continue

                # Check for unsized binary 0b... or hex 0x...
                if len(rest) >= 2 and rest[0] == '0' and rest[1] in 'bBxX':
                    if rest[1] in 'bB':
                        m = re.match(r'^0[bB][01_]+', rest)
                    else:
                        m = re.match(r'^0[xX][0-9a-fA-F_]+', rest)
                    if m:
                        lit_str = m.group(0)
                        tokens.append(Token('lit', lit_str, line_num, col))
                        pos += len(lit_str)
                        continue

                # Check for plain number (including single digit 0 or 1)
                m = NUMBER_RE.match(rest)
                if m:
                    tokens.append(Token('num', m.group(), line_num, col))
                    pos += len(m.group())
                else:
                    raise CircParseError(
                        f"Unexpected character '{ch}'",
                        file=filename, line=line_num, col=col,
                    )
            elif ch.isalpha() or ch == '_':
                m = IDENT_RE.match(line_text, pos)
                if m:
                    ident = m.group()
                    # Check if it's an X literal (not allowed)
                    if ident.upper() == 'X':
                        tokens.append(Token('lit', ident, line_num, col))
                    else:
                        tokens.append(Token('ident', ident, line_num, col))
                    pos = m.end()
                else:
                    raise CircParseError(
                        f"Unexpected character '{ch}'",
                        file=filename, line=line_num, col=col,
                    )
            else:
                raise CircParseError(
                    f"Unexpected character '{ch}'",
                    file=filename, line=line_num, col=col,
                )

    return tokens


# ─── Signal representation ────────────────────────────────────────────────────

class Signal:
    """Represents a signal with name and bit range."""
    __slots__ = ('name', 'msb', 'lsb')

    def __init__(self, name, msb=0, lsb=0):
        self.name = name
        self.msb = msb
        self.lsb = lsb

    @property
    def width(self):
        return self.msb - self.lsb + 1

    def to_dict(self):
        return {"name": self.name, "msb": self.msb, "lsb": self.lsb}


# ─── Expression AST ──────────────────────────────────────────────────────────

class ExprIdent:
    __slots__ = ('name', 'line', 'col')
    def __init__(self, name, line, col):
        self.name = name
        self.line = line
        self.col = col


class ExprLit:
    __slots__ = ('value', 'width', 'line', 'col')
    def __init__(self, value, width, line, col):
        self.value = value  # integer value
        self.width = width  # width in bits
        self.line = line
        self.col = col


class ExprCall:
    __slots__ = ('op', 'args', 'line', 'col')
    def __init__(self, op, args, line, col):
        self.op = op
        self.args = args
        self.line = line
        self.col = col


class ExprIndex:
    """Bit index: v[i]"""
    __slots__ = ('expr', 'index', 'line', 'col')
    def __init__(self, expr, index, line, col):
        self.expr = expr
        self.index = index  # integer index
        self.line = line
        self.col = col


class ExprSlice:
    """Bit slice: v[hi:lo]"""
    __slots__ = ('expr', 'hi', 'lo', 'line', 'col')
    def __init__(self, expr, hi, lo, line, col):
        self.expr = expr
        self.hi = hi  # integer high bound
        self.lo = lo  # integer low bound
        self.line = line
        self.col = col


class ExprConcat:
    """Concatenation: {e1, e2, ...}"""
    __slots__ = ('exprs', 'line', 'col')
    def __init__(self, exprs, line, col):
        self.exprs = exprs
        self.line = line
        self.col = col


# ─── Parser ──────────────────────────────────────────────────────────────────

class Parser:
    def __init__(self, tokens, filename):
        self.tokens = tokens
        self.filename = filename
        self.pos = 0

    def peek(self):
        if self.pos < len(self.tokens):
            return self.tokens[self.pos]
        return None

    def advance(self):
        tok = self.tokens[self.pos]
        self.pos += 1
        return tok

    def expect(self, kind):
        tok = self.peek()
        if tok is None or tok.kind != kind:
            last_tok = self.tokens[self.pos - 1] if self.pos > 0 else None
            if tok:
                raise CircParseError(
                    f"Expected '{kind}' but got '{tok.kind}'",
                    file=self.filename, line=tok.line, col=tok.col,
                )
            elif last_tok:
                raise CircParseError(
                    f"Unexpected end of file",
                    file=self.filename, line=last_tok.line,
                    col=last_tok.col + len(str(last_tok.value)),
                )
            else:
                raise CircParseError(
                    f"Unexpected end of file",
                    file=self.filename, line=1, col=1,
                )
        return self.advance()

    def at_end(self):
        return self.pos >= len(self.tokens)

    def parse(self):
        """Parse the entire token stream into declarations and assignments."""
        inputs = []
        outputs = []
        wires = []
        assignments = []
        has_assignment = False

        while not self.at_end():
            tok = self.peek()

            # Check for assignment: ident '=' ...
            if self.pos + 1 < len(self.tokens) and self.tokens[self.pos + 1].kind == 'eq':
                has_assignment = True
                lhs = tok.value
                lhs_line = tok.line
                lhs_col = tok.col
                self.advance()  # consume ident
                self.advance()  # consume '='
                expr = self._parse_expr()
                assignments.append((lhs, expr, lhs_line, lhs_col))
            else:
                # Must be a declaration
                if has_assignment:
                    raise DeclarationAfterAssignmentError(
                        f"Declaration after assignment is not allowed",
                        file=self.filename, line=tok.line, col=tok.col,
                    )

                kw = tok.value
                kw_upper = kw.upper()

                if kw_upper not in ('INPUT', 'OUTPUT', 'WIRE'):
                    raise CircParseError(
                        f"Expected declaration keyword or assignment, got '{kw}'",
                        file=self.filename, line=tok.line, col=tok.col,
                    )

                self.advance()  # consume keyword

                # Collect signal declarations until we hit something else
                KEYWORDS = {'INPUT', 'OUTPUT', 'WIRE'}
                signals = []
                while not self.at_end():
                    nxt = self.peek()
                    if nxt.kind != 'ident':
                        break
                    # If this looks like a keyword, stop
                    if nxt.value.upper() in KEYWORDS:
                        break
                    # Check if next token after this ident is '=' (assignment)
                    if (self.pos + 1 < len(self.tokens) and
                            self.tokens[self.pos + 1].kind == 'eq'):
                        break

                    # Parse signal declaration (name or name[msb:lsb])
                    sig = self._parse_signal_decl()
                    signals.append(sig)

                if not signals:
                    raise CircParseError(
                        f"Expected at least one signal after '{kw}'",
                        file=self.filename, line=tok.line, col=tok.col,
                    )

                if kw_upper == 'INPUT':
                    inputs.extend(signals)
                elif kw_upper == 'OUTPUT':
                    outputs.extend(signals)
                elif kw_upper == 'WIRE':
                    wires.extend(signals)

        return inputs, outputs, wires, assignments

    def _parse_signal_decl(self):
        """Parse a signal declaration: name or name[msb:lsb]."""
        tok = self.expect('ident')
        name = tok.value
        line, col = tok.line, tok.col

        # Check for bit range
        nxt = self.peek()
        if nxt is not None and nxt.kind == 'lbracket':
            self.advance()  # consume '['
            msb_tok = self.expect('num')
            msb = int(msb_tok.value)
            self.expect('colon')
            lsb_tok = self.expect('num')
            lsb = int(lsb_tok.value)
            self.expect('rbracket')

            if msb < lsb:
                raise CircParseError(
                    f"Invalid bit range [{msb}:{lsb}]: msb must be >= lsb",
                    file=self.filename, line=line, col=col,
                )
            if lsb < 0:
                raise CircParseError(
                    f"Invalid bit range [{msb}:{lsb}]: lsb must be >= 0",
                    file=self.filename, line=line, col=col,
                )

            return Signal(name, msb, lsb)
        else:
            # Scalar signal
            return Signal(name, 0, 0)

    def _parse_expr(self):
        tok = self.peek()
        if tok is None:
            raise CircParseError(
                "Unexpected end of file in expression",
                file=self.filename, line=1, col=1,
            )

        # Concatenation: {e1, e2, ...}
        if tok.kind == 'lbrace':
            return self._parse_concat()

        # Literal
        if tok.kind == 'lit':
            self.advance()
            width, value = parse_literal_value(
                tok.value, self.filename, tok.line, tok.col
            )
            return ExprLit(value, width, tok.line, tok.col)

        # Number (treat as literal)
        if tok.kind == 'num':
            self.advance()
            value = int(tok.value)
            width = max(1, value.bit_length())
            return ExprLit(value, width, tok.line, tok.col)

        if tok.kind == 'ident':
            # Could be a plain identifier, indexed, sliced, or a function call
            self.advance()

            # Check for index or slice: ident[...]
            nxt = self.peek()
            if nxt is not None and nxt.kind == 'lbracket':
                self.advance()  # consume '['

                # Peek to determine if it's index or slice
                inner = self.peek()
                if inner is None:
                    raise CircParseError(
                        "Unexpected end of file in index/slice",
                        file=self.filename, line=tok.line, col=tok.col,
                    )

                # Parse first number
                first_tok = self.expect('num')
                first_val = int(first_tok.value)

                # Check for slice (colon) or index
                nxt2 = self.peek()
                if nxt2 is not None and nxt2.kind == 'colon':
                    # Slice
                    self.advance()  # consume ':'
                    second_tok = self.expect('num')
                    second_val = int(second_tok.value)
                    self.expect('rbracket')

                    # hi and lo are relative to the signal's LSB
                    # For a signal v[msb:lsb], v[hi:lo] extracts bits
                    # The slice bounds are relative to the LSB
                    return ExprSlice(
                        ExprIdent(tok.value, tok.line, tok.col),
                        first_val, second_val,
                        tok.line, tok.col
                    )
                else:
                    # Index
                    self.expect('rbracket')
                    return ExprIndex(
                        ExprIdent(tok.value, tok.line, tok.col),
                        first_val,
                        tok.line, tok.col
                    )

            # Check for function call: ident '(' args ')'
            if nxt is not None and nxt.kind == 'lparen':
                op_name = tok.value.upper()

                if op_name not in OPERATORS:
                    raise CircParseError(
                        f"Unknown operator '{tok.value}'",
                        file=self.filename, line=tok.line, col=tok.col,
                    )

                self.advance()  # consume '('
                args = []

                # Check for empty args
                nxt = self.peek()
                if nxt is not None and nxt.kind == 'rparen':
                    self.advance()  # consume ')'
                    self._check_arity(op_name, len(args), tok.line, tok.col)
                    return ExprCall(op_name, args, tok.line, tok.col)

                args.append(self._parse_expr())

                while not self.at_end():
                    nxt = self.peek()
                    if nxt.kind == 'comma':
                        self.advance()  # consume ','
                        args.append(self._parse_expr())
                    elif nxt.kind == 'rparen':
                        self.advance()  # consume ')'
                        break
                    else:
                        raise CircParseError(
                            f"Expected ',' or ')' in function call",
                            file=self.filename, line=nxt.line, col=nxt.col,
                        )
                else:
                    # Loop ended without finding closing paren (EOF)
                    last_arg = args[-1] if args else None
                    if last_arg:
                        raise CircParseError(
                            f"Unclosed function call - missing ')'",
                            file=self.filename, line=last_arg.line,
                            col=last_arg.col + 1,
                        )
                    else:
                        raise CircParseError(
                            f"Unclosed function call - missing ')'",
                            file=self.filename, line=tok.line, col=tok.col,
                        )

                self._check_arity(op_name, len(args), tok.line, tok.col)
                return ExprCall(op_name, args, tok.line, tok.col)
            else:
                # Plain identifier - check for X
                name_upper = tok.value.upper()
                if name_upper == 'X':
                    raise CircParseError(
                        f"Literal 'X' (unknown/don't-care) is not allowed",
                        file=self.filename, line=tok.line, col=tok.col,
                    )
                return ExprIdent(tok.value, tok.line, tok.col)

        raise CircParseError(
            f"Unexpected token '{tok.value}' in expression",
            file=self.filename, line=tok.line, col=tok.col,
        )

    def _parse_concat(self):
        """Parse a concatenation: {e1, e2, ...}"""
        lbrace = self.expect('lbrace')
        exprs = []

        exprs.append(self._parse_expr())

        while not self.at_end():
            nxt = self.peek()
            if nxt.kind == 'comma':
                self.advance()  # consume ','
                exprs.append(self._parse_expr())
            elif nxt.kind == 'rbrace':
                self.advance()  # consume '}'
                break
            else:
                raise CircParseError(
                    f"Expected ',' or '}}' in concatenation",
                    file=self.filename, line=nxt.line, col=nxt.col,
                )
        else:
            raise CircParseError(
                f"Unclosed concatenation - missing '}}'",
                file=self.filename, line=lbrace.line, col=lbrace.col,
            )

        return ExprConcat(exprs, lbrace.line, lbrace.col)

    def _check_arity(self, op, nargs, line, col):
        min_arity, max_arity, _ = OPERATORS[op]
        if nargs < min_arity:
            raise ArityError(
                f"Operator '{op}' requires at least {min_arity} argument(s), got {nargs}",
                file=self.filename, line=line, col=col,
            )
        if max_arity is not None and nargs > max_arity:
            raise ArityError(
                f"Operator '{op}' requires exactly {max_arity} argument(s), got {nargs}",
                file=self.filename, line=line, col=col,
            )


# ─── Width inference ──────────────────────────────────────────────────────────

def infer_width(expr, signals, filename):
    """Infer the width of an expression. Returns width or raises error."""
    if isinstance(expr, ExprLit):
        return expr.width

    elif isinstance(expr, ExprIdent):
        if expr.name not in signals:
            raise UndefinedNameError(
                f"Name '{expr.name}' is not declared",
                file=filename, line=expr.line, col=expr.col,
            )
        return signals[expr.name].width

    elif isinstance(expr, ExprIndex):
        base_width = infer_width(expr.expr, signals, filename)
        sig = None
        if isinstance(expr.expr, ExprIdent):
            sig = signals.get(expr.expr.name)

        # Index is relative to LSB
        if sig:
            # The index is the bit position relative to LSB
            actual_index = expr.index
            if actual_index < 0 or actual_index >= base_width:
                raise IndexOutOfBoundsError(
                    f"Index {expr.index} out of bounds for signal of width {base_width}",
                    file=filename, line=expr.line, col=expr.col,
                )
        return 1

    elif isinstance(expr, ExprSlice):
        base_width = infer_width(expr.expr, signals, filename)
        sig = None
        if isinstance(expr.expr, ExprIdent):
            sig = signals.get(expr.expr.name)

        # Slice bounds are relative to LSB
        hi, lo = expr.hi, expr.lo
        if hi < lo:
            raise CircParseError(
                f"Invalid slice [{hi}:{lo}]: high must be >= low",
                file=filename, line=expr.line, col=expr.col,
            )
        if lo < 0:
            raise IndexOutOfBoundsError(
                f"Slice lower bound {lo} is negative",
                file=filename, line=expr.line, col=expr.col,
            )
        if hi >= base_width:
            raise IndexOutOfBoundsError(
                f"Slice upper bound {hi} exceeds signal width {base_width}",
                file=filename, line=expr.line, col=expr.col,
            )

        return hi - lo + 1

    elif isinstance(expr, ExprConcat):
        total_width = 0
        for e in expr.exprs:
            total_width += infer_width(e, signals, filename)
        return total_width

    elif isinstance(expr, ExprCall):
        op = expr.op
        arg_widths = [infer_width(arg, signals, filename) for arg in expr.args]
        min_arity, max_arity, width_rule = OPERATORS[op]

        if op in ('NOT', 'BUF'):
            return arg_widths[0]

        elif op in ('AND', 'OR', 'XOR', 'NAND', 'NOR', 'XNOR'):
            # All args must have same width
            first_width = arg_widths[0]
            for i, w in enumerate(arg_widths[1:], 1):
                if w != first_width:
                    raise WidthMismatchError(
                        f"Width mismatch: operand 0 has width {first_width}, operand {i} has width {w}",
                        file=filename, line=expr.line, col=expr.col,
                    )
            return first_width

        elif op in ('MUX', 'ITE'):
            # sel must be width 1, a and b must have same width
            if arg_widths[0] != 1:
                raise WidthMismatchError(
                    f"Selector must have width 1, got {arg_widths[0]}",
                    file=filename, line=expr.line, col=expr.col,
                )
            if arg_widths[1] != arg_widths[2]:
                raise WidthMismatchError(
                    f"Width mismatch: operand 1 has width {arg_widths[1]}, operand 2 has width {arg_widths[2]}",
                    file=filename, line=expr.line, col=expr.col,
                )
            return arg_widths[1]

        elif op in ('REDUCE_AND', 'REDUCE_OR', 'REDUCE_XOR'):
            # Operand width must be >= 1
            if arg_widths[0] < 1:
                raise WidthMismatchError(
                    f"Reduction operator requires operand width >= 1, got {arg_widths[0]}",
                    file=filename, line=expr.line, col=expr.col,
                )
            return 1

        elif op == 'EQ':
            if arg_widths[0] != arg_widths[1]:
                raise WidthMismatchError(
                    f"Width mismatch: operand 0 has width {arg_widths[0]}, operand 1 has width {arg_widths[1]}",
                    file=filename, line=expr.line, col=expr.col,
                )
            return 1

        else:
            raise CircError(f"Unknown operator '{op}'")

    else:
        raise CircError("Unknown expression type")


# ─── Validation ──────────────────────────────────────────────────────────────

def validate_circuit(inputs, outputs, wires, assignments, filename):
    """Validate a parsed circuit. Returns sorted inputs/outputs for output."""

    # Collect all declared names
    all_names = {}  # name -> Signal
    signals = {}  # name -> Signal (same as all_names, but just signals)

    for sig in inputs:
        if sig.name in all_names:
            existing = all_names[sig.name]
            raise DuplicateNameError(
                f"Duplicate name '{sig.name}'",
                file=filename, line=sig.line if hasattr(sig, 'line') else None,
                col=sig.col if hasattr(sig, 'col') else None,
            )
        all_names[sig.name] = sig
        signals[sig.name] = sig

    for sig in outputs:
        if sig.name in all_names:
            existing = all_names[sig.name]
            raise DuplicateNameError(
                f"Duplicate name '{sig.name}'",
                file=filename, line=sig.line if hasattr(sig, 'line') else None,
                col=sig.col if hasattr(sig, 'col') else None,
            )
        all_names[sig.name] = sig
        signals[sig.name] = sig

    for sig in wires:
        if sig.name in all_names:
            existing = all_names[sig.name]
            raise DuplicateNameError(
                f"Duplicate name '{sig.name}'",
                file=filename, line=sig.line if hasattr(sig, 'line') else None,
                col=sig.col if hasattr(sig, 'col') else None,
            )
        all_names[sig.name] = sig
        signals[sig.name] = sig

    input_set = {sig.name for sig in inputs}
    output_set = {sig.name for sig in outputs}
    wire_set = {sig.name for sig in wires}
    assignable = output_set | wire_set

    # Check assignments
    assigned = {}  # name -> (line, col) of assignment

    for lhs, expr, lhs_line, lhs_col in assignments:
        # LHS must be a declared wire or output
        if lhs not in all_names:
            raise UndefinedNameError(
                f"Name '{lhs}' is not declared",
                file=filename, line=lhs_line, col=lhs_col,
            )

        # LHS must not be an input
        if lhs in input_set:
            raise InputAssignmentError(
                f"Cannot assign to input '{lhs}'",
                file=filename, line=lhs_line, col=lhs_col,
            )

        # LHS must not be assigned multiple times
        if lhs in assigned:
            prev_line, prev_col = assigned[lhs]
            raise MultipleAssignmentError(
                f"Signal '{lhs}' is assigned multiple times",
                file=filename, line=lhs_line, col=lhs_col,
            )

        assigned[lhs] = (lhs_line, lhs_col)

        # Validate expression references and widths
        _validate_expr(expr, signals, filename)

    # Check all outputs and wires are assigned
    for sig in outputs:
        if sig.name not in assigned:
            raise UnassignedSignalError(
                f"Output '{sig.name}' is never assigned",
                file=filename, line=None, col=None,
            )

    for sig in wires:
        if sig.name not in assigned:
            raise UnassignedSignalError(
                f"Wire '{sig.name}' is never assigned",
                file=filename, line=None, col=None,
            )

    # Check for cycles using dependency graph
    _check_cycles(assignments, signals, filename)

    # Build sorted output
    sorted_inputs = sorted(
        [sig.to_dict() for sig in inputs],
        key=lambda x: x["name"],
    )
    sorted_outputs = sorted(
        [sig.to_dict() for sig in outputs],
        key=lambda x: x["name"],
    )

    return sorted_inputs, sorted_outputs, signals


def _validate_expr(expr, signals, filename):
    """Validate that all identifiers in an expression are declared and widths are consistent."""
    if isinstance(expr, ExprIdent):
        if expr.name not in signals:
            raise UndefinedNameError(
                f"Name '{expr.name}' is not declared",
                file=filename, line=expr.line, col=expr.col,
            )
    elif isinstance(expr, ExprLit):
        pass  # Literals are always valid after parsing
    elif isinstance(expr, ExprIndex):
        _validate_expr(expr.expr, signals, filename)
        # Check index bounds
        base_width = infer_width(expr.expr, signals, filename)
        if expr.index < 0 or expr.index >= base_width:
            raise IndexOutOfBoundsError(
                f"Index {expr.index} out of bounds for signal of width {base_width}",
                file=filename, line=expr.line, col=expr.col,
            )
    elif isinstance(expr, ExprSlice):
        _validate_expr(expr.expr, signals, filename)
        # Check slice bounds
        base_width = infer_width(expr.expr, signals, filename)
        if expr.lo < 0:
            raise IndexOutOfBoundsError(
                f"Slice lower bound {expr.lo} is negative",
                file=filename, line=expr.line, col=expr.col,
            )
        if expr.hi >= base_width:
            raise IndexOutOfBoundsError(
                f"Slice upper bound {expr.hi} exceeds signal width {base_width}",
                file=filename, line=expr.line, col=expr.col,
            )
    elif isinstance(expr, ExprConcat):
        for e in expr.exprs:
            _validate_expr(e, signals, filename)
    elif isinstance(expr, ExprCall):
        for arg in expr.args:
            _validate_expr(arg, signals, filename)
        # Infer width to check for width mismatches
        infer_width(expr, signals, filename)


def _check_cycles(assignments, signals, filename):
    """Detect cycles in the assignment dependency graph."""
    # Build adjacency: for each LHS, what does it depend on?
    deps = {}  # lhs -> set of dependency names

    def _collect_deps(expr, dep_set):
        if isinstance(expr, ExprIdent):
            dep_set.add(expr.name)
        elif isinstance(expr, ExprCall):
            for arg in expr.args:
                _collect_deps(arg, dep_set)
        elif isinstance(expr, ExprIndex):
            _collect_deps(expr.expr, dep_set)
        elif isinstance(expr, ExprSlice):
            _collect_deps(expr.expr, dep_set)
        elif isinstance(expr, ExprConcat):
            for e in expr.exprs:
                _collect_deps(e, dep_set)

    for lhs, expr, lhs_line, lhs_col in assignments:
        dep_set = set()
        _collect_deps(expr, dep_set)
        deps[lhs] = dep_set

    # DFS-based cycle detection
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {name: WHITE for name in deps}
    path = []

    def dfs(node):
        color[node] = GRAY
        path.append(node)
        for neighbor in deps.get(node, set()):
            if neighbor not in deps:
                # Dependency on input or literal - not in assignment graph
                continue
            if color[neighbor] == GRAY:
                # Found cycle - build cycle path
                cycle_start = path.index(neighbor)
                cycle_path = path[cycle_start:] + [neighbor]
                cycle_str = ' -> '.join(cycle_path)
                raise CycleError(
                    f"Dependency cycle detected: {cycle_str}",
                    file=filename, line=None, col=None,
                )
            if color[neighbor] == WHITE:
                dfs(neighbor)
        path.pop()
        color[node] = BLACK

    for node in deps:
        if color[node] == WHITE:
            dfs(node)


# ─── JSON output helpers ─────────────────────────────────────────────────────

def json_success(command, **kwargs):
    result = {"ok": True, "command": command}
    result.update(kwargs)
    return json.dumps(result, separators=(',', ':'))


def json_error(command, error):
    err_dict = error.to_dict()
    err_dict.setdefault("file", None)
    err_dict.setdefault("line", None)
    err_dict.setdefault("col", None)
    return json.dumps({
        "ok": False,
        "command": command,
        "exit_code": error.exit_code,
        "error": err_dict,
    }, separators=(',', ':'))


# ─── Commands ────────────────────────────────────────────────────────────────

def cmd_check(filepath, use_json):
    """Parse and validate a .circ file."""
    if not os.path.isfile(filepath):
        raise FileNotFoundError_(
            f"File not found: {filepath}",
            file=filepath,
        )

    with open(filepath, 'r') as f:
        text = f.read()

    tokens = tokenize(text, filepath)

    parser = Parser(tokens, filepath)
    inputs, outputs, wires, assignments = parser.parse()

    sorted_inputs, sorted_outputs, signals = validate_circuit(
        inputs, outputs, wires, assignments, filepath,
    )

    if use_json:
        print(json_success(
            "check",
            format="circ",
            inputs=sorted_inputs,
            outputs=sorted_outputs,
        ))
    else:
        print("Circuit is valid.")
        print(f"  Inputs: {', '.join(i['name'] + ('[' + str(i['msb']) + ':' + str(i['lsb']) + ']' if i['msb'] != 0 or i['lsb'] != 0 else '') for i in sorted_inputs)}")
        print(f"  Outputs: {', '.join(o['name'] + ('[' + str(o['msb']) + ':' + str(o['lsb']) + ']' if o['msb'] != 0 or o['lsb'] != 0 else '') for o in sorted_outputs)}")


# ─── Boolean/Vector evaluation ───────────────────────────────────────────────

def _vector_op(op, args, widths):
    """Evaluate a vector operator on a list of integer values.
    args: list of integer values
    widths: list of widths corresponding to each arg
    Returns integer value with appropriate width.
    """
    if op == "NOT":
        width = widths[0]
        mask = (1 << width) - 1
        return (~args[0]) & mask

    elif op == "BUF":
        return args[0]

    elif op == "AND":
        width = widths[0]
        result = args[0]
        for a in args[1:]:
            result &= a
        return result

    elif op == "OR":
        width = widths[0]
        result = args[0]
        for a in args[1:]:
            result |= a
        return result

    elif op == "XOR":
        result = 0
        for a in args:
            result ^= a
        return result

    elif op == "NAND":
        width = widths[0]
        mask = (1 << width) - 1
        result = args[0]
        for a in args[1:]:
            result &= a
        return (~result) & mask

    elif op == "NOR":
        width = widths[0]
        mask = (1 << width) - 1
        result = args[0]
        for a in args[1:]:
            result |= a
        return (~result) & mask

    elif op == "XNOR":
        width = widths[0]
        mask = (1 << width) - 1
        result = 0
        for a in args:
            result ^= a
        return (~result) & mask

    elif op in ("MUX", "ITE"):
        sel = args[0]
        a = args[1]
        b = args[2]
        return a if sel else b

    elif op == "REDUCE_AND":
        width = widths[0]
        val = args[0]
        mask = (1 << width) - 1
        return 1 if (val & mask) == mask else 0

    elif op == "REDUCE_OR":
        val = args[0]
        return 1 if val != 0 else 0

    elif op == "REDUCE_XOR":
        val = args[0]
        result = 0
        while val:
            result ^= (val & 1)
            val >>= 1
        return result

    elif op == "EQ":
        return 1 if args[0] == args[1] else 0

    else:
        raise CircError(f"Unknown operator '{op}'")


def _eval_expr(expr, values, signals):
    """Evaluate an expression AST given a dict of signal name -> integer value.
    Returns integer value.
    """
    if isinstance(expr, ExprLit):
        return expr.value

    elif isinstance(expr, ExprIdent):
        return values[expr.name]

    elif isinstance(expr, ExprIndex):
        base_val = _eval_expr(expr.expr, values, signals)
        # Extract bit at position index (0 = LSB)
        return (base_val >> expr.index) & 1

    elif isinstance(expr, ExprSlice):
        base_val = _eval_expr(expr.expr, values, signals)
        # Extract bits [hi:lo]
        lo = expr.lo
        hi = expr.hi
        width = hi - lo + 1
        mask = (1 << width) - 1
        return (base_val >> lo) & mask

    elif isinstance(expr, ExprConcat):
        # Concatenation: e1 is MSB, e2, e3... follow
        result = 0
        for e in expr.exprs:
            val = _eval_expr(e, values, signals)
            w = infer_width(e, signals, None) if signals else None
            if w is None:
                # Calculate width without signals
                if isinstance(e, ExprLit):
                    w = e.width
                elif isinstance(e, ExprIdent):
                    w = signals[e.name].width if e.name in signals else 1
                else:
                    w = 1
            result = (result << w) | val
        return result

    elif isinstance(expr, ExprCall):
        arg_vals = [_eval_expr(arg, values, signals) for arg in expr.args]
        # Get widths
        arg_widths = []
        for arg in expr.args:
            if isinstance(arg, ExprLit):
                arg_widths.append(arg.width)
            elif isinstance(arg, ExprIdent):
                arg_widths.append(signals[arg.name].width)
            elif isinstance(arg, ExprIndex):
                arg_widths.append(1)
            elif isinstance(arg, ExprSlice):
                arg_widths.append(arg.hi - arg.lo + 1)
            elif isinstance(arg, ExprConcat):
                arg_widths.append(infer_width(arg, signals, None))
            elif isinstance(arg, ExprCall):
                arg_widths.append(infer_width(arg, signals, None))
            else:
                arg_widths.append(1)
        return _vector_op(expr.op, arg_vals, arg_widths)

    else:
        raise CircError("Unknown expression type")


def _topo_sort_assignments(assignments):
    """Topologically sort assignments based on dependencies."""
    # Build dependency graph
    deps = {}  # lhs -> set of names it depends on
    lhs_list = []

    def _collect_deps(expr, dep_set):
        if isinstance(expr, ExprIdent):
            dep_set.add(expr.name)
        elif isinstance(expr, ExprCall):
            for arg in expr.args:
                _collect_deps(arg, dep_set)
        elif isinstance(expr, ExprIndex):
            _collect_deps(expr.expr, dep_set)
        elif isinstance(expr, ExprSlice):
            _collect_deps(expr.expr, dep_set)
        elif isinstance(expr, ExprConcat):
            for e in expr.exprs:
                _collect_deps(e, dep_set)

    for lhs, expr, lhs_line, lhs_col in assignments:
        dep_set = set()
        _collect_deps(expr, dep_set)
        deps[lhs] = dep_set
        lhs_list.append(lhs)

    # Topological sort (Kahn's algorithm)
    in_degree = {lhs: 0 for lhs in lhs_list}
    # adjacency: from dependency -> list of dependents
    adj = defaultdict(list)
    for lhs in lhs_list:
        for dep in deps[lhs]:
            if dep in deps:  # only track dependencies on other assigned signals
                adj[dep].append(lhs)
                in_degree[lhs] += 1

    queue = [lhs for lhs in lhs_list if in_degree[lhs] == 0]
    queue.sort()  # deterministic ordering
    sorted_lhs = []

    while queue:
        node = queue.pop(0)
        sorted_lhs.append(node)
        for neighbor in sorted(adj[node]):
            in_degree[neighbor] -= 1
            if in_degree[neighbor] == 0:
                queue.append(neighbor)
        queue.sort()

    # Build assignment map: lhs -> (expr, line, col)
    assign_map = {}
    for lhs, expr, lhs_line, lhs_col in assignments:
        assign_map[lhs] = (expr, lhs_line, lhs_col)

    return [(lhs, assign_map[lhs][0], assign_map[lhs][1], assign_map[lhs][2])
            for lhs in sorted_lhs]


def cmd_eval(filepath, set_pairs, default_val, allow_extra, use_json, radix='bin'):
    """Evaluate a circuit with given inputs."""
    if not os.path.isfile(filepath):
        raise FileNotFoundError_(
            f"File not found: {filepath}",
            file=filepath,
        )

    with open(filepath, 'r') as f:
        text = f.read()

    tokens = tokenize(text, filepath)

    parser = Parser(tokens, filepath)
    inputs, outputs, wires, assignments = parser.parse()

    sorted_inputs, sorted_outputs, signals = validate_circuit(
        inputs, outputs, wires, assignments, filepath,
    )

    # Build input name set
    input_names = {sig.name: sig for sig in inputs}
    output_names = {sig.name for sig in outputs}

    # Parse --set values
    input_values = {}
    for name, value_str in set_pairs:
        # Check for unknown inputs
        if name not in input_names and not allow_extra:
            raise UnknownInputError(
                f"Unknown input '{name}'",
                file=filepath,
            )

        # Parse value
        try:
            lit_width, lit_value = parse_literal_value(value_str, filepath, None, None)
        except CircParseError as e:
            raise InputValueParseError(
                f"Invalid input value '{value_str}' for '{name}': {e.message}",
                file=filepath,
            )

        if name in input_names:
            # Check width matches
            expected_width = input_names[name].width
            if lit_width != expected_width:
                raise InputWidthMismatchError(
                    f"Input width mismatch for '{name}': expected {expected_width}, got {lit_width}",
                    file=filepath,
                )
            input_values[name] = lit_value

    # Check for missing inputs
    missing = set(input_names.keys()) - set(input_values.keys())
    if missing:
        if default_val is not None:
            for name in missing:
                sig = input_names[name]
                if sig.width != 1:
                    raise InputValueParseError(
                        f"Cannot use --default for vector input '{name}' (width {sig.width})",
                        file=filepath,
                    )
                input_values[name] = default_val
        else:
            missing_sorted = sorted(missing)
            raise MissingInputError(
                f"Missing input(s): {', '.join(missing_sorted)}",
                file=filepath,
            )

    # Topologically sort assignments for evaluation order
    sorted_assignments = _topo_sort_assignments(assignments)

    # Evaluate
    values = dict(input_values)
    for lhs, expr, lhs_line, lhs_col in sorted_assignments:
        values[lhs] = _eval_expr(expr, values, signals)

    # Collect output results sorted by name
    output_results = []
    for name in sorted(output_names):
        sig = signals[name]
        value = values[name]
        formatted = format_value(value, sig.width, radix)
        output_results.append({
            "name": name,
            "msb": sig.msb,
            "lsb": sig.lsb,
            "value": formatted
        })

    if use_json:
        print(json_success(
            "eval",
            mode="2val",
            radix=radix,
            inputs=sorted_inputs,
            outputs=output_results,
        ))
    else:
        for item in output_results:
            print(f"{item['name']}={item['value']}")


# ─── Main CLI ────────────────────────────────────────────────────────────────

def _parse_eval_args(cmd_args):
    """Parse arguments for the eval command.
    Returns (filepath, set_pairs, default_val, allow_extra, use_json, radix).
    """
    filepath = None
    set_pairs = []
    default_val = None
    allow_extra = False
    use_json = False
    radix = 'bin'

    i = 0
    while i < len(cmd_args):
        arg = cmd_args[i]

        if arg == '--json':
            use_json = True
            i += 1
        elif arg == '--set':
            if i + 1 >= len(cmd_args):
                raise CliUsageError("--set requires an argument of the form name=value")
            pair = cmd_args[i + 1]
            if '=' not in pair:
                raise CliUsageError(f"--set argument must be of the form name=value, got '{pair}'")
            name, value = pair.split('=', 1)
            set_pairs.append((name, value))
            i += 2
        elif arg == '--default':
            if i + 1 >= len(cmd_args):
                raise CliUsageError("--default requires an argument (0 or 1)")
            val_str = cmd_args[i + 1]
            if val_str not in ('0', '1'):
                raise CliUsageError(f"--default argument must be 0 or 1, got '{val_str}'")
            default_val = int(val_str)
            i += 2
        elif arg == '--allow-extra':
            allow_extra = True
            i += 1
        elif arg == '--radix':
            if i + 1 >= len(cmd_args):
                raise CliUsageError("--radix requires an argument (bin, hex, or dec)")
            radix_val = cmd_args[i + 1].lower()
            if radix_val not in ('bin', 'hex', 'dec'):
                raise CliUsageError(f"--radix argument must be bin, hex, or dec, got '{radix_val}'")
            radix = radix_val
            i += 2
        elif arg.startswith('--'):
            raise CliUsageError(f"Unknown option '{arg}'")
        else:
            # Positional argument - should be the file
            if filepath is None:
                filepath = arg
            else:
                raise CliUsageError(f"Unexpected argument '{arg}'")
            i += 1

    if filepath is None:
        raise CliUsageError("eval command requires a file argument.")

    return filepath, set_pairs, default_val, allow_extra, use_json, radix


def main():
    args = sys.argv[1:]

    # Check for --help
    if '--help' in args:
        # Always plain text for help
        print_help()
        sys.exit(EXIT_SUCCESS)

    # Check for --version
    if '--version' in args:
        use_json = '--json' in args
        if use_json:
            print(json.dumps({
                "ok": True,
                "command": "__version__",
                "version": VERSION,
            }, separators=(',', ':')))
        else:
            print(VERSION)
        sys.exit(EXIT_SUCCESS)

    # Check for --json flag globally
    use_json = '--json' in args

    # Filter out --json from args for command parsing
    cmd_args = [a for a in args if a != '--json']

    if not cmd_args:
        # No command specified
        err = CliUsageError("No command specified. Use --help for usage information.")
        if use_json:
            print(json_error("__cli__", err))
        else:
            print(f"Error: {err.message}", file=sys.stderr)
        sys.exit(EXIT_CLI_USAGE)

    command = cmd_args[0]

    if command == 'check':
        if len(cmd_args) < 2:
            err = CliUsageError("check command requires a file argument.")
            if use_json:
                print(json_error("check", err))
            else:
                print(f"Error: {err.message}", file=sys.stderr)
            sys.exit(EXIT_CLI_USAGE)

        filepath = cmd_args[1]
        try:
            cmd_check(filepath, use_json)
        except CircError as e:
            if use_json:
                print(json_error("check", e))
            else:
                loc = ""
                if e.file:
                    loc += f" in {e.file}"
                if e.line is not None:
                    loc += f" at line {e.line}"
                if e.col is not None:
                    loc += f", col {e.col}"
                print(f"Error: {e.message}{loc}", file=sys.stderr)
            sys.exit(e.exit_code)
        except Exception as e:
            err = CircError(f"Internal error: {e}")
            if use_json:
                print(json_error("check", err))
            else:
                print(f"Internal error: {e}", file=sys.stderr)
            sys.exit(EXIT_INTERNAL_ERROR)

    elif command == 'eval':
        try:
            filepath, set_pairs, default_val, allow_extra, eval_json, radix = _parse_eval_args(cmd_args[1:])
            # Global --json overrides eval-specific json detection
            actual_json = use_json or eval_json
            cmd_eval(filepath, set_pairs, default_val, allow_extra, actual_json, radix)
        except CliUsageError as e:
            if use_json:
                print(json_error("eval", e))
            else:
                print(f"Error: {e.message}", file=sys.stderr)
            sys.exit(e.exit_code)
        except CircError as e:
            if use_json:
                print(json_error("eval", e))
            else:
                loc = ""
                if e.file:
                    loc += f" in {e.file}"
                if e.line is not None:
                    loc += f" at line {e.line}"
                if e.col is not None:
                    loc += f", col {e.col}"
                print(f"Error: {e.message}{loc}", file=sys.stderr)
            sys.exit(e.exit_code)
        except Exception as e:
            err = CircError(f"Internal error: {e}")
            if use_json:
                print(json_error("eval", err))
            else:
                print(f"Internal error: {e}", file=sys.stderr)
            sys.exit(EXIT_INTERNAL_ERROR)

    else:
        err = CliUsageError(f"Unknown command '{command}'. Use --help for usage information.")
        if use_json:
            print(json_error("__cli__", err))
        else:
            print(f"Error: {err.message}", file=sys.stderr)
        sys.exit(EXIT_CLI_USAGE)


def print_help():
    print("""Usage: circopt.py <command> [options] [arguments]

Global flags:
  --help       Print this help message and exit
  --version    Print version and exit
  --json       Output results as JSON

Commands:
  check <file.circ>                      Parse and validate a circuit file
  eval <file.circ> [options]             Evaluate circuit with given inputs
    --set name=value                     Set input value (can be repeated)
    --default 0|1                        Default value for missing inputs
    --allow-extra                        Allow unknown input names
    --radix bin|hex|dec                  Output format (default: bin)
    --json                               Output results as JSON""")


if __name__ == '__main__':
    main()
