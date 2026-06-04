#!/usr/bin/env python3
"""
LogQL - Nested Queries and Correlated Subqueries (Part 5)
Supports scalar subqueries, table subqueries, and correlation.
"""

import argparse
import json
import sys
from typing import Any, List, Optional, Union, Tuple, Dict, Set
from enum import Enum
from dataclasses import dataclass, field


# =============================================================================
# Tokenization
# =============================================================================

class TokenType(Enum):
    SELECT = "SELECT"
    FROM = "FROM"
    WHERE = "WHERE"
    GROUP = "GROUP"
    BY = "BY"
    AS = "AS"
    AND = "AND"
    OR = "OR"
    STAR = "*"
    COMMA = ","
    LPAREN = "("
    RPAREN = ")"
    LBRACK = "["
    RBRACK = "]"
    IDENT = "IDENT"
    STRING = "STRING"
    NUMBER = "NUMBER"
    BOOL = "BOOL"
    NULL = "NULL"
    OP_EQ = "="
    OP_NE = "!="
    OP_LT = "<"
    OP_LE = "<="
    OP_GT = ">"
    OP_GE = ">="
    DOT = "."
    PIPE = "|"
    LCURLY = "{"
    RCURLY = "}"
    COLON = ":"
    EOF = "EOF"

    # NEW tokens for Part 5
    POCKET = "POCKET"
    BEHOLDS = "BEHOLDS"
    AMONGST = "AMONGST"
    EITHERWISE = "EITHERWISE"
    EVERYWISE = "EVERYWISE"
    UPTREE = "UPTREE"

    # Aggregate functions
    COUNT = "COUNT"
    SUM = "SUM"
    AVG = "AVG"
    AVERAGE = "AVERAGE"
    MIN = "MIN"
    MAX = "MAX"
    UNIQUE = "UNIQUЕ"

    # CONFLATE keywords
    CONFLATE = "CONFLATE"
    INTERSECTING = "INTERSECTING"
    PRESERVING = "PRESERVING"
    LEFT = "LEFT"
    RIGHT = "RIGHT"
    BOTH = "BOTH"
    UPON = "UPON"

    # GLOSS keywords
    GLOSS = "GLOSS"
    STRICT = "STRICT"
    DEFAULT = "DEFAULT"
    CANON = "CANON"


@dataclass
class Token:
    type: TokenType
    value: str
    position: int = 0

    def __repr__(self):
        return f"Token({self.type.name}, {repr(self.value)})"


# =============================================================================
# AST Nodes
# =============================================================================

@dataclass
class FieldRef:
    parts: List[str]

    @property
    def key(self) -> str:
        return ".".join(self.parts)

    @property
    def is_outer_ref(self) -> bool:
        return len(self.parts) >= 2 and self.parts[0].lower() == "uptree"

    def get_outer_alias(self) -> Optional[str]:
        if self.is_outer_ref and len(self.parts) >= 2:
            return self.parts[1]
        return None

    def get_field_path(self) -> List[str]:
        if self.is_outer_ref and len(self.parts) >= 2:
            return self.parts[2:]
        return self.parts


@dataclass
class AggregateCall:
    func: TokenType
    arg: Optional[FieldRef]
    as_alias: Optional[str] = None

    @property
    def canonical_key(self) -> str:
        func_name = self.func.name
        if self.arg is None:
            return f"{func_name}(*)"
        return f"{func_name}({self.arg.key})"


@dataclass
class SelectItem:
    field_ref: Optional[FieldRef] = None
    aggregate: Optional[AggregateCall] = None
    as_alias: Optional[str] = None
    is_star_expansion: bool = False
    canon_ref: Optional['CanonRef'] = None
    scalar_subquery: Optional['ScalarSubqueryExpr'] = None

    @property
    def output_key(self) -> str:
        if self.as_alias:
            return self.as_alias
        if self.aggregate:
            return self.aggregate.canonical_key
        if self.canon_ref:
            return self.canon_ref.key
        if self.scalar_subquery:
            return f"pocket_subquery"
        return self.field_ref.key if self.field_ref else "unknown"

    @property
    def is_aggregate(self) -> bool:
        return self.aggregate is not None

    def get_field_refs(self) -> List[FieldRef]:
        refs = []
        if self.field_ref:
            refs.append(self.field_ref)
        if self.aggregate and self.aggregate.arg:
            refs.append(self.aggregate.arg)
        if self.canon_ref:
            refs.append(FieldRef(parts=["CANON", self.canon_ref.name]))
        if self.scalar_subquery:
            refs.extend(self._extract_refs_from_query(self.scalar_subquery.subquery.query))
        return refs

    def _extract_refs_from_query(self, query: 'Query') -> List[FieldRef]:
        refs = []
        for item in query.select_list.items:
            refs.extend(item.get_field_refs())
        if query.where_expr:
            refs.extend(self._extract_refs_from_bool(query.where_expr))
        if query.gloss_clause:
            for decl in query.gloss_clause.declarations:
                for cand in decl.candidates:
                    refs.append(FieldRef(parts=cand.parts))
        return refs

    def _extract_refs_from_bool(self, expr: 'BooleanExpr') -> List[FieldRef]:
        if isinstance(expr, (BinaryExpr, ExistsPredicate, InPredicate, AnyPredicate, AllPredicate)):
            return self._collect_field_refs_from_expr(expr)
        elif isinstance(expr, ParenExpr):
            return self._extract_refs_from_bool(expr.inner)
        elif isinstance(expr, Predicate):
            return [expr.field_ref]
        return []

    def _collect_field_refs_from_expr(self, expr) -> List[FieldRef]:
        refs = []
        if isinstance(expr, BinaryExpr):
            refs.extend(self._extract_refs_from_bool(expr.left))
            refs.extend(self._extract_refs_from_bool(expr.right))
        elif isinstance(expr, ExistsPredicate):
            refs.extend(self._extract_refs_from_subquery_expr(expr.table_subexpr))
        elif isinstance(expr, InPredicate):
            refs.append(expr.value_expr)
            refs.extend(self._extract_refs_from_subquery_expr(expr.table_subexpr))
        elif isinstance(expr, AnyPredicate):
            refs.append(expr.value_expr)
            refs.extend(self._extract_refs_from_subquery_expr(expr.table_subexpr))
        elif isinstance(expr, AllPredicate):
            refs.append(expr.value_expr)
            refs.extend(self._extract_refs_from_subquery_expr(expr.table_subexpr))
        return refs

    def _extract_refs_from_subquery_expr(self, subexpr: 'TableSubqueryExpr') -> List[FieldRef]:
        return self._extract_refs_from_query(subexpr.subquery.query)


@dataclass
class SelectList:
    items: List[SelectItem]
    is_star: bool = False
    star_expansions: List[str] = field(default_factory=list)

    def has_aggregates(self) -> bool:
        return any(item.is_aggregate for item in self.items)


# =============================================================================
# GLOSS / Canonical Labels (Part 4)
# =============================================================================

@dataclass
class CanonSource:
    parts: List[str]

    @property
    def key(self) -> str:
        return ".".join(self.parts)


@dataclass
class CanonDecl:
    name: str
    candidates: List[CanonSource]
    default: Any = None


@dataclass
class CanonRef:
    name: str

    @property
    def key(self) -> str:
        return f"CANON.{self.name}"


@dataclass
class GlossClause:
    declarations: List[CanonDecl]
    strict: bool = False


# =============================================================================
# Subquery and Correlation Nodes (Part 5)
# =============================================================================

@dataclass
class Subquery:
    query: 'Query'
    is_table: bool = False


@dataclass
class ScalarSubqueryExpr:
    subquery: Subquery


@dataclass
class TableSubqueryExpr:
    subquery: Subquery


# =============================================================================
# Boolean Expressions (Part 5)
# =============================================================================

@dataclass
class BooleanExpr:
    pass


@dataclass
class BinaryExpr(BooleanExpr):
    op_token: Token
    left: BooleanExpr
    right: BooleanExpr


@dataclass
class ParenExpr(BooleanExpr):
    inner: BooleanExpr


@dataclass
class Predicate(BooleanExpr):
    field_ref: FieldRef
    op_token: Token
    literal: Any


@dataclass
class ExistsPredicate(BooleanExpr):
    table_subexpr: TableSubqueryExpr  # BEHOLDS POCKET[...]


@dataclass
class InPredicate(BooleanExpr):
    value_expr: FieldRef
    table_subexpr: TableSubqueryExpr  # value AMONGST POCKET[...]


