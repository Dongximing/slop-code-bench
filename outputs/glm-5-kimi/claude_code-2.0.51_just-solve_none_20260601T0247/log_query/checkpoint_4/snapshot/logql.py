#!/usr/bin/env python3
"""
LogQL Part 3 - A command-line program that reads NDJSON logs from multiple sources,
parses a SQL-like query with GROUP BY, aggregations, and multi-source conflation (joins),
filters rows with boolean logic, and returns results as JSON.
"""

import argparse
import json
import sys
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Union


# Token Types

class TokenType:
    # Keywords
    SELECT = 'SELECT'
    FROM = 'FROM'
    WHERE = 'WHERE'
    AND = 'AND'
    OR = 'OR'
    TRUE = 'TRUE'
    FALSE = 'FALSE'
    NULL = 'NULL'
    AS = 'AS'
    GROUP = 'GROUP'
    BY = 'BY'
    COUNT = 'COUNT'
    SUM = 'SUM'
    AVG = 'AVG'
    AVERAGE = 'AVERAGE'
    MIN = 'MIN'
    MAX = 'MAX'
    UNIQUE = 'UNIQUE'
    CONFLATE = 'CONFLATE'
    INTERSECTING = 'INTERSECTING'
    PRESERVING = 'PRESERVING'
    LEFT = 'LEFT'
    RIGHT = 'RIGHT'
    BOTH = 'BOTH'
    UPON = 'UPON'
    GLOSS = 'GLOSS'
    CANON = 'CANON'
    DEFAULT = 'DEFAULT'
    STRICT = 'STRICT'

    # Literals and identifiers
    NUMBER = 'NUMBER'
    STRING = 'STRING'
    IDENT = 'IDENT'

    # Operators
    EQ = '='
    NE = '!='
    LT = '<'
    LE = '<='
    GT = '>'
    GE = '>='

    # Punctuation
    COMMA = ','
    DOT = '.'
    STAR = '*'
    LPAREN = '('
    RPAREN = ')'
    LBRACE = '{'
    RBRACE = '}'
    PIPE = '|'
    COLON = ':'

    EOF = 'EOF'


@dataclass
class Token:
    type: str
    value: Any
    pos: int  # Position in the query string


# Lexer

class LexerError(Exception):
    pass


class Lexer:
    KEYWORDS = {
        'select': TokenType.SELECT,
        'from': TokenType.FROM,
        'where': TokenType.WHERE,
        'and': TokenType.AND,
        'or': TokenType.OR,
        'true': TokenType.TRUE,
        'false': TokenType.FALSE,
        'null': TokenType.NULL,
        'as': TokenType.AS,
        'group': TokenType.GROUP,
        'by': TokenType.BY,
        'count': TokenType.COUNT,
        'sum': TokenType.SUM,
        'avg': TokenType.AVG,
        'average': TokenType.AVERAGE,
        'min': TokenType.MIN,
        'max': TokenType.MAX,
        'unique': TokenType.UNIQUE,
        'conflate': TokenType.CONFLATE,
        'intersecting': TokenType.INTERSECTING,
        'preserving': TokenType.PRESERVING,
        'left': TokenType.LEFT,
        'right': TokenType.RIGHT,
        'both': TokenType.BOTH,
        'upon': TokenType.UPON,
        'gloss': TokenType.GLOSS,
        'canon': TokenType.CANON,
        'default': TokenType.DEFAULT,
        'strict': TokenType.STRICT,
    }

    def __init__(self, query: str):
        self.query = query
        self.pos = 0
        self.length = len(query)

    def peek(self, offset: int = 0) -> Optional[str]:
        pos = self.pos + offset
        if pos < self.length:
            return self.query[pos]
        return None

    def advance(self) -> Optional[str]:
        if self.pos < self.length:
            ch = self.query[self.pos]
            self.pos += 1
            return ch
        return None

    def skip_whitespace(self):
        while self.pos < self.length and self.query[self.pos].isspace():
            self.pos += 1

    def read_number(self) -> Token:
        start = self.pos
        has_dot = False

        # Optional negative sign
        if self.peek() == '-':
            self.advance()

        # Integer part
        if self.peek() == '0':
            self.advance()
        elif self.peek() and self.peek().isdigit():
            while self.peek() and self.peek().isdigit():
                self.advance()
        else:
            raise LexerError(f"Invalid number at position {start}")

        # Decimal part
        if self.peek() == '.':
            has_dot = True
            self.advance()  # consume '.'
            if not (self.peek() and self.peek().isdigit()):
                raise LexerError(f"Invalid number at position {start}")
            while self.peek() and self.peek().isdigit():
                self.advance()

        value_str = self.query[start:self.pos]
        if has_dot:
            value = float(value_str)
        else:
            value = int(value_str)

        return Token(TokenType.NUMBER, value, start)

    def read_string(self) -> Token:
        start = self.pos
        self.advance()  # consume opening quote

        result = []
        while True:
            ch = self.peek()
            if ch is None:
                raise LexerError(f"Unterminated string starting at position {start}")
            if ch == '"':
                self.advance()  # consume closing quote
                break
            if ch == '\\':
                self.advance()
                next_ch = self.peek()
                if next_ch == '"':
                    result.append('"')
                    self.advance()
                elif next_ch == '\\':
                    result.append('\\')
                    self.advance()
                else:
                    raise LexerError(f"Invalid escape sequence at position {self.pos}")
            else:
                result.append(ch)
                self.advance()

        return Token(TokenType.STRING, ''.join(result), start)

    def read_identifier(self) -> Token:
        start = self.pos

        # First character must be letter or underscore
        ch = self.peek()
        if not (ch and (ch.isalpha() or ch == '_')):
            raise LexerError(f"Invalid identifier at position {start}")

        while True:
            ch = self.peek()
            if ch and (ch.isalnum() or ch == '_'):
                self.advance()
            else:
                break

        value = self.query[start:self.pos]

        # Check if it's a keyword (case-insensitive)
        lower_value = value.lower()
        if lower_value in self.KEYWORDS:
            return Token(self.KEYWORDS[lower_value], value, start)

        return Token(TokenType.IDENT, value, start)

    def next_token(self) -> Token:
        self.skip_whitespace()

        if self.pos >= self.length:
            return Token(TokenType.EOF, None, self.pos)

        start = self.pos
        ch = self.peek()

        # Number (including negative)
        if ch.isdigit() or (ch == '-' and self.peek(1) and self.peek(1).isdigit()):
            return self.read_number()

        # String
        if ch == '"':
            return self.read_string()

        # Identifier or keyword
        if ch.isalpha() or ch == '_':
            return self.read_identifier()

        # Operators and punctuation
        if ch == '=':
            self.advance()
            return Token(TokenType.EQ, '=', start)

        if ch == '!' and self.peek(1) == '=':
            self.advance()
            self.advance()
            return Token(TokenType.NE, '!=', start)

        if ch == '<':
            self.advance()
            if self.peek() == '=':
                self.advance()
                return Token(TokenType.LE, '<=', start)
            return Token(TokenType.LT, '<', start)

        if ch == '>':
            self.advance()
            if self.peek() == '=':
                self.advance()
                return Token(TokenType.GE, '>=', start)
            return Token(TokenType.GT, '>', start)

        if ch == ',':
            self.advance()
            return Token(TokenType.COMMA, ',', start)

        if ch == '.':
            self.advance()
            return Token(TokenType.DOT, '.', start)

        if ch == '*':
            self.advance()
            return Token(TokenType.STAR, '*', start)

        if ch == '(':
            self.advance()
            return Token(TokenType.LPAREN, '(', start)

        if ch == ')':
            self.advance()
            return Token(TokenType.RPAREN, ')', start)

        if ch == '{':
            self.advance()
            return Token(TokenType.LBRACE, '{', start)

        if ch == '}':
            self.advance()
            return Token(TokenType.RBRACE, '}', start)

        if ch == '|':
            self.advance()
            return Token(TokenType.PIPE, '|', start)

        if ch == ':':
            self.advance()
            return Token(TokenType.COLON, ':', start)

        raise LexerError(f"Unexpected character '{ch}' at position {self.pos}")

    def tokenize(self) -> List[Token]:
        tokens = []
        while True:
            token = self.next_token()
            tokens.append(token)
            if token.type == TokenType.EOF:
                break
        return tokens


