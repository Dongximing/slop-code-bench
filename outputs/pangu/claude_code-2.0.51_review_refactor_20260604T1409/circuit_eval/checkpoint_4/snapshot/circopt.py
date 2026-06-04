#!/usr/bin/env python3
"""
Circuit optimizer CLI - Part 3: Vectors, richer expressions, and --radix
"""

import os
import sys
import re
import json
import argparse
from typing import Optional, List, Dict, Tuple, Set


VERSION = "0.2.0"


# =============================================================================
# Error Classes
# =============================================================================

class CircuitError(Exception):
    """Base error for circuit-related errors."""
    def __init__(self, message: str, file: Optional[str] = None,
                 line: Optional[int] = None, col: Optional[int] = None):
        super().__init__(message)
        self.message = message
        self.file = file
        self.line = line
        self.col = col


class CliUsageError(CircuitError):
    exit_code = 1


class FileNotFoundError(CircuitError):
    exit_code = 1

    def __init__(self, message: str, filepath: str):
        super().__init__(message, file=filepath)


class CircParseError(CircuitError):
    exit_code = 2


class WidthMismatchError(CircuitError):
    exit_code = 3


class IndexOutOfBoundsError(CircuitError):
    exit_code = 3


class InputWidthMismatchError(CircuitError):
    exit_code = 3


class DeclarationAfterAssignmentError(CircuitError):
    exit_code = 3


class DuplicateNameError(CircuitError):
    exit_code = 3


class UndefinedNameError(CircuitError):
    exit_code = 3


class UnassignedSignalError(CircuitError):
    exit_code = 3


class InputAssignmentError(CircuitError):
    exit_code = 3


class MultipleAssignmentError(CircuitError):
    exit_code = 3


class MissingInputError(CircuitError):
    exit_code = 1


class UnknownInputError(CircuitError):
    exit_code = 1


class InputValueParseError(CircuitError):
    exit_code = 2


class RadixNotAllowedIn3ValError(CircuitError):
    exit_code = 1


class ArityError(CircuitError):
    exit_code = 3


class CycleError(CircuitError):
    exit_code = 3


# =============================================================================
# AST Nodes
# =============================================================================

class ASTNode:
    pass


class SignalRef(ASTNode):
    """Reference to a signal with optional slice/index.

    name: signal name
    msb: None for scalar reference, otherwise MSB of slice/index
    lsb: None for scalar reference, LSB of slice (for single bit, msb=lsb)
    """
    def __init__(self, name: str, msb: Optional[int] = None, lsb: Optional[int] = None):
        self.name = name
        self.msb = msb
        self.lsb = lsb

    def __repr__(self):
        if self.msb is None:
            return f"SignalRef({self.name})"
        elif self.msb == self.lsb:
            return f"SignalRef({self.name}[{self.msb}])"
        else:
            return f"SignalRef({self.name}[{self.msb}:{self.lsb}])"


class Literal(ASTNode):
    """Literal value with width.

    value: integer value
    width: bit width
    """
    def __init__(self, value: int, width: int):
        self.value = value
        self.width = width

    def __repr__(self):
        return f"Literal(0b{self.value:0{self.width}b}, width={self.width})"


class Concat(ASTNode):
    """Concatenation: {e1, e2, ..., ek}"""
    def __init__(self, parts: List[ASTNode]):
        self.parts = parts

    def __repr__(self):
        return f"Concat([{', '.join(repr(p) for p in self.parts)}])"


class OpCall(ASTNode):
    """Operator call: OP(arg1, arg2, ...)"""
    def __init__(self, op: str, args: List[ASTNode]):
        self.op = op
        self.args = args

    def __repr__(self):
        return f"OpCall({self.op}, [{', '.join(repr(a) for a in self.args)}])"


# =============================================================================
# Signal Declaration
# =============================================================================

class SignalDecl:
    """Signal declaration with width information.

    name: signal name
    sig_type: 'input', 'output', or 'wire'
    msb: MSB of the signal (None for scalar in old format)
    lsb: LSB of the signal (None for scalar in old format)
    """
    def __init__(self, name: str, sig_type: str, msb: Optional[int] = None, lsb: Optional[int] = None):
        self.name = name
        self.sig_type = sig_type
        self.msb = msb
        self.lsb = lsb

    @property
    def width(self) -> int:
        if self.msb is None:
            return 1
        return self.msb - self.lsb + 1

    def __repr__(self):
        if self.msb is None:
            return f"SignalDecl({self.name}, {self.sig_type})"
        return f"SignalDecl({self.name}[{self.msb}:{self.lsb}], {self.sig_type})"


# =============================================================================
# Token Types
# =============================================================================

TOKEN_ID = 'ID'
TOKEN_KW = 'KW'
TOKEN_OP = 'OP'
TOKEN_LITERAL = 'LITERAL'
TOKEN_NUMBER = 'NUMBER'
TOKEN_LBRACE = '{'
TOKEN_RBRACE = '}'
TOKEN_LBRACK = '['
TOKEN_RBRACK = ']'
TOKEN_COLON = ':'
TOKEN_EQ = '='
TOKEN_COMMA = ','
TOKEN_LPAREN = '('
TOKEN_RPAREN = ')'
TOKEN_EOF = 'EOF'


class Token:
    def __init__(self, type: str, value: str, line: int, col: int):
        self.type = type
        self.value = value
        self.line = line
        self.col = col

    def __repr__(self):
        return f"Token({self.type}, {self.value!r}, line={self.line}, col={self.col})"


# =============================================================================
# Lexer
# =============================================================================