@dataclass
class AnyPredicate(BooleanExpr):
    """<value> <op> EITHERWISE POCKET[...]"""
    value_expr: FieldRef
    op_token: Token
    table_subexpr: TableSubqueryExpr


@dataclass
class AllPredicate(BooleanExpr):
    """<value> <op> EVERYWISE POCKET[...]"""
    value_expr: FieldRef
    op_token: Token
    table_subexpr: TableSubqueryExpr


# =============================================================================
# Query Structure
# =============================================================================

@dataclass
class Conjunct:
    left: FieldRef
    right: FieldRef


@dataclass
class JoinPred:
    conjuncts: List[Conjunct]


class JoinFlavor(Enum):
    INTERSECTING = "intersecting"
    PRESERVING_LEFT = "preserving_left"
    PRESERVING_RIGHT = "preserving_right"
    PRESERVING_BOTH = "preserving_both"


@dataclass
class ConflateClause:
    alias: str
    flavor: JoinFlavor
    join_pred: JoinPred


@dataclass
class Query:
    select_list: SelectList
    where_expr: Optional[BooleanExpr] = None
    group_by: Optional[List[FieldRef]] = None
    conflate_clauses: List[ConflateClause] = field(default_factory=list)
    gloss_clause: Optional[GlossClause] = None


# =============================================================================
# Errors
# =============================================================================

class LogQLError(Exception):
    def __init__(self, message: str, position: Optional[int] = None):
        self.message = message
        self.position = position
        super().__init__(message)


# =============================================================================
# Tokenizer
# =============================================================================

class Tokenizer:
    KEYWORDS = {
        "select": TokenType.SELECT,
        "from": TokenType.FROM,
        "where": TokenType.WHERE,
        "group": TokenType.GROUP,
        "by": TokenType.BY,
        "as": TokenType.AS,
        "and": TokenType.AND,
        "or": TokenType.OR,
        "true": TokenType.BOOL,
        "false": TokenType.BOOL,
        "null": TokenType.NULL,
        "count": TokenType.COUNT,
        "sum": TokenType.SUM,
        "avg": TokenType.AVG,
        "average": TokenType.AVERAGE,
        "min": TokenType.MIN,
        "max": TokenType.MAX,
        "unique": TokenType.UNIQUE,
        "conflate": TokenType.CONFLATE,
        "intersecting": TokenType.INTERSECTING,
        "preserving": TokenType.PRESERVING,
        "left": TokenType.LEFT,
        "right": TokenType.RIGHT,
        "both": TokenType.BOTH,
        "upon": TokenType.UPON,
        "gloss": TokenType.GLOSS,
        "strict": TokenType.STRICT,
        "default": TokenType.DEFAULT,
        "canon": TokenType.CANON,
        "pocket": TokenType.POCKET,
        "behaveslike": TokenType.BEHOLDS,
        "behaves": TokenType.BEHOLDS,  # alias
        "amongst": TokenType.AMONGST,
        "eitherwise": TokenType.EITHERWISE,
        "everywise": TokenType.EVERYWISE,
        "uptree": TokenType.UPTREE,
    }

    def __init__(self, query: str):
        self.query = query
        self.pos = 0
        self.length = len(query)

    def peek(self, offset: int = 0) -> str:
        p = self.pos + offset
        if p >= self.length:
            return ""
        return self.query[p]

    def consume(self) -> str:
        char = self.peek()
        self.pos += 1
        return char

    def skip_whitespace(self):
        while self.pos < self.length and self.peek().isspace():
            self.consume()

    def make_token(self, type_: TokenType, value: str) -> Token:
        return Token(type_, value, self.pos - len(value))

    def read_identifier(self) -> Token:
        start = self.pos
        while self.pos < self.length:
            ch = self.peek()
            if ch.isalnum() or ch == '_':
                self.consume()
            else:
                break
        val = self.query[start:self.pos]
        lower = val.lower()
        if lower in self.KEYWORDS:
            return self.make_token(self.KEYWORDS[lower], val)
        return self.make_token(TokenType.IDENT, val)

    def read_number(self) -> Token:
        start = self.pos
        has_decimal = False
        if self.peek() == '-':
            self.consume()
        while self.pos < self.length:
            ch = self.peek()
            if ch.isdigit():
                self.consume()
            elif ch == '.' and not has_decimal:
                has_decimal = True
                self.consume()
            else:
                break
        return self.make_token(TokenType.NUMBER, self.query[start:self.pos])

    def read_string(self) -> Token:
        start = self.pos
        self.consume()  # opening "
        parts = []
        while self.pos < self.length:
            ch = self.consume()
            if ch == '"':
                return self.make_token(TokenType.STRING, "".join(parts))
            elif ch == '\\':
                nxt = self.peek()
                if nxt == '"' or nxt == '\\':
                    parts.append(self.consume())
                else:
                    raise LogQLError(f"Invalid escape sequence \\\\{nxt}", self.pos - 1)
            else:
                parts.append(ch)
        raise LogQLError("Unterminated string literal", start)

    def tokenize(self) -> List[Token]:
        tokens = []
        while self.pos < self.length:
            self.skip_whitespace()
            start = self.pos
            ch = self.peek()

            if ch == '"':
                tokens.append(self.read_string())
            elif ch.isalpha() or ch == '_':
                tokens.append(self.read_identifier())
            elif ch.isdigit() or (ch == '-' and self.peek(1).isdigit()):
                tokens.append(self.read_number())
            elif ch == '*':
                tokens.append(self.make_token(TokenType.STAR, "*"))
                self.consume()
            elif ch == ',':
                tokens.append(self.make_token(TokenType.COMMA, ","))
                self.consume()
            elif ch == '(':
                tokens.append(self.make_token(TokenType.LPAREN, "("))
                self.consume()
            elif ch == ')':
                tokens.append(self.make_token(TokenType.RPAREN, ")"))
                self.consume()
            elif ch == '[':
                tokens.append(self.make_token(TokenType.LBRACK, "["))
                self.consume()
            elif ch == ']':
                tokens.append(self.make_token(TokenType.RBRACK, "]"))
                self.consume()
            elif ch == '.':
                tokens.append(self.make_token(TokenType.DOT, "."))
                self.consume()
            elif ch == '|':
                tokens.append(self.make_token(TokenType.PIPE, "|"))
                self.consume()
            elif ch == '{':
                tokens.append(self.make_token(TokenType.LCURLY, "{"))
                self.consume()
            elif ch == '}':
                tokens.append(self.make_token(TokenType.RCURLY, "}"))
                self.consume()
            elif ch == ':':
                tokens.append(self.make_token(TokenType.COLON, ":"))
                self.consume()
            elif ch == '!':
                if self.peek(1) == '=':
                    self.consume()
                    self.consume()
                    tokens.append(self.make_token(TokenType.OP_NE, "!="))
                else:
                    raise LogQLError("Unexpected '!'", self.pos)
            elif ch == '<':
                self.consume()
                if self.peek() == '=':
                    self.consume()
                    tokens.append(self.make_token(TokenType.OP_LE, "<="))
                else:
                    tokens.append(self.make_token(TokenType.OP_LT, "<"))
            elif ch == '>':
                self.consume()
                if self.peek() == '=':
                    self.consume()
                    tokens.append(self.make_token(TokenType.OP_GE, ">="))
                else:
                    tokens.append(self.make_token(TokenType.OP_GT, ">"))
            elif ch == '=':
                tokens.append(self.make_token(TokenType.OP_EQ, "="))
                self.consume()
            else:
                raise LogQLError(f"Unexpected character '{ch}'", self.pos)

        tokens.append(Token(TokenType.EOF, "", self.pos))
        return tokens


# =============================================================================
# Parser
# =============================================================================

