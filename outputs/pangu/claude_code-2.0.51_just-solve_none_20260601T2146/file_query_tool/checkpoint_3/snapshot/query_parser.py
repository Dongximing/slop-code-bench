"""SQL query parser using sqlparse."""

import re
import sqlparse
from sqlparse.sql import Identifier
from sqlparse.tokens import Keyword, Number
from typing import Optional, Tuple, List, Dict, Any


def parse_query(sql: str) -> Dict[str, Any]:
    """Parse SQL into structured query plan."""
    parsed = sqlparse.parse(sql.strip())
    if not parsed:
        raise ValueError("No valid SQL statement found")

    stmt = parsed[0]
    return {
        'select': _extract_select(stmt),
        'from': _extract_from(stmt),
        'where': _extract_where(stmt),
        'group_by': _extract_group_by(stmt),
        'having': _extract_having(stmt),
        'order_by': _extract_order_by(stmt),
        'limit': _extract_limit(stmt),
        'offset': _extract_offset(stmt),
    }


def _extract_clause(stmt, clause_name: str, stop_keywords: List[str]) -> str:
    """Extract a clause expression by name."""
    in_clause = False
    clause_parts = []

    for token in stmt.flatten():
        if token.ttype in Keyword:
            if token.value.upper() == clause_name:
                in_clause = True
                continue
            elif token.value.upper() in stop_keywords:
                in_clause = False

        if in_clause and token.value.strip():
            clause_parts.append(token.value)

    return ' '.join(clause_parts).strip()


def _extract_keywords_list(stmt, keyword: str, stop_keywords: List[str]) -> List[str]:
    """Extract a list of items from a keyword clause."""
    items = []
    in_list = False

    for token in stmt.flatten():
        if token.ttype in Keyword:
            if token.value.upper() == keyword:
                in_list = True
                continue
            elif token.value.upper() in stop_keywords:
                in_list = False

        if in_list and token.value.strip() and token.value.strip() != ',':
            items.append(token.value.strip())

    return items


def _extract_number_clause(stmt, keyword: str, stop_keywords: List[str]) -> Optional[int]:
    """Extract a numeric value from a clause."""
    in_clause = False

    for token in stmt.flatten():
        if token.ttype in Keyword:
            if token.value.upper() == keyword:
                in_clause = True
                continue
            elif token.value.upper() in stop_keywords:
                in_clause = False

        if in_clause and token.value.strip():
            # Try to parse as number
            try:
                val = token.value.strip()
                if val.isdigit() or (val.startswith('-') and val[1:].isdigit()):
                    return int(val)
            except (ValueError, AttributeError):
                pass

    return None


def _extract_select(stmt) -> Tuple[List[str], bool]:
    """Extract SELECT clause expressions and DISTINCT flag."""
    select_exprs = []
    distinct = False
    in_select = False

    for token in stmt.flatten():
        if token.ttype in Keyword and token.value.upper() == 'SELECT':
            in_select = True
            continue
        elif token.ttype in Keyword and token.value.upper() in ['FROM', 'WHERE', 'GROUP', 'HAVING', 'ORDER', 'LIMIT', 'OFFSET']:
            in_select = False
            continue

        if in_select and token.value.strip():
            val = token.value.strip()
            if val.upper() == 'DISTINCT':
                distinct = True
            else:
                select_exprs.append(val)

    return select_exprs, distinct


