#!/usr/bin/env python3
"""
LogQL — Part 2: GROUP BY and Aggregations

Supports:
- SELECT with aggregate functions (COUNT, SUM, AVG, MIN, MAX, UNIQUE)
- GROUP BY field references
- WHERE filtering (from Part 1)
- Field references with dotted paths
"""

import argparse
import json
import sys
from collections import OrderedDict
from typing import Any, Optional


# Tokenizer

class TokenType:
    SELECT = "SELECT"
    FROM = "FROM"
    WHERE = "WHERE"
    GROUP_BY = "GROUP_BY"
    AS = "AS"
    COMMA = ","
    DOT = "."
    STAR = "*"
    LPAREN = "("
    RPAREN = ")"
    LBRACKET = "["
    RBRACKET = "]"
    EQ = "="
    NE = "!="
    LT = "<"
    LTE = "<="
    GT = ">"
    GTE = ">="
    AND = "AND"
    OR = "OR"
    NOT = "NOT"
    NULL = "NULL"
    TRUE = "TRUE"
    FALSE = "FALSE"
    CONFLATE = "CONFLATE"
    INTERSECTING = "INTERSECTING"
    PRESERVING = "PRESERVING"
    LEFT = "LEFT"
    RIGHT = "RIGHT"
    BOTH = "BOTH"
    UPON = "UPON"
    POCKET = "POCKET"
    BEHOLDS = "BEHOLDS"
    AMONGST = "AMONGST"
    EITHERWISE = "EITHERWISE"
    EVERYWISE = "EVERYWISE"
    UPTREE = "UPTREE"
    IDENT = "IDENT"
    STRING = "STRING"
    NUMBER = "NUMBER"
    EOF = "EOF"


class Token:
    def __init__(self, type_: str, value: str, pos: int):
        self.type = type_
        self.value = value
        self.pos = pos

    def __repr__(self):
        return f"Token({self.type}, {repr(self.value)})"


KEYWORDS = {
    "SELECT": TokenType.SELECT,
    "FROM": TokenType.FROM,
    "WHERE": TokenType.WHERE,
    "GROUP": TokenType.IDENT,
    "BY": TokenType.IDENT,
    "AS": TokenType.AS,
    "CONFLATE": TokenType.CONFLATE,
    "INTERSECTING": TokenType.INTERSECTING,
    "PRESERVING": TokenType.PRESERVING,
    "LEFT": TokenType.LEFT,
    "RIGHT": TokenType.RIGHT,
    "BOTH": TokenType.BOTH,
    "UPON": TokenType.UPON,
    "POCKET": TokenType.POCKET,
    "BEHOLDS": TokenType.BEHOLDS,
    "AMONGST": TokenType.AMONST,
    "EITHERWISE": TokenType.EITHERWISE,
    "EVERYWISE": TokenType.EVERYWISE,
    "UPTREE": TokenType.UPTREE,
    "AND": TokenType.AND,
    "OR": TokenType.OR,
    "NOT": TokenType.NOT,
    "NULL": TokenType.NULL,
    "TRUE": TokenType.TRUE,
    "FALSE": TokenType.FALSE,
    "COUNT": TokenType.IDENT,
    "SUM": TokenType.IDENT,
    "AVG": TokenType.IDENT,
    "AVERAGE": TokenType.IDENT,
    "MIN": TokenType.IDENT,
    "MAX": TokenType.IDENT,
    "UNIQUE": TokenType.IDENT,
}


class Tokenizer:
    def __init__(self, query: str):
        self.query = query
        self.pos = 0
        self.tokens: list[Token] = []
        self.current_char = query[self.pos] if query else None

    def advance(self):
        self.pos += 1
        if self.pos >= len(self.query):
            self.current_char = None
        else:
            self.current_char = self.query[self.pos]

    def skip_whitespace(self):
        while self.current_char is not None and self.current_char.isspace():
            self.advance()

    def read_identifier(self) -> str:
        start = self.pos
        while self.current_char is not None and (self.current_char.isalnum() or self.current_char == '_'):
            self.advance()
        return self.query[start:self.pos]

    def read_number(self) -> str:
        start = self.pos
        while self.current_char is not None and (self.current_char.isdigit() or self.current_char == '.'):
            self.advance()
        return self.query[start:self.pos]

    def read_string(self) -> str:
        start = self.pos + 1
        self.advance()  # skip opening quote
        result = []
        while self.current_char is not None and self.current_char != '"':
            if self.current_char == '\\':
                self.advance()
                if self.current_char is not None:
                    result.append(self.current_char)
            else:
                result.append(self.current_char)
            self.advance()
        if self.current_char == '"':
            self.advance()
        return ''.join(result)

    def tokenize(self) -> list[Token]:
        while self.current_char is not None:
            if self.current_char.isspace():
                self.skip_whitespace()
                continue

            if self.current_char.isalpha() or self.current_char == '_':
                ident = self.read_identifier()
                upper_ident = ident.upper()
                if upper_ident in KEYWORDS:
                    # Special handling for GROUP BY
                    if upper_ident == "GROUP":
                        self.tokens.append(Token(TokenType.IDENT, ident, self.pos))
                    else:
                        self.tokens.append(Token(KEYWORDS[upper_ident], ident, self.pos))
                else:
                    self.tokens.append(Token(TokenType.IDENT, ident, self.pos))
                continue

            if self.current_char.isdigit() or (self.current_char == '.' and self.pos + 1 < len(self.query) and self.query[self.pos + 1].isdigit()):
                num = self.read_number()
                self.tokens.append(Token(TokenType.NUMBER, num, self.pos))
                continue

            if self.current_char == '"':
                self.tokens.append(Token(TokenType.STRING, self.read_string(), self.pos))
                continue

            # Operators and punctuation
            if self.current_char == '=':
                self.advance()
                self.tokens.append(Token(TokenType.EQ, '=', self.pos))
                continue
            if self.current_char == '!' and self.pos + 1 < len(self.query) and self.query[self.pos + 1] == '=':
                self.advance()
                self.advance()
                self.tokens.append(Token(TokenType.NE, '!=', self.pos))
                continue
            if self.current_char == '<' and self.pos + 1 < len(self.query) and self.query[self.pos + 1] == '=':
                self.advance()
                self.advance()
                self.tokens.append(Token(TokenType.LTE, '<=', self.pos))
                continue
            if self.current_char == '>' and self.pos + 1 < len(self.query) and self.query[self.pos + 1] == '=':
                self.advance()
                self.advance()
                self.tokens.append(Token(TokenType.GTE, '>=', self.pos))
                continue
            if self.current_char == '<':
                self.advance()
                self.tokens.append(Token(TokenType.LT, '<', self.pos))
                continue
            if self.current_char == '>':
                self.advance()
                self.tokens.append(Token(TokenType.GT, '>', self.pos))
                continue
            if self.current_char == ',':
                self.advance()
                self.tokens.append(Token(TokenType.COMMA, ',', self.pos))
                continue
            if self.current_char == '.':
                self.advance()
                self.tokens.append(Token(TokenType.DOT, '.', self.pos))
                continue
            if self.current_char == '*':
                self.advance()
                self.tokens.append(Token(TokenType.STAR, '*', self.pos))
                continue
            if self.current_char == '(':
                self.advance()
                self.tokens.append(Token(TokenType.LPAREN, '(', self.pos))
                continue
            if self.current_char == '[':
                self.advance()
                self.tokens.append(Token(TokenType.LBRACKET, '[', self.pos))
                continue
            if self.current_char == ')':
                self.advance()
                self.tokens.append(Token(TokenType.RPAREN, ')', self.pos))
                continue
            if self.current_char == ']':
                self.advance()
                self.tokens.append(Token(TokenType.RBRACKET, ']', self.pos))
                continue

            # Unknown character
            raise ValueError(f"Unexpected character: {self.current_char} at position {self.pos}")

        self.tokens.append(Token(TokenType.EOF, '', self.pos))
        return self.tokens


