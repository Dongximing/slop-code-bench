#!/usr/bin/env python3
"""
LogQL - Part 1: Parsing, Filtering, Projection
A command-line program that reads NDJSON logs, parses SQL-like queries,
filters rows with boolean logic, and returns selected fields as JSON.
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
    IDENT = "IDENT"
    STRING = "STRING"
    NUMBER = "NUMBER"
    BOOL = "BOOL"   # true or false
    NULL = "NULL"   # null
    OP_EQ = "="
    OP_NE = "!="
    OP_LT = "<"
    OP_LE = "<="
    OP_GT = ">"
    OP_GE = ">="
    DOT = "."
    EOF = "EOF"
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
    FROM = "FROM"
    INTERSECTING = "INTERSECTING"
    PRESERVING = "PRESERVING"
    LEFT = "LEFT"
    RIGHT = "RIGHT"
    BOTH = "BOTH"
    UPON = "UPON"


@dataclass
class Token:
    type: TokenType
    value: str
    position: int = 0

    def __repr__(self):
        return f"Token({self.type.name}, {repr(self.value)})"


class Tokenizer:
    """Converts query string into tokens."""

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
            elif ch == '*' or ch == ',':
                tokens.append(self.make_token(
                    TokenType.STAR if ch == '*' else TokenType.COMMA, ch))
                self.consume()
            elif ch == '(':
                tokens.append(self.make_token(TokenType.LPAREN, "("))
                self.consume()
            elif ch == ')':
                tokens.append(self.make_token(TokenType.RPAREN, ")"))
                self.consume()
            elif ch == '.':
                tokens.append(self.make_token(TokenType.DOT, "."))
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
# AST Nodes
# =============================================================================

@dataclass
class FieldRef:
    parts: List[str]

    @property
    def key(self) -> str:
        return ".".join(self.parts)

    @property
    def alias(self) -> Optional[str]:
        """Get the alias part if this is a qualified field ref."""
        return self.parts[0] if self.parts else None

    def __hash__(self):
        return hash(self.key)

    def __eq__(self, other):
        return isinstance(other, FieldRef) and self.parts == other.parts


@dataclass
class AggregateCall:
    func: TokenType  # COUNT, SUM, AVG, MIN, MAX, UNIQUE
    arg: Optional[FieldRef]  # None for COUNT(*)
    as_alias: Optional[str] = None

    @property
    def canonical_key(self) -> str:
        """Get the canonical output key for this aggregate."""
        func_name = self.func.name
        if self.arg is None:
            return f"{func_name}(*)"
        return f"{func_name}({self.arg.key})"


@dataclass
class SelectItem:
    """A single item in the SELECT list."""
    field_ref: Optional[FieldRef] = None  # For regular field references
    aggregate: Optional[AggregateCall] = None  # For aggregate calls
    as_alias: Optional[str] = None  # Optional AS alias
    is_star_expansion: bool = False  # For alias.* expansion (e.g., "a.*")

    @property
    def output_key(self) -> str:
        """Get the output key for this select item."""
        if self.as_alias:
            return self.as_alias
        if self.aggregate:
            return self.aggregate.canonical_key
        return self.field_ref.key

    @property
    def is_aggregate(self) -> bool:
        return self.aggregate is not None

    def get_field_refs(self) -> List[FieldRef]:
        """Get all field references used by this item."""
        refs = []
        if self.field_ref:
            refs.append(self.field_ref)
        if self.aggregate and self.aggregate.arg:
            refs.append(self.aggregate.arg)
        return refs


@dataclass
class SelectList:
    items: List[SelectItem]
    is_star: bool = False
    star_expansions: List[str] = field(default_factory=list)  # Aliases for alias.* expansion

    def has_aggregates(self) -> bool:
        return any(item.is_aggregate for item in self.items)

    def get_aggregate_items(self) -> List[SelectItem]:
        return [item for item in self.items if item.is_aggregate]

    def get_non_aggregate_items(self) -> List[SelectItem]:
        return [item for item in self.items if not item.is_aggregate]

    def get_all_field_refs(self) -> List[FieldRef]:
        refs = []
        for item in self.items:
            refs.extend(item.get_field_refs())
        return refs

    def has_star_expansions(self) -> bool:
        return len(self.star_expansions) > 0


@dataclass
class BooleanExpr:
    pass


@dataclass
class BinaryExpr(BooleanExpr):
    op_token: Token   # AND or OR
    left: BooleanExpr
    right: BooleanExpr


@dataclass
class ParenExpr(BooleanExpr):
    inner: BooleanExpr


@dataclass
class Predicate(BooleanExpr):
    field_ref: FieldRef
    op_token: Token   # comparison operator
    literal: Any


@dataclass
class Query:
    select_list: SelectList
    where_expr: Optional[BooleanExpr] = None
    group_by: Optional[List[FieldRef]] = None  # GROUP BY fields
    conflate_clauses: List['ConflateClause'] = field(default_factory=list)


@dataclass
class Conjunct:
    """A single equality predicate in a join condition."""
    left: FieldRef   # qualified field ref from one alias
    right: FieldRef  # qualified field ref from another alias


@dataclass
class JoinPred:
    """AND-only list of conjuncts."""
    conjuncts: List[Conjunct]


class JoinFlavor(Enum):
    INTERSECTING = "intersecting"  # inner join
    PRESERVING_LEFT = "preserving_left"  # left outer
    PRESERVING_RIGHT = "preserving_right"  # right outer
    PRESERVING_BOTH = "preserving_both"  # full outer


@dataclass
class ConflateClause:
    """A CONFLATE clause that joins another source."""
    alias: str              # alias to conflate with
    flavor: JoinFlavor      # join flavor (default INTERSECTING)
    join_pred: JoinPred     # join predicates


# =============================================================================
# Errors
# =============================================================================

class LogQLError(Exception):
    def __init__(self, message: str, position: Optional[int] = None):
        self.message = message
        self.position = position
        super().__init__(message)


# =============================================================================
# Parser
# =============================================================================

class Parser:
    """Recursive-descent parser."""

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

        from_alias = self.consume(TokenType.IDENT)
        if from_alias.value.lower() != "logs":
            # Support any alias for FROM
            pass

        where_expr = None
        if self.peek().type == TokenType.WHERE:
            self.consume(TokenType.WHERE)
            where_expr = self.parse_boolean_expr()

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
        return Query(select_list, where_expr, group_by, conflate_clauses)

    def parse_select_list(self) -> SelectList:
        if self.peek().type == TokenType.STAR:
            self.consume(TokenType.STAR)
            return SelectList([], is_star=True)

        fields = []
        seen: Set[str] = set()
        star_expansions = []

        while True:
            item = self.parse_select_item()

            # Check for alias.* expansion
            if item.field_ref and item.field_ref.key.endswith('.*'):
                alias = item.field_ref.key[:-2]  # Remove ".*"
                star_expansions.append(alias)
                item.is_star_expansion = True

            # For items with AS alias, use the alias for duplicate checking
            output_key = item.output_key
            if output_key in seen:
                raise LogQLError(f"Duplicate output key '{output_key}'")
            seen.add(output_key)

            fields.append(item)

            if self.peek().type != TokenType.COMMA:
                break
            self.consume(TokenType.COMMA)

        return SelectList(fields, is_star=False, star_expansions=star_expansions)

    def parse_field_ref(self) -> FieldRef:
        parts = []
        while self.peek().type == TokenType.IDENT:
            ident = self.consume(TokenType.IDENT)
            parts.append(ident.value)
            if self.peek().type == TokenType.DOT:
                self.consume()  # consume '.'
            else:
                break
        if not parts:
            raise LogQLError("Expected identifier", self.peek().position)
        return FieldRef(parts)

    def parse_boolean_expr(self, min_precedence: int = 0) -> BooleanExpr:
        # Parse left-hand side
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

        field_ref = self.parse_field_ref()
        op_tok = self.peek()

        if op_tok.type not in (TokenType.OP_EQ, TokenType.OP_NE, TokenType.OP_LT,
                                TokenType.OP_LE, TokenType.OP_GT, TokenType.OP_GE):
            raise LogQLError(f"Expected comparison operator, got {op_tok.type.name}", op_tok.position)
        self.consume()

        literal = self.parse_literal()
        return Predicate(field_ref=field_ref, op_token=op_tok, literal=literal)

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
    """Unescape \" and \\ in a string."""
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