def tokenize(line: str, line_num: int) -> List[Token]:
    """Tokenize a single line.

    Returns list of Token objects.
    """
    tokens = []
    pos = 0
    length = len(line)

    # Operator patterns (case-insensitive)
    operators = {'AND', 'OR', 'XOR', 'NAND', 'NOR', 'XNOR', 'NOT', 'BUF',
                 'MUX', 'ITE', 'REDUCE_AND', 'REDUCE_OR', 'REDUCE_XOR', 'EQ'}

    while pos < length:
        col_pos = pos + 1  # 1-based column

        # Skip whitespace
        if line[pos] in ' \t':
            pos += 1
            continue

        # Comment
        if line[pos] == '#':
            break  # Rest of line is comment, ignore

        # Identifiers (letters, underscores, digits after first char)
        if re.match(r'[a-zA-Z_]', line[pos:]):
            match = re.match(r'[a-zA-Z_][a-zA-Z0-9_]*', line[pos:])
            if match:
                token = match.group()
                upper_token = token.upper()
                if upper_token in operators:
                    tokens.append(Token(TOKEN_OP, upper_token, line_num, col_pos))
                elif upper_token in {'INPUT', 'OUTPUT', 'WIRE'}:
                    tokens.append(Token(TOKEN_KW, upper_token, line_num, col_pos))
                else:
                    tokens.append(Token(TOKEN_ID, token, line_num, col_pos))
                pos += len(token)
                continue

        # Numbers (decimal integers for msb:lsb notation)
        if re.match(r'[0-9]', line[pos:]):
            match = re.match(r'[0-9]+', line[pos:])
            if match:
                tokens.append(Token(TOKEN_NUMBER, match.group(), line_num, col_pos))
                pos += len(match.group())
                continue

        # Literals: 0, 1 (scalar), sized/unsized vectors
        if line[pos] in '01':
            # Check if this is a sized literal like 8'b1010
            rest = line[pos:]
            sized_match = re.match(r'(\d+)\'[bBhHdD]([\d01a-fA-F_]+)', rest)
            if sized_match:
                # This is a sized literal
                width = int(sized_match.group(1))
                radix_str = sized_match.group(2).lower()
                value_str = sized_match.group(3).replace('_', '')

                # Parse based on radix
                if radix_str in ('b', 'h', 'd'):
                    try:
                        if radix_str == 'b':
                            value = int(value_str, 2)
                        elif radix_str == 'h':
                            value = int(value_str, 16)
                        else:
                            value = int(value_str, 10)
                    except ValueError:
                        raise CircParseError(
                            f"Invalid literal: {sized_match.group(0)}",
                            file=None, line=line_num, col=col_pos
                        )

                    # Check value fits in width
                    if value >= (1 << width):
                        raise CircParseError(
                            f"Value {value} too large for {width}-bit literal",
                            file=None, line=line_num, col=col_pos
                        )

                    tokens.append(Token(TOKEN_LITERAL, f"{width}'{radix_str}{value_str}", line_num, col_pos))
                    pos += len(sized_match.group(0))
                    continue
            else:
                # Scalar literal
                tokens.append(Token(TOKEN_LITERAL, line[pos], line_num, col_pos))
                pos += 1
                continue

        # Check for 0b or 0x binary/hex literals
        if line[pos:pos+2] in ('0b', '0x'):
            prefix = line[pos:pos+2]
            rest = line[pos+2:]
            # Must have at least one digit after prefix
            match = re.match(r'[0-9a-fA-F]+', rest)
            if match:
                digits = match.group()
                tokens.append(Token(TOKEN_LITERAL, f"{prefix}{digits}", line_num, col_pos))
                pos += 2 + len(digits)
                continue
            else:
                raise CircParseError(
                    f"Binary/hex literal must have at least one digit",
                    file=None, line=line_num, col=col_pos
                )

        # Braces and brackets
        if line[pos] in '{}[]':
            tokens.append(Token(line[pos], line[pos], line_num, col_pos))
            pos += 1
            continue

        # Colon
        if line[pos] == ':':
            tokens.append(Token(TOKEN_COLON, ':', line_num, col_pos))
            pos += 1
            continue

        # Comma
        if line[pos] == ',':
            tokens.append(Token(TOKEN_COMMA, ',', line_num, col_pos))
            pos += 1
            continue

        # Equals sign
        if line[pos] == '=':
            tokens.append(Token(TOKEN_EQ, '=', line_num, col_pos))
            pos += 1
            continue

        # Unknown character
        raise CircParseError(
            f"Unexpected character: {line[pos]}",
            file=None, line=line_num, col=col_pos
        )

    tokens.append(Token(TOKEN_EOF, '', line_num, len(line) + 1))
    return tokens


# =============================================================================
# Parser
# =============================================================================