# AST Nodes

@dataclass
class FieldRef:
    parts: List[str]

    def to_string(self) -> str:
        return '.'.join(self.parts)

    def alias(self) -> str:
        """Get the alias (first part) of a qualified field reference."""
        return self.parts[0]

    def field_path(self) -> List[str]:
        """Get the field path after the alias."""
        return self.parts[1:]


@dataclass
class CanonRef:
    """Reference to a canonical label (CANON.name)."""
    name: str

    def to_string(self) -> str:
        return f"CANON.{self.name}"


@dataclass
class Literal:
    value: Any


@dataclass
class Predicate:
    field: FieldRef
    op: str
    literal: Literal


@dataclass
class BinaryExpr:
    left: Union['BooleanExpr', Predicate]
    op: str  # 'AND' or 'OR'
    right: Union['BooleanExpr', Predicate]


BooleanExpr = Union[Predicate, BinaryExpr]


@dataclass
class JoinConjunct:
    left_field: Union[FieldRef, CanonRef]
    right_field: Union[FieldRef, CanonRef]


@dataclass
class JoinPredicate:
    conjuncts: List[JoinConjunct]


@dataclass
class CanonDecl:
    """A canonical label declaration."""
    name: str
    candidates: List[FieldRef]  # Ordered list of candidate field paths
    default_value: Optional[Literal] = None  # DEFAULT value if all candidates are null


@dataclass
class GlossClause:
    """GLOSS clause containing canonical label declarations."""
    strict: bool  # True if GLOSS STRICT
    declarations: List[CanonDecl]


@dataclass
class ConflateClause:
    alias: str
    flavor: str  # 'INTERSECTING', 'PRESERVING LEFT', 'PRESERVING RIGHT', 'PRESERVING BOTH'
    predicate: JoinPredicate


@dataclass
class AggCall:
    func: str  # 'COUNT', 'SUM', 'AVG', 'MIN', 'MAX', 'UNIQUE'
    field: Optional[Union[FieldRef, CanonRef]]  # None for COUNT(*)
    is_star: bool = False  # True for COUNT(*)

    def canonical_string(self) -> str:
        if self.is_star:
            return f"{self.func}(*)"
        return f"{self.func}({self.field.to_string()})"


@dataclass
class SelectItem:
    field: Optional[Union[FieldRef, CanonRef]]  # None if this is an aggregate or star expansion
    agg: Optional[AggCall]  # None if this is a field reference
    alias_star: Optional[str] = None  # For alias.* expansion
    alias: Optional[str] = None  # AS alias

    def output_key(self) -> str:
        if self.alias:
            return self.alias
        if self.field:
            return self.field.to_string()
        if self.agg:
            return self.agg.canonical_string()
        return ""


@dataclass
class SelectClause:
    star: bool
    items: List[SelectItem]  # For non-star selects


@dataclass
class Query:
    select: SelectClause
    from_alias: str
    where: Optional[BooleanExpr]
    gloss: Optional[GlossClause]
    conflate_clauses: List[ConflateClause]
    group_by: Optional[List[Union[FieldRef, CanonRef]]]


# Parser

class ParseError(Exception):
    pass


class SemanticError(Exception):
    pass


AGGREGATE_FUNCS = {
    TokenType.COUNT: 'COUNT',
    TokenType.SUM: 'SUM',
    TokenType.AVG: 'AVG',
    TokenType.AVERAGE: 'AVG',  # AVERAGE is synonym for AVG
    TokenType.MIN: 'MIN',
    TokenType.MAX: 'MAX',
    TokenType.UNIQUE: 'UNIQUE',
}


