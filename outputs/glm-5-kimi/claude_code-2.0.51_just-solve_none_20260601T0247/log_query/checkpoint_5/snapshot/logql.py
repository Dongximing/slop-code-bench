#!/usr/bin/env python3
"""
LogQL Part 5 - A command-line program that reads NDJSON logs from multiple sources,
parses a SQL-like query with GROUP BY, aggregations, multi-source conflation (joins),
nested queries, and correlated subqueries.
"""

import argparse
import json
import sys
from dataclasses import dataclass, field
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
    # Part 5 keywords
    POCKET = 'POCKET'
    BEHOLDS = 'BEHOLDS'
    AMONGST = 'AMONGST'
    EITHERWISE = 'EITHERWISE'
    EVERYWISE = 'EVERYWISE'
    UPTREE = 'UPTREE'
    HAVING = 'HAVING'

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
    LBRACKET = '['
    RBRACKET = ']'
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
        'pocket': TokenType.POCKET,
        'beholds': TokenType.BEHOLDS,
        'amongst': TokenType.AMONGST,
        'eitherwise': TokenType.EITHERWISE,
        'everywise': TokenType.EVERYWISE,
        'uptree': TokenType.UPTREE,
        'having': TokenType.HAVING,
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

        if self.peek() == '-':
            self.advance()

        if self.peek() == '0':
            self.advance()
        elif self.peek() and self.peek().isdigit():
            while self.peek() and self.peek().isdigit():
                self.advance()
        else:
            raise LexerError(f"Invalid number at position {start}")

        if self.peek() == '.':
            has_dot = True
            self.advance()
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
        self.advance()

        result = []
        while True:
            ch = self.peek()
            if ch is None:
                raise LexerError(f"Unterminated string starting at position {start}")
            if ch == '"':
                self.advance()
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

        if ch.isdigit() or (ch == '-' and self.peek(1) and self.peek(1).isdigit()):
            return self.read_number()

        if ch == '"':
            return self.read_string()

        if ch.isalpha() or ch == '_':
            return self.read_identifier()

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

        if ch == '[':
            self.advance()
            return Token(TokenType.LBRACKET, '[', start)

        if ch == ']':
            self.advance()
            return Token(TokenType.RBRACKET, ']', start)

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
class OuterRef:
    """Reference to a field from an enclosing query (UPTREE.alias.field...)."""
    alias: str
    parts: List[str]

    def to_string(self) -> str:
        return f"UPTREE.{self.alias}." + '.'.join(self.parts)


@dataclass
class Literal:
    value: Any


@dataclass
class ScalarSubquery:
    """POCKET(<query>) - yields a single value."""
    query: 'Query'
    query_text: str = ""  # Original query text for output key


@dataclass
class TableSubquery:
    """POCKET[<query>] - yields a single-column multiset."""
    query: 'Query'


@dataclass
class ValueExpr:
    """A value expression - can be field ref, canon ref, literal, or scalar subquery."""
    value: Union[FieldRef, CanonRef, OuterRef, Literal, ScalarSubquery]


@dataclass
class Predicate:
    """Comparison predicate: value_expr op value_expr."""
    left: Union[FieldRef, CanonRef, OuterRef, Literal, ScalarSubquery]
    op: str
    right: Union[FieldRef, CanonRef, OuterRef, Literal, ScalarSubquery]


@dataclass
class BinaryExpr:
    left: 'BooleanExpr'
    op: str  # 'AND' or 'OR'
    right: 'BooleanExpr'


@dataclass
class BeholdsExpr:
    """BEHOLDS POCKET[<query>] - EXISTS predicate."""
    subquery: TableSubquery


@dataclass
class AmongstExpr:
    """value AMONGST POCKET[<query>] - IN predicate."""
    value: Union[FieldRef, CanonRef, OuterRef, Literal, ScalarSubquery]
    subquery: TableSubquery


@dataclass
class EitherwiseExpr:
    """value op EITHERWISE POCKET[<query>] - ANY predicate."""
    value: Union[FieldRef, CanonRef, OuterRef, Literal, ScalarSubquery]
    op: str
    subquery: TableSubquery


@dataclass
class EverywiseExpr:
    """value op EVERYWISE POCKET[<query>] - ALL predicate."""
    value: Union[FieldRef, CanonRef, OuterRef, Literal, ScalarSubquery]
    op: str
    subquery: TableSubquery


BooleanExpr = Union[Predicate, BinaryExpr, BeholdsExpr, AmongstExpr, EitherwiseExpr, EverywiseExpr]


@dataclass
class JoinConjunct:
    left_field: Union[FieldRef, CanonRef, OuterRef]
    right_field: Union[FieldRef, CanonRef, OuterRef]


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
    field: Optional[Union[FieldRef, CanonRef, OuterRef]]  # None for COUNT(*)
    is_star: bool = False  # True for COUNT(*)

    def canonical_string(self) -> str:
        if self.is_star:
            return f"{self.func}(*)"
        return f"{self.func}({self.field.to_string()})"