class Parser:
    """Parser for circuit expressions."""

    def __init__(self, tokens: List[Token], filepath: str):
        self.tokens = tokens
        self.filepath = filepath
        self.pos = 0

    def peek(self) -> Token:
        return self.tokens[self.pos]

    def consume(self, expected_type: Optional[str] = None,
                expected_value: Optional[str] = None) -> Token:
        tok = self.peek()
        if expected_type is not None and tok.type != expected_type:
            raise self.error(f"Expected token type {expected_type}, got {tok.type}")
        if expected_value is not None and tok.value != expected_value:
            raise self.error(f"Expected {expected_value}, got {tok.value}")
        self.pos += 1
        return tok

    def error(self, message: str) -> CircParseError:
        tok = self.peek()
        return CircParseError(
            message,
            file=self.filepath,
            line=tok.line,
            col=tok.col
        )

    def parse_expression(self) -> ASTNode:
        """Parse an expression: concatenation, signal ref, literal, or op call."""
        tok = self.peek()

        # If it's an identifier followed by [, it's a slice/index
        if tok.type == TOKEN_ID:
            name = tok.value
            self.consume(TOKEN_ID)

            # Check for slice or index
            if self.peek().type == TOKEN_LBRACK:
                return self.parse_signal_ref_with_select(name)
            else:
                return SignalRef(name)

        elif tok.type == TOKEN_LITERAL:
            return self.parse_literal()

        elif tok.type == TOKEN_LBRACE:
            # Concatenation: {e1, e2, ...}
            return self.parse_concat()

        elif tok.type == TOKEN_OP:
            # Operator call: OP(...)
            return self.parse_op_call()

        else:
            raise self.error(f"Unexpected token in expression: {tok.value}")

    def parse_signal_ref_with_select(self, name: str) -> SignalRef:
        """Parse a signal reference with optional slice/index: v[i] or v[hi:lo]"""
        self.consume(TOKEN_LBRACK)

        # Parse first number (msb or index)
        if self.peek().type != TOKEN_NUMBER:
            raise self.error("Expected number in slice/index")
        msb_val = int(self.consume().value)

        # Check if it's a slice (with colon) or single bit index
        if self.peek().type == TOKEN_COLON:
            self.consume(TOKEN_COLON)  # consume ':'

            # Parse lsb
            if self.peek().type != TOKEN_NUMBER:
                raise self.error("Expected number after colon in slice")
            lsb_val = int(self.consume().value)

            self.consume(TOKEN_RBRACK)

            # Validate: msb >= lsb >= 0
            if msb_val < lsb_val:
                raise self.error(f"Invalid slice: msb ({msb_val}) < lsb ({lsb_val})")
            if lsb_val < 0:
                raise self.error(f"Invalid slice: lsb ({lsb_val}) < 0")

            return SignalRef(name, msb_val, lsb_val)
        else:
            # Single bit index
            self.consume(TOKEN_RBRACK)
            return SignalRef(name, msb_val, msb_val)  # msb=lsb for single bit

    def parse_literal(self) -> Literal:
        """Parse a literal value."""
        tok = self.consume(TOKEN_LITERAL)
        val = tok.value

        if val in ('0', '1'):
            return Literal(int(val), 1)
        elif val.startswith('0b'):
            # Binary literal: 0b1101
            bits = val[2:]
            if not bits:
                raise self.error("Binary literal must have at least one digit")
            value = int(bits, 2)
            return Literal(value, len(bits))
        elif val.startswith('0x'):
            # Hex literal: 0x0f
            hex_digits = val[2:]
            if not hex_digits:
                raise self.error("Hex literal must have at least one digit")
            value = int(hex_digits, 16)
            return Literal(value, len(hex_digits) * 4)
        else:
            # Sized literal: 8'b1010, 8'hff, 8'd200
            match = re.match(r'(\d+)\'[bBhHdD]([\d01a-fA-F_]+)', val)
            if match:
                width = int(match.group(1))
                radix_str = match.group(2).lower()
                value_str = match.group(3).replace('_', '')

                try:
                    if radix_str == 'b':
                        value = int(value_str, 2)
                    elif radix_str == 'h':
                        value = int(value_str, 16)
                    else:  # 'd'
                        value = int(value_str, 10)
                except ValueError:
                    raise self.error(f"Invalid literal: {val}")

                if value >= (1 << width):
                    raise self.error(f"Value {value} too large for {width}-bit literal")

                return Literal(value, width)
            else:
                raise self.error(f"Invalid literal format: {val}")

    def parse_concat(self) -> Concat:
        """Parse concatenation: {e1, e2, ...}"""
        self.consume(TOKEN_LBRACE)

        parts = []
        while True:
            if self.peek().type == TOKEN_RBRACE:
                break

            parts.append(self.parse_expression())

            if self.peek().type == TOKEN_COMMA:
                self.consume(TOKEN_COMMA)
                continue
            elif self.peek().type == TOKEN_RBRACE:
                break

        self.consume(TOKEN_RBRACE)
        return Concat(parts)

    def parse_op_call(self) -> OpCall:
        """Parse operator call: OP(arg1, arg2, ...)"""
        op_tok = self.consume(TOKEN_OP)
        op = op_tok.value

        self.consume(TOKEN_LPAREN)

        args = []
        while True:
            if self.peek().type == TOKEN_RPAREN:
                break

            args.append(self.parse_expression())

            if self.peek().type == TOKEN_COMMA:
                self.consume(TOKEN_COMMA)
                continue
            elif self.peek().type == TOKEN_RPAREN:
                break

        self.consume(TOKEN_RPAREN)
        return OpCall(op, args)

    def parse_assignment_line(self) -> Tuple[str, ASTNode]:
        """Parse an assignment line: lhs = expression"""
        # Parse LHS (identifier)
        lhs_tok = self.consume(TOKEN_ID)
        lhs = lhs_tok.value

        self.consume(TOKEN_EQ)

        # Parse RHS expression
        expr = self.parse_expression()

        # Make sure we're at EOF
        if self.peek().type != TOKEN_EOF:
            raise self.error(f"Unexpected token after expression: {self.peek().value}")

        return lhs, expr

    def parse_declaration(self) -> List[SignalDecl]:
        """Parse a declaration line: INPUT/OUTPUT/WIRE name[msb:lsb]"""
        kw_tok = self.consume(TOKEN_KW)
        decl_type = kw_tok.value.lower()

        decls = []
        while True:
            # Parse identifier
            id_tok = self.consume(TOKEN_ID)
            name = id_tok.value

            # Check for width specifier
            msb = None
            lsb = None
            if self.peek().type == TOKEN_LBRACK:
                self.consume(TOKEN_LBRACK)

                # Parse msb
                if self.peek().type != TOKEN_NUMBER:
                    raise self.error(f"Expected number after [ for {name}")
                msb = int(self.consume().value)

                # Check for slice or single bit
                if self.peek().type == TOKEN_COLON:
                    self.consume(TOKEN_COLON)

                    if self.peek().type != TOKEN_NUMBER:
                        raise self.error(f"Expected number after : for {name}")
                    lsb = int(self.consume().value)

                    if msb < lsb:
                        raise self.error(f"Invalid range for {name}: msb ({msb}) < lsb ({lsb})")
                    if lsb < 0:
                        raise self.error(f"Invalid range for {name}: lsb ({lsb}) < 0")
                else:
                    # Single bit - msb=lsb
                    lsb = msb

                self.consume(TOKEN_RBRACK)

            decls.append(SignalDecl(name, decl_type, msb, lsb))

            # Check for more names or end of line
            if self.peek().type == TOKEN_EOF:
                break
            elif self.peek().type == TOKEN_COMMA:
                self.consume(TOKEN_COMMA)
                continue
            else:
                raise self.error(f"Unexpected token: {self.peek().value}")

        return decls