class Parser:
    def __init__(self, tokens: List[Token]):
        self.tokens = tokens
        self.pos = 0

    def current(self) -> Token:
        return self.tokens[self.pos]

    def peek(self, offset: int = 0) -> Token:
        pos = self.pos + offset
        if pos < len(self.tokens):
            return self.tokens[pos]
        return self.tokens[-1]  # EOF

    def advance(self) -> Token:
        token = self.current()
        if self.pos < len(self.tokens) - 1:
            self.pos += 1
        return token

    def expect(self, token_type: str) -> Token:
        token = self.current()
        if token.type != token_type:
            raise ParseError(f"Expected {token_type}, got {token.type} at position {token.pos}")
        return self.advance()

    def match(self, *token_types: str) -> bool:
        return self.current().type in token_types

    def parse(self) -> Query:
        # SELECT
        self.expect(TokenType.SELECT)
        select = self.parse_select_list()

        # FROM <alias>
        self.expect(TokenType.FROM)
        from_ident = self.expect(TokenType.IDENT)
        from_alias = from_ident.value

        # Optional WHERE
        where = None
        if self.match(TokenType.WHERE):
            self.advance()
            where = self.parse_boolean_expr()

        # Optional GLOSS clause
        gloss = None
        if self.match(TokenType.GLOSS):
            self.advance()
            gloss = self.parse_gloss_clause()

        # Optional CONFLATE clauses
        conflate_clauses = []
        while self.match(TokenType.CONFLATE):
            self.advance()
            conflate = self.parse_conflate_clause()
            conflate_clauses.append(conflate)

        # Optional GROUP BY
        group_by = None
        if self.match(TokenType.GROUP):
            self.advance()
            self.expect(TokenType.BY)
            group_by = self.parse_group_list()

        self.expect(TokenType.EOF)

        # Semantic validation
        self.validate_query(select, from_alias, gloss, conflate_clauses, group_by)

        return Query(select, from_alias, where, gloss, conflate_clauses, group_by)

    def validate_query(self, select: SelectClause, from_alias: str,
                       gloss: Optional[GlossClause],
                       conflate_clauses: List[ConflateClause],
                       group_by: Optional[List[Union[FieldRef, CanonRef]]]):
        if len(conflate_clauses) > 0 and select.star:
            raise SemanticError("E_SEMANTIC: SELECT * is forbidden when CONFLATE is present")

        # Build canon name set from GLOSS
        canon_names = set()
        if gloss:
            for decl in gloss.declarations:
                if decl.name in canon_names:
                    raise SemanticError(f"E_SEMANTIC: redeclared CANON label '{decl.name}'")
                canon_names.add(decl.name)

        # Validate output keys are unique
        output_keys = []
        for item in select.items:
            if item.alias_star:
                # We'll expand later; for now skip
                continue
            key = item.output_key()
            if key in output_keys:
                raise SemanticError(f"E_SEMANTIC: duplicate output key '{key}'")
            output_keys.append(key)

        # Validate GROUP BY has no duplicates
        if group_by:
            group_keys = []
            for field in group_by:
                key = field.to_string()
                if key in group_keys:
                    raise SemanticError(f"E_SEMANTIC: duplicate field '{key}' in GROUP BY")
                group_keys.append(key)

        # Check CANON references are declared
        for item in select.items:
            if isinstance(item.field, CanonRef):
                if item.field.name not in canon_names:
                    raise SemanticError(f"E_SEMANTIC: undeclared CANON.{item.field.name}")
            if item.agg and isinstance(item.agg.field, CanonRef):
                if item.agg.field.name not in canon_names:
                    raise SemanticError(f"E_SEMANTIC: undeclared CANON.{item.agg.field.name}")

        if group_by:
            for field in group_by:
                if isinstance(field, CanonRef):
                    if field.name not in canon_names:
                        raise SemanticError(f"E_SEMANTIC: undeclared CANON.{field.name}")

        # If has aggregates, non-aggregate fields must be in GROUP BY
        has_aggregates = any(item.agg is not None for item in select.items)
        non_agg_fields = [item.field for item in select.items if item.field is not None and item.agg is None]

        if has_aggregates:
            if group_by is None:
                for field in non_agg_fields:
                    raise SemanticError(f"Non-aggregate field '{field.to_string()}' must be in GROUP BY")
            else:
                group_by_strs = [f.to_string() for f in group_by]
                for field in non_agg_fields:
                    if field.to_string() not in group_by_strs:
                        raise SemanticError(f"Non-aggregate field '{field.to_string()}' must be in GROUP BY")

        # Validate that all aliases in select/group_by exist
        all_aliases = {from_alias}
        for cc in conflate_clauses:
            all_aliases.add(cc.alias)

        has_conflate = len(conflate_clauses) > 0

        # Check qualified field refs (excluding CANON refs)
        for item in select.items:
            if item.field and isinstance(item.field, FieldRef):
                if has_conflate:
                    if len(item.field.parts) < 2:
                        raise SemanticError(f"E_SEMANTIC: field reference must be qualified when CONFLATE is present")
                    if item.field.alias() not in all_aliases:
                        raise SemanticError(f"E_SEMANTIC: unknown alias '{item.field.alias()}'")
            if item.agg and item.agg.field and isinstance(item.agg.field, FieldRef):
                if has_conflate:
                    if len(item.agg.field.parts) < 2:
                        raise SemanticError(f"E_SEMANTIC: aggregate argument must be qualified when CONFLATE is present")
                    if item.agg.field.alias() not in all_aliases:
                        raise SemanticError(f"E_SEMANTIC: unknown alias '{item.agg.field.alias()}'")

        if group_by:
            for field in group_by:
                if isinstance(field, FieldRef):
                    if has_conflate:
                        if len(field.parts) < 2:
                            raise SemanticError(f"E_SEMANTIC: field reference must be qualified when CONFLATE is present")
                        if field.alias() not in all_aliases:
                            raise SemanticError(f"E_SEMANTIC: unknown alias '{field.alias()}'")

        # Validate GLOSS candidate references
        if gloss:
            for decl in gloss.declarations:
                for candidate in decl.candidates:
                    if candidate.alias() not in all_aliases:
                        raise SemanticError(f"E_SEMANTIC: unknown alias '{candidate.alias()}' in GLOSS")

    def parse_select_list(self) -> SelectClause:
        if self.match(TokenType.STAR):
            self.advance()
            return SelectClause(star=True, items=[])

        items = []
        item = self.parse_select_item()
        items.append(item)

        while self.match(TokenType.COMMA):
            self.advance()
            item = self.parse_select_item()
            items.append(item)

        return SelectClause(star=False, items=items)

    def parse_select_item(self) -> SelectItem:
        # Check for aggregate
        if self.match(*AGGREGATE_FUNCS.keys()):
            agg = self.parse_agg_call()
            alias = None
            if self.match(TokenType.AS):
                self.advance()
                alias_token = self.expect(TokenType.IDENT)
                alias = alias_token.value
            return SelectItem(field=None, agg=agg, alias=alias)

        # Check for alias.* pattern: IDENT DOT STAR
        if self.match(TokenType.IDENT) and self.peek(1).type == TokenType.DOT and self.peek(2).type == TokenType.STAR:
            alias_name = self.advance().value  # IDENT
            self.advance()  # DOT
            self.advance()  # STAR
            return SelectItem(field=None, agg=None, alias_star=alias_name)

        # Check for CANON.* - should be an error
        if self.match(TokenType.CANON) and self.peek(1).type == TokenType.DOT and self.peek(2).type == TokenType.STAR:
            raise ParseError("E_PARSE: CANON.* wildcard not allowed")

        # Regular field or CANON reference
        field = self.parse_field_or_canon_ref()
        alias = None
        if self.match(TokenType.AS):
            self.advance()
            alias_token = self.expect(TokenType.IDENT)
            alias = alias_token.value
        return SelectItem(field=field, agg=None, alias=alias)

    def parse_gloss_clause(self) -> GlossClause:
        """Parse GLOSS [STRICT] { declarations }."""
        strict = False
        if self.match(TokenType.STRICT):
            self.advance()
            strict = True

        self.expect(TokenType.LBRACE)

        declarations = []
        decl = self.parse_canon_decl()
        declarations.append(decl)

        while self.match(TokenType.COMMA):
            self.advance()
            decl = self.parse_canon_decl()
            declarations.append(decl)

        self.expect(TokenType.RBRACE)

        return GlossClause(strict=strict, declarations=declarations)

    def parse_canon_decl(self) -> CanonDecl:
        """Parse a canonical label declaration: name := field1 | field2 [DEFAULT value]."""
        name_token = self.expect(TokenType.IDENT)
        name = name_token.value

        # Parse :=  (COLON followed by EQ)
        self.expect(TokenType.COLON)
        self.expect(TokenType.EQ)

        # Parse first candidate
        candidates = []
        field = self.parse_field_ref()
        candidates.append(field)

        # Parse additional candidates separated by |
        while self.match(TokenType.PIPE):
            self.advance()
            field = self.parse_field_ref()
            candidates.append(field)

        # Check for DEFAULT
        default_value = None
        if self.match(TokenType.DEFAULT):
            self.advance()
            default_value = self.parse_literal()

        return CanonDecl(name=name, candidates=candidates, default_value=default_value)

    def parse_conflate_clause(self) -> ConflateClause:
        # Parse join flavor
        if self.match(TokenType.INTERSECTING):
            flavor = 'INTERSECTING'
            self.advance()
        elif self.match(TokenType.PRESERVING):
            self.advance()
            if self.match(TokenType.LEFT):
                flavor = 'PRESERVING LEFT'
                self.advance()
            elif self.match(TokenType.RIGHT):
                flavor = 'PRESERVING RIGHT'
                self.advance()
            elif self.match(TokenType.BOTH):
                flavor = 'PRESERVING BOTH'
                self.advance()
            else:
                raise ParseError(f"Expected LEFT, RIGHT, or BOTH after PRESERVING")
        else:
            # Default to INTERSECTING
            flavor = 'INTERSECTING'

        # Parse alias
        alias_token = self.expect(TokenType.IDENT)
        alias = alias_token.value

        # UPON
        self.expect(TokenType.UPON)

        # Parse join predicate
        predicate = self.parse_join_predicate()

        # Validate that conjuncts reference different aliases (only for FieldRef, not CanonRef)
        for conj in predicate.conjuncts:
            if isinstance(conj.left_field, FieldRef) and isinstance(conj.right_field, FieldRef):
                if conj.left_field.alias() == conj.right_field.alias():
                    raise SemanticError("E_SEMANTIC: join conjunct must reference two different aliases")

        return ConflateClause(alias, flavor, predicate)

    def parse_join_predicate(self) -> JoinPredicate:
        conjuncts = []
        conj = self.parse_join_conjunct()
        conjuncts.append(conj)

        while self.match(TokenType.AND):
            self.advance()
            conj = self.parse_join_conjunct()
            conjuncts.append(conj)

        return JoinPredicate(conjuncts)

    def parse_join_conjunct(self) -> JoinConjunct:
        left = self.parse_field_or_canon_ref()
        self.expect(TokenType.EQ)
        right = self.parse_field_or_canon_ref()
        return JoinConjunct(left, right)

    def parse_agg_call(self) -> AggCall:
        func_token = self.advance()
        func_name = AGGREGATE_FUNCS[func_token.type]

        self.expect(TokenType.LPAREN)

        if self.match(TokenType.STAR):
            self.advance()
            self.expect(TokenType.RPAREN)
            if func_name != 'COUNT':
                raise ParseError(f"{func_name}(*) is not valid, only COUNT(*) supports *")
            return AggCall(func='COUNT', field=None, is_star=True)
        else:
            field = self.parse_field_or_canon_ref()
            self.expect(TokenType.RPAREN)
            return AggCall(func=func_name, field=field, is_star=False)

    def parse_group_list(self) -> List[Union[FieldRef, CanonRef]]:
        fields = []
        field = self.parse_field_or_canon_ref()
        fields.append(field)

        while self.match(TokenType.COMMA):
            self.advance()
            field = self.parse_field_or_canon_ref()
            field_str = field.to_string()
            for existing in fields:
                if existing.to_string() == field_str:
                    raise SemanticError(f"E_SEMANTIC: duplicate field '{field_str}' in GROUP BY")
            fields.append(field)

        return fields

    def parse_field_ref(self) -> FieldRef:
        parts = []

        ident = self.expect(TokenType.IDENT)
        parts.append(ident.value)

        while self.match(TokenType.DOT):
            self.advance()
            # Check if next is STAR - if so, stop (handled by caller)
            if self.match(TokenType.STAR):
                break
            ident = self.expect(TokenType.IDENT)
            parts.append(ident.value)

        return FieldRef(parts)

    def parse_field_or_canon_ref(self) -> Union[FieldRef, CanonRef]:
        """Parse either a field reference or a CANON reference."""
        if self.match(TokenType.CANON):
            self.advance()
            self.expect(TokenType.DOT)
            name_token = self.expect(TokenType.IDENT)
            return CanonRef(name=name_token.value)
        else:
            return self.parse_field_ref()

    def parse_boolean_expr(self) -> BooleanExpr:
        return self.parse_or_expr()

    def parse_or_expr(self) -> BooleanExpr:
        left = self.parse_and_expr()

        while self.match(TokenType.OR):
            self.advance()
            right = self.parse_and_expr()
            left = BinaryExpr(left, 'OR', right)

        return left

    def parse_and_expr(self) -> BooleanExpr:
        left = self.parse_primary_expr()

        while self.match(TokenType.AND):
            self.advance()
            right = self.parse_primary_expr()
            left = BinaryExpr(left, 'AND', right)

        return left

    def parse_primary_expr(self) -> BooleanExpr:
        if self.match(TokenType.LPAREN):
            self.advance()
            expr = self.parse_boolean_expr()
            self.expect(TokenType.RPAREN)
            return expr

        return self.parse_predicate()

    def parse_predicate(self) -> Predicate:
        field = self.parse_field_ref()

        if self.match(TokenType.EQ, TokenType.NE, TokenType.LT,
                      TokenType.LE, TokenType.GT, TokenType.GE):
            op_token = self.advance()
            op = op_token.value
        else:
            raise ParseError(f"Expected comparison operator at position {self.current().pos}")

        literal = self.parse_literal()

        return Predicate(field, op, literal)

    def parse_literal(self) -> Literal:
        token = self.current()

        if token.type == TokenType.NUMBER:
            self.advance()
            return Literal(token.value)

        if token.type == TokenType.STRING:
            self.advance()
            return Literal(token.value)

        if token.type == TokenType.TRUE:
            self.advance()
            return Literal(True)

        if token.type == TokenType.FALSE:
            self.advance()
            return Literal(False)

        if token.type == TokenType.NULL:
            self.advance()
            return Literal(None)

        raise ParseError(f"Expected literal value at position {token.pos}")