@dataclass
class SelectItem:
    field: Optional[Union[FieldRef, CanonRef, OuterRef, ScalarSubquery]]  # None if this is an aggregate or star expansion
    agg: Optional[AggCall]  # None if this is a field reference
    alias_star: Optional[str] = None  # For alias.* expansion
    alias: Optional[str] = None  # AS alias

    def output_key(self) -> str:
        if self.alias:
            return self.alias
        if self.field:
            if isinstance(self.field, ScalarSubquery):
                return self.field.query_text if self.field.query_text else "POCKET(...)"
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
    group_by: Optional[List[Union[FieldRef, CanonRef, OuterRef]]]
    having: Optional[BooleanExpr] = None  # HAVING clause for filtering after GROUP BY


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
    def __init__(self, tokens: List[Token], query_text: str = ""):
        self.tokens = tokens
        self.pos = 0
        self.query_text = query_text

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

        where = None
        if self.match(TokenType.WHERE):
            self.advance()
            where = self.parse_boolean_expr()

        gloss = None
        if self.match(TokenType.GLOSS):
            self.advance()
            gloss = self.parse_gloss_clause()

        conflate_clauses = []
        while self.match(TokenType.CONFLATE):
            self.advance()
            conflate = self.parse_conflate_clause()
            conflate_clauses.append(conflate)

        group_by = None
        if self.match(TokenType.GROUP):
            self.advance()
            self.expect(TokenType.BY)
            group_by = self.parse_group_list()

        having = None
        if self.match(TokenType.HAVING):
            self.advance()
            having = self.parse_boolean_expr()

        self.expect(TokenType.EOF)

        self.validate_query(select, from_alias, gloss, conflate_clauses, group_by)

        return Query(select, from_alias, where, gloss, conflate_clauses, group_by, having)

    def validate_query(self, select: SelectClause, from_alias: str,
                       gloss: Optional[GlossClause],
                       conflate_clauses: List[ConflateClause],
                       group_by: Optional[List[Union[FieldRef, CanonRef, OuterRef]]]):
        if len(conflate_clauses) > 0 and select.star:
            raise SemanticError("E_SEMANTIC: SELECT * is forbidden when CONFLATE is present")

        canon_names = set()
        if gloss:
            for decl in gloss.declarations:
                if decl.name in canon_names:
                    raise SemanticError(f"E_SEMANTIC: redeclared CANON label '{decl.name}'")
                canon_names.add(decl.name)

        output_keys = []
        for item in select.items:
            if item.alias_star:
                continue
            key = item.output_key()
            if key in output_keys:
                raise SemanticError(f"E_SEMANTIC: duplicate output key '{key}'")
            output_keys.append(key)

        if group_by:
            group_keys = []
            for field in group_by:
                key = field.to_string()
                if key in group_keys:
                    raise SemanticError(f"E_SEMANTIC: duplicate field '{key}' in GROUP BY")
                group_keys.append(key)

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

        has_aggregates = any(item.agg is not None for item in select.items)
        non_agg_fields = [item.field for item in select.items if item.field is not None and item.agg is None]

        if has_aggregates:
            if group_by is None:
                for field in non_agg_fields:
                    if not isinstance(field, ScalarSubquery):
                        raise SemanticError(f"Non-aggregate field '{field.to_string()}' must be in GROUP BY")
            else:
                group_by_strs = [f.to_string() for f in group_by]
                for field in non_agg_fields:
                    if not isinstance(field, ScalarSubquery) and field.to_string() not in group_by_strs:
                        raise SemanticError(f"Non-aggregate field '{field.to_string()}' must be in GROUP BY")

        all_aliases = {from_alias}
        for cc in conflate_clauses:
            all_aliases.add(cc.alias)

        has_conflate = len(conflate_clauses) > 0

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
        # Check for scalar subquery
        if self.match(TokenType.POCKET) and self.peek(1).type == TokenType.LPAREN:
            subquery = self.parse_scalar_subquery()
            alias = None
            if self.match(TokenType.AS):
                self.advance()
                alias_token = self.expect(TokenType.IDENT)
                alias = alias_token.value
            return SelectItem(field=subquery, agg=None, alias=alias)

        if self.match(*AGGREGATE_FUNCS.keys()):
            agg = self.parse_agg_call()
            alias = None
            if self.match(TokenType.AS):
                self.advance()
                alias_token = self.expect(TokenType.IDENT)
                alias = alias_token.value
            return SelectItem(field=None, agg=agg, alias=alias)

        if self.match(TokenType.IDENT) and self.peek(1).type == TokenType.DOT and self.peek(2).type == TokenType.STAR:
            alias_name = self.advance().value
            self.advance()
            self.advance()
            return SelectItem(field=None, agg=None, alias_star=alias_name)

        if self.match(TokenType.CANON) and self.peek(1).type == TokenType.DOT and self.peek(2).type == TokenType.STAR:
            raise ParseError("E_PARSE: CANON.* wildcard not allowed")

        field = self.parse_field_or_canon_or_outer_ref()
        alias = None
        if self.match(TokenType.AS):
            self.advance()
            alias_token = self.expect(TokenType.IDENT)
            alias = alias_token.value
        return SelectItem(field=field, agg=None, alias=alias)

    def parse_scalar_subquery(self) -> ScalarSubquery:
        """Parse POCKET(<query>)."""
        start_pos = self.current().pos
        self.expect(TokenType.POCKET)
        self.expect(TokenType.LPAREN)

        # Extract the subquery text and advance lexer position
        inner_query, end_pos = self.extract_query_text_with_end(is_paren=True)
        subquery = self.parse_subquery(inner_query)

        # Advance token position to where we ended in the query text
        self.advance_to_position(end_pos)
        self.expect(TokenType.RPAREN)

        return ScalarSubquery(query=subquery, query_text=f"POCKET({inner_query})")

    def parse_table_subquery(self) -> TableSubquery:
        """Parse POCKET[<query>]."""
        start_pos = self.current().pos
        self.expect(TokenType.POCKET)
        self.expect(TokenType.LBRACKET)

        inner_query, end_pos = self.extract_query_text_with_end(is_paren=False)
        subquery = self.parse_subquery(inner_query)

        # Advance token position to where we ended in the query text
        self.advance_to_position(end_pos)
        self.expect(TokenType.RBRACKET)

        return TableSubquery(query=subquery)

    def extract_query_text_with_end(self, is_paren: bool = True) -> tuple:
        """Extract the query text from current position, handling nested POCKETs.
        Returns (query_text, end_position).
        is_paren: True if we're inside POCKET(...), False if inside POCKET[...]"""
        # Start from current position in the original query text
        start_pos = self.current().pos

        # We need to find the matching closing paren/bracket in the original query text
        # We start at depth 1 because we're already inside POCKET( or POCKET[
        depth_paren = 1 if is_paren else 0
        depth_bracket = 0 if is_paren else 1
        pos = start_pos
        query_len = len(self.query_text)

        while pos < query_len:
            ch = self.query_text[pos]

            if ch == '(':
                depth_paren += 1
            elif ch == ')':
                if depth_paren > 0:
                    depth_paren -= 1
                if depth_paren == 0 and depth_bracket == 0:
                    # Found the matching close
                    pos += 1
                    break
            elif ch == '[':
                depth_bracket += 1
            elif ch == ']':
                if depth_bracket > 0:
                    depth_bracket -= 1
                if depth_paren == 0 and depth_bracket == 0:
                    # Found the matching close
                    pos += 1
                    break
            elif ch == '"':
                # Skip string
                pos += 1
                while pos < query_len:
                    if self.query_text[pos] == '\\':
                        pos += 2  # Skip escape sequence
                    elif self.query_text[pos] == '"':
                        pos += 1
                        break
                    else:
                        pos += 1
                continue

            pos += 1

        # pos now points just past the closing paren/bracket
        # The closing delimiter is at pos-1
        # Return pos-1 so that advance_to_position lands on the closing delimiter token
        query_text = self.query_text[start_pos:pos-1].strip()  # Exclude the closing delimiter
        return query_text, pos - 1

    def advance_to_position(self, target_pos: int):
        """Advance token position until we reach or pass the target position."""
        while self.pos < len(self.tokens) - 1:  # -1 to not go past EOF
            token = self.tokens[self.pos]
            if token.pos >= target_pos:
                break
            self.pos += 1

    def parse_subquery(self, query_text: str) -> Query:
        """Parse a nested subquery."""
        lexer = Lexer(query_text)
        tokens = lexer.tokenize()
        parser = Parser(tokens, query_text)
        return parser.parse()

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

        # :=  (COLON followed by EQ)
        self.expect(TokenType.COLON)
        self.expect(TokenType.EQ)

        candidates = []
        field = self.parse_field_ref()
        candidates.append(field)

        while self.match(TokenType.PIPE):
            self.advance()
            field = self.parse_field_ref()
            candidates.append(field)

        default_value = None
        if self.match(TokenType.DEFAULT):
            self.advance()
            default_value = self.parse_literal()

        return CanonDecl(name=name, candidates=candidates, default_value=default_value)

    def parse_conflate_clause(self) -> ConflateClause:
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
            flavor = 'INTERSECTING'

        alias_token = self.expect(TokenType.IDENT)
        alias = alias_token.value

        self.expect(TokenType.UPON)

        predicate = self.parse_join_predicate()

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
        left = self.parse_field_or_canon_or_outer_ref()
        self.expect(TokenType.EQ)
        right = self.parse_field_or_canon_or_outer_ref()
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
            field = self.parse_field_or_canon_or_outer_ref()
            self.expect(TokenType.RPAREN)
            return AggCall(func=func_name, field=field, is_star=False)

    def parse_group_list(self) -> List[Union[FieldRef, CanonRef, OuterRef]]:
        fields = []
        field = self.parse_field_or_canon_or_outer_ref()
        fields.append(field)

        while self.match(TokenType.COMMA):
            self.advance()
            field = self.parse_field_or_canon_or_outer_ref()
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

    def parse_outer_ref(self) -> OuterRef:
        """Parse UPTREE.alias.field..."""
        self.expect(TokenType.UPTREE)
        self.expect(TokenType.DOT)

        alias_token = self.expect(TokenType.IDENT)
        alias = alias_token.value

        self.expect(TokenType.DOT)

        parts = []
        ident = self.expect(TokenType.IDENT)
        parts.append(ident.value)

        while self.match(TokenType.DOT):
            self.advance()
            ident = self.expect(TokenType.IDENT)
            parts.append(ident.value)

        return OuterRef(alias=alias, parts=parts)

    def parse_field_or_canon_or_outer_ref(self) -> Union[FieldRef, CanonRef, OuterRef]:
        """Parse either a field reference, CANON reference, or UPTREE reference."""
        if self.match(TokenType.UPTREE):
            return self.parse_outer_ref()
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
        left = self.parse_primary_bool_expr()

        while self.match(TokenType.AND):
            self.advance()
            right = self.parse_primary_bool_expr()
            left = BinaryExpr(left, 'AND', right)

        return left

    def parse_primary_bool_expr(self) -> BooleanExpr:
        if self.match(TokenType.LPAREN):
            self.advance()
            expr = self.parse_boolean_expr()
            self.expect(TokenType.RPAREN)
            return expr

        # Check for BEHOLDS
        if self.match(TokenType.BEHOLDS):
            self.advance()
            subquery = self.parse_table_subquery()
            return BeholdsExpr(subquery=subquery)

        # Check for value-based predicates (might be AMONGST, EITHERWISE, EVERYWISE, or regular comparison)
        return self.parse_value_predicate()

    def parse_value_predicate(self) -> BooleanExpr:
        """Parse a value expression that might be part of a predicate."""
        left = self.parse_value_expr()

        # Check for comparison operators
        if self.match(TokenType.EQ, TokenType.NE, TokenType.LT,
                      TokenType.LE, TokenType.GT, TokenType.GE):
            op_token = self.advance()
            op = op_token.value

            # Check for EITHERWISE or EVERYWISE
            if self.match(TokenType.EITHERWISE):
                self.advance()
                subquery = self.parse_table_subquery()
                return EitherwiseExpr(value=left, op=op, subquery=subquery)
            elif self.match(TokenType.EVERYWISE):
                self.advance()
                subquery = self.parse_table_subquery()
                return EverywiseExpr(value=left, op=op, subquery=subquery)
            else:
                right = self.parse_value_expr()
                return Predicate(left=left, op=op, right=right)

        # Check for AMONGST (uses implicit =)
        if self.match(TokenType.AMONGST):
            self.advance()
            subquery = self.parse_table_subquery()
            return AmongstExpr(value=left, subquery=subquery)

        raise ParseError(f"Expected comparison operator at position {self.current().pos}")

    def parse_value_expr(self) -> Union[FieldRef, CanonRef, OuterRef, Literal, ScalarSubquery]:
        """Parse a value expression."""
        # Check for aggregate function (for HAVING clause)
        if self.match(*AGGREGATE_FUNCS.keys()):
            agg = self.parse_agg_call()
            return Literal(agg.canonical_string())  # Return as string reference

        # Check for scalar subquery
        if self.match(TokenType.POCKET) and self.peek(1).type == TokenType.LPAREN:
            return self.parse_scalar_subquery()

        # Check for outer reference
        if self.match(TokenType.UPTREE):
            return self.parse_outer_ref()

        # Check for CANON reference
        if self.match(TokenType.CANON):
            self.advance()
            self.expect(TokenType.DOT)
            name_token = self.expect(TokenType.IDENT)
            return CanonRef(name=name_token.value)

        # Check for literal
        if self.match(TokenType.NUMBER, TokenType.STRING, TokenType.TRUE, TokenType.FALSE, TokenType.NULL):
            return self.parse_literal()

        # Field reference
        return self.parse_field_ref()

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
    decl = None
    for d in gloss.declarations:
        if d.name == canon_ref.name:
            decl = d
            break

    if decl is None:
        raise EvaluationError(f"LOGQL_ERROR: undeclared CANON.{canon_ref.name}")

    non_null_values = []
    for candidate in decl.candidates:
        value = get_qualified_field_value(records, candidate)
        if value is not None:
            non_null_values.append(value)

    if gloss.strict and len(non_null_values) > 1:
        first = non_null_values[0]
        for v in non_null_values[1:]:
            if not deep_equal(first, v):
                raise EvaluationError(f"LOGQL_ERROR: conflict in CANON.{canon_ref.name}")

    if non_null_values:
        return non_null_values[0]

    if decl.default_value is not None:
        return decl.default_value.value

    return None