class Parser:
    def __init__(self, tokens: List[Token]):
        self.tokens = tokens
        self.pos = 0

    def peek(self, offset: int = 0) -> Token:
        p = self.pos + offset
        if p >= len(self.tokens):
            return self.tokens[-1]
        return self.tokens[p]

    def consume(self, expected_type: Optional[TokenType] = None,
                expected_value: Optional[str] = None) -> Token:
        t = self.peek()
        if expected_type is not None and t.type != expected_type:
            raise LogQLError(f"Expected {expected_type.name}, got {t.type.name}", t.position)
        if expected_value is not None and t.value != expected_value:
            raise LogQLError(f"Expected '{expected_value}', got '{t.value}'", t.position)
        self.pos += 1
        return t

    def parse_query(self) -> Query:
        self.consume(TokenType.SELECT)
        select_list = self.parse_select_list()
        self.consume(TokenType.FROM)
        self.consume(TokenType.IDENT)  # from_alias (bound via --source)

        where_expr = None
        if self.peek().type == TokenType.WHERE:
            self.consume(TokenType.WHERE)
            where_expr = self.parse_boolean_expr()

        gloss_clause = None
        if self.peek().type == TokenType.GLOSS:
            self.consume(TokenType.GLOSS)
            gloss_clause = self.parse_gloss_clause()

        conflate_clauses = []
        while self.peek().type == TokenType.CONFLATE:
            self.consume(TokenType.CONFLATE)
            clause = self.parse_conflate_clause()
            conflate_clauses.append(clause)

        group_by = None
        if self.peek().type == TokenType.GROUP:
            self.consume(TokenType.GROUP)
            self.consume(TokenType.BY)
            group_by = self.parse_group_list()

        self.consume(TokenType.EOF)
        return Query(select_list, where_expr, group_by, conflate_clauses, gloss_clause)

    def parse_select_list(self) -> SelectList:
        if self.peek().type == TokenType.STAR:
            self.consume(TokenType.STAR)
            return SelectList([], is_star=True)

        fields = []
        seen: Set[str] = set()
        star_expansions = []

        while True:
            item = self.parse_select_item()

            if item.field_ref and item.field_ref.key.endswith('.*'):
                alias = item.field_ref.key[:-2]
                star_expansions.append(alias)
                item.is_star_expansion = True

            output_key = item.output_key
            if output_key in seen:
                raise LogQLError(f"Duplicate output key '{output_key}'")
            seen.add(output_key)

            fields.append(item)

            if self.peek().type != TokenType.COMMA:
                break
            self.consume(TokenType.COMMA)

        return SelectList(fields, is_star=False, star_expansions=star_expansions)

    def parse_select_item(self) -> SelectItem:
        # Check for CANON.<name>
        if self.peek().type == TokenType.IDENT:
            ident = self.consume(TokenType.IDENT)
            if ident.value.lower() == "canon" and self.peek().type == TokenType.DOT:
                self.consume(TokenType.DOT)
                name_tok = self.consume(TokenType.IDENT)
                canon_ref = CanonRef(name=name_tok.value)

                as_alias = None
                if self.peek().type == TokenType.AS:
                    self.consume(TokenType.AS)
                    as_alias = self.consume(TokenType.IDENT).value

                return SelectItem(canon_ref=canon_ref, as_alias=as_alias)

        # Check for POCKET subquery (scalar)
        if self.peek().type == TokenType.POCKET:
            subquery = self.parse_subquery(is_table=False)
            as_alias = None
            if self.peek().type == TokenType.AS:
                self.consume(TokenType.AS)
                as_alias = self.consume(TokenType.IDENT).value
            return SelectItem(scalar_subquery=ScalarSubqueryExpr(subquery=subquery), as_alias=as_alias)

        # Check for aggregate function
        agg_types = {
            TokenType.COUNT: lambda: AggregateCall(TokenType.COUNT, None),
            TokenType.SUM: lambda: AggregateCall(TokenType.SUM, self.parse_field_ref()),
            TokenType.AVG: lambda: AggregateCall(TokenType.AVG, self.parse_field_ref()),
            TokenType.MIN: lambda: AggregateCall(TokenType.MIN, self.parse_field_ref()),
            TokenType.MAX: lambda: AggregateCall(TokenType.MAX, self.parse_field_ref()),
            TokenType.UNIQUE: lambda: AggregateCall(TokenType.UNIQUE, self.parse_field_ref()),
        }

        if self.peek().type in agg_types:
            func_token = self.peek()
            self.consume()
            if func_token.type == TokenType.COUNT:
                if self.peek().type == TokenType.STAR:
                    self.consume(TokenType.STAR)
                    agg = AggregateCall(func_token.type, None)
                else:
                    arg = self.parse_field_ref()
                    agg = AggregateCall(func_token.type, arg)
            else:
                arg = self.parse_field_ref()
                agg = AggregateCall(func_token.type, arg)

            as_alias = None
            if self.peek().type == TokenType.AS:
                self.consume(TokenType.AS)
                as_alias = self.consume(TokenType.IDENT).value

            return SelectItem(aggregate=agg, as_alias=as_alias)

        # Regular field reference
        field_ref = self.parse_field_ref_or_outer()

        as_alias = None
        if self.peek().type == TokenType.AS:
            self.consume(TokenType.AS)
            as_alias = self.consume(TokenType.IDENT).value

        return SelectItem(field_ref=field_ref, as_alias=as_alias)

    def parse_field_ref(self) -> FieldRef:
        parts = []
        while self.peek().type == TokenType.IDENT:
            ident = self.consume(TokenType.IDENT)
            parts.append(ident.value)
            if self.peek().type == TokenType.DOT:
                self.consume()
            else:
                break
        if not parts:
            raise LogQLError("Expected identifier", self.peek().position)
        return FieldRef(parts)

    def parse_outer_ref(self) -> FieldRef:
        """Parse UPTREE.<alias>.<field...>"""
        if not (self.peek().type == TokenType.IDENT and
                self.peek().value.lower() == "uptree"):
            raise LogQLError("Expected UPTREE", self.peek().position)
        self.consume()  # consume UPTREE
        if self.peek().type != TokenType.DOT:
            raise LogQLError("Expected '.' after UPTREE", self.peek().position)
        self.consume()  # consume '.'
        if self.peek().type != TokenType.IDENT:
            raise LogQLError("Expected alias after UPTREE.", self.peek().position)
        alias = self.consume(TokenType.IDENT).value

        parts = ["UPTREE", alias]

        while self.peek().type == TokenType.DOT:
            self.consume(TokenType.DOT)  # consume '.'
            if self.peek().type != TokenType.IDENT:
                raise LogQLError("Expected field name after '.'", self.peek().position)
            parts.append(self.consume(TokenType.IDENT).value)

        return FieldRef(parts=parts)

    def parse_field_ref_or_outer(self) -> FieldRef:
        """Parse either regular field ref or UPTREE outer ref."""
        if self.peek().type == TokenType.IDENT and self.peek().value.lower() == "uptree":
            return self.parse_outer_ref()
        return self.parse_field_ref()

    def parse_boolean_expr(self, min_precedence: int = 0) -> BooleanExpr:
        left = self.parse_predicate_or_paren()

        while True:
            tok = self.peek()
            if tok.type == TokenType.AND:
                prec = 2
            elif tok.type == TokenType.OR:
                prec = 1
            else:
                break

            if prec < min_precedence:
                break

            self.consume()
            right = self.parse_boolean_expr(prec + 1)
            left = BinaryExpr(op_token=tok, left=left, right=right)

        return left

    def parse_predicate_or_paren(self) -> BooleanExpr:
        if self.peek().type == TokenType.LPAREN:
            self.consume(TokenType.LPAREN)
            expr = self.parse_boolean_expr()
            self.consume(TokenType.RPAREN)
            return ParenExpr(inner=expr)

        # Check for BEHOLDS (EXISTS)
        if self.peek().type == TokenType.BEHOLDS:
            self.consume(TokenType.BEHOLDS)
            table_sub = self.parse_table_subexpr()
            return ExistsPredicate(table_subexpr=table_sub)

        field_ref = self.parse_field_ref_or_outer()
        op_tok = self.peek()

        # Check for AMONGST (IN)
        if op_tok.type == TokenType.AMONGST:
            self.consume(TokenType.AMONGST)
            table_sub = self.parse_table_subexpr()
            return InPredicate(value_expr=field_ref, table_subexpr=table_sub)

        # Check for comparison with EITHERWISE or EVERYWISE
        if op_tok.type in (TokenType.OP_EQ, TokenType.OP_NE, TokenType.OP_LT,
                          TokenType.OP_LE, TokenType.OP_GT, TokenType.OP_GE):
            self.consume()  # consume comparison operator

            if self.peek().type in (TokenType.EITHERWISE, TokenType.EVERYWISE):
                is_all = self.peek().type == TokenType.EVERYWISE
                self.consume()
                table_sub = self.parse_table_subexpr()
                if is_all:
                    return AllPredicate(value_expr=field_ref, op_token=op_tok,
                                        table_subexpr=table_sub)
                else:
                    return AnyPredicate(value_expr=field_ref, op_token=op_tok,
                                        table_subexpr=table_sub)

            literal = self.parse_literal()
            return Predicate(field_ref=field_ref, op_token=op_tok, literal=literal)

        raise LogQLError(f"Expected comparison operator, got {op_tok.type.name}", op_tok.position)

    def parse_table_subexpr(self) -> TableSubqueryExpr:
        """Parse POCKET[<query>]"""
        if self.peek().type != TokenType.POCKET:
            raise LogQLError("Expected POCKET", self.peek().position)
        subquery = self.parse_subquery(is_table=True)
        return TableSubqueryExpr(subquery=subquery)

    def parse_subquery(self, is_table: bool = False) -> Subquery:
        """Parse POCKET(<query>) or POCKET[<query>]"""
        if self.peek().type != TokenType.POCKET:
            raise LogQLError(f"Expected POCKET, got {self.peek().type.name}", self.peek().position)
        self.consume()

        if is_table:
            if self.peek().type != TokenType.LBRACK:
                raise LogQLError("Expected '[' for table subquery", self.peek().position)
            self.consume()  # consume '['
        else:
            if self.peek().type != TokenType.LPAREN:
                raise LogQLError("Expected '(' for scalar subquery", self.peek().position)
            self.consume()  # consume '('

        nested_query = self.parse_nested_query()

        if is_table:
            if self.peek().type != TokenType.RBRACK:
                raise LogQLError("Expected ']' for table subquery", self.peek().position)
            self.consume()  # consume ']'
        else:
            if self.peek().type != TokenType.RPAREN:
                raise LogQLError("Expected ')' for scalar subquery", self.peek().position)
            self.consume()  # consume ')'

        return Subquery(query=nested_query, is_table=is_table)

    def parse_nested_query(self) -> Query:
        """Parse a complete nested query."""
        self.consume(TokenType.SELECT)
        select_list = self.parse_select_list()
        self.consume(TokenType.FROM)
        from_alias = self.consume(TokenType.IDENT)  # the subquery's source alias

        where_expr = None
        if self.peek().type == TokenType.WHERE:
            self.consume(TokenType.WHERE)
            where_expr = self.parse_boolean_expr()

        gloss_clause = None
        if self.peek().type == TokenType.GLOSS:
            self.consume(TokenType.GLOSS)
            gloss_clause = self.parse_gloss_clause()

        conflate_clauses = []
        while self.peek().type == TokenType.CONFLATE:
            self.consume(TokenType.CONFLATE)
            clause = self.parse_conflate_clause()
            conflate_clauses.append(clause)

        group_by = None
        if self.peek().type == TokenType.GROUP:
            self.consume(TokenType.GROUP)
            self.consume(TokenType.BY)
            group_by = self.parse_group_list()

        return Query(select_list, where_expr, group_by, conflate_clauses, gloss_clause)

    def parse_gloss_clause(self) -> GlossClause:
        strict = False
        if self.peek().type == TokenType.STRICT:
            self.consume(TokenType.STRICT)
            strict = True

        if self.peek().type != TokenType.LCURLY:
            raise LogQLError("Expected '{' after GLOSS", self.peek().position)
        self.consume(TokenType.LCURLY)

        declarations = []
        while True:
            decl = self.parse_canon_decl()
            declarations.append(decl)
            if self.peek().type == TokenType.COMMA:
                self.consume(TokenType.COMMA)
                continue
            break

        if self.peek().type != TokenType.RCURLY:
            raise LogQLError("Expected '}' closing GLOSS block", self.peek().position)
        self.consume(TokenType.RCURLY)

        return GlossClause(declarations=declarations, strict=strict)

    def parse_canon_decl(self) -> CanonDecl:
        """Parse: name := source [| source...] [DEFAULT literal]"""
        name_tok = self.consume(TokenType.IDENT)
        name = name_tok.value

        # Parse :=" (colon followed by equals)
        if self.peek().type == TokenType.COLON:
            self.consume(TokenType.COLON)
            if self.peek().type != TokenType.OP_EQ:
                raise LogQLError("Expected '=' after ':'", self.peek().position)
            self.consume(TokenType.OP_EQ)
        elif self.peek().type == TokenType.OP_EQ:
            self.consume(TokenType.OP_EQ)
        else:
            raise LogQLError("Expected ':=' in GLOSS declaration", self.peek().position)

        candidates = []
        candidates.append(self.parse_canon_source())

        while self.peek().type == TokenType.PIPE:
            self.consume(TokenType.PIPE)
            candidates.append(self.parse_canon_source())

        default = None
        if self.peek().type == TokenType.DEFAULT:
            self.consume(TokenType.DEFAULT)
            default = self.parse_literal()

        return CanonDecl(name=name, candidates=candidates, default=default)

    def parse_canon_source(self) -> CanonSource:
        parts = []
        while self.peek().type == TokenType.IDENT:
            ident = self.consume(TokenType.IDENT)
            parts.append(ident.value)
            if self.peek().type == TokenType.DOT:
                self.consume(TokenType.DOT)
            else:
                break
        if not parts:
            raise LogQLError("Expected field path in GLOSS declaration", self.peek().position)
        return CanonSource(parts=parts)

    def parse_group_list(self) -> List[FieldRef]:
        fields = []
        while True:
            parts = []
            if self.peek().type == TokenType.IDENT:
                ident = self.consume(TokenType.IDENT)
                parts.append(ident.value)
                if self.peek().type == TokenType.DOT:
                    self.consume(TokenType.DOT)
                    if self.peek().type == TokenType.IDENT:
                        ident = self.consume(TokenType.IDENT)
                        parts.append(ident.value)
            field_ref = FieldRef(parts)
            fields.append(field_ref)
            if self.peek().type != TokenType.COMMA:
                break
            self.consume(TokenType.COMMA)
        return fields

    def parse_conflate_clause(self) -> ConflateClause:
        alias = self.consume(TokenType.IDENT)

        flavor = JoinFlavor.INTERSECTING
        side = None

        if self.peek().type == TokenType.INTERSECTING:
            self.consume()
            flavor = JoinFlavor.INTERSECTING
        elif self.peek().type == TokenType.PRESERVING:
            self.consume()
            if self.peek().type == TokenType.LEFT:
                side = "left"
                self.consume()
            elif self.peek().type == TokenType.RIGHT:
                side = "right"
                self.consume()
            elif self.peek().type == TokenType.BOTH:
                side = "both"
                self.consume()

            if side == "left":
                flavor = JoinFlavor.PRESERVING_LEFT
            elif side == "right":
                flavor = JoinFlavor.PRESERVING_RIGHT
            elif side == "both":
                flavor = JoinFlavor.PRESERVING_BOTH
            else:
                flavor = JoinFlavor.PRESERVING_LEFT

        if self.peek().type != TokenType.UPON:
            raise LogQLError("Expected UPON in CONFLATE clause", self.peek().position)
        self.consume(TokenType.UPON)

        conjuncts = []
        conjuncts.append(self.parse_conjunct())
        while self.peek().type == TokenType.AND:
            self.consume(TokenType.AND)
            conjuncts.append(self.parse_conjunct())

        join_pred = JoinPred(conjuncts=conjuncts)
        return ConflateClause(alias=alias.value, flavor=flavor, join_pred=join_pred)

    def parse_conjunct(self) -> Conjunct:
        left = self.parse_field_ref_or_canon()
        if self.peek().type != TokenType.OP_EQ:
            raise LogQLError("Expected '=' in UPON conjunct", self.peek().position)
        self.consume(TokenType.OP_EQ)
        right = self.parse_field_ref_or_canon()
        return Conjunct(left=left, right=right)

    def parse_field_ref_or_canon(self) -> FieldRef:
        """Parse FieldRef or CANON.<name> as FieldRef."""
        if self.peek().type == TokenType.IDENT:
            ident = self.consume(TokenType.IDENT)
            if ident.value.lower() == "canon" and self.peek().type == TokenType.DOT:
                self.consume(TokenType.DOT)
                name_tok = self.consume(TokenType.IDENT)
                return FieldRef(parts=["CANON", name_tok.value])
            else:
                return FieldRef(parts=[ident.value])
        elif self.peek().type == TokenType.DOT:
            self.consume()
            ident = self.consume(TokenType.IDENT)
            return FieldRef(parts=["."] + [ident.value])
        raise LogQLError("Expected field reference", self.peek().position)

    def parse_literal(self) -> Any:
        tok = self.peek()
        if tok.type == TokenType.STRING:
            self.consume(TokenType.STRING)
            return unescape_string(tok.value)
        elif tok.type == TokenType.NUMBER:
            self.consume(TokenType.NUMBER)
            val = tok.value
            if "." in val:
                return float(val)
            return int(val)
        elif tok.type == TokenType.BOOL:
            self.consume(TokenType.BOOL)
            return tok.value.lower() == "true"
        elif tok.type == TokenType.NULL:
            self.consume(TokenType.NULL)
            return None
        else:
            raise LogQLError(f"Expected literal, got {tok.type.name}", tok.position)