# Evaluator

class EvaluationError(Exception):
    pass


def get_field_value(record: dict, field: FieldRef) -> Any:
    """Get the value of a dotted field reference from a record."""
    value = record
    for part in field.parts:
        if value is None:
            return None
        if isinstance(value, dict):
            value = value.get(part)
        else:
            return None
    return value


def get_qualified_field_value(records: Dict[str, dict], field: FieldRef) -> Any:
    """Get the value of a qualified field reference from aliased records."""
    alias = field.alias()
    if alias not in records:
        return None
    record = records[alias]
    if record is None:
        return None
    # Get the field path after the alias
    field_path = field.field_path()
    value = record
    for part in field_path:
        if value is None:
            return None
        if isinstance(value, dict):
            value = value.get(part)
        else:
            return None
    return value


def resolve_canon_ref(records: Dict[str, dict], canon_ref: CanonRef, gloss: GlossClause) -> Any:
    """Resolve a CANON reference to a value based on GLOSS declarations."""
    # Find the declaration for this canonical name
    decl = None
    for d in gloss.declarations:
        if d.name == canon_ref.name:
            decl = d
            break

    if decl is None:
        raise EvaluationError(f"LOGQL_ERROR: undeclared CANON.{canon_ref.name}")

    # Collect all non-null candidate values
    non_null_values = []
    for candidate in decl.candidates:
        value = get_qualified_field_value(records, candidate)
        if value is not None:
            non_null_values.append(value)

    # STRICT mode: check for conflicts
    if gloss.strict and len(non_null_values) > 1:
        # Check if all values are equal
        first = non_null_values[0]
        for v in non_null_values[1:]:
            if not deep_equal(first, v):
                raise EvaluationError(f"LOGQL_ERROR: conflict in CANON.{canon_ref.name}")

    # Return first non-null value, or default, or None
    if non_null_values:
        return non_null_values[0]

    if decl.default_value is not None:
        return decl.default_value.value

    return None