def resolve_outer_ref(outer_ref: OuterRef, correlation_context: List[Dict[str, dict]]) -> Any:
    """Resolve an UPTREE reference from the correlation context."""
    alias = outer_ref.alias

    # Search from innermost to outermost
    for ctx in reversed(correlation_context):
        if alias in ctx:
            record = ctx[alias]
            if record is None:
                return None
            value = record
            for part in outer_ref.parts:
                if value is None:
                    return None
                if isinstance(value, dict):
                    value = value.get(part)
                else:
                    return None
            return value

    return None


def compare_values(left: Any, op: str, right: Any) -> bool:
    """Compare two values with the given operator."""
    if op == '=':
        return deep_equal(left, right)
    elif op == '!=':
        return not deep_equal(left, right)
    else:
        if left is None or right is None:
            return False

        if type(left) != type(right):
            return False

        if isinstance(left, (dict, list)):
            return False

        if isinstance(left, bool):
            return False

        # String and number comparisons
        if op == '<':
            return left < right
        if op == '<=':
            return left <= right
        if op == '>':
            return left > right
        if op == '>=':
            return left >= right

    return False


def evaluate_value_expr(
    value: Union[FieldRef, CanonRef, OuterRef, Literal, ScalarSubquery],
    records: Dict[str, dict],
    gloss: Optional[GlossClause],
    sources: Dict[str, str],
    correlation_context: List[Dict[str, dict]]
) -> Any:
    """Evaluate a value expression."""
    if isinstance(value, Literal):
        return value.value

    if isinstance(value, FieldRef):
        return get_qualified_field_value(records, value)

    if isinstance(value, CanonRef):
        if gloss is None:
            raise EvaluationError(f"LOGQL_ERROR: CANON reference without GLOSS")
        return resolve_canon_ref(records, value, gloss)

    if isinstance(value, OuterRef):
        return resolve_outer_ref(value, correlation_context)

    if isinstance(value, ScalarSubquery):
        return evaluate_scalar_subquery(value, records, gloss, sources, correlation_context)

    return None