# Parser

class ParseError(Exception):
    pass


class ASTNode:
    pass


class FieldRef(ASTNode):
    def __init__(self, parts: list[str]):
        self.parts = parts

    def __repr__(self):
        return f"FieldRef({self.parts})"

    def as_text(self) -> str:
        return ".".join(self.parts)


class Literal(ASTNode):
    def __init__(self, value: Any):
        self.value = value

    def __repr__(self):
        return f"Literal({repr(self.value)})"


class AggCall(ASTNode):
    def __init__(self, func: str, arg: Optional[FieldRef]):  # arg is None for COUNT(*)
        self.func = func.upper()
        self.arg = arg

    def __repr__(self):
        return f"AggCall({self.func}, {self.arg})"

    def canonical_key(self) -> str:
        if self.arg is None:
            return f"{self.func}(*)"
        return f"{self.func}({self.arg.as_text()})"


class SelectItem(ASTNode):
    def __init__(self, item: Any, alias: Optional[str]):
        self.item = item  # FieldRef or AggCall
        self.alias = alias

    def __repr__(self):
        return f"SelectItem({self.item}, alias={self.alias})"

    def output_key(self) -> str:
        if self.alias is not None:
            return self.alias
        if isinstance(self.item, FieldRef):
            return self.item.as_text()
        # AggCall
        return self.item.canonical_key()


class BinaryExpr(ASTNode):
    def __init__(self, op: str, left: ASTNode, right: ASTNode):
        self.op = op
        self.left = left
        self.right = right

    def __repr__(self):
        return f"BinaryExpr({repr(self.op)}, {self.left}, {self.right})"


class UnaryExpr(ASTNode):
    def __init__(self, op: str, expr: ASTNode):
        self.op = op
        self.expr = expr

    def __repr__(self):
        return f"UnaryExpr({repr(self.op)}, {self.expr})"


class JoinFlavor(ASTNode):
    """Represents the join flavor: INTERSECTING, PRESERVING LEFT/RIGHT/BOTH."""
    def __init__(self, flavor: str, side: Optional[str] = None):
        self.flavor = flavor  # "INTERSECTING" or "PRESERVING"
        self.side = side      # None for INTERSECTING, "LEFT", "RIGHT", or "BOTH" for PRESERVING

    def __repr__(self):
        if self.side:
            return f"JoinFlavor({self.flavor} {self.side})"
        return f"JoinFlavor({self.flavor})"


class JoinConjunct(ASTNode):
    """Represents a single equality predicate in the UPON clause: alias.field = alias.field"""
    def __init__(self, left: FieldRef, right: FieldRef):
        self.left = left
        self.right = right

    def __repr__(self):
        return f"JoinConjunct({self.left} = {self.right})"


class JoinPredicate(ASTNode):
    """Represents the UPON clause with AND-connected equality predicates."""
    def __init__(self, conjuncts: list[JoinConjunct]):
        self.conjuncts = conjuncts

    def __repr__(self):
        return f"JoinPredicate({self.conjuncts})"


class ConflatedQuery(ASTNode):
    """Represents a query with CONFLATE clauses for multi-source joining."""
    def __init__(
        self,
        select_items: list[SelectItem],
        from_alias: str,
        conflate_clauses: list[tuple[str, JoinFlavor, JoinPredicate]],
        where_expr: Optional[ASTNode],
        group_by: Optional[list[FieldRef]]
    ):
        self.select_items = select_items
        self.from_alias = from_alias
        self.conflate_clauses = conflate_clauses  # list of (alias, flavor, predicate) tuples
        self.where_expr = where_expr
        self.group_by = group_by

    def __repr__(self):
        return f"ConflatedQuery(select={self.select_items}, from={self.from_alias}, conflate={self.conflate_clauses}, where={self.where_expr}, group_by={self.group_by})"


class WildcardSelect(ASTNode):
    """Represents SELECT alias.*"""
    def __init__(self, alias: str):
        self.alias = alias

    def __repr__(self):
        return f"WildcardSelect({self.alias}.*)"


# ============================================================================
# Nested Query AST Nodes (Part 5)
# ============================================================================

class ScalarSubquery(ASTNode):
    """Represents a scalar subquery: POCKET(<query>)"""
    def __init__(self, query):
        self.query = query

    def __repr__(self):
        return f"ScalarSubquery({self.query})"


class TableSubquery(ASTNode):
    """Represents a table subquery: POCKET[<query>]"""
    def __init__(self, query):
        self.query = query

    def __repr__(self):
        return f"TableSubquery([...])"


class OuterRef(ASTNode):
    """Represents a correlation reference: UPTREE.alias.field"""
    def __init__(self, parts: list[str]):  # e.g., ['alias', 'field'] or ['alias', 'nested', 'field']
        self.parts = parts

    def __repr__(self):
        return f"OuterRef({self.parts})"

    def as_text(self) -> str:
        return ".".join(self.parts)


class ExistsPredicate(ASTNode):
    """Represents BEHOLDS table_subexpr (EXISTS)"""
    def __init__(self, table_subexpr: TableSubquery):
        self.table_subexpr = table_subexpr

    def __repr__(self):
        return f"ExistsPredicate(BEHOLDS ...)"


class InPredicate(ASTNode):
    """Represents value AMONGST table_subexpr (IN)"""
    def __init__(self, value: ASTNode, table_subexpr: TableSubquery):
        self.value = value
        self.table_subexpr = table_subexpr

    def __repr__(self):
        return f"InPredicate({self.value} AMONGST ...)"


class AnyPredicate(ASTNode):
    """Represents value <op> EITHERWISE table_subexpr (ANY)"""
    def __init__(self, value: ASTNode, op: str, table_subexpr: TableSubquery):
        self.value = value
        self.op = op
        self.table_subexpr = table_subexpr

    def __repr__(self):
        return f"AnyPredicate({self.value} {self.op} EITHERWISE ...)"


class AllPredicate(ASTNode):
    """Represents value <op> EVERYWISE table_subexpr (ALL)"""
    def __init__(self, value: ASTNode, op: str, table_subexpr: TableSubquery):
        self.value = value
        self.op = op
        self.table_subexpr = table_subexpr

    def __repr__(self):
        return f"AllPredicate({self.value} {self.op} EVERYWISE ...)"