def parse_circ_file(filepath: str) -> Tuple[List[SignalDecl], List[Tuple[str, ASTNode]]]:
    """Parse a .circ file and return (declarations, assignments).

    Returns:
        declarations: List of SignalDecl objects
        assignments: List of (lhs, expression) tuples
    """
    with open(filepath, 'r') as f:
        lines = f.readlines()

    declarations = []
    assignments = []
    seen_assignment = False

    for line_num, line in enumerate(lines, 1):
        line = line.rstrip('\n\r')

        # Skip empty lines and comments
        stripped = line.lstrip()
        if not stripped or stripped.startswith('#'):
            continue

        try:
            tokens = tokenize(line, line_num)
        except CircParseError as e:
            if e.file is None:
                e.file = filepath
            raise

        if not tokens:
            continue

        parser = Parser(tokens, filepath)

        # Check if it's a declaration
        if tokens[0].type == TOKEN_KW:
            # Declaration line
            if seen_assignment:
                raise DeclarationAfterAssignmentError(
                    f"Declaration found after assignment",
                    file=filepath,
                    line=line_num,
                    col=1
                )

            try:
                decls = parser.parse_declaration()
                declarations.extend(decls)
            except CircParseError as e:
                if e.file is None:
                    e.file = filepath
                raise

        elif tokens[0].type == TOKEN_ID:
            # Assignment line
            seen_assignment = True

            try:
                lhs, expr = parser.parse_assignment_line()
                assignments.append((lhs, expr))
            except CircParseError as e:
                if e.file is None:
                    e.file = filepath
                raise
        else:
            raise CircParseError(
                f"Expected keyword (input/output/wire) or identifier",
                file=filepath,
                line=line_num,
                col=1
            )

    return declarations, assignments


# =============================================================================
# Expression Evaluator (for circuit evaluation)
# =============================================================================

def eval_expression(expr: ASTNode, signal_values: Dict[str, int],
                    signal_widths: Dict[str, int],
                    filepath: str) -> Tuple[int, int]:
    """Evaluate an expression and return (value, width).

    Args:
        expr: AST node to evaluate
        signal_values: Dict mapping signal names to their integer values
        signal_widths: Dict mapping signal names to their widths
        filepath: For error reporting

    Returns:
        Tuple of (value, width)
    """
    if isinstance(expr, SignalRef):
        if expr.name in signal_values:
            # Get the full signal value
            full_value = signal_values[expr.name]
            width = signal_widths[expr.name]

            if expr.msb is None:
                # Scalar reference
                return full_value, 1
            else:
                # Slice or single bit
                if expr.msb >= width or expr.lsb >= width:
                    raise IndexOutOfBoundsError(
                        f"Index {expr.msb}:{expr.lsb} out of bounds for signal {expr.name} (width {width})",
                        file=filepath
                    )
                # Extract bits from lsb to msb
                mask = ((1 << (expr.msb - expr.lsb + 1)) - 1) << expr.lsb
                slice_value = (full_value & mask) >> expr.lsb
                return slice_value, expr.msb - expr.lsb + 1
        else:
            raise UndefinedNameError(
                f"Undefined signal: {expr.name}",
                file=filepath
            )

    elif isinstance(expr, Literal):
        return expr.value, expr.width

    elif isinstance(expr, Concat):
        # Evaluate all parts
        parts_values = []
        total_width = 0
        for part in expr.parts:
            val, width = eval_expression(part, signal_values, signal_widths, filepath)
            parts_values.append((val, width))
            total_width += width

        # Concatenate: first part is MSB
        result = 0
        for val, width in parts_values:
            result = (result << width) | val

        return result, total_width

    elif isinstance(expr, OpCall):
        op = expr.op.upper()
        args_results = []
        for arg in expr.args:
            val, width = eval_expression(arg, signal_values, signal_widths, filepath)
            args_results.append((val, width))

        # Bitwise operators (width-preserving)
        if op in ('NOT', 'BUF'):
            if len(args_results) != 1:
                raise WidthMismatchError(
                    f"Operator {op} requires 1 operand, got {len(args_results)}",
                    file=filepath
                )
            val, width = args_results[0]
            if op == 'NOT':
                return (~val) & ((1 << width) - 1), width
            else:  # BUF
                return val, width

        elif op in ('AND', 'OR', 'XOR', 'NAND', 'NOR', 'XNOR'):
            if len(args_results) < 2:
                raise WidthMismatchError(
                    f"Operator {op} requires at least 2 operands, got {len(args_results)}",
                    file=filepath
                )

            # Check all operands have same width
            first_width = args_results[0][1]
            for val, width in args_results:
                if width != first_width:
                    raise WidthMismatchError(
                        f"Operator {op}: operand widths {width} and {first_width} don't match",
                        file=filepath
                    )

            mask = (1 << first_width) - 1
            result = args_results[0][0]

            if op == 'AND':
                for val, _ in args_results[1:]:
                    result &= val
            elif op == 'OR':
                for val, _ in args_results[1:]:
                    result |= val
            elif op == 'XOR':
                for val, _ in args_results[1:]:
                    result ^= val
            elif op == 'NAND':
                for val, _ in args_results[1:]:
                    result &= val
                result = mask & ~result
            elif op == 'NOR':
                for val, _ in args_results[1:]:
                    result |= val
                result = mask & ~result
            elif op == 'XNOR':
                for val, _ in args_results[1:]:
                    result ^= val
                result = mask & ~result

            return result & mask, first_width

        elif op in ('MUX', 'ITE'):
            if len(args_results) != 3:
                raise ArityError(
                    f"Operator {op} requires 3 operands, got {len(args_results)}",
                    file=filepath
                )

            sel_val, sel_width = args_results[0]
            a_val, a_width = args_results[1]
            b_val, b_width = args_results[2]

            if sel_width != 1:
                raise WidthMismatchError(
                    f"Operator {op}: selector width {sel_width} must be 1",
                    file=filepath
                )
            if a_width != b_width:
                raise WidthMismatchError(
                    f"Operator {op}: operand widths {a_width} and {b_width} don't match",
                    file=filepath
                )

            result = a_val if sel_val else b_val
            return result, a_width

        elif op in ('REDUCE_AND', 'REDUCE_OR', 'REDUCE_XOR'):
            if len(args_results) != 1:
                raise WidthMismatchError(
                    f"Operator {op} requires 1 operand, got {len(args_results)}",
                    file=filepath
                )

            val, width = args_results[0]
            if width < 1:
                raise WidthMismatchError(
                    f"Operator {op}: operand width {width} must be >= 1",
                    file=filepath
                )

            mask = (1 << width) - 1
            val &= mask

            if op == 'REDUCE_AND':
                # Check if all bits are 1
                result = 1 if val == mask else 0
            elif op == 'REDUCE_OR':
                result = 1 if val != 0 else 0
            else:  # REDUCE_XOR
                result = 0
                for i in range(width):
                    result ^= (val >> i) & 1

            return result, 1

        elif op == 'EQ':
            if len(args_results) != 2:
                raise ArityError(
                    f"Operator EQ requires 2 operands, got {len(args_results)}",
                    file=filepath
                )

            a_val, a_width = args_results[0]
            b_val, b_width = args_results[1]

            if a_width != b_width:
                raise WidthMismatchError(
                    f"Operator EQ: operand widths {a_width} and {b_width} don't match",
                    file=filepath
                )

            return int(a_val == b_val), 1

        else:
            raise ArityError(
                f"Unknown operator: {op}",
                file=filepath
            )

    else:
        raise ValueError(f"Unknown AST node type: {type(expr)}")