def evaluate_predicate_qualified(
    records: Dict[str, dict],
    pred: Predicate,
    gloss: Optional[GlossClause],
    sources: Dict[str, str],
    correlation_context: List[Dict[str, dict]]
) -> bool:
    """Evaluate a predicate against qualified records."""
    left = evaluate_value_expr(pred.left, records, gloss, sources, correlation_context)
    right = evaluate_value_expr(pred.right, records, gloss, sources, correlation_context)
    return compare_values(left, pred.op, right)


def evaluate_boolean_expr_qualified(
    records: Dict[str, dict],
    expr: BooleanExpr,
    gloss: Optional[GlossClause],
    sources: Dict[str, str],
    correlation_context: List[Dict[str, dict]]
) -> bool:
    """Evaluate a boolean expression against qualified records."""
    if isinstance(expr, Predicate):
        return evaluate_predicate_qualified(records, expr, gloss, sources, correlation_context)

    if isinstance(expr, BinaryExpr):
        left_result = evaluate_boolean_expr_qualified(records, expr.left, gloss, sources, correlation_context)

        if expr.op == 'OR':
            return left_result or evaluate_boolean_expr_qualified(records, expr.right, gloss, sources, correlation_context)
        elif expr.op == 'AND':
            return left_result and evaluate_boolean_expr_qualified(records, expr.right, gloss, sources, correlation_context)

    if isinstance(expr, BeholdsExpr):
        return evaluate_beholds(expr, records, gloss, sources, correlation_context)

    if isinstance(expr, AmongstExpr):
        return evaluate_amongst(expr, records, gloss, sources, correlation_context)

    if isinstance(expr, EitherwiseExpr):
        return evaluate_eitherwise(expr, records, gloss, sources, correlation_context)

    if isinstance(expr, EverywiseExpr):
        return evaluate_everywise(expr, records, gloss, sources, correlation_context)

    return False


def evaluate_scalar_subquery(
    subquery: ScalarSubquery,
    records: Dict[str, dict],
    gloss: Optional[GlossClause],
    sources: Dict[str, str],
    correlation_context: List[Dict[str, dict]]
) -> Any:
    """Evaluate a scalar subquery and return a single value."""
    # Add current records to correlation context
    new_context = correlation_context + [records]

    results = process_query_internal(sources, subquery.query, new_context)

    if not results:
        return None

    # Return the first value from the first result
    first_result = results[0]
    if first_result:
        values = list(first_result.values())
        if values:
            return values[0]

    return None


def evaluate_beholds(
    expr: BeholdsExpr,
    records: Dict[str, dict],
    gloss: Optional[GlossClause],
    sources: Dict[str, str],
    correlation_context: List[Dict[str, dict]]
) -> bool:
    """Evaluate BEHOLDS (EXISTS) predicate."""
    new_context = correlation_context + [records]

    results = process_query_internal(sources, expr.subquery.query, new_context)

    return len(results) > 0