def compare_values(left: Any, op: str, right: Any) -> bool:
    """Compare two values with the given operator."""
    # Handle null comparisons
    if op == '=':
        if right is None:
            # = null matches null values and missing fields (which resolve to null)
            return left is None
        if left is None:
            return False  # non-null != null
    elif op == '!=':
        if right is None:
            # != null matches present, non-null scalar values
            if left is None:
                return False
            # Objects and arrays are not scalars
            if isinstance(left, (dict, list)):
                return False
            return True
        if left is None:
            return True  # null != non-null
    else:
        # Ordering operators with null always false
        if left is None or right is None:
            return False

    # Both non-null from here
    if left is None or right is None:
        return False

    # Type mismatch
    if type(left) != type(right):
        return False

    # Objects and arrays cannot be compared (except = null which is handled above)
    if isinstance(left, (dict, list)):
        return False

    # Boolean comparisons
    if isinstance(left, bool):
        if op in ('<', '<=', '>', '>='):
            return False
        if op == '=':
            return left == right
        if op == '!=':
            return left != right

    # String and number comparisons
    if op == '=':
        return left == right
    if op == '!=':
        return left != right
    if op == '<':
        return left < right
    if op == '<=':
        return left <= right
    if op == '>':
        return left > right
    if op == '>=':
        return left >= right

    return False


def evaluate_predicate(record: dict, pred: Predicate) -> bool:
    """Evaluate a predicate against a record."""
    left = get_field_value(record, pred.field)
    right = pred.literal.value
    return compare_values(left, pred.op, right)


def evaluate_boolean_expr(record: dict, expr: BooleanExpr) -> bool:
    """Evaluate a boolean expression against a record."""
    if isinstance(expr, Predicate):
        return evaluate_predicate(record, expr)

    if isinstance(expr, BinaryExpr):
        left_result = evaluate_boolean_expr(record, expr.left)

        if expr.op == 'OR':
            return left_result or evaluate_boolean_expr(record, expr.right)
        elif expr.op == 'AND':
            return left_result and evaluate_boolean_expr(record, expr.right)

    return False


def evaluate_predicate_qualified(records: Dict[str, dict], pred: Predicate) -> bool:
    """Evaluate a predicate against qualified records."""
    left = get_qualified_field_value(records, pred.field)
    right = pred.literal.value
    return compare_values(left, pred.op, right)


def evaluate_boolean_expr_qualified(records: Dict[str, dict], expr: BooleanExpr) -> bool:
    """Evaluate a boolean expression against qualified records."""
    if isinstance(expr, Predicate):
        return evaluate_predicate_qualified(records, expr)

    if isinstance(expr, BinaryExpr):
        left_result = evaluate_boolean_expr_qualified(records, expr.left)

        if expr.op == 'OR':
            return left_result or evaluate_boolean_expr_qualified(records, expr.right)
        elif expr.op == 'AND':
            return left_result and evaluate_boolean_expr_qualified(records, expr.right)

    return False


def project_record(record: dict, select: SelectClause) -> dict:
    """Project a record according to the select clause."""
    if select.star:
        return record

    result = {}
    for item in select.items:
        if item.alias_star:
            continue  # Handle elsewhere
        key = item.output_key()
        value = get_field_value(record, item.field)
        result[key] = value

    return result


# Deep equality for joins

def deep_equal(a: Any, b: Any) -> bool:
    """Deep equality check for values, including arrays and objects."""
    if type(a) != type(b):
        return False

    if isinstance(a, dict):
        if set(a.keys()) != set(b.keys()):
            return False
        return all(deep_equal(a[k], b[k]) for k in a)

    if isinstance(a, list):
        if len(a) != len(b):
            return False
        return all(deep_equal(x, y) for x, y in zip(a, b))

    return a == b