def unescape_string(s: str) -> str:
    result = []
    i = 0
    while i < len(s):
        ch = s[i]
        if ch == '\\' and i + 1 < len(s):
            nxt = s[i + 1]
            if nxt == '"' or nxt == '\\':
                result.append(nxt)
                i += 2
            else:
                result.append(ch)
                i += 1
        else:
            result.append(ch)
            i += 1
    return "".join(result)


# =============================================================================
# Evaluation
# =============================================================================

def deep_equal(a: Any, b: Any) -> bool:
    """Deep equality check including null handling."""
    if a is None or b is None:
        return a is None and b is None
    return a == b


def compare_values(left: Any, op: Token, right: Any) -> bool:
    op_type = op.type

    # Type mismatch (excluding null)
    if type(left) != type(right) and not (left is None or right is None):
        if not (isinstance(left, (int, float)) and isinstance(right, (int, float))):
            return False

    # null handling
    if left is None or right is None:
        if op_type == TokenType.OP_EQ:
            return left is None and right is None
        elif op_type == TokenType.OP_NE:
            return not (left is None and right is None)
        else:
            return False

    # Objects and arrays
    if isinstance(left, (dict, list)):
        if op_type == TokenType.OP_EQ:
            return left == right
        elif op_type == TokenType.OP_NE:
            return left != right
        else:
            return False

    # Booleans
    if isinstance(left, bool):
        if op_type == TokenType.OP_EQ:
            return left == right
        elif op_type == TokenType.OP_NE:
            return left != right
        else:
            return False

    # Numbers
    if isinstance(left, (int, float)):
        if op_type == TokenType.OP_EQ:
            return left == right
        elif op_type == TokenType.OP_NE:
            return left != right
        elif op_type == TokenType.OP_LT:
            return left < right
        elif op_type == TokenType.OP_LE:
            return left <= right
        elif op_type == TokenType.OP_GT:
            return left > right
        elif op_type == TokenType.OP_GE:
            return left >= right

    # Strings
    if isinstance(left, str):
        if op_type == TokenType.OP_EQ:
            return left == right
        elif op_type == TokenType.OP_NE:
            return left != right
        elif op_type == TokenType.OP_LT:
            return left < right
        elif op_type == TokenType.OP_LE:
            return left <= right
        elif op_type == TokenType.OP_GT:
            return left > right
        elif op_type == TokenType.OP_GE:
            return left >= right

    return False