class Parser:
    def __init__(self, tokens: list[Token]):
        self.tokens = tokens
        self.pos = 0

    def peek(self) -> Token:
        if self.pos >= len(self.tokens):
            return Token(TokenType.EOF, '', self.pos)
        return self.tokens[self.pos]

    def consume(self, type_: str, value: Optional[str] = None) -> Token:
        token = self.peek()
        if token.type != type_:
            raise ParseError(f"Expected {type_}, got {token.type}")
        if value is not None and token.value != value:
            raise ParseError(f"Expected {value}, got {token.value}")
        self.pos += 1
        return token

    def parse_query(self, allow_subquery: bool = True) -> ConflatedQuery:
        select_items = self.parse_select()
        self.consume(TokenType.FROM)
        from_alias = self.parse_alias()

        # Parse CONFLATE clauses (0 or more)
        conflate_clauses = []
        while self.peek().type == TokenType.CONFLATE:
            self.consume(TokenType.CONFLATE)
            alias = self.parse_alias()

            # Parse optional join flavor
            flavor = None
            if self.peek().type == TokenType.INTERSECTING:
                self.consume(TokenType.INTERSECTING)
                flavor = JoinFlavor("INTERSECTING")
            elif self.peek().type == TokenType.PRESERVING:
                self.consume(TokenType.PRESERVING)
                if self.peek().type in (TokenType.LEFT, TokenType.RIGHT, TokenType.BOTH):
                    side = self.peek().value.upper()
                    self.consume(self.peek().type)
                    flavor = JoinFlavor("PRESERVING", side)
                else:
                    raise ParseError("Expected LEFT, RIGHT, or BOTH after PRESERVING")
            else:
                # Default is INTERSECTING
                flavor = JoinFlavor("INTERSECTING")

            self.consume(TokenType.UPON)
            predicate = self.parse_join_predicate()
            conflate_clauses.append((alias, flavor, predicate))

        where_expr = None
        if self.peek().type == TokenType.WHERE:
            self.consume(TokenType.WHERE)
            where_expr = self.parse_boolean_expr()

        group_by = None
        # Handle GROUP BY (GROUP is an IDENT token, followed by BY IDENT)
        if (self.peek().type == TokenType.IDENT and
            self.peek().value.upper() == "GROUP" and
            self.pos + 2 < len(self.tokens) and
            self.tokens[self.pos + 1].type == TokenType.IDENT and
            self.tokens[self.pos + 1].value.upper() == "BY"):
            self.consume(TokenType.IDENT)  # consume GROUP
            self.consume(TokenType.IDENT)  # consume BY
            group_by = self.parse_group_by()

        self.consume(TokenType.EOF)
        return ConflatedQuery(select_items, from_alias, conflate_clauses, where_expr, group_by)

    def parse_select(self) -> list[SelectItem]:
        self.consume(TokenType.SELECT)
        items = [self.parse_select_item()]
        while self.peek().type == TokenType.COMMA:
            self.consume(TokenType.COMMA)
            items.append(self.parse_select_item())
        return items

    def parse_select_item(self) -> SelectItem:
        token = self.peek()

        # Check for POCKET() scalar subquery
        if token.type == TokenType.POCKET:
            subquery = self.parse_scalar_subquery()
            alias = None
            if self.peek().type == TokenType.IDENT and self.peek().value.upper() == "AS":
                self.consume(TokenType.IDENT)  # consume AS
                alias = self.consume(TokenType.IDENT).value
            return SelectItem(subquery, alias)

        # Check for SELECT alias.* (wildcard expansion)
        if token.type == TokenType.IDENT:
            ident = self.consume(TokenType.IDENT).value
            if self.peek().type == TokenType.DOT:
                self.consume(TokenType.DOT)
                if self.peek().type == TokenType.STAR:
                    self.consume(TokenType.STAR)
                    return SelectItem(WildcardSelect(ident), None)
                else:
                    # It's a qualified field ref, not wildcard
                    second = self.consume(TokenType.IDENT).value
                    parts = [ident, second]
                    while self.peek().type == TokenType.DOT:
                        self.consume(TokenType.DOT)
                        parts.append(self.consume(TokenType.IDENT).value)
                    field = FieldRef(parts)
                    alias = None
                    if self.peek().type == TokenType.IDENT and self.peek().value.upper() == "AS":
                        self.consume(TokenType.IDENT)  # consume AS
                        alias = self.consume(TokenType.IDENT).value
                    return SelectItem(field, alias)
            # Not a wildcard, just a simple field ref
            field = FieldRef([ident])
            alias = None
            if self.peek().type == TokenType.IDENT and self.peek().value.upper() == "AS":
                self.consume(TokenType.IDENT)  # consume AS
                alias = self.consume(TokenType.IDENT).value
            return SelectItem(field, alias)

        # Check if it's an aggregate call
        if token.type == TokenType.IDENT and token.value.upper() in ("COUNT", "SUM", "AVG", "AVERAGE", "MIN", "MAX", "UNIQUE"):
            agg = self.parse_agg_call()
            alias = None
            if self.peek().type == TokenType.IDENT and self.peek().value.upper() == "AS":
                self.consume(TokenType.IDENT)  # consume AS
                alias = self.consume(TokenType.IDENT).value
            return SelectItem(agg, alias)

        # Check for SELECT *
        if token.type == TokenType.STAR:
            self.consume(TokenType.STAR)
            # SELECT * with no aggregates - handled specially
            return SelectItem(FieldRef(["*"]), None)

        raise ParseError(f"Unexpected token in select list: {token}")

    def parse_agg_call(self) -> AggCall:
        func_token = self.consume(TokenType.IDENT)
        func = func_token.value.upper()
        self.consume(TokenType.LPAREN)

        # Check for COUNT(*)
        if self.peek().type == TokenType.STAR:
            self.consume(TokenType.STAR)
            self.consume(TokenType.RPAREN)
            return AggCall(func, None)

        # Must be a qualified field ref for aggregates in Part 3
        field = self.parse_field_ref()
        self.consume(TokenType.RPAREN)
        return AggCall(func, field)

    def parse_scalar_subquery(self) -> ScalarSubquery:
        """Parse POCKET(<query>)"""
        self.consume(TokenType.POCKET)
        self.consume(TokenType.LPAREN)
        # Parse the inner query
        inner_query = self.parse_query(allow_subquery=True)
        self.consume(TokenType.RPAREN)
        return ScalarSubquery(inner_query)

    def parse_table_subquery(self) -> TableSubquery:
        """Parse POCKET[<query>]"""
        self.consume(TokenType.POCKET)
        self.consume(TokenType.LBRACKET)
        # Parse the inner query
        inner_query = self.parse_query(allow_subquery=True)
        self.consume(TokenType.RBRACKET)
        return TableSubquery(inner_query)

    def parse_outer_ref(self) -> OuterRef:
        """Parse UPTREE.alias.field..."""
        self.consume(TokenType.UPTREE)
        self.consume(TokenType.DOT)
        parts = []
        # First part after UPTREE. is the alias
        parts.append(self.consume(TokenType.IDENT).value)
        # Additional dotted parts
        while self.peek().type == TokenType.DOT:
            self.consume(TokenType.DOT)
            parts.append(self.consume(TokenType.IDENT).value)
        return OuterRef(parts)

    def parse_field_ref(self) -> FieldRef:
        ident = self.consume(TokenType.IDENT).value
        parts = [ident]

        while self.peek().type == TokenType.DOT:
            self.consume(TokenType.DOT)
            parts.append(self.consume(TokenType.IDENT).value)

        return FieldRef(parts)

    def parse_join_predicate(self) -> JoinPredicate:
        """Parse UPON clause: field = field [AND field = field]*"""
        conjuncts = []
        while True:
            # Left side: qualified field ref
            left_parts = []
            ident = self.consume(TokenType.IDENT).value
            left_parts.append(ident)
            while self.peek().type == TokenType.DOT:
                self.consume(TokenType.DOT)
                left_parts.append(self.consume(TokenType.IDENT).value)
            left_field = FieldRef(left_parts)

            self.consume(TokenType.EQ)

            # Right side: qualified field ref
            right_parts = []
            ident = self.consume(TokenType.IDENT).value
            right_parts.append(ident)
            while self.peek().type == TokenType.DOT:
                self.consume(TokenType.DOT)
                right_parts.append(self.consume(TokenType.IDENT).value)
            right_field = FieldRef(right_parts)

            conjuncts.append(JoinConjunct(left_field, right_field))

            if self.peek().type != TokenType.AND:
                break
            self.consume(TokenType.AND)

        return JoinPredicate(conjuncts)

    def parse_group_by(self) -> list[FieldRef]:
        fields = [self.parse_field_ref()]
        while self.peek().type == TokenType.COMMA:
            self.consume(TokenType.COMMA)
            fields.append(self.parse_field_ref())
        return fields

    def parse_boolean_expr(self) -> ASTNode:
        return self.parse_or_expr()

    def parse_or_expr(self) -> ASTNode:
        left = self.parse_and_expr()
        while self.peek().type == TokenType.OR:
            self.consume(TokenType.OR)
            right = self.parse_and_expr()
            left = BinaryExpr("OR", left, right)
        return left

    def parse_and_expr(self) -> ASTNode:
        left = self.parse_not_expr()
        while self.peek().type == TokenType.AND:
            self.consume(TokenType.AND)
            right = self.parse_not_expr()
            left = BinaryExpr("AND", left, right)
        return left

    def parse_not_expr(self) -> ASTNode:
        if self.peek().type == TokenType.NOT:
            self.consume(TokenType.NOT)
            expr = self.parse_not_expr()
            return UnaryExpr("NOT", expr)
        return self.parse_predicate()

    def parse_predicate(self) -> ASTNode:
        """Parse predicate expressions including BEHOLDS, AMONGST, EITHERWISE, EVERYWISE."""
        # Check for BEHOLDS table_subexpr
        if self.peek().type == TokenType.BEHOLDS:
            self.consume(TokenType.BEHOLDS)
            table_subexpr = self.parse_table_subquery()
            return ExistsPredicate(table_subexpr)

        # Parse left side of potential comparison or AMONGST
        left = self.parse_primary()

        # Check for AMONGST table_subexpr
        if self.peek().type == TokenType.AMONST:
            self.consume(TokenType.AMONST)
            table_subexpr = self.parse_table_subquery()
            return InPredicate(left, table_subexpr)

        # Check for comparison operators
        token = self.peek()
        if token.type in (TokenType.EQ, TokenType.NE, TokenType.LT, TokenType.LTE, TokenType.GT, TokenType.GTE):
            op_type = self.consume(token.type).type
            op_map = {
                TokenType.EQ: "==",
                TokenType.NE: "!=",
                TokenType.LT: "<",
                TokenType.LTE: "<=",
                TokenType.GT: ">",
                TokenType.GTE: ">=",
            }
            op = op_map[op_type]

            # Check for EITHERWISE or EVERYWISE
            if self.peek().type == TokenType.EITHERWISE:
                self.consume(TokenType.EITHERWISE)
                table_subexpr = self.parse_table_subquery()
                return AnyPredicate(left, op, table_subexpr)
            elif self.peek().type == TokenType.EVERYWISE:
                self.consume(TokenType.EVERYWISE)
                table_subexpr = self.parse_table_subquery()
                return AllPredicate(left, op, table_subexpr)

            # Regular comparison
            right = self.parse_primary()
            return BinaryExpr(op, left, right)

        return left

    def parse_primary(self) -> ASTNode:
        token = self.peek()

        # Check for UPTREE correlation reference
        if token.type == TokenType.UPTREE:
            return self.parse_outer_ref()

        # Check for POCKET() scalar subquery
        if token.type == TokenType.POCKET:
            return self.parse_scalar_subquery()

        if token.type == TokenType.IDENT:
            ident = self.consume(TokenType.IDENT).value
            # Check for NULL, TRUE, FALSE
            upper = ident.upper()
            if upper == "NULL":
                return Literal(None)
            if upper == "TRUE":
                return Literal(True)
            if upper == "FALSE":
                return Literal(False)

            # Field reference (may have dots)
            parts = [ident]
            while self.peek().type == TokenType.DOT:
                self.consume(TokenType.DOT)
                parts.append(self.consume(TokenType.IDENT).value)
            return FieldRef(parts)

        if token.type == TokenType.STRING:
            self.consume(TokenType.STRING)
            # Re-read the value since consume advances
            val = self.tokens[self.pos - 1].value
            return Literal(val)

        if token.type == TokenType.NUMBER:
            self.consume(TokenType.NUMBER)
            val = self.tokens[self.pos - 1].value
            # Determine if it's integer or float
            if '.' in val:
                return Literal(float(val))
            return Literal(int(val))

        if token.type == TokenType.LPAREN:
            self.consume(TokenType.LPAREN)
            expr = self.parse_boolean_expr()
            self.consume(TokenType.RPAREN)
            return expr

        raise ParseError(f"Unexpected token: {token}")