def parse_input_literal(value_str: str, expected_width: int,
                        filepath: str, line: int = None, col: int = None) -> int:
    """Parse an input literal value and return the integer value.

    Validates that the width matches the expected width.
    """
    if value_str in ('0', '1'):
        if expected_width != 1:
            raise InputWidthMismatchError(
                f"Input width mismatch: got 1 but expected {expected_width}",
                file=filepath, line=line, col=col
            )
        return int(value_str)

    elif value_str.startswith('0b'):
        bits = value_str[2:]
        if not bits:
            raise InputValueParseError(
                f"Binary literal must have at least one digit",
                file=filepath, line=line, col=col
            )
        value = int(bits, 2)
        width = len(bits)

        if width != expected_width:
            raise InputWidthMismatchError(
                f"Input width mismatch: got {width} but expected {expected_width}",
                file=filepath, line=line, col=col
            )
        return value

    elif value_str.startswith('0x'):
        hex_digits = value_str[2:]
        if not hex_digits:
            raise InputValueParseError(
                f"Hex literal must have at least one digit",
                file=filepath, line=line, col=col
            )
        value = int(hex_digits, 16)
        width = len(hex_digits) * 4

        if width != expected_width:
            raise InputWidthMismatchError(
                f"Input width mismatch: got {width} but expected {expected_width}",
                file=filepath, line=line, col=col
            )
        return value

    else:
        # Sized literal: 8'b1010, 8'hff, 8'd200
        match = re.match(r'(\d+)\'[bBhHdD]([\d01a-fA-F_]+)', value_str)
        if match:
            width = int(match.group(1))
            radix_str = match.group(2).lower()
            value_str_clean = match.group(3).replace('_', '')

            if width != expected_width:
                raise InputWidthMismatchError(
                    f"Input width mismatch: got {width} but expected {expected_width}",
                    file=filepath, line=line, col=col
                )

            try:
                if radix_str == 'b':
                    value = int(value_str_clean, 2)
                elif radix_str == 'h':
                    value = int(value_str_clean, 16)
                else:  # 'd'
                    value = int(value_str_clean, 10)
            except ValueError:
                raise InputValueParseError(
                    f"Invalid literal: {value_str}",
                    file=filepath, line=line, col=col
                )

            if value >= (1 << width):
                raise CircParseError(
                    f"Value {value} too large for {width}-bit literal",
                    file=filepath, line=line, col=col
                )

            return value
        else:
            raise InputValueParseError(
                f"Invalid input literal format: {value_str}",
                file=filepath, line=line, col=col
            )


def format_vector_value(value: int, width: int, radix: str) -> str:
    """Format a vector value according to the specified radix.

    Args:
        value: Integer value
        width: Bit width
        radix: 'bin', 'hex', or 'dec'

    Returns:
        Formatted string
    """
    value &= (1 << width) - 1

    if radix == 'bin':
        return f"0b{value:0{width}b}"
    elif radix == 'hex':
        # Calculate minimum number of hex digits
        hex_digits = (width + 3) // 4
        return f"0x{value:0{hex_digits}x}"
    elif radix == 'dec':
        return str(value)
    else:
        return str(value)


# =============================================================================
# Validation
# =============================================================================