def evaluate_amongst(
    expr: AmongstExpr,
    records: Dict[str, dict],
    gloss: Optional[GlossClause],
    sources: Dict[str, str],
    correlation_context: List[Dict[str, dict]]
) -> bool:
    """Evaluate AMONGST (IN) predicate."""
    value = evaluate_value_expr(expr.value, records, gloss, sources, correlation_context)

    new_context = correlation_context + [records]

    results = process_query_internal(sources, expr.subquery.query, new_context)

    for result in results:
        for v in result.values():
            if deep_equal(value, v):
                return True

    return False


def evaluate_eitherwise(
    expr: EitherwiseExpr,
    records: Dict[str, dict],
    gloss: Optional[GlossClause],
    sources: Dict[str, str],
    correlation_context: List[Dict[str, dict]]
) -> bool:
    """Evaluate EITHERWISE (ANY) predicate."""
    value = evaluate_value_expr(expr.value, records, gloss, sources, correlation_context)

    new_context = correlation_context + [records]

    results = process_query_internal(sources, expr.subquery.query, new_context)

    if not results:
        return False

    for result in results:
        for v in result.values():
            if compare_values(value, expr.op, v):
                return True

    return False


def evaluate_everywise(
    expr: EverywiseExpr,
    records: Dict[str, dict],
    gloss: Optional[GlossClause],
    sources: Dict[str, str],
    correlation_context: List[Dict[str, dict]]
) -> bool:
    """Evaluate EVERYWISE (ALL) predicate."""
    value = evaluate_value_expr(expr.value, records, gloss, sources, correlation_context)

    new_context = correlation_context + [records]

    results = process_query_internal(sources, expr.subquery.query, new_context)

    if not results:
        return True

    for result in results:
        for v in result.values():
            if not compare_values(value, expr.op, v):
                return False

    return True


# Deep equality for joins