# ==============================================================================
# Evaluation
# ==============================================================================

def deep_equal(a: Any, b: Any) -> bool:
    """Deep equality for JSON values that ignores object key order."""
    if type(a) != type(b):
        return False

    if a is None or b is None:
        return a is None and b is None

    if isinstance(a, (int, float, str, bool)):
        return a == b

    if isinstance(a, list):
        if len(a) != len(b):
            return False
        return all(deep_equal(x, y) for x, y in zip(a, b))

    if isinstance(a, dict):
        if len(a) != len(b):
            return False
        # Check if all keys match and values are equal
        if set(a.keys()) != set(b.keys()):
            return False
        return all(deep_equal(a[k], b[k]) for k in a.keys())

    return False


def get_nested_value(obj: dict, parts: list[str]) -> Any:
    """Get a nested value from a dict using dotted path parts."""
    current = obj
    for part in parts:
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def eval_expr(expr: ASTNode, row: dict) -> Any:
    """Evaluate an expression against a data row."""
    if isinstance(expr, FieldRef):
        if expr.parts == ["*"]:
            return "*"  # Special marker
        return get_nested_value(row, expr.parts)

    if isinstance(expr, Literal):
        return expr.value

    if isinstance(expr, BinaryExpr):
        left = eval_expr(expr.left, row)
        right = eval_expr(expr.right, row)

        if expr.op == "==":
            if left is None or right is None:
                return False
            return left == right
        if expr.op == "!=":
            if left is None or right is None:
                return False
            return left != right
        if expr.op == "<":
            if left is None or right is None:
                return False
            if isinstance(left, (int, float)) and isinstance(right, (int, float)):
                return left < right
            if isinstance(left, str) and isinstance(right, str):
                return left < right
            return False
        if expr.op == "<=":
            if left is None or right is None:
                return False
            if isinstance(left, (int, float)) and isinstance(right, (int, float)):
                return left <= right
            if isinstance(left, str) and isinstance(right, str):
                return left <= right
            return False
        if expr.op == ">":
            if left is None or right is None:
                return False
            if isinstance(left, (int, float)) and isinstance(right, (int, float)):
                return left > right
            if isinstance(left, str) and isinstance(right, str):
                return left > right
            return False
        if expr.op == ">=":
            if left is None or right is None:
                return False
            if isinstance(left, (int, float)) and isinstance(right, (int, float)):
                return left >= right
            if isinstance(left, str) and isinstance(right, str):
                return left >= right
            return False

    if isinstance(expr, UnaryExpr):
        val = eval_expr(expr.expr, row)
        if expr.op == "NOT":
            return not val

    raise ValueError(f"Unknown expr type: {type(expr)}")


def eval_subquery(sources: dict[str, list[dict]], subquery_expr: ASTNode, outer_row: dict) -> Any:
    """Evaluate a subquery expression in the context of an outer row.

    Args:
        sources: The data sources
        subquery_expr: A ScalarSubquery, TableSubquery, or other ASTNode
        outer_row: The current row from the outer query (for correlation)

    Returns:
        For ScalarSubquery: a single value
        For TableSubquery: a list of values (single column)
    """
    if isinstance(subquery_expr, ScalarSubquery):
        # Evaluate scalar subquery
        # Extract the inner query and execute it with correlation binding
        inner_query = subquery_expr.query
        return execute_subquery_scalar(sources, inner_query, outer_row)

    elif isinstance(subquery_expr, TableSubquery):
        # Evaluate table subquery
        inner_query = subquery_expr.query
        return execute_subquery_table(sources, inner_query, outer_row)

    elif isinstance(subquery_expr, OuterRef):
        # Correlation reference - get value from outer row
        # parts[0] is the alias, rest is the field path
        return get_nested_value(outer_row, subquery_expr.parts)

    else:
        # Regular expression evaluation
        return eval_expr(subquery_expr, outer_row)