def evaluate(expr: BooleanExpr, record: Dict, sources: Dict[str, List],
             outer_refs: Dict[str, Dict] = None) -> bool:
    """Evaluate a boolean expression against a record (with subquery support)."""
    if outer_refs is None:
        outer_refs = {}

    if isinstance(expr, BinaryExpr):
        left_val = evaluate(expr.left, record, sources, outer_refs)
        if expr.op_token.type == TokenType.AND:
            if not left_val:
                return False
            return evaluate(expr.right, record, sources, outer_refs)
        elif expr.op_token.type == TokenType.OR:
            if left_val:
                return True
            return evaluate(expr.right, record, sources, outer_refs)

    elif isinstance(expr, ParenExpr):
        return evaluate(expr.inner, record, sources, outer_refs)

    elif isinstance(expr, Predicate):
        field_val = get_nested(record, expr.field_ref.parts)
        return compare_values(field_val, expr.op_token, expr.literal)

    elif isinstance(expr, ExistsPredicate):
        # BEHOLDS - check if subquery returns any rows
        subquery = expr.table_subexpr.subquery
        return len(evaluate_subquery(subquery, record, sources)) > 0

    elif isinstance(expr, InPredicate):
        # Check if value is in subquery results
        value = get_nested(record, expr.value_expr.parts)
        subquery = expr.table_subexpr.subquery
        results = evaluate_subquery(subquery, record, sources)
        for row in results:
            # Get first column value
            if len(row) > 0:
                if deep_equal(value, row[0]):
                    return True
        return False

    elif isinstance(expr, AnyPredicate):
        # Check if value <op> ANY item in subquery
        value = get_nested(record, expr.value_expr.parts)
        subquery = expr.table_subexpr.subquery
        results = evaluate_subquery(subquery, record, sources)

        if len(results) == 0:
            return False  # EITHERWISE over empty set is false

        for row in results:
            if len(row) > 0:
                if compare_values(value, expr.op_token, row[0]):
                    return True
        return False

    elif isinstance(expr, AllPredicate):
        # Check if value <op> ALL items in subquery
        value = get_nested(record, expr.value_expr.parts)
        subquery = expr.table_subexpr.subquery
        results = evaluate_subquery(subquery, record, sources)

        if len(results) == 0:
            return True  # EVERYWISE over empty set is true

        for row in results:
            if len(row) > 0:
                if not compare_values(value, expr.op_token, row[0]):
                    return False
        return True

    return False


def evaluate_subquery(subquery: Subquery, outer_record: Dict,
                      sources: Dict[str, List]) -> List[List]:
    """Evaluate a subquery, optionally with correlation from outer query."""
    # Build outer refs map from outer record
    outer_refs = {}
    for part in outer_record.keys():
        # This record represents the outer query's FROM source
        outer_refs['__outer'] = outer_record

    results = evaluate_query(subquery.query, sources, outer_refs)
    return results


def evaluate_query(query: Query, sources: Dict[str, List],
                   outer_refs: Dict[str, Dict] = None) -> List[List]:
    """Evaluate a query and return list of rows (each row is list of values)."""
    if outer_refs is None:
        outer_refs = {}

    # Determine the FROM source for this query
    # For nested queries, the FROM alias is the subquery's source

    from_alias = None
    # Get the source that's used as FROM in this query
    if hasattr(query, 'from_alias'):
        from_alias = query.from_alias
    else:
        # For outermost query - we need to figure out from select_list field refs
        # Actually we need to know which source this query is running against
        # Since the structure doesn't store the FROM alias explicitly, we need
        # to derive it from the select list field references
        pass

    # For now, let me restructure to store from_alias in Query
    return []  # Placeholder


def get_nested(obj: Any, parts: List[str]) -> Any:
    """Traverse nested structure. Returns None if any intermediate is missing/invalid."""
    current = obj
    for part in parts:
        if not isinstance(current, dict):
            return None
        current = current.get(part)
        if current is None:
            return None
    return current


def project_record(record: Dict, select_list: SelectList) -> Dict:
    """Extract selected fields from a record."""
    if select_list.is_star:
        return dict(record)

    result = {}
    for field_ref in select_list.fields:
        result[field_ref.key] = get_nested(record, field_ref.parts)
    return result


def evaluate_query_full(query: Query, sources: Dict[str, List],
                        outer_refs: Dict[str, Dict] = None) -> List[List]:
    """Evaluate a query and return list of rows."""
    if outer_refs is None:
        outer_refs = {}

    # Get the FROM alias
    # Since we removed from_alias from Query, we need to extract it from select_list refs
    from_aliases = set()
    for item in query.select_list.items:
        for fr in item.get_field_refs():
            if not fr.parts[0].startswith("CANON") and not fr.is_outer_ref:
                from_aliases.add(fr.parts[0])
            elif fr.is_outer_ref:
                # UPTREE references - the second part is the alias
                from_aliases.add(fr.get_outer_alias())

    # Also check WHERE clause
    if query.where_expr:
        from_aliases.update(_extract_aliases_from_bool(query.where_expr, outer_refs))

    # GLOSS clause sources
    if query.gloss_clause:
        for decl in query.gloss_clause.declarations:
            for cand in decl.candidates:
                if cand.parts:
                    from_aliases.add(cand.parts[0])

    # If we have multiple FROM sources (via conflate), need to handle
    # For now, let's handle single source
    if not from_aliases:
        return []

    source_alias = list(from_aliases)[0]
    if source_alias not in sources:
        return []

    source_records = sources[source_alias]

    # Build full outer refs map for correlation
    full_outer_refs = dict(outer_refs)
    for alias, records in sources.items():
        full_outer_refs[alias] = {"_records": records}

    results = []
    for record in source_records:
        # Create the evaluation context for this record
        context = dict(record)

        # Add all sources for correlation
        for alias, records in sources.items():
            full_outer_refs[alias] = {"_records": records}

        # Evaluate WHERE
        if query.where_expr and not evaluate(query.where_expr, context, sources, full_outer_refs):
            continue

        # Project: build output row from select list
        row = project_to_row(record, query.select_list, context, sources, full_outer_refs)
        results.append(row)

    # Handle GROUP BY (simplified)
    if query.group_by:
        results = apply_group_by(results, query.group_by, query.select_list)

    return results