def compare_values(left: Any, op: Token, right: Any) -> bool:
    """Compare two values according to LogQL rules."""
    op_type = op.type

    # Type mismatch: always false
    if type(left) != type(right) and not (left is None or right is None):
        # Special case: int and float can compare
        if not (isinstance(left, (int, float)) and isinstance(right, (int, float))):
            return False

    # null handling
    if left is None or right is None:
        if op_type == TokenType.OP_EQ:
            return left is None and right is None
        elif op_type == TokenType.OP_NE:
            return not (left is None and right is None)
        else:
            # <, >, <=, >= with null are false
            return False

    # Objects and arrays
    if isinstance(left, (dict, list)):
        if op_type == TokenType.OP_EQ:
            return left is right or left == right
        elif op_type == TokenType.OP_NE:
            return left != right
        else:
            return False

    # Booleans - only = and != allowed
    if isinstance(left, bool):
        if op_type == TokenType.OP_EQ:
            return left == right
        elif op_type == TokenType.OP_NE:
            return left != right
        else:
            return False

    # Numbers (int or float)
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


def evaluate(expr: BooleanExpr, record: Dict[str, Any]) -> bool:
    """Evaluate a boolean expression against a record."""
    if isinstance(expr, BinaryExpr):
        left_val = evaluate(expr.left, record)
        # Short-circuit evaluation
        if expr.op_token.type == TokenType.AND:
            if not left_val:
                return False
            return evaluate(expr.right, record)
        elif expr.op_token.type == TokenType.OR:
            if left_val:
                return True
            return evaluate(expr.right, record)

    elif isinstance(expr, ParenExpr):
        return evaluate(expr.inner, record)

    elif isinstance(expr, Predicate):
        field_val = get_nested(record, expr.field_ref.parts)
        return compare_values(field_val, expr.op_token, expr.literal)

    return False