def execute_subquery_scalar(sources: dict[str, list[dict]], query: ConflatedQuery, outer_row: dict) -> Any:
    """Execute a scalar subquery and return a single value."""
    # Execute the query with correlation, get results
    results = execute_query_with_correlation(sources, query, outer_row)

    if not results:
        return None

    # For scalar subquery, we expect at most one row with one value
    if len(results) > 1:
        # This is actually a cardinal violation, but spec says scalar should return single value
        # We take the first value from the first row
        pass

    first_row = results[0]
    # Get the first (or only) value from the row
    if not first_row:
        return None

    # Find the first non-empty value in the row
    for key, value in first_row.items():
        # Skip the query text placeholder for queries without AS
        if key == "_query_text":
            continue
        return value

    return None


def execute_subquery_table(sources: dict[str, list[dict]], query: ConflatedQuery, outer_row: dict) -> list:
    """Execute a table subquery and return a list of values (single column)."""
    results = execute_query_with_correlation(sources, query, outer_row)

    if not results:
        return []

    # For table subquery, return all values from the single column
    # We take the first column of the result set
    values = []
    for row in results:
        if not row:
            continue
        # Get the first value in the row
        first_key = None
        for key in row:
            if key == "_query_text":
                continue
            first_key = key
            break
        if first_key is not None:
            values.append(row[first_key])

    return values


def evaluate_predicate(predicate: ASTNode, sources: dict[str, list[dict]], outer_row: dict) -> bool:
    """Evaluate a predicate (including EXISTS, IN, ANY, ALL) with correlation support."""

    if isinstance(predicate, ExistsPredicate):
        # BEHOLDS - check if subquery returns any rows
        table_subexpr = predicate.table_subexpr
        inner_query = table_subexpr.query
        results = execute_query_with_correlation(sources, inner_query, outer_row)
        return len(results) > 0

    elif isinstance(predicate, InPredicate):
        # AMONGST - check if value is in subquery results
        value = eval_subquery(sources, predicate.value, outer_row)
        table_values = eval_subquery(sources, predicate.table_subexpr, outer_row)
        # Deep equality comparison
        for tv in table_values:
            if deep_equal(value, tv):
                return True
        return False

    elif isinstance(predicate, AnyPredicate):
        # EITHERWISE - check if value <op> ANY subquery result
        left_value = eval_subquery(sources, predicate.value, outer_row)
        table_values = eval_subquery(sources, predicate.table_subexpr, outer_row)

        if not table_values:
            return False  # EITHERWISE over empty set is false

        for tv in table_values:
            if compare_values(left_value, tv, predicate.op):
                return True
        return False

    elif isinstance(predicate, AllPredicate):
        # EVERYWISE - check if value <op> ALL subquery results
        left_value = eval_subquery(sources, predicate.value, outer_row)
        table_values = eval_subquery(sources, predicate.table_subexpr, outer_row)

        if not table_values:
            return True  # EVERYWISE over empty set is true

        for tv in table_values:
            if not compare_values(left_value, tv, predicate.op):
                return False
        return True

    else:
        # Regular boolean expression, evaluate normally
        return eval_expr(predicate, outer_row)


def compare_values(a: Any, b: Any, op: str) -> bool:
    """Compare two values with the given operator using lexicographic ordering for strings."""
    # Check for None values
    if a is None or b is None:
        return False

    # Type must match for ordering operations
    if type(a) != type(b):
        return False

    if op == "==":
        return a == b
    if op == "!=":
        return a != b
    if op == "<":
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            return a < b
        if isinstance(a, str) and isinstance(b, str):
            return a < b
        return False
    if op == "<=":
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            return a <= b
        if isinstance(a, str) and isinstance(b, str):
            return a <= b
        return False
    if op == ">":
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            return a > b
        if isinstance(b, (int, float)):
            return a > b
        if isinstance(a, str) and isinstance(b, str):
            return a > b
        return False
    if op == ">=":
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            return a >= b
        if isinstance(a, str) and isinstance(b, str):
            return a >= b
        return False

    return False


def execute_query_with_correlation(
    sources: dict[str, list[dict]],
    query: ConflatedQuery,
    outer_row: dict
) -> list[dict]:
    """Execute a query with an outer row for correlation binding."""
    # This executes the query using the outer_row for any correlation references

    select_items = query.select_items
    from_alias = query.from_alias
    conflate_clauses = query.conflate_clauses
    where_expr = query.where_expr
    group_by = query.group_by

    # Validate from_alias exists
    if from_alias not in sources:
        error_semantic(f"Unknown alias: {from_alias}")

    # Collect all referenced aliases
    all_aliases = {from_alias}
    for alias, _, _ in conflate_clauses:
        if alias not in sources:
            error_semantic(f"Unknown alias: {alias}")
        if alias in all_aliases:
            error_semantic(f"Duplicate alias in query: {alias}")
        all_aliases.add(alias)

    has_agg = has_aggregates(select_items)

    # Check for bare SELECT * when CONFLATE is present
    if conflate_clauses and any(isinstance(item.item, FieldRef) and item.item.parts == ["*"] for item in select_items):
        error_semantic("SELECT * is forbidden when any CONFLATE is present")

    # Validate that all field references use qualified names when CONFLATE is present
    if conflate_clauses:
        check_qualified_refs(select_items, group_by)

    validate_select_items_conflation(select_items, group_by)
    validate_group_by_conflation(select_items, group_by)

    # Build the joined rows from sources
    anchor_rows = sources[from_alias]
    joined_rows = build_joined_rows(anchor_rows, sources, conflate_clauses)

    # Apply WHERE filtering with correlation support
    filtered_rows = []
    for row in joined_rows:
        if where_expr is None:
            filtered_rows.append(row)
        else:
            # Evaluate predicate with correlation context
            result = evaluate_predicate(where_expr, sources, outer_row)
            if result:
                filtered_rows.append(row)

    if not filtered_rows:
        # Return empty result with appropriate structure
        return [{}]

    # Handle GLOSS inside subqueries
    if any(isinstance(item.item, dict) and item.item.get('__gloss__') for item in select_items):
        # Handle GLOSS - this needs special processing
        return handle_gloss_query(select_items, filtered_rows, sources, outer_row, group_by)

    # Determine if global aggregation or grouped
    if group_by is None and has_agg:
        # Global aggregation
        return [compute_global_aggregation_select(filtered_rows, select_items)]

    if group_by is None and not has_agg:
        # No aggregation, no grouping - return filtered rows with selected fields
        return compute_no_aggregation_select(filtered_rows, select_items)

    # Grouped aggregation or grouped field selection without aggregates
    return compute_grouped_select(filtered_rows, select_items, group_by)


def handle_gloss_query(select_items: list[SelectItem], rows: list[dict], sources: dict[str, list[dict]], outer_row: dict, group_by: Optional[list[FieldRef]]) -> list[dict]:
    """Handle GLOSS clauses inside subqueries."""
    # Extract the gloss definitions from select items
    gloss_map = {}
    new_select_items = []

    for item in select_items:
        if isinstance(item.item, dict) and item.item.get('__gloss__'):
            # This is a gloss definition
            canon_name = item.item.get('__canon_name')
            source_field = item.item.get('__source_field')
            gloss_map[canon_name] = source_field
        else:
            new_select_items.append(item)

    if not gloss_map:
        return []

    # Apply gloss to each row - create canonical fields
    rows_with_gloss = []
    for row in rows:
        new_row = dict(row)
        for canon_name, source_field in gloss_map.items():
            # Get the value from the source field
            value = get_nested_value(row, source_field.parts)
            new_row[canon_name] = value
        rows_with_gloss.append(new_row)

    # Continue with normal processing
    if group_by is None:
        return compute_no_aggregation_select(rows_with_gloss, new_select_items)
    else:
        return compute_grouped_select(rows_with_gloss, new_select_items, group_by)