def _extract_aliases_from_bool(expr: BooleanExpr, outer_refs: Dict) -> Set[str]:
    """Extract aliases used in a boolean expression."""
    aliases = set()
    if isinstance(expr, BinaryExpr):
        aliases.update(_extract_aliases_from_bool(expr.left, outer_refs))
        aliases.update(_extract_aliases_from_bool(expr.right, outer_refs))
    elif isinstance(expr, ParenExpr):
        aliases.update(_extract_aliases_from_bool(expr.inner, outer_refs))
    elif isinstance(expr, Predicate):
        if expr.field_ref.parts:
            if expr.field_ref.parts[0] != "CANON":
                aliases.add(expr.field_ref.parts[0])
    elif isinstance(expr, ExistsPredicate):
        # Table subexpr doesn't directly contribute aliases for outer
        pass
    elif isinstance(expr, InPredicate):
        if expr.value_expr.parts and expr.value_expr.parts[0] != "CANON":
            aliases.add(expr.value_expr.parts[0])
    elif isinstance(expr, AnyPredicate):
        if expr.value_expr.parts and expr.value_expr.parts[0] != "CANON":
            aliases.add(expr.value_expr.parts[0])
    elif isinstance(expr, AllPredicate):
        if expr.value_expr.parts and expr.value_expr.parts[0] != "CANON":
            aliases.add(expr.value_expr.parts[0])
    return aliases


def project_to_row(record: Dict, select_list: SelectList,
                   context: Dict, sources: Dict, outer_refs: Dict) -> List:
    """Project a record to a row based on select list."""
    row = []
    for item in select_list.items:
        if item.field_ref:
            val = get_nested(record, item.field_ref.parts)
            row.append(val)
        elif item.aggregate:
            val = compute_aggregate(item.aggregate, [record])
            row.append(val)
        elif item.canon_ref:
            val = resolve_canon(item.canon_ref, record)
            row.append(val)
        elif item.scalar_subquery:
            val = evaluate_scalar_subquery(item.scalar_subquery.subquery, context, sources)
            row.append(val)
    return row


def resolve_canon(canon_ref: CanonRef, record: Dict) -> Any:
    """Resolve a canon reference from the record's canonical labels."""
    # This would be populated by GLOSS clause processing
    # For now, simple lookup
    return record.get(f"CANON.{canon_ref.name}")


def compute_aggregate(agg: AggregateCall, records: List) -> Any:
    """Compute an aggregate over records."""
    if agg.func == TokenType.COUNT:
        if agg.arg is None:  # COUNT(*)
            return len(records)
        else:
            return sum(1 for r in records if get_nested(r, agg.arg.parts) is not None)
    elif agg.func == TokenType.SUM:
        total = 0
        for r in records:
            val = get_nested(r, agg.arg.parts)
            if val is not None:
                total += val
        return total
    elif agg.func == TokenType.AVG:
        vals = [get_nested(r, agg.arg.parts) for r in records
                if get_nested(r, agg.arg.parts) is not None]
        if not vals:
            return None
        return sum(vals) / len(vals)
    elif agg.func == TokenType.MIN:
        vals = [get_nested(r, agg.arg.parts) for r in records
                if get_nested(r, agg.arg.parts) is not None]
        return min(vals) if vals else None
    elif agg.func == TokenType.MAX:
        vals = [get_nested(r, agg.arg.parts) for r in records
                if get_nested(r, agg.arg.parts) is not None]
        return max(vals) if vals else None
    elif agg.func == TokenType.UNIQUE:
        vals = set()
        for r in records:
            val = get_nested(r, agg.arg.parts)
            if val is not None:
                vals.add(val)
        return list(vals)
    return None


def evaluate_scalar_subquery(subquery: Subquery, outer_context: Dict,
                            sources: Dict) -> Any:
    """Evaluate a scalar subquery and return a single value."""
    results = evaluate_subquery(subquery, outer_context, sources)

    if not results:
        return None  # Scalar POCKET over empty set returns null

    # Scalar subquery should return at most one column and one row
    if len(results) > 0 and len(results[0]) > 0:
        return results[0][0]
    return None


def evaluate_subquery_with_correlation(subquery: Subquery, outer_record: Dict,
                                       sources: Dict) -> List[List]:
    """Evaluate a subquery with correlation to outer record."""
    # Build outer references from the outer record
    outer_refs = {"__outer": outer_record}

    # For each field in outer record that corresponds to a source
    for key, val in outer_record.items():
        # Check if this matches a source alias
        for alias, records in sources.items():
            if key == alias:
                outer_refs[alias] = {"_record": val}
                break

    # Now execute the subquery with these outer references
    return evaluate_subquery_internal(subquery.query, sources, outer_refs)


def evaluate_subquery_internal(query: Query, sources: Dict,
                               outer_refs: Dict) -> List[List]:
    """Internal subquery evaluation with correlation support."""
    from_alias = "__outer"  # Default for correlated subqueries

    # Get source records
    source_records = []
    for alias, records in sources.items():
        source_records = records
        from_alias = alias
        break

    results = []
    for record in source_records:
        context = dict(record)

        # Build full outer refs including this record
        for alias, records in sources.items():
            outer_refs[alias] = {"_records": records}

        # Check WHERE with correlation support
        if query.where_expr and not evaluate(query.where_expr, context, sources, outer_refs):
            continue

        # Project to row
        row = project_to_row_with_outer(record, query.select_list, context, sources, outer_refs)
        results.append(row)

    return results


def project_to_row_with_outer(record: Dict, select_list: SelectList,
                              context: Dict, sources: Dict,
                              outer_refs: Dict) -> List:
    """Project record to row with support for UPTREE outer references."""
    row = []
    for item in select_list.items:
        if item.field_ref:
            # Handle UPTREE references
            if item.field_ref.is_outer_ref:
                alias = item.field_ref.get_outer_alias()
                field_path = item.field_ref.get_field_path()
                if alias in outer_refs:
                    outer_record = outer_refs[alias]
                    if isinstance(outer_record, dict) and "_record" in outer_record:
                        # Single outer record
                        val = get_nested(outer_record["_record"], field_path)
                    elif isinstance(outer_record, dict) and "_records" in outer_record:
                        # This is a full source - use the correlated value from context
                        val = context.get(".".join(field_path))
                        if val is None:
                            val = get_nested(record, field_path)
                    else:
                        val = get_nested(record, item.field_ref.parts)
                else:
                    val = get_nested(record, item.field_ref.parts)
            else:
                val = get_nested(record, item.field_ref.parts)
            row.append(val)
        elif item.aggregate:
            val = compute_aggregate(item.aggregate, [record])
            row.append(val)
        elif item.canon_ref:
            val = resolve_canon(item.canon_ref, record)
            row.append(val)
        elif item.scalar_subquery:
            val = evaluate_scalar_subquery_with_correlation(
                item.scalar_subquery.subquery, context, sources, outer_refs)
            row.append(val)
    return row


def evaluate_scalar_subquery_with_correlation(subquery: Subquery, outer_context: Dict,
                                              sources: Dict,
                                              outer_refs: Dict) -> Any:
    """Evaluate scalar subquery with full correlation support."""
    results = []

    # Get the FROM source for this subquery
    from_alias = "__outer"  # For correlated queries, use outer context

    # Get source from subquery's FROM
    # Since we don't store it in Query, we need to look at the select list refs

    # Actually, for correlation, the subquery uses UPTREE references
    # So we just need to run it with the outer refs
    for alias, records in sources.items():
        for record in records:
            combined_context = {**outer_context, **record}
            combined_outer_refs = dict(outer_refs)
            # Add all sources
            for src_alias, src_records in sources.items():
                combined_outer_refs[src_alias] = {"_records": src_records}
            # Add the current outer context
            combined_outer_refs["__outer"] = outer_context

            # Also add the outer record under its alias
            if "__outer" in combined_context:
                for k, v in combined_context.items():
                    if k.startswith("__outer."):
                        pass  # correlation handling

            if subquery.query.where_expr and not evaluate(
                subquery.query.where_expr, combined_context, sources, combined_outer_refs):
                continue

            row = project_to_row_with_outer(
                record, subquery.query.select_list, combined_context,
                sources, combined_outer_refs)
            results.append(row)
            if len(results) > 1:
                # Scalar should return at most one row
                break

    if not results:
        return None

    if len(results) > 0 and len(results[0]) > 0:
        return results[0][0]
    return None