def validate_circ_file(filepath: str, declarations: List[SignalDecl],
                       assignments: List[Tuple[str, ASTNode]]) -> Dict[str, object]:
    """Validate declarations and assignments, compute widths."""

    # Track all declared signals
    inputs: Dict[str, SignalDecl] = {}
    outputs: Dict[str, SignalDecl] = {}
    wires: Dict[str, SignalDecl] = {}
    all_signals: Dict[str, SignalDecl] = {}

    # Process declarations
    for decl in declarations:
        if decl.name in all_signals:
            raise DuplicateNameError(
                f"Duplicate signal name: {decl.name}",
                file=filepath
            )
        all_signals[decl.name] = decl
        if decl.sig_type == 'input':
            inputs[decl.name] = decl
        elif decl.sig_type == 'output':
            outputs[decl.name] = decl
        elif decl.sig_type == 'wire':
            wires[decl.name] = decl

    # Track assignments
    assigned: Dict[str, Tuple[str, int]] = {}  # name -> (op, line_num)

    # Build dependency graph for cycle detection
    graph: Dict[str, List[str]] = {}

    # Operator arities
    op_arity = {
        'NOT': 1,
        'BUF': 1,
        'AND': 2,
        'OR': 2,
        'XOR': 2,
        'NAND': 2,
        'NOR': 2,
        'XNOR': 2,
        'MUX': 3,
        'ITE': 3,
        'REDUCE_AND': 1,
        'REDUCE_OR': 1,
        'REDUCE_XOR': 1,
        'EQ': 2,
    }

    for i, (lhs, expr) in enumerate(assignments):
        line_num = i + 1

        # Check LHS is valid
        if lhs not in all_signals:
            raise UndefinedNameError(
                f"Undefined signal: {lhs}",
                file=filepath
            )

        signal_decl = all_signals[lhs]
        if signal_decl.sig_type == 'input':
            raise InputAssignmentError(
                f"Cannot assign to input: {lhs}",
                file=filepath
            )

        # Check for multiple assignment
        if lhs in assigned:
            raise MultipleAssignmentError(
                f"Multiple assignment to: {lhs}",
                file=filepath
            )

        assigned[lhs] = ('expr', line_num)

        # Check expression and collect dependencies
        graph[lhs] = []

        def collect_deps(node: ASTNode):
            if isinstance(node, SignalRef):
                if node.msb is not None:  # Has slice
                    # Check bounds against declared width if available
                    if node.name in all_signals:
                        decl = all_signals[node.name]
                        width = decl.width
                        if node.msb >= width or node.lsb >= width:
                            raise IndexOutOfBoundsError(
                                f"Index {node.msb}:{node.lsb} out of bounds for signal {node.name} (width {width})",
                                file=filepath
                            )
                if node.name not in ('0', '1'):
                    graph[lhs].append(node.name)
            elif isinstance(node, Literal):
                pass  # literals have no deps
            elif isinstance(node, Concat):
                for part in node.parts:
                    collect_deps(part)
            elif isinstance(node, OpCall):
                # Check arity
                op_upper = node.op.upper()
                if op_upper in op_arity:
                    min_arity = op_arity[op_upper]
                    if len(node.args) < min_arity:
                        raise ArityError(
                            f"Operator {node.op} requires at least {min_arity} operands, got {len(node.args)}",
                            file=filepath
                        )
                else:
                    raise ArityError(
                        f"Unknown operator: {node.op}",
                        file=filepath
                    )
                for arg in node.args:
                    collect_deps(arg)

        try:
            collect_deps(expr)
        except (IndexOutOfBoundsError, ArityError):
            raise
        except Exception as e:
            raise CircParseError(
                f"Error in expression: {e}",
                file=filepath
            )

    # Check for unassigned signals (only wires and outputs)
    for name, sig_type in [('wire', wires), ('output', outputs)]:
        for decl in all_signals.values():
            if decl.sig_type in ('wire', 'output'):
                if decl.name not in assigned:
                    raise UnassignedSignalError(
                        f"Unassigned signal: {decl.name}",
                        file=filepath
                    )

    # Check for cycles
    def find_cycle() -> Optional[List[str]]:
        visited: Set[str] = set()
        rec_stack: List[str] = []

        def dfs(node: str) -> Optional[List[str]]:
            visited.add(node)
            rec_stack.append(node)

            for neighbor in graph.get(node, []):
                if neighbor not in visited:
                    result = dfs(neighbor)
                    if result:
                        return result
                elif neighbor in rec_stack:
                    start_idx = rec_stack.index(neighbor)
                    return rec_stack[start_idx:] + [node]

            rec_stack.pop()
            return None

        for node in graph:
            if node not in visited:
                cycle = dfs(node)
                if cycle:
                    return cycle
        return None

    cycle = find_cycle()
    if cycle:
        cycle_path = " -> ".join(cycle)
        raise CycleError(
            f"Cycle detected: {cycle_path}",
            file=filepath
        )

    # Return success info
    sorted_inputs = sorted(inputs.values(), key=lambda d: d.name)
    sorted_outputs = sorted(outputs.values(), key=lambda d: d.name)

    return {
        'inputs': [{'name': d.name, 'msb': d.msb, 'lsb': d.lsb} for d in sorted_inputs],
        'outputs': [{'name': d.name, 'msb': d.msb, 'lsb': d.lsb} for d in sorted_outputs],
    }


# =============================================================================
# CLI
# =============================================================================

def format_error_json(error: CircuitError, command: str) -> str:
    """Format error as JSON envelope."""
    error_type = type(error).__name__
    result = {
        'ok': False,
        'command': command,
        'exit_code': error.exit_code,
        'error': {
            'type': error_type,
            'message': error.message,
            'file': error.file,
            'line': error.line,
            'col': error.col,
        }
    }
    return json.dumps(result)


def format_version_json() -> str:
    """Format version output as JSON."""
    return json.dumps({
        'ok': True,
        'command': '__version__',
        'version': VERSION,
    })


def cmd_check(filepath: str, json_output: bool) -> int:
    """Execute check command."""
    # Check file exists first
    if not os.path.exists(filepath):
        if json_output:
            print(format_error_json(
                FileNotFoundError(f"File not found: {filepath}", filepath),
                'check'
            ))
        else:
            print(f"Error: File not found: {filepath}", file=sys.stderr)
        return 1

    try:
        declarations, assignments = parse_circ_file(filepath)
        result = validate_circ_file(filepath, declarations, assignments)

        if json_output:
            output = {
                'ok': True,
                'command': 'check',
                'format': 'circ',
                'inputs': result['inputs'],
                'outputs': result['outputs'],
            }
            print(json.dumps(output))
        else:
            input_strs = []
            for i in result['inputs']:
                if i['msb'] is not None:
                    input_strs.append(f"{i['name']}[{i['msb']}:{i['lsb']}]")
                else:
                    input_strs.append(i['name'])
            output_strs = []
            for o in result['outputs']:
                if o['msb'] is not None:
                    output_strs.append(f"{o['name']}[{o['msb']}:{o['lsb']}]")
                else:
                    output_strs.append(o['name'])
            print(f"Inputs: {', '.join(input_strs)}")
            print(f"Outputs: {', '.join(output_strs)}")

        return 0

    except CircuitError as e:
        if json_output:
            print(format_error_json(e, 'check'))
        else:
            print(f"Error: {e.message}", file=sys.stderr)
        return e.exit_code