def compute_global_aggregation_select(logs: list[dict], select_items: list[SelectItem]) -> dict:
    """Compute global aggregates over rows."""
    result = {}

    for item in select_items:
        key = item.output_key()
        agg = item.item

        if isinstance(agg, AggCall):
            result[key] = evaluate_agg(agg, logs)

    return result


def compute_no_aggregation_select(logs: list[dict], select_items: list[SelectItem]) -> list[dict]:
    """No aggregation, no GROUP BY - return rows with selected fields."""
    results = []
    for row in logs:
        result = {}
        for item in select_items:
            key = item.output_key()
            field_ref = item.item
            val = get_nested_value(row, field_ref.parts)
            result[key] = val
        results.append(result)
    return results


def compute_grouped_select(logs: list[dict], select_items: list[SelectItem], group_by: list[FieldRef]) -> list[dict]:
    """Compute grouped results with first-encountered ordering."""
    groups = OrderedDict()
    group_key_order = []

    # Build groups
    for row in logs:
        # Create group key
        key_parts = []
        for field in group_by:
            val = get_nested_value(row, field.parts)
            # Coerce arrays and objects to None for grouping
            if isinstance(val, (list, dict)):
                val = None
            key_parts.append(val)
        group_key = tuple(key_parts)

        if group_key not in groups:
            groups[group_key] = []
            group_key_order.append(group_key)
        groups[group_key].append(row)

    # Compute results for each group
    results = []
    for group_key in group_key_order:
        group_logs = groups[group_key]
        result = {}

        for item in select_items:
            key = item.output_key()

            if isinstance(item.item, AggCall):
                result[key] = evaluate_agg(item.item, group_logs)
            else:
                # Field reference - get from group key
                field_text = item.item.as_text()
                if item.alias is not None:
                    field_text = item.alias

                # Find the index in group_by
                idx = None
                for i, gb_field in enumerate(group_by):
                    if gb_field.as_text() == field_text:
                        idx = i
                        break

                if idx is not None:
                    result[key] = group_key[idx]
                else:
                    result[key] = None

        results.append(result)

    return results


# ==============================================================================
# Error codes
# ==============================================================================

def error_syntax(msg: str):
    print(f"Syntax error: {msg}", file=sys.stderr)
    sys.exit(1)


def error_semantic(msg: str):
    print(f"Semantic error: {msg}", file=sys.stderr)
    sys.exit(1)


# ==============================================================================
# Main execution
# ==============================================================================

def load_logs(log_file: str) -> list[dict]:
    logs = []
    with open(log_file, 'r') as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                logs.append(json.loads(line))
            except json.JSONDecodeError as e:
                error_semantic(f"Invalid JSON on line {line_num}: {e}")
    return logs


def has_aggregates(select_items: list[SelectItem]) -> bool:
    for item in select_items:
        if isinstance(item.item, AggCall):
            return True
    return False


def validate_select_items(select_items: list[SelectItem], group_by: Optional[list[FieldRef]]) -> list[str]:
    """Validate SELECT items and return output keys."""
    output_keys = []
    seen_keys = set()

    for item in select_items:
        key = item.output_key()
        if key in seen_keys:
            error_semantic(f"Duplicate output key: {key}")
        seen_keys.add(key)
        output_keys.append(key)

    return output_keys


def validate_group_by(select_items: list[SelectItem], group_by: Optional[list[FieldRef]]):
    """Validate GROUP BY rules."""
    if group_by is None:
        return

    # Check for duplicate GROUP BY fields
    seen = set()
    for field in group_by:
        text = field.as_text()
        if text in seen:
            error_semantic(f"Duplicate GROUP BY field: {text}")
        seen.add(text)

    # Check that all non-aggregate SELECT items are in GROUP BY
    for item in select_items:
        if isinstance(item.item, AggCall):
            continue
        # It's a field reference
        field_text = item.item.as_text()
        if item.alias is not None:
            field_text = item.alias
        group_by_fields = {f.as_text() for f in group_by}
        if field_text not in group_by_fields:
            error_semantic(f"Field '{field_text}' in SELECT must appear in GROUP BY")


def execute_query(sources: dict[str, list[dict]], query: ConflatedQuery) -> list[dict]:
    """Execute the query with multi-source conflation and return results."""
    select_items = query.select_items
    from_alias = query.from_alias
    conflate_clauses = query.conflate_clauses
    where_expr = query.where_expr
    group_by = query.group_by

    # Validate from_alias exists
    if from_alias not in sources:
        error_semantic(f"Unknown alias: {from_alias}")

    # Collect all referenced aliases
    all_aliases = {from_alias}
    for alias, _, _ in conflate_clauses:
        if alias not in sources:
            error_semantic(f"Unknown alias: {alias}")
        if alias in all_aliases:
            error_semantic(f"Duplicate alias in query: {alias}")
        all_aliases.add(alias)

    has_agg = has_aggregates(select_items)

    # Check for bare SELECT * when CONFLATE is present
    if conflate_clauses and any(isinstance(item.item, FieldRef) and item.item.parts == ["*"] for item in select_items):
        error_semantic("SELECT * is forbidden when any CONFLATE is present")

    # Validate that all field references use qualified names when CONFLATE is present
    if conflate_clauses:
        check_qualified_refs(select_items, group_by)

    validate_select_items_conflation(select_items, group_by)
    validate_group_by_conflation(select_items, group_by)

    # Build the joined rows
    anchor_rows = sources[from_alias]
    joined_rows = build_joined_rows(anchor_rows, sources, conflate_clauses)

    # Apply WHERE filtering with subquery and correlation support
    filtered_rows = []
    for row in joined_rows:
        if where_expr is None:
            filtered_rows.append(row)
        else:
            # Evaluate WHERE with correlation context - treat row as the outer row
            result = evaluate_predicate(where_expr, sources, row)
            if result:
                filtered_rows.append(row)

    if not filtered_rows:
        # Return empty result with appropriate structure
        return [{}]

    # Determine if global aggregation or grouped
    if group_by is None and has_agg:
        # Global aggregation
        return compute_global_aggregation_conflation(filtered_rows, select_items)

    if group_by is None and not has_agg:
        # No aggregation, no grouping - return filtered rows with selected fields
        return compute_no_aggregation_conflation(filtered_rows, select_items)

    # Grouped aggregation or grouped field selection without aggregates
    return compute_grouped_conflation(filtered_rows, select_items, group_by)


def compute_global_aggregation(logs: list[dict], select_items: list[SelectItem]) -> list[dict]:
    """Compute global aggregates over all rows."""
    result = {}

    for item in select_items:
        key = item.output_key()
        agg = item.item

        if isinstance(agg, AggCall):
            result[key] = evaluate_agg(agg, logs)

    return [result]


def compute_no_aggregation(logs: list[dict], select_items: list[SelectItem]) -> list[dict]:
    """No aggregation, no GROUP BY - return filtered rows with selected fields."""
    results = []
    for row in logs:
        result = {}
        for item in select_items:
            key = item.output_key()
            field_ref = item.item
            val = get_nested_value(row, field_ref.parts)
            result[key] = val
        results.append(result)
    return results