def _extract_from(stmt) -> Dict[str, Any]:
    """Extract FROM clause with JOINs."""
    result = {'main_table': None, 'joins': []}
    in_from = False
    in_join = False
    current_join = None
    join_type = 'INNER'

    for token in stmt.tokens:
        if token.ttype in Keyword:
            val = token.value.upper()
            if val == 'FROM':
                in_from = True
                in_join = False
            elif val in ['INNER', 'LEFT', 'RIGHT', 'FULL']:
                join_type = val
            elif val == 'JOIN':
                in_join = True
            elif val == 'ON':
                if current_join:
                    current_join['in_on'] = True
            elif val in ['WHERE', 'GROUP', 'HAVING', 'ORDER', 'LIMIT', 'OFFSET']:
                in_from = False
                in_join = False
                if current_join:
                    current_join['in_on'] = False

        elif in_from or in_join:
            val = token.value.strip()
            if val and val != ',' and isinstance(token, Identifier):
                table_name = token.get_name()
                if in_join:
                    result['joins'].append({
                        'table': table_name,
                        'type': join_type,
                        'alias': token.get_alias(),
                        'on_cols': []
                    })
                    current_join = result['joins'][-1]
                elif result['main_table'] is None:
                    result['main_table'] = table_name

    return result


def _extract_where(stmt) -> str:
    """Extract WHERE clause expression."""
    return _extract_clause(stmt, 'WHERE', ['GROUP', 'HAVING', 'ORDER', 'LIMIT', 'OFFSET'])


def _extract_having(stmt) -> str:
    """Extract HAVING clause expression."""
    return _extract_clause(stmt, 'HAVING', ['ORDER', 'LIMIT', 'OFFSET'])


def _extract_group_by(stmt) -> List[str]:
    """Extract GROUP BY clause."""
    return _extract_keywords_list(stmt, 'GROUP', ['BY', 'HAVING', 'ORDER', 'LIMIT', 'OFFSET'])


def _extract_order_by(stmt) -> List[Tuple[str, bool]]:
    """Extract ORDER BY clause."""
    columns = []
    in_order = False

    for token in stmt.flatten():
        if token.ttype in Keyword and token.value.upper() == 'ORDER':
            in_order = True
            continue
        elif token.ttype in Keyword and token.value.upper() == 'BY':
            continue
        elif token.ttype in Keyword and token.value.upper() in ['LIMIT', 'OFFSET']:
            in_order = False

        if in_order and token.value.strip():
            val = token.value.strip().upper()
            if val == 'DESC':
                if columns:
                    columns[-1] = (columns[-1][0], True)
            elif val == 'ASC':
                if columns:
                    columns[-1] = (columns[-1][0], False)
            elif val != ',':
                columns.append((token.value.strip(), False))

    return columns


def _extract_limit(stmt) -> Optional[int]:
    """Extract LIMIT clause."""
    return _extract_number_clause(stmt, 'LIMIT', ['OFFSET'])


def _extract_offset(stmt) -> Optional[int]:
    """Extract OFFSET clause."""
    return _extract_number_clause(stmt, 'OFFSET', ['LIMIT'])


KEYWORDS = {'AND', 'OR', 'NOT', 'SELECT', 'FROM', 'WHERE', 'GROUP', 'HAVING', 'ORDER',
            'LIMIT', 'OFFSET', 'INNER', 'LEFT', 'RIGHT', 'FULL', 'JOIN', 'ON', 'BY', 'ASC', 'DESC'}


def extract_alias(expr: str) -> Optional[str]:
    """Extract alias from expression if present."""
    expr = expr.strip()

    if ' as ' in expr.lower():
        return expr.split('AS')[-1].strip().strip('"').strip("'")

    parts = expr.rsplit(' ', 1)
    if len(parts) == 2:
        potential_alias = parts[1].strip('"').strip("'")
        if potential_alias.upper() not in KEYWORDS:
            try:
                float(potential_alias)
                return None
            except ValueError:
                return potential_alias
    return None


def find_join_key(where_expr: str, table1: str, table2: str) -> Optional[Tuple[str, str]]:
    """Find join key from WHERE or ON clause."""
    if not where_expr:
        return None

    patterns = [
        rf'{table1}\\.(\\w+)\\s*=\\s*{table2}\\.(\\w+)',
        rf'{table2}\\.(\\w+)\\s*=\\s*{table1}\\.(\\w+)',
    ]

    for pattern in patterns:
        match = re.search(pattern, where_expr, re.IGNORECASE)
        if match:
            return (match.group(1), match.group(2))

    return None