# =============================================================================
# 2-Valued Evaluation
# =============================================================================

def cmd_eval(filepath: str, set_inputs: List[str], default: Optional[int],
             allow_extra: bool, json_output: bool, radix: str, mode: str) -> int:
    """Execute eval command with vector support."""

    # Check file exists
    if not os.path.exists(filepath):
        if json_output:
            print(json.dumps({
                'ok': False,
                'command': 'eval',
                'exit_code': 1,
                'error': {
                    'type': 'FileNotFoundError',
                    'message': f"File not found: {filepath}",
                    'file': filepath,
                    'line': None,
                    'col': None,
                }
            }))
        else:
            print(f"Error: File not found: {filepath}", file=sys.stderr)
        return 1

    try:
        declarations, assignments = parse_circ_file(filepath)
        result_info = validate_circ_file(filepath, declarations, assignments)
    except CircuitError as e:
        if json_output:
            print(format_error_json(e, 'eval'))
        else:
            print(f"Error: {e.message}", file=sys.stderr)
        return e.exit_code

    # Check radix restriction for 3val mode
    if mode == '3val' and radix != 'bin':
        error = RadixNotAllowedIn3ValError(
            f"Radix {radix} not allowed in 3-valued mode",
            file=filepath
        )
        if json_output:
            print(format_error_json(error, 'eval'))
        else:
            print(f"Error: {error.message}", file=sys.stderr)
        return error.exit_code

    # Build signal width lookup
    signal_widths = {}
    for decl in declarations:
        signal_widths[decl.name] = decl.width

    # Parse input assignments
    input_values = {}
    for assignment in set_inputs:
        if '=' not in assignment:
            if json_output:
                print(json.dumps({
                    'ok': False,
                    'command': 'eval',
                    'exit_code': 2,
                    'error': {
                        'type': 'InputValueParseError',
                        'message': f"Invalid input format: {assignment}. Expected name=value",
                        'file': None,
                        'line': None,
                        'col': None,
                    }
                }))
            else:
                print(f"Error: Invalid input format: {assignment}. Expected name=value", file=sys.stderr)
            return 2

        name, value_str = assignment.split('=', 1)
        name = name.strip()
        value_str = value_str.strip()

        # Get expected width for this input
        expected_width = signal_widths.get(name)
        if expected_width is None:
            if not allow_extra:
                if json_output:
                    print(json.dumps({
                        'ok': False,
                        'command': 'eval',
                        'exit_code': 1,
                        'error': {
                            'type': 'UnknownInputError',
                            'message': f"Unknown input: {name}",
                            'file': None,
                            'line': None,
                            'col': None,
                        }
                    }))
                else:
                    print(f"Error: Unknown input: {name}", file=sys.stderr)
                return 1
            # Allow extra inputs if --allow-extra is set
            # We'll just store as-is with width 1 for now
            # but we need width for evaluation
            input_values[name] = int(value_str)  # Just try to parse as int for simplicity
            continue

        try:
            value = parse_input_literal(value_str, expected_width, filepath)
            input_values[name] = value
        except InputWidthMismatchError as e:
            if json_output:
                print(format_error_json(e, 'eval'))
            else:
                print(f"Error: {e.message}", file=sys.stderr)
            return e.exit_code
        except InputValueParseError as e:
            if json_output:
                print(format_error_json(e, 'eval'))
            else:
                print(f"Error: {e.message}", file=sys.stderr)
            return e.exit_code

    # Get declared input names
    declared_inputs = {d.name for d in declarations if d.sig_type == 'input'}

    # Check for missing inputs
    missing_inputs = declared_inputs - set(input_values.keys())
    if missing_inputs:
        if default is None:
            if json_output:
                print(json.dumps({
                    'ok': False,
                    'command': 'eval',
                    'exit_code': 1,
                    'error': {
                        'type': 'MissingInputError',
                        'message': f"Missing inputs: {', '.join(sorted(missing_inputs))}",
                        'file': None,
                        'line': None,
                        'col': None,
                    }
                }))
            else:
                print(f"Error: Missing inputs: {', '.join(sorted(missing_inputs))}", file=sys.stderr)
            return 1
        else:
            # Fill missing inputs with default value
            for name in missing_inputs:
                width = signal_widths[name]
                # Fill with all bits set to default
                input_values[name] = default * ((1 << width) - 1)

    # Build dependency graph for topological evaluation
    graph: Dict[str, List[str]] = {}
    for lhs, expr in assignments:
        graph[lhs] = []
        # Collect dependencies
        def collect_deps(node: ASTNode):
            if isinstance(node, SignalRef):
                if node.name not in ('0', '1'):
                    graph[lhs].append(node.name)
            elif isinstance(node, Concat):
                for part in node.parts:
                    collect_deps(part)
            elif isinstance(node, OpCall):
                for arg in node.args:
                    collect_deps(arg)

        collect_deps(expr)

    # Evaluate in topological order
    # Process assignments repeatedly until all are computed
    values = dict(input_values)  # Start with input values

    max_iterations = len(assignments) * 10  # Safety limit
    for _ in range(max_iterations):
        progress = False
        for lhs, expr in assignments:
            if lhs in values:
                continue  # Already computed

            # Check if all dependencies are available
            deps_ready = True
            for dep in graph[lhs]:
                if dep not in values:
                    deps_ready = False
                    break

            if deps_ready:
                val, width = eval_expression(expr, values, signal_widths, filepath)
                values[lhs] = val
                progress = True

        if not progress:
            break

    # Check for uncomputed values (cycles or missing dependencies)
    for lhs, _ in assignments:
        if lhs not in values:
            # Find what's missing
            for dep in graph[lhs]:
                if dep not in values:
                    raise CycleError(
                        f"Cannot evaluate: missing dependency {dep} for {lhs}",
                        file=filepath
                    )

    # Get output values
    outputs = []
    for out_info in result_info['outputs']:
        out_name = out_info['name']
        width = signal_widths[out_name]
        value = values.get(out_name, 0)

        if width == 1:
            # Scalar output: always show 0 or 1
            formatted = str(value & 1)
        else:
            # Vector output: use specified radix
            formatted = format_vector_value(value, width, radix)

        outputs.append({
            'name': out_name,
            'msb': out_info['msb'],
            'lsb': out_info['lsb'],
            'value': formatted
        })

    # Sort outputs by name
    outputs.sort(key=lambda x: x['name'])

    # Output results
    if json_output:
        print(json.dumps({
            'ok': True,
            'command': 'eval',
            'mode': '2val',
            'radix': radix,
            'outputs': outputs
        }))
    else:
        for out in outputs:
            print(f"{out['name']}={out['value']}")

    return 0