# Aggregation

def normalize_for_grouping(value: Any) -> Any:
    """Normalize a value for use as a group key.

    Arrays and objects are coerced to null for grouping.
    """
    if isinstance(value, (dict, list)):
        return None
    return value


def make_hashable(value: Any) -> Any:
    """Convert a value to a hashable representation for dict keys."""
    if value is None:
        return None
    if isinstance(value, bool):
        # Need to distinguish True from 1
        return ('__bool__', value)
    if isinstance(value, int):
        return ('__int__', value)
    if isinstance(value, float):
        return ('__float__', value)
    if isinstance(value, str):
        return ('__str__', value)
    if isinstance(value, list):
        return ('__list__', tuple(make_hashable(v) for v in value))
    if isinstance(value, dict):
        return ('__dict__', tuple(sorted((k, make_hashable(v)) for k, v in value.items())))
    # For other types, use the value directly
    return value


class AggregateState:
    def update(self, value: Any, record: dict):
        raise NotImplementedError

    def result(self) -> Any:
        raise NotImplementedError


class CountStarState(AggregateState):
    def __init__(self):
        self.count = 0

    def update(self, value: Any, record: dict):
        self.count += 1

    def result(self) -> Any:
        return self.count


class CountFieldState(AggregateState):
    def __init__(self):
        self.count = 0

    def update(self, value: Any, record: dict):
        if value is not None:
            self.count += 1

    def result(self) -> Any:
        return self.count


class SumState(AggregateState):
    def __init__(self):
        self.total = 0.0
        self.has_values = False

    def update(self, value: Any, record: dict):
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            self.total += value
            self.has_values = True

    def result(self) -> Any:
        if not self.has_values:
            return None
        if self.total == int(self.total):
            return int(self.total)
        return self.total


class AvgState(AggregateState):
    def __init__(self):
        self.total = 0.0
        self.count = 0

    def update(self, value: Any, record: dict):
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            self.total += value
            self.count += 1

    def result(self) -> Any:
        if self.count == 0:
            return None
        return self.total / self.count


class MinMaxState(AggregateState):
    def __init__(self, is_min: bool):
        self.is_min = is_min
        self.number_result = None
        self.string_result = None
        self.has_number = False
        self.has_string = False

    def update(self, value: Any, record: dict):
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if not self.has_number:
                self.number_result = value
                self.has_number = True
            else:
                if self.is_min:
                    self.number_result = min(self.number_result, value)
                else:
                    self.number_result = max(self.number_result, value)
        elif isinstance(value, str):
            if not self.has_string:
                self.string_result = value
                self.has_string = True
            else:
                if self.is_min:
                    self.string_result = min(self.string_result, value)
                else:
                    self.string_result = max(self.string_result, value)

    def result(self) -> Any:
        if self.has_number:
            if isinstance(self.number_result, float) and self.number_result == int(self.number_result):
                return int(self.number_result)
            return self.number_result
        if self.has_string:
            return self.string_result
        return None


class UniqueState(AggregateState):
    def __init__(self):
        self.values = []
        self.seen = []

    def update(self, value: Any, record: dict):
        for seen_val in self.seen:
            if deep_equal(value, seen_val):
                return
        self.seen.append(value)
        self.values.append(value)

    def result(self) -> Any:
        return self.values


def create_aggregate_state(agg: AggCall) -> AggregateState:
    if agg.func == 'COUNT':
        if agg.is_star:
            return CountStarState()
        return CountFieldState()
    elif agg.func == 'SUM':
        return SumState()
    elif agg.func == 'AVG':
        return AvgState()
    elif agg.func == 'MIN':
        return MinMaxState(is_min=True)
    elif agg.func == 'MAX':
        return MinMaxState(is_min=False)
    elif agg.func == 'UNIQUE':
        return UniqueState()
    else:
        raise EvaluationError(f"Unknown aggregate function: {agg.func}")


class GroupKey:
    def __init__(self, values: List[Any]):
        self.values = values

    def __hash__(self):
        return hash(tuple(make_hashable(v) for v in self.values))

    def __eq__(self, other):
        if not isinstance(other, GroupKey):
            return False
        if len(self.values) != len(other.values):
            return False
        return all(deep_equal(a, b) for a, b in zip(self.values, other.values))


class GroupState:
    def __init__(self, agg_calls: List[AggCall], gloss: Optional[GlossClause] = None):
        self.agg_states = [create_aggregate_state(agg) for agg in agg_calls]
        self.gloss = gloss

    def update(self, record: Dict[str, dict], agg_calls: List[AggCall], gloss: Optional[GlossClause] = None):
        for i, agg in enumerate(agg_calls):
            value = None
            if not agg.is_star:
                if isinstance(agg.field, CanonRef):
                    if gloss is None:
                        raise EvaluationError(f"LOGQL_ERROR: CANON reference without GLOSS")
                    value = resolve_canon_ref(record, agg.field, gloss)
                else:
                    value = get_qualified_field_value(record, agg.field)
            self.agg_states[i].update(value, record)

    def get_results(self) -> List[Any]:
        return [state.result() for state in self.agg_states]


def load_ndjson_file(path: str) -> List[dict]:
    """Load an NDJSON file and return a list of records."""
    records = []
    try:
        with open(path, 'r', encoding='utf-8') as f:
            for line_num, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as e:
                    output_error("json_error", f"Invalid JSON at line {line_num}: {e}")
                if not isinstance(record, dict):
                    output_error("json_error", f"Record at line {line_num} is not a JSON object")
                records.append(record)
    except FileNotFoundError:
        output_error("file_error", f"File not found: {path}")
    except IOError as e:
        output_error("file_error", f"Error reading file: {e}")
    return records


def evaluate_join_conjunct(records: Dict[str, dict], conjunct: JoinConjunct, gloss: Optional[GlossClause] = None) -> Any:
    """Evaluate a join conjunct field reference against qualified records."""
    if isinstance(conjunct.left_field, CanonRef):
        if gloss is None:
            raise EvaluationError(f"LOGQL_ERROR: CANON reference without GLOSS")
        return resolve_canon_ref(records, conjunct.left_field, gloss)
    else:
        return get_qualified_field_value(records, conjunct.left_field)


def evaluate_join_conjunct_equality(records: Dict[str, dict], conjunct: JoinConjunct, gloss: Optional[GlossClause] = None) -> bool:
    """Evaluate a join conjunct equality against qualified records."""
    left_val = evaluate_join_conjunct(records, conjunct, gloss)
    right_val = evaluate_join_conjunct_right(records, conjunct, gloss)
    # Use deep equality for join predicates
    return deep_equal(left_val, right_val)