def compute_grouped(logs: list[dict], select_items: list[SelectItem], group_by: list[FieldRef]) -> list[dict]:
    """Compute grouped results with first-encountered ordering."""
    groups = OrderedDict()  # group_key -> list of rows
    group_key_order = []    # preserve first-encountered order

    # Build groups
    for row in logs:
        # Create group key
        key_parts = []
        for field in group_by:
            val = get_nested_value(row, field.parts)
            # Coerce arrays and objects to None for grouping
            if isinstance(val, (list, dict)):
                val = None
            key_parts.append(val)
        group_key = tuple(key_parts)

        if group_key not in groups:
            groups[group_key] = []
            group_key_order.append(group_key)
        groups[group_key].append(row)

    # Compute results for each group
    results = []
    for group_key in group_key_order:
        group_logs = groups[group_key]
        result = {}

        for item in select_items:
            key = item.output_key()

            if isinstance(item.item, AggCall):
                result[key] = evaluate_agg(item.item, group_logs)
            else:
                # Field reference - get from group key
                field_text = item.item.as_text()
                if item.alias is not None:
                    field_text = item.alias

                # Find the index in group_by
                idx = None
                for i, gb_field in enumerate(group_by):
                    if gb_field.as_text() == field_text:
                        idx = i
                        break

                if idx is not None:
                    result[key] = group_key[idx]
                else:
                    # Should not happen due to validation
                    result[key] = None

        results.append(result)

    return results


def evaluate_agg(agg: AggCall, logs: list[dict]) -> Any:
    """Evaluate an aggregate function over a list of rows."""
    func = agg.func

    if func == "COUNT":
        if agg.arg is None:  # COUNT(*)
            return len(logs)
        # COUNT(field)
        count = 0
        for row in logs:
            val = get_nested_value(row, agg.arg.parts)
            # Arrays and objects count as present
            if val is not None:
                count += 1
        return count

    if func in ("SUM", "AVG"):
        total = 0.0
        count = 0
        for row in logs:
            val = get_nested_value(row, agg.arg.parts)
            if isinstance(val, (int, float)):
                total += val
                count += 1
        if count == 0:
            return None
        if func == "SUM":
            return total if total == int(total) else total
        return total / count

    if func == "MIN":
        # Try numbers first, then strings
        numbers = []
        strings = []
        for row in logs:
            val = get_nested_value(row, agg.arg.parts)
            if isinstance(val, (int, float)):
                numbers.append(val)
            elif isinstance(val, str):
                strings.append(val)

        if numbers:
            return min(numbers)
        if strings:
            return min(strings)
        return None

    if func == "MAX":
        numbers = []
        strings = []
        for row in logs:
            val = get_nested_value(row, agg.arg.parts)
            if isinstance(val, (int, float)):
                numbers.append(val)
            elif isinstance(val, str):
                strings.append(val)

        if numbers:
            return max(numbers)
        if strings:
            return max(strings)
        return None

    if func == "UNIQUE":
        seen = []
        for row in logs:
            val = get_nested_value(row, agg.arg.parts)
            # For grouping purposes, coerce arrays and objects to their canonical form
            # but keep them as values in the output
            found = False
            for existing in seen:
                if deep_equal(existing, val):
                    found = True
                    break
            if not found:
                seen.append(val)
        return seen

    raise ValueError(f"Unknown aggregate function: {func}")


# ==============================================================================
# Validation and Join Execution (Part 3)
# ==============================================================================

def check_qualified_refs(select_items: list[SelectItem], group_by: Optional[list[FieldRef]]):
    """Check that all field references are qualified when CONFLATE is present."""
    for item in select_items:
        if isinstance(item.item, FieldRef) and item.item.parts != ["*"]:
            # Must have at least 2 parts: alias.field
            if len(item.item.parts) < 2:
                error_semantic(f"Unqualified field reference: {item.item.as_text()}. Must be qualified when CONFLATE is present")
        elif isinstance(item.item, AggCall) and item.item.arg is not None:
            # Aggregate arguments must be qualified
            if len(item.item.arg.parts) < 2:
                error_semantic(f"Unqualified field reference in aggregate: {item.item.arg.as_text()}")
        elif isinstance(item.item, WildcardSelect):
            pass  # alias.* is always qualified

    if group_by:
        for field in group_by:
            if len(field.parts) < 2:
                error_semantic(f"Unqualified field reference in GROUP BY: {field.as_text()}")


def validate_select_items_conflation(select_items: list[SelectItem], group_by: Optional[list[FieldRef]]):
    """Validate SELECT items and return output keys for conflation queries."""
    output_keys = []
    seen_keys = set()

    for item in select_items:
        key = item.output_key()
        if key in seen_keys:
            error_semantic(f"Duplicate output key: {key}")
        seen_keys.add(key)
        output_keys.append(key)

    return output_keys


def validate_group_by_conflation(select_items: list[SelectItem], group_by: Optional[list[FieldRef]]):
    """Validate GROUP BY rules for conflation queries."""
    if group_by is None:
        return

    # Check for duplicate GROUP BY fields
    seen = set()
    for field in group_by:
        text = field.as_text()
        if text in seen:
            error_semantic(f"Duplicate GROUP BY field: {text}")
        seen.add(text)

    # Check that all non-aggregate SELECT items are in GROUP BY
    for item in select_items:
        if isinstance(item.item, AggCall):
            continue
        if isinstance(item.item, WildcardSelect):
            # Wildcard selects are not checked against GROUP BY
            continue
        # It's a field reference
        field_text = item.item.as_text()
        if item.alias is not None:
            field_text = item.alias

        group_by_fields = {f.as_text() for f in group_by}
        if field_text not in group_by_fields:
            error_semantic(f"Field '{field_text}' in SELECT must appear in GROUP BY")


def build_joined_rows(
    anchor_rows: list[dict],
    sources: dict[str, list[dict]],
    conflate_clauses: list[tuple[str, JoinFlavor, JoinPredicate]]
) -> list[dict]:
    """Build joined rows from anchor and conflated sources."""
    if not conflate_clauses:
        # No conflation, just prefix each row with its alias
        alias = list(sources.keys())[0]
        return [{f"{alias}.{k}": v for k, v in row.items()} for row in anchor_rows]

    # Process conflate clauses sequentially
    current_rows = anchor_rows
    current_sources = {list(sources.keys())[0]: anchor_rows}

    for conflate_alias, flavor, predicate in conflate_clauses:
        conflate_source = sources[conflate_alias]
        current_rows = perform_join(
            current_rows,
            current_sources,
            conflate_alias,
            conflate_source,
            flavor,
            predicate
        )
        current_sources[conflate_alias] = conflate_source

    return current_rows