def project_record(record: Dict[str, Any], select_list: SelectList) -> Dict[str, Any]:
    """Extract selected fields from a record."""
    if select_list.is_star:
        return dict(record)

    result = {}
    for field_ref in select_list.fields:
        result[field_ref.key] = get_nested(record, field_ref.parts)
    return result


# =============================================================================
# NDJSON Reader
# =============================================================================

def read_ndjson(filepath: str) -> List[Dict[str, Any]]:
    """Read NDJSON file, ignoring blank lines."""
    records = []
    with open(filepath, 'r', encoding='utf-8') as f:
        for line_num, line in enumerate(f, 1):
            line = line.rstrip('\n\r')
            # Skip blank lines and lines with only whitespace
            if not line or line.isspace():
                continue
            try:
                record = json.loads(line)
                records.append(record)
            except json.JSONDecodeError as e:
                raise LogQLError(f"Invalid JSON at line {line_num}: {e}", line_num)
    return records


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
    parser.add_argument("--log-file", help="Path to NDJSON file (shorthand for --source logs=<path>)")
    parser.add_argument("--source", action="append", help="Bind an alias to an NDJSON file (--source <alias>=<path>)")
    return parser.parse_args()


def main():
    args = parse_args()

    try:
        # Build source alias mapping
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
                    raise LogQLError(f"Duplicate alias binding: '{alias}'")
                sources[alias] = path

        if not sources:
            raise LogQLError("At least one source is required (use --source or --log-file)")

        # Tokenize
        tokenizer = Tokenizer(args.query)
        tokens = tokenizer.tokenize()

        # Parse
        parser = Parser(tokens)
        query = parser.parse_query()

        # Read all source files
        records = {}
        for alias, path in sources.items():
            records[alias] = read_ndjson(path)

        # Filter and project
        results = []
        for record in records:
            if query.where_expr is None or evaluate(query.where_expr, record):
                results.append(project_record(record, query.select_list))

        # Output
        output_json = json.dumps(results, ensure_ascii=False)

        if args.output:
            with open(args.output, 'w', encoding='utf-8') as f:
                f.write(output_json)
        else:
            print(output_json)

    except LogQLError as e:
        error_obj = {"error": e.message}
        if e.position is not None:
            error_obj["position"] = e.position
        json.dump(error_obj, sys.stderr, ensure_ascii=False)
        sys.stderr.write("\n")
        sys.exit(1)
    except Exception as e:
        error_obj = {"error": str(e)}
        json.dump(error_obj, sys.stderr, ensure_ascii=False)
        sys.stderr.write("\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