def evaluate_join_conjunct_right(records: Dict[str, dict], conjunct: JoinConjunct, gloss: Optional[GlossClause] = None) -> Any:
    """Evaluate the right side of a join conjunct."""
    if isinstance(conjunct.right_field, CanonRef):
        if gloss is None:
            raise EvaluationError(f"LOGQL_ERROR: CANON reference without GLOSS")
        return resolve_canon_ref(records, conjunct.right_field, gloss)
    else:
        return get_qualified_field_value(records, conjunct.right_field)


def evaluate_join_predicate(records: Dict[str, dict], predicate: JoinPredicate, gloss: Optional[GlossClause] = None) -> bool:
    """Evaluate a join predicate (AND of conjuncts) against qualified records."""
    for conjunct in predicate.conjuncts:
        if not evaluate_join_conjunct_equality(records, conjunct, gloss):
            return False
    return True


def process_query(sources: Dict[str, str], query: Query) -> List[dict]:
    """Process a query against multiple sources."""

    # Load all source files
    source_records: Dict[str, List[dict]] = {}
    for alias, path in sources.items():
        source_records[alias] = load_ndjson_file(path)

    # Build initial anchor records from FROM alias
    anchor_alias = query.from_alias
    anchor_records = source_records[anchor_alias]

    # Apply WHERE filter (on anchor records, unqualified)
    if query.where:
        filtered_anchor = []
        for record in anchor_records:
            if evaluate_boolean_expr(record, query.where):
                filtered_anchor.append(record)
        anchor_records = filtered_anchor

    has_conflate = len(query.conflate_clauses) > 0
    has_gloss = query.gloss is not None

    # For Part 2-style queries (no conflate, no gloss), use simple record processing
    if not has_conflate and not has_gloss:
        # Convert back to simple records
        records = anchor_records
        return compute_aggregations_simple(records, query)

    # For queries with gloss but no conflate, we still need to handle CANON refs
    if not has_conflate and has_gloss:
        # Convert to {alias: record} format for consistency
        result_rows: List[Dict[str, Optional[dict]]] = []
        for record in anchor_records:
            result_rows.append({anchor_alias: record})
        return compute_aggregations_qualified(result_rows, query)

    # Convert anchor records to {alias: record} format for conflation
    # Each result row is a dict mapping alias -> record (or None for missing)
    result_rows: List[Dict[str, Optional[dict]]] = []
    for record in anchor_records:
        result_rows.append({anchor_alias: record})

    # Apply each CONFLATE clause in order
    for cc in query.conflate_clauses:
        new_result_rows = []
        conflate_alias = cc.alias
        conflate_records = source_records[conflate_alias]

        # Track which conflate records have been matched (for PRESERVING RIGHT/BOTH)
        matched_conflate_indices = set()

        # For each current result row, find matching conflate records
        for row in result_rows:
            matched_for_this_row = []

            for cf_idx, cf_record in enumerate(conflate_records):
                # Build a test records dict
                test_records = dict(row)
                test_records[conflate_alias] = cf_record

                # Evaluate the join predicate
                if evaluate_join_predicate(test_records, cc.predicate, query.gloss):
                    matched_for_this_row.append((cf_idx, cf_record))
                    matched_conflate_indices.add(cf_idx)

            if matched_for_this_row:
                # Add matched pairs
                for cf_idx, cf_record in matched_for_this_row:
                    new_row = dict(row)
                    new_row[conflate_alias] = cf_record
                    new_result_rows.append(new_row)
            else:
                # No matches for this anchor row
                if cc.flavor in ('PRESERVING LEFT', 'PRESERVING BOTH'):
                    # Keep the anchor row with null for conflate alias
                    new_row = dict(row)
                    new_row[conflate_alias] = None
                    new_result_rows.append(new_row)

        # Handle PRESERVING RIGHT/BOTH: add unmatched conflate records
        if cc.flavor in ('PRESERVING RIGHT', 'PRESERVING BOTH'):
            for cf_idx, cf_record in enumerate(conflate_records):
                if cf_idx not in matched_conflate_indices:
                    new_row = {anchor_alias: None, conflate_alias: cf_record}
                    # Also include any other aliases from previous conflations as None
                    for alias in row.keys():
                        if alias != anchor_alias and alias != conflate_alias:
                            new_row[alias] = None
                    new_result_rows.append(new_row)

        result_rows = new_result_rows

    # Now we have result_rows, which are {alias: record} dicts
    # Apply SELECT projection and GROUP BY aggregation

    if query.group_by:
        # Perform grouping and aggregation
        return compute_aggregations_qualified(result_rows, query)
    elif any(item.agg for item in query.select.items):
        # Aggregate without GROUP BY
        return compute_aggregations_qualified(result_rows, query)
    else:
        # Simple projection
        return project_qualified_records(result_rows, query.select)


def compute_aggregations_simple(records: List[dict], query: Query) -> List[dict]:
    """Compute aggregations on simple records (Part 2-style, no conflate)."""
    select = query.select
    group_by = query.group_by

    # Collect aggregate calls and their output keys
    agg_calls = []
    agg_keys = []
    for item in select.items:
        if item.agg:
            agg_calls.append(item.agg)
            agg_keys.append(item.output_key())

    has_aggregates = len(agg_calls) > 0

    if not has_aggregates:
        # Simple projection
        if select.star:
            return records
        result = []
        for record in records:
            row = {}
            for item in select.items:
                if item.field:
                    key = item.output_key()
                    value = get_field_value(record, item.field)
                    row[key] = value
            result.append(row)
        return result

    if group_by is None:
        # Single group for all records
        groups_data = [create_aggregate_state(agg) for agg in agg_calls]
        for record in records:
            for i, agg in enumerate(agg_calls):
                value = None
                if not agg.is_star:
                    value = get_field_value(record, agg.field)
                groups_data[i].update(value, record)
        results = [state.result() for state in groups_data]
        return [{k: v for k, v in zip(agg_keys, results)}]

    # Group by fields
    groups = {}
    order_counter = 0

    for record in records:
        key_values = []
        for field in group_by:
            value = get_field_value(record, field)
            value = normalize_for_grouping(value)
            key_values.append(value)

        group_key = GroupKey(key_values)

        if group_key not in groups:
            groups[group_key] = ([create_aggregate_state(agg) for agg in agg_calls], order_counter)
            order_counter += 1

        agg_states, _ = groups[group_key]
        for i, agg in enumerate(agg_calls):
            value = None
            if not agg.is_star:
                value = get_field_value(record, agg.field)
            agg_states[i].update(value, record)

    sorted_groups = sorted(groups.items(), key=lambda x: x[1][1])

    results = []
    for group_key, (agg_states, _) in sorted_groups:
        agg_results = [state.result() for state in agg_states]
        row = {}

        # Add aggregate results
        for i, key in enumerate(agg_keys):
            row[key] = agg_results[i]

        # Add group by fields
        for i, field in enumerate(group_by):
            # Find the select item for this field
            field_str = field.to_string()
            for item in select.items:
                if item.field and item.field.to_string() == field_str:
                    row[item.output_key()] = group_key.values[i]
                    break

        results.append(row)

    return results