def apply_group_by(results: List[List], group_by: List[FieldRef],
                   select_list: SelectList) -> List[List]:
    """Apply GROUP BY aggregation."""
    groups: Dict[Tuple, List] = {}
    for row in results:
        key = tuple(row[i] for i in range(len(group_by)))
        if key not in groups:
            groups[key] = []
        groups[key].append(row)

    final = []
    for key, group_rows in groups.items():
        # Build aggregated row
        row = list(key)
        # Add aggregate values
        for i, item in enumerate(select_list.items):
            if i < len(group_by):
                continue  # Already added as grouping column
            if item.is_aggregate:
                # Collect values for this column across group
                col_idx = len(group_by) + sum(1 for j in range(i) if j >= len(group_by) and select_list.items[j].is_aggregate)
                vals = [r[col_idx] if col_idx < len(r) else None for r in group_rows]
                val = compute_aggregate(item.aggregate, [{select_list.items[j].field_ref.key if j < len(select_list.items) and select_list.items[j].field_ref else "": r[col_idx] if col_idx < len(r) else None for j, r in enumerate(group_rows)}])
                row.append(val)
            else:
                row.append(group_rows[0][len(group_by) + sum(1 for j in range(i) if j >= len(group_by) and not select_list.items[j].is_aggregate)])
        final.append(row)
    return final


# =============================================================================
# Simplified evaluation for actual subquery execution
# =============================================================================

def evaluate_query_simplest(query: Query, sources: Dict) -> List[Dict]:
    """Simplified query evaluation returning list of result dicts."""
    # Get the main source (aliased via --source)
    main_source = None
    for alias, records in sources.items():
        main_source = records
        break

    if main_source is None:
        return []

    results = []
    for record in main_source:
        # Build context for evaluation (includes all sources)
        context = {alias: recs for alias, recs in sources.items()}

        # Evaluate WHERE
        if query.where_expr and not evaluate_expr_with_context(query.where_expr, record, context):
            continue

        # Project to output dict
        output = project_record_enhanced(record, query.select_list, context)
        results.append(output)

    return results


def evaluate_expr_with_context(expr: BooleanExpr, record: Dict,
                               context: Dict) -> bool:
    """Evaluate expression with full context for subqueries."""
    if isinstance(expr, BinaryExpr):
        left_val = evaluate_expr_with_context(expr.left, record, context)
        if expr.op_token.type == TokenType.AND:
            if not left_val:
                return False
            return evaluate_expr_with_context(expr.right, record, context)
        elif expr.op_token.type == TokenType.OR:
            if left_val:
                return True
            return evaluate_expr_with_context(expr.right, record, context)

    elif isinstance(expr, ParenExpr):
        return evaluate_expr_with_context(expr.inner, record, context)

    elif isinstance(expr, Predicate):
        field_val = get_nested(record, expr.field_ref.parts)
        return compare_values(field_val, expr.op_token, expr.literal)

    elif isinstance(expr, ExistsPredicate):
        # BEHOLDS - check if subquery returns rows
        subquery = expr.table_subexpr.subquery
        subquery_results = evaluate_subquery_with_context(subquery, record, context)
        return len(subquery_results) > 0

    elif isinstance(expr, InPredicate):
        value = get_nested(record, expr.value_expr.parts)
        subquery = expr.table_subexpr.subquery
        subquery_results = evaluate_subquery_with_context(subquery, record, context)

        for row in subquery_results:
            if len(row) > 0 and deep_equal(value, row[0]):
                return True
        return False

    elif isinstance(expr, AnyPredicate):
        value = get_nested(record, expr.value_expr.parts)
        subquery = expr.table_subexpr.subquery
        subquery_results = evaluate_subquery_with_context(subquery, record, context)

        if len(subquery_results) == 0:
            return False  # EITHERWISE over empty set is false

        for row in subquery_results:
            if len(row) > 0 and compare_values(value, expr.op_token, row[0]):
                return True
        return False

    elif isinstance(expr, AllPredicate):
        value = get_nested(record, expr.value_expr.parts)
        subquery = expr.table_subexpr.subquery
        subquery_results = evaluate_subquery_with_context(subquery, record, context)

        if len(subquery_results) == 0:
            return True  # EVERYWISE over empty set is true

        for row in subquery_results:
            if len(row) > 0 and not compare_values(value, expr.op_token, row[0]):
                return False
        return True

    return False


def evaluate_subquery_with_context(subquery: Subquery, outer_record: Dict,
                                   context: Dict) -> List[List]:
    """Evaluate subquery with correlation from outer context."""
    query = subquery.query

    # Get the source for this subquery
    # Extract from query's select list
    subquery_sources = {}
    for alias, records in context.items():
        subquery_sources[alias] = records

    # Determine which source this subquery is querying from
    from_alias = None
    for item in query.select_list.items:
        for fr in item.get_field_refs():
            if not fr.parts[0].startswith("CANON"):
                if fr.parts[0] in subquery_sources:
                    from_alias = fr.parts[0]
                    break
        if from_alias:
            break

    # Check WHERE for source alias
    if query.where_expr:
        # Extract aliases from WHERE
        where_aliases = _extract_aliases_from_expr(query.where_expr)
        for alias in where_aliases:
            if alias in subquery_sources:
                from_alias = alias
                break

    if from_alias is None:
        from_alias = list(subquery_sources.keys())[0] if subquery_sources else None

    if from_alias is None or from_alias not in subquery_sources:
        return []

    source_records = subquery_sources[from_alias]
    results = []

    for record in source_records:
        # Create evaluation context with outer record
        eval_context = {**record}
        # Make outer record available for correlation
        outer_refs = {}
        # Store outer record under its alias
        for alias in context.keys():
            outer_refs[alias] = {"_records": context[alias]}
        # Also store specific outer record reference for UPTREE
        outer_refs["__outer"] = outer_record
        for alias in context.keys():
            for r in context[alias]:
                # Check if this is the matching record
                # For correlation, we compare field by field
                pass

        # For correlation with UPTREE: the outer record's field values are
        # directly available and can be looked up
        # We need to merge outer record's values into context
        for key, val in outer_record.items():
            eval_context[key] = val

        # Build full outer refs context for UPTREE lookups
        full_outer_refs = {}
        for alias, records in context.items():
            full_outer_refs[alias] = {"_records": records}
        # Also add the outer record specifically
        full_outer_refs["__outer"] = outer_record

        if query.where_expr and not evaluate_expr_with_context(query.where_expr, eval_context, full_outer_refs):
            continue

        # Project to row
        row = project_record_to_row_enhanced(record, query.select_list,
                                             eval_context, full_outer_refs)
        results.append(row)

    return results


def _extract_aliases_from_expr(expr: BooleanExpr) -> Set[str]:
    """Extract source aliases from an expression."""
    aliases = set()
    if isinstance(expr, BinaryExpr):
        aliases.update(_extract_aliases_from_expr(expr.left))
        aliases.update(_extract_aliases_from_expr(expr.right))
    elif isinstance(expr, ParenExpr):
        aliases.update(_extract_aliases_from_expr(expr.inner))
    elif isinstance(expr, Predicate):
        if expr.field_ref.parts and expr.field_ref.parts[0] != "CANON":
            aliases.add(expr.field_ref.parts[0])
    elif isinstance(expr, (ExistsPredicate, InPredicate, AnyPredicate, AllPredicate)):
        # Table subqueries don't contribute FROM aliases directly
        pass
    return aliases