def json_cli_error(command: str, error_type: str, message: str) -> None:
    """Print JSON error for CLI errors."""
    print(json.dumps({
        'ok': False,
        'command': command,
        'exit_code': 1,
        'error': {
            'type': error_type,
            'message': message,
            'file': None,
            'line': None,
            'col': None,
        }
    }))


def main():
    import argparse

    parser = argparse.ArgumentParser(
        prog='circopt',
        description='Circuit optimizer',
        add_help=False,
    )
    parser.add_argument('--help', action='store_true', default=False, dest='help_flag')
    parser.add_argument('--version', action='store_true', default=False, dest='version_flag')
    parser.add_argument('--json', action='store_true', default=False, dest='json_flag')

    # Parse known args first (global flags)
    namespace, remaining = parser.parse_known_args()

    # Check for help/version flags
    if namespace.help_flag:
        print("Usage: circopt [OPTIONS] <COMMAND> [ARGS]")
        print("")
        print("Options:")
        print("  --help, -h      Show this help message")
        print("  --version, -v   Show version information")
        print("  --json          Output as JSON")
        print("")
        print("Commands:")
        print("  check <file>    Parse and validate a .circ file")
        print("  eval <file>     Evaluate circuit with given inputs")
        return 0

    if namespace.version_flag:
        if namespace.json_flag:
            print(format_version_json())
        else:
            print(VERSION)
        return 0

    # Parse remaining args for command and file
    if not remaining:
        if namespace.json_flag:
            json_cli_error('__cli__', 'CliUsageError', 'No command specified')
        else:
            print("Error: No command specified", file=sys.stderr)
            print("Use --help for usage information", file=sys.stderr)
        return 1

    # The first non-option is the command
    command = remaining[0]

    # Find the file argument (second arg, or after -- if present)
    args_after_command = remaining[1:]
    file_arg = None

    for arg in args_after_command:
        if not arg.startswith('-'):
            file_arg = arg
            break

    # Handle commands
    if command == 'check':
        if file_arg is None:
            if namespace.json_flag:
                json_cli_error('check', 'CliUsageError', 'Missing required argument: <file>')
            else:
                print("Error: Missing required argument: <file>", file=sys.stderr)
            return 1
        return cmd_check(file_arg, namespace.json_flag)

    elif command == 'eval':
        if file_arg is None:
            if namespace.json_flag:
                json_cli_error('eval', 'CliUsageError', 'Missing required argument: <file>')
            else:
                print("Error: Missing required argument: <file>", file=sys.stderr)
            return 1

        # Parse eval-specific arguments
        set_inputs = []
        default = None
        allow_extra = False
        radix = 'bin'
        mode = '2val'

        i = 0
        while i < len(args_after_command):
            arg = args_after_command[i]
            if arg == '--set' and i + 1 < len(args_after_command):
                set_inputs.append(args_after_command[i + 1])
                i += 2
            elif arg == '--default' and i + 1 < len(args_after_command):
                val = args_after_command[i + 1]
                if val == '0':
                    default = 0
                elif val == '1':
                    default = 1
                else:
                    if namespace.json_flag:
                        json_cli_error('eval', 'InputValueParseError', f"Invalid default value: {val}. Must be 0 or 1")
                    else:
                        print(f"Error: Invalid default value: {val}. Must be 0 or 1", file=sys.stderr)
                    return 2
                i += 2
            elif arg == '--allow-extra':
                allow_extra = True
                i += 1
            elif arg == '--radix' and i + 1 < len(args_after_command):
                radix_val = args_after_command[i + 1]
                if radix_val in ('bin', 'hex', 'dec'):
                    radix = radix_val
                else:
                    if namespace.json_flag:
                        json_cli_error('eval', 'CliUsageError', f"Invalid radix: {radix_val}. Must be bin, hex, or dec")
                    else:
                        print(f"Error: Invalid radix: {radix_val}. Must be bin, hex, or dec", file=sys.stderr)
                    return 1
                i += 2
            elif arg == '--mode' and i + 1 < len(args_after_command):
                mode_val = args_after_command[i + 1]
                if mode_val in ('2val', '3val'):
                    mode = mode_val
                else:
                    if namespace.json_flag:
                        json_cli_error('eval', 'CliUsageError', f"Invalid mode: {mode_val}. Must be 2val or 3val")
                    else:
                        print(f"Error: Invalid mode: {mode_val}. Must be 2val or 3val", file=sys.stderr)
                    return 1
                i += 2
            elif arg == file_arg:
                # Skip the file argument
                i += 1
            else:
                i += 1

        return cmd_eval(file_arg, set_inputs, default, allow_extra, namespace.json_flag, radix, mode)

    else:
        if namespace.json_flag:
            json_cli_error('__cli__', 'CliUsageError', f"Unknown command: {command}")
        else:
            print(f"Error: Unknown command: {command}", file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