def perform_join(
    left_rows: list[dict],
    left_sources: dict[str, list[dict]],
    right_alias: str,
    right_rows: list[dict],
    flavor: JoinFlavor,
    predicate: JoinPredicate
) -> list[dict]:
    """Perform a single join operation between left and right sources."""
    left_alias = list(left_sources.keys())[0]
    result = []

    def build_matched_row(left: dict, right: dict) -> dict:
        """Merge two rows into one, with qualified keys."""
        merged = {}
        for k, v in left.items():
            merged[k] = v
        for k, v in right.items():
            merged[k] = v
        return merged

    def check_predicate(left: dict, right: dict) -> bool:
        """Check if all conjuncts in the predicate are satisfied."""
        for conjunct in predicate.conjuncts:
            left_val = get_joined_value(left, conjunct.left, left_alias)
            right_val = get_joined_value(right, conjunct.right, right_alias)
            if not deep_equal(left_val, right_val):
                return False
        return True

    def get_joined_value(row: dict, field: FieldRef, source_alias: str) -> Any:
        """Get a value from a joined row, handling qualified key names."""
        field_str = field.as_text()
        qualified_str = f"{source_alias}.{field_str}"
        # Try qualified key first
        if qualified_str in row:
            return row[qualified_str]
        # Try unqualified key (for backward compatibility with anchor)
        if len(field.parts) > 1:
            unqualified = ".".join(field.parts[1:])
            if unqualified in row:
                return row[unqualified]
        return None

    # Build index for right rows based on join keys
    right_index = {}
    for right_row in right_rows:
        # Create a key from the right side of each conjunct
        key_parts = []
        for conjunct in predicate.conjuncts:
            val = get_joined_value(right_row, conjunct.right, right_alias)
            key_parts.append(val)
        join_key = tuple(key_parts)
        if join_key not in right_index:
            right_index[join_key] = []
        right_index[join_key].append(right_row)

    used_right_indices = set()
    matched_pairs = []

    # First pass: find all matching pairs
    for left_row in left_rows:
        # Create a key from the left side of each conjunct
        key_parts = []
        for conjunct in predicate.conjuncts:
            val = get_joined_value(left_row, conjunct.left, left_alias)
            key_parts.append(val)
        join_key = tuple(key_parts)

        matching_rights = right_index.get(join_key, [])
        for right_row in matching_rights:
            matched_row = build_matched_row(left_row, right_row)
            matched_pairs.append((matched_row, left_row, right_row))
            used_right_indices.add(id(right_row))

    # Process based on join flavor
    if flavor.flavor == "INTERSECTING":
        result = [row for row, _, _ in matched_pairs]
    elif flavor.flavor == "PRESERVING":
        if flavor.side == "LEFT":
            # Matched pairs first
            for row, left, _ in matched_pairs:
                result.append(row)
            left_indices_with_match = {id(row) for _, left, _ in matched_pairs}
            for left_row in left_rows:
                if id(left_row) not in left_indices_with_match:
                    null_expanded = {}
                    for k, v in left_row.items():
                        null_expanded[k] = v
                    if right_rows:
                        for rk, rv in right_rows[0].items():
                            null_expanded[f"{right_alias}.{rk}"] = None
                    result.append(null_expanded)
        elif flavor.side == "RIGHT":
            # Matched pairs first
            for row, _, right in matched_pairs:
                result.append(row)
            # Unmatched right rows
            right_indices_with_match = {id(row) for _, _, right in matched_pairs}
            for right_row in right_rows:
                if id(right_row) not in right_indices_with_match:
                    # Create row with left side nullified
                    null_expanded = {}
                    if left_rows:
                        for lk, lv in left_rows[0].items():
                            null_expanded[lk] = None
                    for rk, rv in right_row.items():
                        null_expanded[f"{right_alias}.{rk}"] = rv
                    result.append(null_expanded)
        elif flavor.side == "BOTH":
            # Matched pairs first
            for row, _, _ in matched_pairs:
                result.append(row)
            # Unmatched left rows
            left_indices_with_match = {id(row) for _, left, _ in matched_pairs}
            for left_row in left_rows:
                if id(left_row) not in left_indices_with_match:
                    null_expanded = {}
                    for k, v in left_row.items():
                        null_expanded[k] = v
                    if right_rows:
                        for rk, rv in right_rows[0].items():
                            null_expanded[f"{right_alias}.{rk}"] = None
                    result.append(null_expanded)
            # Unmatched right rows
            right_indices_with_match = {id(row) for _, _, right in matched_pairs}
            for right_row in right_rows:
                if id(right_row) not in right_indices_with_match:
                    null_expanded = {}
                    if left_rows:
                        for lk, lv in left_rows[0].items():
                            null_expanded[lk] = None
                    for rk, rv in right_row.items():
                        null_expanded[f"{right_alias}.{rk}"] = rv
                    result.append(null_expanded)

    return result


def compute_global_aggregation_conflation(rows: list[dict], select_items: list[SelectItem]) -> list[dict]:
    """Compute global aggregates over joined rows."""
    result = {}

    for item in select_items:
        key = item.output_key()
        agg = item.item

        if isinstance(agg, AggCall):
            result[key] = evaluate_agg(agg, rows)
        elif isinstance(agg, WildcardSelect):
            pass

    return [result]


def compute_no_aggregation_conflation(rows: list[dict], select_items: list[SelectItem]) -> list[dict]:
    """No aggregation, no GROUP BY - return joined rows with selected fields."""
    results = []
    for row in rows:
        result = {}
        for item in select_items:
            key = item.output_key()
            if isinstance(item.item, WildcardSelect):
                # Expand alias.* to matching qualified keys
                alias = item.item.alias
                prefix = f"{alias}."
                for k, v in row.items():
                    if k.startswith(prefix):
                        result[k] = v
                # Also add the unqualified version if it exists and matches
                for k, v in row.items():
                    if not k.startswith(prefix) and "." not in k:
                        qualified_key = f"{alias}.{k}"
                        if qualified_key not in result:
                            result[qualified_key] = v
            elif isinstance(item.item, FieldRef):
                val = get_nested_value(row, item.item.parts)
                result[key] = val
        results.append(result)
    return results


def compute_grouped_conflation(rows: list[dict], select_items: list[SelectItem], group_by: list[FieldRef]) -> list[dict]:
    """Compute grouped results with proper ordering for conflation queries."""
    groups = OrderedDict()  # group_key -> list of rows
    group_key_order = []    # preserve first-encountered order

    for row in rows:
        # Create group key
        key_parts = []
        for field in group_by:
            val = get_nested_value(row, field.parts)
            # Coerce arrays and objects to None for grouping
            if isinstance(val, (list, dict)):
                val = None
            key_parts.append(val)
        group_key = tuple(key_parts)

        if group_key not in groups:
            groups[group_key] = []
            group_key_order.append(group_key)
        groups[group_key].append(row)

    # Compute results for each group in group_key_order
    results = []
    for group_key in group_key_order:
        group_logs = groups[group_key]
        result = {}

        for item in select_items:
            key = item.output_key()

            if isinstance(item.item, AggCall):
                result[key] = evaluate_agg(item.item, group_logs)
            else:
                # Field reference - get from group key
                field_text = item.item.as_text()
                if item.alias is not None:
                    field_text = item.alias

                # Find the index in group_by
                idx = None
                for i, gb_field in enumerate(group_by):
                    if gb_field.as_text() == field_text:
                        idx = i
                        break

                if idx is not None:
                    result[key] = group_key[idx]
                else:
                    result[key] = None

        results.append(result)

    return results


def main():
    parser = argparse.ArgumentParser(
        description="LogQL - Part 3: Multi-Source Conflation / Joins"
    )
    parser.add_argument("--query", required=True, help="SQL-like query string")
    parser.add_argument("--output", help="Output file path (default: stdout)")
    parser.add_argument("--log-file", help="Path to NDJSON log file (shorthand for --source logs=<path>)")
    parser.add_argument("--source", action="append", help="Bind an alias to an NDJSON file (format: alias=path)")

    args = parser.parse_args()

    try:
        query_str = args.query.strip()

        # Build sources map from --source flags
        sources = {}
        if args.log_file:
            sources["logs"] = args.log_file
        if args.source:
            for src in args.source:
                if "=" not in src:
                    error_semantic(f"Invalid --source format: {src}. Expected alias=path")
                alias, path = src.split("=", 1)
                alias = alias.strip()
                path = path.strip()
                if not alias:
                    error_semantic("Alias cannot be empty")
                if not path:
                    error_semantic("Path cannot be empty")
                if alias in sources:
                    error_semantic(f"Duplicate alias binding: {alias}")
                sources[alias] = path

        if not sources:
            error_semantic("At least one source is required (use --source or --log-file)")

        # Tokenize and parse
        tokenizer = Tokenizer(query_str)
        tokens = tokenizer.tokenize()

        parser = Parser(tokens)
        query = parser.parse_query()

        # Load and execute
        loaded_sources = {alias: load_logs(path) for alias, path in sources.items()}
        results = execute_query(loaded_sources, query)

        # Output
        output_json = json.dumps(results)
        if args.output:
            with open(args.output, 'w') as f:
                f.write(output_json)
        else:
            print(output_json)

    except ParseError as e:
        error_syntax(str(e))
    except Exception as e:
        error_semantic(str(e))


if __name__ == "__main__":
    main()