def project_record_to_row_enhanced(record: Dict, select_list: SelectList,
                                   context: Dict, outer_refs: Dict) -> List:
    """Project record to row with UPTREE correlation."""
    row = []
    for item in select_list.items:
        if item.field_ref:
            # Check for UPTREE reference
            if len(item.field_ref.parts) >= 2 and item.field_ref.parts[0].lower() == "uptree":
                # UPTREE.<alias>.<field...>
                alias = item.field_ref.parts[1]
                field_path = item.field_ref.parts[2:]

                # Look up in outer refs
                if alias in outer_refs:
                    outer_record = outer_refs[alias]
                    if isinstance(outer_record, dict) and "_record" in outer_record:
                        # Specific record
                        val = get_nested(outer_record["_record"], field_path)
                    elif isinstance(outer_record, dict) and "_records" in outer_record:
                        # Full source - look for correlation in context
                        # Check if there's a correlated value
                        key = ".".join(field_path)
                        if key in context:
                            val = context[key]
                        else:
                            # Not correlated, use main record
                            val = get_nested(record, item.field_ref.parts[2:])
                    else:
                        val = outer_record.get(".".join(field_path))
                else:
                    # Not in outer refs, try context
                    val = context.get(".".join(item.field_ref.parts[2:]))

                if val is None:
                    # Fallback: try to find in any source record
                    for alias_key, src_data in outer_refs.items():
                        if isinstance(src_data, dict) and "_record" in src_data:
                            val = get_nested(src_data["_record"], field_path)
                            if val is not None:
                                break
            else:
                val = get_nested(record, item.field_ref.parts)

            row.append(val)
        elif item.aggregate:
            val = compute_aggregate_simple(item.aggregate, [record])
            row.append(val)
        elif item.canon_ref:
            val = None  # Placeholder for canonical resolution
            row.append(val)
        elif item.scalar_subquery:
            val = evaluate_scalar_subquery_with_context(
                item.scalar_subquery.subquery, record, outer_refs)
            row.append(val)
    return row


def compute_aggregate_simple(agg: AggregateCall, records: List[Dict]) -> Any:
    """Compute aggregate over list of records."""
    if agg.func == TokenType.COUNT:
        if agg.arg is None:
            return len(records)
        return sum(1 for r in records if get_nested(r, agg.arg.parts) is not None)
    elif agg.func == TokenType.SUM:
        total = 0
        for r in records:
            val = get_nested(r, agg.arg.parts)
            if val is not None:
                total += val
        return total
    elif agg.func == TokenType.AVG:
        vals = [get_nested(r, agg.arg.parts) for r in records
                if get_nested(r, agg.arg.parts) is not None]
        if not vals:
            return None
        return sum(vals) / len(vals)
    elif agg.func == TokenType.MIN:
        vals = [get_nested(r, agg.arg.parts) for r in records
                if get_nested(r, agg.arg.parts) is not None]
        return min(vals) if vals else None
    elif agg.func == TokenType.MAX:
        vals = [get_nested(r, agg.arg.parts) for r in records
                if get_nested(r, agg.arg.parts) is not None]
        return max(vals) if vals else None
    elif agg.func == TokenType.UNIQUE:
        vals = []
        for r in records:
            val = get_nested(r, agg.arg.parts)
            if val is not None and val not in vals:
                vals.append(val)
        return vals
    return None


def evaluate_scalar_subquery_with_context(subquery: Subquery, outer_record: Dict,
                                          outer_refs: Dict) -> Any:
    """Evaluate scalar subquery with correlation."""
    query = subquery.query

    sources = {}
    for alias, data in outer_refs.items():
        if isinstance(data, dict) and "_records" in data:
            sources[alias] = data["_records"]
        else:
            sources[alias] = [data] if isinstance(data, dict) else data

    # Run subquery with outer context available
    subquery_results = evaluate_subquery_with_context_simple(subquery, outer_record, sources)

    if not subquery_results:
        return None

    if len(subquery_results) > 0 and len(subquery_results[0]) > 0:
        return subquery_results[0][0]
    return None


def evaluate_subquery_with_context_simple(subquery: Subquery, outer_record: Dict,
                                          sources: Dict) -> List[List]:
    """Evaluate subquery with correlation support."""
    query = subquery.query

    # Determine source
    from_alias = None
    for item in query.select_list.items:
        for fr in item.get_field_refs():
            if not fr.parts[0].startswith("CANON"):
                if fr.parts[0] in sources:
                    from_alias = fr.parts[0]
                    break
        if from_alias:
            break

    if query.where_expr:
        where_aliases = _extract_aliases_from_expr(query.where_expr)
        for alias in where_aliases:
            if alias in sources:
                from_alias = alias
                break

    if from_alias is None:
        from_alias = list(sources.keys())[0] if sources else None

    if from_alias is None:
        return []

    source_records = sources.get(from_alias, [])
    results = []

    for record in source_records:
        # Merge context
        eval_context = {**outer_record, **record}

        # Build outer refs
        full_outer_refs = {}
        for alias, recs in sources.items():
            full_outer_refs[alias] = {"_records": recs}
        full_outer_refs["__outer"] = outer_record

        # Evaluate WHERE
        if query.where_expr and not evaluate_expr_with_context(query.where_expr, eval_context, full_outer_refs):
            continue

        # Project to row
        row = project_record_to_row_enhanced(record, query.select_list,
                                              eval_context, full_outer_refs)
        results.append(row)

    return results


def project_record_enhanced(record: Dict, select_list: SelectList,
                            context: Dict) -> Dict:
    """Project record to dict with subquery support."""
    result = {}
    for item in select_list.items:
        key = item.output_key
        if item.field_ref:
            val = get_nested(record, item.field_ref.parts)
            result[key] = val
        elif item.aggregate:
            val = compute_aggregate_simple(item.aggregate, [record])
            result[key] = val
        elif item.canon_ref:
            result[key] = None
        elif item.scalar_subquery:
            # Evaluate subquery with correlation
            val = evaluate_scalar_subquery_simple_context(
                item.scalar_subquery.subquery, record, context)
            result[key] = val
    return result


def evaluate_scalar_subquery_simple_context(subquery: Subquery, outer_record: Dict,
                                            context: Dict) -> Any:
    """Evaluate scalar subquery with simple context correlation."""
    # Build sources from context
    sources = {}
    for key, val in context.items():
        if isinstance(val, list) and len(val) > 0 and isinstance(val[0], dict):
            sources[key] = val

    results = evaluate_subquery_with_context_simple(subquery, outer_record, sources)

    if not results:
        return None

    if len(results) > 0 and len(results[0]) > 0:
        return results[0][0]
    return None


# =============================================================================
# Main
# =============================================================================

def parse_args():
    parser = argparse.ArgumentParser(
        description="LogQL - Query NDJSON logs with SQL-like syntax",
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--query", required=True, help="SQL-like query string")
    parser.add_argument("--output", help="Write output to file instead of stdout")
    parser.add_argument("--log-file", help="Path to NDJSON file")
    parser.add_argument("--source", action="append", help="Bind alias to NDJSON file (--source <alias>=<path>)")
    return parser.parse_args()


def main():
    args = parse_args()

    try:
        # Build sources
        sources = {}
        if args.log_file:
            sources['logs'] = args.log_file
        if args.source:
            for src in args.source:
                if '=' not in src:
                    raise LogQLError(f"Invalid --source format: '{src}'. Expected <alias>=<path>")
                alias, path = src.split('=', 1)
                alias = alias.strip()
                path = path.strip()
                if not alias:
                    raise LogQLError("Alias cannot be empty")
                if not path:
                    raise LogQLError("Path cannot be empty")
                if alias in sources:
                    raise LogQLError(f"Duplicate alias: '{alias}'")
                sources[alias] = path

        if not sources:
            raise LogQLError("At least one source required (--source or --log-file)")

        # Tokenize
        tokenizer = Tokenizer(args.query)
        tokens = tokenizer.tokenize()

        # Parse
        parser = Parser(tokens)
        query = parser.parse_query()

        # Read source files into memory
        loaded_sources = {}
        for alias, path in sources.items():
            records = []
            with open(path, 'r', encoding='utf-8') as f:
                for line_num, line in enumerate(f, 1):
                    line = line.rstrip('\n\r')
                    if not line or line.isspace():
                        continue
                    try:
                        record = json.loads(line)
                        records.append(record)
                    except json.JSONDecodeError as e:
                        raise LogQLError(f"Invalid JSON at {path}:{line_num}: {e}", line_num)
            loaded_sources[alias] = records

        # Execute query
        results = evaluate_query_simplest(query, loaded_sources)

        # Output
        output_json = json.dumps(results, ensure_ascii=False)

        if args.output:
            with open(args.output, 'w', encoding='utf-8') as f:
                f.write(output_json)
        else:
            print(output_json)

    except LogQLError as e:
        error_obj = {"error": f"LOGQL_ERROR: {e.message}"}
        if e.position is not None:
            error_obj["position"] = e.position
        json.dump(error_obj, sys.stderr, ensure_ascii=False)
        sys.stderr.write("\n")
        sys.exit(1)
    except Exception as e:
        error_obj = {"error": f"LOGQL_ERROR: {str(e)}"}
        json.dump(error_obj, sys.stderr, ensure_ascii=False)
        sys.stderr.write("\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