def project_qualified_records(rows: List[Dict[str, Optional[dict]]], select: SelectClause, gloss: Optional[GlossClause] = None) -> List[dict]:
    """Project qualified records according to the select clause."""
    results = []

    for row in rows:
        result = {}

        for item in select.items:
            if item.alias_star:
                # Expand alias.*
                alias = item.alias_star
                record = row.get(alias)
                if record:
                    # Emit top-level keys as qualified keys
                    for key in record.keys():
                        qualified_key = f"{alias}.{key}"
                        result[qualified_key] = record[key]
            elif item.field:
                key = item.output_key()
                if isinstance(item.field, CanonRef):
                    if gloss is None:
                        raise EvaluationError(f"LOGQL_ERROR: CANON reference without GLOSS")
                    value = resolve_canon_ref(row, item.field, gloss)
                else:
                    value = get_qualified_field_value(row, item.field)
                result[key] = value
            elif item.agg:
                # Should not happen here - handled in aggregation
                pass

        results.append(result)

    return results


def compute_aggregations_qualified(rows: List[Dict[str, Optional[dict]]], query: Query) -> List[dict]:
    """Compute aggregations on qualified records."""
    select = query.select
    group_by = query.group_by
    gloss = query.gloss

    # Collect aggregate calls and their output keys
    agg_calls = []
    agg_keys = []
    for item in select.items:
        if item.agg:
            agg_calls.append(item.agg)
            agg_keys.append(item.output_key())

    has_aggregates = len(agg_calls) > 0

    if not has_aggregates:
        return project_qualified_records(rows, select, gloss)

    if group_by is None:
        # Single group for all records
        group = GroupState(agg_calls, gloss)
        for row in rows:
            group.update(row, agg_calls, gloss)
        results = group.get_results()
        return [{k: v for k, v in zip(agg_keys, results)}]

    # Group by fields
    groups = {}
    order_counter = 0

    for row in rows:
        key_values = []
        for field in group_by:
            if isinstance(field, CanonRef):
                if gloss is None:
                    raise EvaluationError(f"LOGQL_ERROR: CANON reference without GLOSS")
                value = resolve_canon_ref(row, field, gloss)
            else:
                value = get_qualified_field_value(row, field)
            value = normalize_for_grouping(value)
            key_values.append(value)

        group_key = GroupKey(key_values)

        if group_key not in groups:
            groups[group_key] = (GroupState(agg_calls, gloss), order_counter)
            order_counter += 1

        group_state, _ = groups[group_key]
        group_state.update(row, agg_calls, gloss)

    sorted_groups = sorted(groups.items(), key=lambda x: x[1][1])

    results = []
    for group_key, (group_state, _) in sorted_groups:
        agg_results = group_state.get_results()
        row = {}

        # Add aggregate results
        for i, key in enumerate(agg_keys):
            row[key] = agg_results[i]

        # Add group by fields
        for i, field in enumerate(group_by):
            # Find the select item for this field
            field_str = field.to_string()
            for item in select.items:
                if item.field and item.field.to_string() == field_str:
                    row[item.output_key()] = group_key.values[i]
                    break

        results.append(row)

    return results


# Error Output

def output_error(error_type: str, message: str):
    """Output a structured error to stderr and exit."""
    error_obj = {
        "error": error_type,
        "message": message
    }
    print(json.dumps(error_obj), file=sys.stderr)
    sys.exit(1)


# Main

def parse_query(query_str: str) -> Query:
    """Parse a query string into a Query object."""
    try:
        lexer = Lexer(query_str)
        tokens = lexer.tokenize()
        parser = Parser(tokens)
        return parser.parse()
    except (LexerError, ParseError) as e:
        output_error("E_PARSE", str(e))
    except SemanticError as e:
        output_error("E_SEMANTIC", str(e))


def main():
    parser = argparse.ArgumentParser(description='LogQL Part 3 - Query NDJSON logs with multi-source conflation')
    parser.add_argument('--log-file', help='Path to NDJSON log file (shorthand for --source logs=<path>)')
    parser.add_argument('--query', required=True, help='SQL-like query string')
    parser.add_argument('--output', help='Output file path (optional)')
    parser.add_argument('--source', action='append', default=[],
                        help='Bind an alias to an NDJSON file (format: alias=path), can be repeated')

    args = parser.parse_args()

    # Parse sources
    sources: Dict[str, str] = {}

    # Handle --log-file shorthand
    if args.log_file:
        sources['logs'] = args.log_file

    # Handle --source arguments
    for source_arg in args.source:
        if '=' not in source_arg:
            output_error("E_SEMANTIC", f"Invalid --source format: {source_arg}, expected alias=path")
        alias, path = source_arg.split('=', 1)
        alias = alias.strip()
        path = path.strip()
        if not alias:
            output_error("E_SEMANTIC", f"Empty alias in --source: {source_arg}")
        if alias in sources:
            output_error("E_SEMANTIC", f"Duplicate alias binding: {alias}")
        sources[alias] = path

    # Parse the query
    query = parse_query(args.query)

    # Validate that all aliases in the query are bound
    all_aliases = {query.from_alias}
    for cc in query.conflate_clauses:
        all_aliases.add(cc.alias)

    for alias in all_aliases:
        if alias not in sources:
            output_error("E_SEMANTIC", f"Alias '{alias}' not bound to any source")

    # Process query
    results = process_query(sources, query)

    # Output
    output_json = json.dumps(results)

    if args.output:
        try:
            with open(args.output, 'w', encoding='utf-8') as f:
                f.write(output_json)
                f.write('\n')
        except IOError as e:
            output_error("file_error", f"Error writing output file: {e}")
    else:
        print(output_json)


if __name__ == '__main__':
    main()