def deep_equal(a: Any, b: Any) -> bool:
    """Deep equality check for values, including arrays and objects."""
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False

    if type(a) != type(b):
        # Special case: int and float comparison
        if isinstance(a, (int, float)) and isinstance(b, (int, float)) and not isinstance(a, bool) and not isinstance(b, bool):
            return a == b
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

    Arrays and objects are coerced to null for grouping."""

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

    def update(self, record: Dict[str, dict], agg_calls: List[AggCall],
               gloss: Optional[GlossClause] = None, sources: Dict[str, str] = None,
               correlation_context: List[Dict[str, dict]] = None):
        for i, agg in enumerate(agg_calls):
            value = None
            if not agg.is_star:
                if isinstance(agg.field, CanonRef):
                    if gloss is None:
                        raise EvaluationError(f"LOGQL_ERROR: CANON reference without GLOSS")
                    value = resolve_canon_ref(record, agg.field, gloss)
                elif isinstance(agg.field, OuterRef):
                    value = resolve_outer_ref(agg.field, correlation_context or [])
                elif isinstance(agg.field, FieldRef):
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
                    output_error("E_PARSE", f"Invalid JSON at line {line_num}: {e}")
                if not isinstance(record, dict):
                    output_error("E_PARSE", f"Record at line {line_num} is not a JSON object")
                records.append(record)
    except FileNotFoundError:
        output_error("E_IO", f"File not found: {path}")
    except IOError as e:
        output_error("E_IO", f"Error reading file: {e}")
    return records


def evaluate_join_conjunct(records: Dict[str, dict], conjunct: JoinConjunct,
                           gloss: Optional[GlossClause] = None,
                           correlation_context: List[Dict[str, dict]] = None) -> Any:
    """Evaluate a join conjunct field reference against qualified records."""
    if isinstance(conjunct.left_field, CanonRef):
        if gloss is None:
            raise EvaluationError(f"LOGQL_ERROR: CANON reference without GLOSS")
        return resolve_canon_ref(records, conjunct.left_field, gloss)
    elif isinstance(conjunct.left_field, OuterRef):
        return resolve_outer_ref(conjunct.left_field, correlation_context or [])
    else:
        return get_qualified_field_value(records, conjunct.left_field)


def evaluate_join_conjunct_right(records: Dict[str, dict], conjunct: JoinConjunct,
                                  gloss: Optional[GlossClause] = None,
                                  correlation_context: List[Dict[str, dict]] = None) -> Any:
    """Evaluate the right side of a join conjunct."""
    if isinstance(conjunct.right_field, CanonRef):
        if gloss is None:
            raise EvaluationError(f"LOGQL_ERROR: CANON reference without GLOSS")
        return resolve_canon_ref(records, conjunct.right_field, gloss)
    elif isinstance(conjunct.right_field, OuterRef):
        return resolve_outer_ref(conjunct.right_field, correlation_context or [])
    else:
        return get_qualified_field_value(records, conjunct.right_field)


def evaluate_join_conjunct_equality(records: Dict[str, dict], conjunct: JoinConjunct,
                                     gloss: Optional[GlossClause] = None,
                                     correlation_context: List[Dict[str, dict]] = None) -> bool:
    """Evaluate a join conjunct equality against qualified records."""
    left_val = evaluate_join_conjunct(records, conjunct, gloss, correlation_context)
    right_val = evaluate_join_conjunct_right(records, conjunct, gloss, correlation_context)
    return deep_equal(left_val, right_val)


def evaluate_join_predicate(records: Dict[str, dict], predicate: JoinPredicate,
                            gloss: Optional[GlossClause] = None,
                            correlation_context: List[Dict[str, dict]] = None) -> bool:
    """Evaluate a join predicate (AND of conjuncts) against qualified records."""
    for conjunct in predicate.conjuncts:
        if not evaluate_join_conjunct_equality(records, conjunct, gloss, correlation_context):
            return False
    return True


def process_query_internal(sources: Dict[str, str], query: Query,
                          correlation_context: List[Dict[str, dict]] = None) -> List[dict]:
    """Process a query against multiple sources with correlation context."""
    if correlation_context is None:
        correlation_context = []

    source_records: Dict[str, List[dict]] = {}
    for alias, path in sources.items():
        source_records[alias] = load_ndjson_file(path)

    anchor_alias = query.from_alias
    anchor_records = source_records[anchor_alias]

    if query.where:
        filtered_anchor = []
        for record in anchor_records:
            # Create a single-record dict for the anchor
            records_dict = {anchor_alias: record}
            if evaluate_boolean_expr_qualified(records_dict, query.where, query.gloss, sources, correlation_context):
                filtered_anchor.append(record)
        anchor_records = filtered_anchor

    has_conflate = len(query.conflate_clauses) > 0
    has_gloss = query.gloss is not None

    if not has_conflate and not has_gloss:
        records = anchor_records
        return compute_aggregations_simple(records, query, sources, correlation_context)

    if not has_conflate and has_gloss:
        result_rows: List[Dict[str, Optional[dict]]] = []
        for record in anchor_records:
            result_rows.append({anchor_alias: record})
        return compute_aggregations_qualified(result_rows, query, sources, correlation_context)

    result_rows: List[Dict[str, Optional[dict]]] = []
    for record in anchor_records:
        result_rows.append({anchor_alias: record})

    for cc in query.conflate_clauses:
        new_result_rows = []
        conflate_alias = cc.alias
        conflate_records = source_records[conflate_alias]

        matched_conflate_indices = set()

        for row in result_rows:
            matched_for_this_row = []

            for cf_idx, cf_record in enumerate(conflate_records):
                test_records = dict(row)
                test_records[conflate_alias] = cf_record

                if evaluate_join_predicate(test_records, cc.predicate, query.gloss, correlation_context):
                    matched_for_this_row.append((cf_idx, cf_record))
                    matched_conflate_indices.add(cf_idx)

            if matched_for_this_row:
                for cf_idx, cf_record in matched_for_this_row:
                    new_row = dict(row)
                    new_row[conflate_alias] = cf_record
                    new_result_rows.append(new_row)
            else:
                if cc.flavor in ('PRESERVING LEFT', 'PRESERVING BOTH'):
                    new_row = dict(row)
                    new_row[conflate_alias] = None
                    new_result_rows.append(new_row)

        if cc.flavor in ('PRESERVING RIGHT', 'PRESERVING BOTH'):
            for cf_idx, cf_record in enumerate(conflate_records):
                if cf_idx not in matched_conflate_indices:
                    new_row = {anchor_alias: None, conflate_alias: cf_record}
                    for alias in row.keys():
                        if alias != anchor_alias and alias != conflate_alias:
                            new_row[alias] = None
                    new_result_rows.append(new_row)

        result_rows = new_result_rows

    if query.group_by:
        return compute_aggregations_qualified(result_rows, query, sources, correlation_context)
    elif any(item.agg for item in query.select.items):
        return compute_aggregations_qualified(result_rows, query, sources, correlation_context)
    else:
        return project_qualified_records(result_rows, query.select, query.gloss, sources, correlation_context)


def compute_aggregations_simple(records: List[dict], query: Query,
                                 sources: Dict[str, str] = None,
                                 correlation_context: List[Dict[str, dict]] = None) -> List[dict]:
    """Compute aggregations on simple records (no conflate)."""
    if sources is None:
        sources = {}
    if correlation_context is None:
        correlation_context = []

    select = query.select
    group_by = query.group_by

    agg_calls = []
    agg_keys = []
    for item in select.items:
        if item.agg:
            agg_calls.append(item.agg)
            agg_keys.append(item.output_key())

    has_aggregates = len(agg_calls) > 0

    if not has_aggregates:
        if select.star:
            return records
        result = []
        for record in records:
            row = {}
            from_alias = query.from_alias
            records_dict = {from_alias: record}
            for item in select.items:
                if item.field:
                    key = item.output_key()
                    if isinstance(item.field, ScalarSubquery):
                        value = evaluate_scalar_subquery(item.field, records_dict, query.gloss, sources, correlation_context)
                    elif isinstance(item.field, CanonRef):
                        value = resolve_canon_ref(records_dict, item.field, query.gloss)
                    elif isinstance(item.field, OuterRef):
                        value = resolve_outer_ref(item.field, correlation_context)
                    elif isinstance(item.field, FieldRef):
                        value = get_qualified_field_value(records_dict, item.field)
                    else:
                        value = None
                    row[key] = value
            result.append(row)
        return result

    if group_by is None:
        groups_data = [create_aggregate_state(agg) for agg in agg_calls]
        for record in records:
            from_alias = query.from_alias
            records_dict = {from_alias: record}
            for i, agg in enumerate(agg_calls):
                value = None
                if not agg.is_star:
                    if isinstance(agg.field, CanonRef):
                        value = resolve_canon_ref(records_dict, agg.field, query.gloss)
                    elif isinstance(agg.field, OuterRef):
                        value = resolve_outer_ref(agg.field, correlation_context)
                    elif isinstance(agg.field, FieldRef):
                        value = get_qualified_field_value(records_dict, agg.field)
                groups_data[i].update(value, record)
        results = [state.result() for state in groups_data]
        return [{k: v for k, v in zip(agg_keys, results)}]

    groups = {}
    order_counter = 0

    for record in records:
        from_alias = query.from_alias
        records_dict = {from_alias: record}

        key_values = []
        for field in group_by:
            if isinstance(field, CanonRef):
                value = resolve_canon_ref(records_dict, field, query.gloss)
            elif isinstance(field, OuterRef):
                value = resolve_outer_ref(field, correlation_context)
            else:
                value = get_qualified_field_value(records_dict, field)
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
                if isinstance(agg.field, CanonRef):
                    value = resolve_canon_ref(records_dict, agg.field, query.gloss)
                elif isinstance(agg.field, OuterRef):
                    value = resolve_outer_ref(agg.field, correlation_context)
                elif isinstance(agg.field, FieldRef):
                    value = get_qualified_field_value(records_dict, agg.field)
            agg_states[i].update(value, record)

    sorted_groups = sorted(groups.items(), key=lambda x: x[1][1])

    results = []
    for group_key, (agg_states, _) in sorted_groups:
        agg_results = [state.result() for state in agg_states]
        row = {}

        for i, key in enumerate(agg_keys):
            row[key] = agg_results[i]

        for i, field in enumerate(group_by):
            field_str = field.to_string()
            for item in select.items:
                if item.field and item.field.to_string() == field_str:
                    row[item.output_key()] = group_key.values[i]
                    break

        # Apply HAVING filter
        if query.having:
            if not evaluate_having(query.having, row, agg_results, agg_keys, group_by, group_key, query.gloss, sources, correlation_context, query.from_alias):
                continue

        results.append(row)

    return results


def project_qualified_records(rows: List[Dict[str, Optional[dict]]], select: SelectClause,
                              gloss: Optional[GlossClause] = None,
                              sources: Dict[str, str] = None,
                              correlation_context: List[Dict[str, dict]] = None) -> List[dict]:
    """Project qualified records according to the select clause."""
    if sources is None:
        sources = {}
    if correlation_context is None:
        correlation_context = []

    results = []

    for row in rows:
        result = {}

        for item in select.items:
            if item.alias_star:
                alias = item.alias_star
                record = row.get(alias)
                if record:
                    for key in record.keys():
                        qualified_key = f"{alias}.{key}"
                        result[qualified_key] = record[key]
            elif item.field:
                key = item.output_key()
                if isinstance(item.field, ScalarSubquery):
                    value = evaluate_scalar_subquery(item.field, row, gloss, sources, correlation_context)
                elif isinstance(item.field, CanonRef):
                    if gloss is None:
                        raise EvaluationError(f"LOGQL_ERROR: CANON reference without GLOSS")
                    value = resolve_canon_ref(row, item.field, gloss)
                elif isinstance(item.field, OuterRef):
                    value = resolve_outer_ref(item.field, correlation_context)
                else:
                    value = get_qualified_field_value(row, item.field)
                result[key] = value
            elif item.agg:
                pass

        results.append(result)

    return results


def compute_aggregations_qualified(rows: List[Dict[str, Optional[dict]]], query: Query,
                                   sources: Dict[str, str] = None,
                                   correlation_context: List[Dict[str, dict]] = None) -> List[dict]:
    """Compute aggregations on qualified records."""
    if sources is None:
        sources = {}
    if correlation_context is None:
        correlation_context = []

    select = query.select
    group_by = query.group_by
    gloss = query.gloss

    agg_calls = []
    agg_keys = []
    for item in select.items:
        if item.agg:
            agg_calls.append(item.agg)
            agg_keys.append(item.output_key())

    has_aggregates = len(agg_calls) > 0

    if not has_aggregates:
        return project_qualified_records(rows, select, gloss, sources, correlation_context)

    if group_by is None:
        group = GroupState(agg_calls, gloss)
        for row in rows:
            group.update(row, agg_calls, gloss, sources, correlation_context)
        results = group.get_results()
        return [{k: v for k, v in zip(agg_keys, results)}]

    groups = {}
    order_counter = 0

    for row in rows:
        key_values = []
        for field in group_by:
            if isinstance(field, CanonRef):
                if gloss is None:
                    raise EvaluationError(f"LOGQL_ERROR: CANON reference without GLOSS")
                value = resolve_canon_ref(row, field, gloss)
            elif isinstance(field, OuterRef):
                value = resolve_outer_ref(field, correlation_context)
            else:
                value = get_qualified_field_value(row, field)
            value = normalize_for_grouping(value)
            key_values.append(value)

        group_key = GroupKey(key_values)

        if group_key not in groups:
            groups[group_key] = (GroupState(agg_calls, gloss), order_counter)
            order_counter += 1

        group_state, _ = groups[group_key]
        group_state.update(row, agg_calls, gloss, sources, correlation_context)

    sorted_groups = sorted(groups.items(), key=lambda x: x[1][1])

    results = []
    for group_key, (group_state, _) in sorted_groups:
        agg_results = group_state.get_results()
        row = {}

        for i, key in enumerate(agg_keys):
            row[key] = agg_results[i]

        for i, field in enumerate(group_by):
            field_str = field.to_string()
            for item in select.items:
                if item.field and item.field.to_string() == field_str:
                    row[item.output_key()] = group_key.values[i]
                    break

        # Apply HAVING filter
        if query.having:
            if not evaluate_having(query.having, row, agg_results, agg_keys, group_by, group_key, gloss, sources, correlation_context, query.from_alias):
                continue

        results.append(row)

    return results


def evaluate_having(having: BooleanExpr, row: dict, agg_results: List[Any], agg_keys: List[str],
                    group_by: List, group_key: 'GroupKey', gloss: Optional[GlossClause],
                    sources: Dict[str, str], correlation_context: List[Dict[str, dict]],
                    from_alias: str = None) -> bool:
    """Evaluate a HAVING clause against a grouped result row."""
    # Create a mock records dict for evaluation
    # For HAVING, we need to handle references to aggregate results and group by fields
    # This is a simplified evaluation that handles the common cases

    # Build a proper correlation context with the from_alias
    # The group key values correspond to the GROUP BY fields
    # We need to construct a record that represents the grouped values
    group_record = {}
    for i, field in enumerate(group_by):
        if isinstance(field, FieldRef) and len(field.parts) >= 2:
            # This is a qualified field like b.rid
            alias = field.alias()
            if alias not in group_record:
                group_record[alias] = {}
            # Set the nested value
            value = group_key.values[i]
            for part in field.field_path()[:-1]:
                if part not in group_record[alias]:
                    group_record[alias][part] = {}
            # Handle simple case
            if len(field.field_path()) == 1:
                group_record[alias][field.field_path()[0]] = value

    # Merge the row (aggregate results) into the correlation context
    # The row contains aggregate results like {"COUNT(*)": 2, "b.rid": "r1"}
    # We also need to add the group by fields
    for i, field in enumerate(group_by):
        if isinstance(field, FieldRef):
            field_str = field.to_string()
            if field_str in row:
                # Already in row, make sure it's also accessible via UPTREE
                pass

    # Create a combined context for the subquery
    # The outer context should contain the grouped values
    new_context = correlation_context + [group_record if group_record else row]

    if isinstance(having, BeholdsExpr):
        # For BEHOLDS, we need to evaluate the subquery with correlation
        # The correlation context should include the current group's values
        results = process_query_internal(sources, having.subquery.query, new_context)
        return len(results) > 0

    if isinstance(having, AmongstExpr):
        # For AMONGST, evaluate the value and check against the subquery
        value = evaluate_having_value_expr(having.value, row, agg_results, agg_keys, group_by, group_key, gloss, sources, correlation_context)
        results = process_query_internal(sources, having.subquery.query, new_context)
        for result in results:
            for v in result.values():
                if deep_equal(value, v):
                    return True
        return False

    if isinstance(having, EitherwiseExpr):
        value = evaluate_having_value_expr(having.value, row, agg_results, agg_keys, group_by, group_key, gloss, sources, correlation_context)
        results = process_query_internal(sources, having.subquery.query, new_context)
        if not results:
            return False
        for result in results:
            for v in result.values():
                if compare_values(value, having.op, v):
                    return True
        return False

    if isinstance(having, EverywiseExpr):
        value = evaluate_having_value_expr(having.value, row, agg_results, agg_keys, group_by, group_key, gloss, sources, correlation_context)
        results = process_query_internal(sources, having.subquery.query, new_context)
        if not results:
            return True
        for result in results:
            for v in result.values():
                if not compare_values(value, having.op, v):
                    return False
        return True

    if isinstance(having, Predicate):
        left = evaluate_having_value_expr(having.left, row, agg_results, agg_keys, group_by, group_key, gloss, sources, correlation_context)
        right = evaluate_having_value_expr(having.right, row, agg_results, agg_keys, group_by, group_key, gloss, sources, correlation_context)
        return compare_values(left, having.op, right)

    if isinstance(having, BinaryExpr):
        left_result = evaluate_having(having.left, row, agg_results, agg_keys, group_by, group_key, gloss, sources, correlation_context)
        if having.op == 'OR':
            return left_result or evaluate_having(having.right, row, agg_results, agg_keys, group_by, group_key, gloss, sources, correlation_context)
        elif having.op == 'AND':
            return left_result and evaluate_having(having.right, row, agg_results, agg_keys, group_by, group_key, gloss, sources, correlation_context)

    return False


def evaluate_having_value_expr(value_expr, row: dict, agg_results: List[Any], agg_keys: List[str],
                                group_by: List, group_key: 'GroupKey', gloss: Optional[GlossClause],
                                sources: Dict[str, str], correlation_context: List[Dict[str, dict]]) -> Any:
    """Evaluate a value expression in a HAVING context."""
    if isinstance(value_expr, Literal):
        # Check if this is an aggregate function reference (stored as string like "COUNT(*)")
        if isinstance(value_expr.value, str) and value_expr.value in agg_keys:
            idx = agg_keys.index(value_expr.value)
            return agg_results[idx]
        return value_expr.value

    if isinstance(value_expr, FieldRef):
        # Check if it's an aggregate result reference
        field_str = value_expr.to_string()
        if field_str in agg_keys:
            idx = agg_keys.index(field_str)
            return agg_results[idx]
        # Check if it's a group by field
        for i, field in enumerate(group_by):
            if field.to_string() == field_str:
                return group_key.values[i]
        # Return from row if available
        return row.get(field_str)

    if isinstance(value_expr, OuterRef):
        return resolve_outer_ref(value_expr, correlation_context)

    if isinstance(value_expr, CanonRef):
        if gloss is None:
            raise EvaluationError(f"LOGQL_ERROR: CANON reference without GLOSS")
        return resolve_canon_ref(row, value_expr, gloss)

    if isinstance(value_expr, ScalarSubquery):
        new_context = correlation_context + [row]
        return evaluate_scalar_subquery(value_expr, row, gloss, sources, new_context)

    return None


# Error Output

def output_error(code: str, message: str):
    """Output a structured error to stderr and exit."""
    error_obj = {
        "error": f"LOGQL_ERROR: {message}",
        "code": code
    }
    print(json.dumps(error_obj), file=sys.stderr)
    sys.exit(1)


# Main

def parse_query(query_str: str) -> Query:
    """Parse a query string into a Query object."""
    try:
        lexer = Lexer(query_str)
        tokens = lexer.tokenize()
        parser = Parser(tokens, query_str)
        return parser.parse()
    except (LexerError, ParseError) as e:
        output_error("E_PARSE", str(e))
    except SemanticError as e:
        output_error("E_SEMANTIC", str(e))


def process_query(sources: Dict[str, str], query: Query) -> List[dict]:
    """Process a query against multiple sources."""
    return process_query_internal(sources, query, [])


def main():
    parser = argparse.ArgumentParser(description='LogQL Part 5 - Query NDJSON logs with nested queries')
    parser.add_argument('--log-file', help='Path to NDJSON log file (shorthand for --source logs=<path>)')
    parser.add_argument('--query', required=True, help='SQL-like query string')
    parser.add_argument('--output', help='Output file path (optional)')
    parser.add_argument('--source', action='append', default=[],
                        help='Bind an alias to an NDJSON file (format: alias=path), can be repeated')

    args = parser.parse_args()

    sources: Dict[str, str] = {}

    if args.log_file:
        sources['logs'] = args.log_file

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

    query = parse_query(args.query)

    # Collect all aliases needed
    all_aliases = {query.from_alias}
    for cc in query.conflate_clauses:
        all_aliases.add(cc.alias)

    # Check all aliases are bound
    for alias in all_aliases:
        if alias not in sources:
            output_error("E_SEMANTIC", f"Alias '{alias}' not bound to any source")

    results = process_query(sources, query)

    output_json = json.dumps(results)

    if args.output:
        try:
            with open(args.output, 'w', encoding='utf-8') as f:
                f.write(output_json)
                f.write('\n')
        except IOError as e:
            output_error("E_IO", f"Error writing output file: {e}")
    else:
        print(output_json)


if __name__ == '__main__':
    main()
