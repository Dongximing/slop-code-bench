"""SQL query parser using sqlparse."""

import re
import sqlparse
from sqlparse.sql import Identifier
from sqlparse.tokens import Keyword
from typing import Any

_KEYWORDS = {'AND', 'OR', 'NOT', 'SELECT', 'FROM', 'WHERE', 'GROUP', 'HAVING', 'ORDER',
             'LIMIT', 'OFFSET', 'INNER', 'LEFT', 'RIGHT', 'FULL', 'JOIN', 'ON', 'BY', 'ASC', 'DESC',
             'OVER', 'PARTITION', 'ROWS', 'RANGE', 'PRECEDING', 'FOLLOWING', 'CURRENT', 'UNBOUNDED'}


def parse_query(sql: str) -> dict:
    """Parse SQL into structured query plan."""
    stmt = sqlparse.parse(sql.strip())[0]
    return {
        'select': _parse_select(stmt),
        'from': _parse_from(stmt),
        'where': _extract_expression(stmt, 'WHERE', ['GROUP', 'HAVING', 'ORDER', 'LIMIT', 'OFFSET']),
        'group_by': _parse_list(stmt, 'GROUP', ['BY', 'HAVING', 'ORDER', 'LIMIT', 'OFFSET']),
        'having': _extract_expression(stmt, 'HAVING', ['ORDER', 'LIMIT', 'OFFSET']),
        'order_by': _parse_order_by(stmt),
        'limit': _parse_number(stmt, 'LIMIT', ['OFFSET']),
        'offset': _parse_number(stmt, 'OFFSET', ['LIMIT']),
        'window_functions': _parse_window_functions(stmt),
    }


def _extract_expression(stmt, keyword: str, stop_keywords: list) -> str:
    in_clause = False
    parts = []
    for token in stmt.flatten():
        if token.ttype in Keyword:
            if token.value.upper() == keyword:
                in_clause = True
                continue
            elif token.value.upper() in stop_keywords:
                in_clause = False
        if in_clause and token.value.strip():
            parts.append(token.value)
    return ' '.join(parts).strip()


def _parse_list(stmt, keyword: str, stop_keywords: list) -> list:
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


def _parse_number(stmt, keyword: str, stop_keywords: list) -> int | None:
    in_clause = False
    for token in stmt.flatten():
        if token.ttype in Keyword:
            if token.value.upper() == keyword:
                in_clause = True
                continue
            elif token.value.upper() in stop_keywords:
                in_clause = False
        if in_clause and token.value.strip():
            val = token.value.strip()
            if val.isdigit() or (val.startswith('-') and val[1:].isdigit()):
                return int(val)
    return None


def _parse_select(stmt) -> tuple:
    selects = []
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
                selects.append(val)
    return selects, distinct


def _parse_window_functions(stmt) -> list:
    """Extract window function specifications from the SELECT clause."""
    window_funcs = []
    in_select = False
    current_expr = []

    for token in stmt.flatten():
        if token.ttype in Keyword and token.value.upper() == 'SELECT':
            in_select = True
            continue
        elif token.ttype in Keyword and token.value.upper() in ['FROM', 'WHERE', 'GROUP', 'HAVING', 'ORDER', 'LIMIT', 'OFFSET']:
            in_select = False
            current_expr = []
            continue

        if in_select:
            val = token.value.strip()
            if val == ',':
                expr = ''.join(current_expr).strip()
                if expr:
                    window_spec = _extract_window_spec(expr)
                    if window_spec:
                        window_funcs.append(window_spec)
                current_expr = []
            else:
                current_expr.append(val + ' ')

    # Handle last expression
    if current_expr:
        expr = ''.join(current_expr).strip()
        if expr:
            window_spec = _extract_window_spec(expr)
            if window_spec:
                window_funcs.append(window_spec)

    return window_funcs


def _extract_window_spec(expr: str) -> dict | None:
    """Extract window specification from an expression with OVER()."""
    expr = expr.strip()
    over_match = re.search(r'\bOVER\s*\(\s*\)', expr, re.IGNORECASE)
    if over_match:
        # Empty OVER() - window over entire result set
        func_match = re.match(r'^([^(]+)\s+OVER\s*\(\s*\)', expr, re.IGNORECASE)
        if func_match:
            func = func_match.group(1).strip()
            return {
                'function': func.upper(),
                'partition_by': [],
                'order_by': [],
                'frame_clause': None,
                'full_expr': expr
            }

    # Check for OVER with spec
    over_start = re.search(r'\bOVER\s*\(', expr, re.IGNORECASE)
    if over_start:
        # Extract content between OVER( and closing )
        start_pos = over_start.end()
        paren_count = 1
        end_pos = start_pos
        for i, c in enumerate(expr[start_pos:]):
            if c == '(':
                paren_count += 1
            elif c == ')':
                paren_count -= 1
                if paren_count == 0:
                    end_pos = start_pos + i
                    break

        window_body = expr[start_pos:end_pos].strip()
        func_part = expr[:over_start.start()].strip()

        # Parse the function part (including parentheses)
        func_match = re.match(r'^(.*?)(?:\s+OVER)', func_part, re.IGNORECASE)
        func = func_match.group(1).strip() if func_match else func_part

        return _parse_window_body(window_body, func.strip())

    return None


def _parse_window_body(window_body: str, func: str) -> dict:
    """Parse the body of a window specification."""
    partition_by = []
    order_by = []
    frame_clause = None

    body_upper = window_body.upper()

    # Extract frame clause (everything after ORDER BY or at the end)
    frame_match = re.search(r'(?:(ROWS|RANGE)\b.*)$', window_body, re.IGNORECASE)
    if frame_match:
        frame_clause = frame_match.group(0).strip()
        # Remove frame clause from body for other parsing
        window_body_for_parse = window_body[:frame_match.start()].strip()
    else:
        window_body_for_parse = window_body

    # Parse PARTITION BY
    in_partition = False
    parts = []
    for token in sqlparse.parse(f"SELECT {window_body_for_parse}")[0].flatten():
        val = token.value.strip()
        if token.ttype in Keyword and val.upper() == 'PARTITION':
            in_partition = True
            continue
        elif token.ttype in Keyword and val.upper() == 'BY':
            continue
        elif token.ttype in Keyword and val.upper() in ['ORDER', 'ROWS', 'RANGE']:
            in_partition = False
        if in_partition and val and val != ',':
            parts.append(val.rstrip(','))
    partition_by = parts

    # Parse ORDER BY
    in_order = False
    order_parts = []
    for token in sqlparse.parse(f"SELECT {window_body_for_parse}")[0].flatten():
        val = token.value.strip()
        if token.ttype in Keyword and val.upper() == 'ORDER':
            in_order = True
            continue
        if token.ttype in Keyword and val.upper() == 'BY':
            continue
        if token.ttype in Keyword and val.upper() in ['ROWS', 'RANGE', 'PARTITION']:
            in_order = False
        if in_order and val:
            if val.upper() == 'DESC':
                if order_parts:
                    order_parts[-1] = (order_parts[-1][0], True)
            elif val.upper() == 'ASC':
                if order_parts:
                    order_parts[-1] = (order_parts[-1][0], False)
            elif val != ',':
                order_parts.append((val.rstrip(','), False))
    order_by = order_parts

    return {
        'function': func.upper(),
        'partition_by': partition_by,
        'order_by': order_by,
        'frame_clause': frame_clause,
        'full_expr': window_body_for_parse + (' ' + frame_clause if frame_clause else '')
    }


def _parse_from(stmt) -> dict:
    result = {'main_table': None, 'joins': []}
    in_from = False
    in_join = False
    join_type = 'INNER'
    current_join = None

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


def _parse_order_by(stmt) -> list:
    columns = []
    in_order = False
    for token in stmt.flatten():
        if token.ttype in Keyword and token.value.upper() == 'ORDER':
            in_order = True
            continue
        if token.ttype in Keyword and token.value.upper() == 'BY':
            continue
        if token.ttype in Keyword and token.value.upper() in ['LIMIT', 'OFFSET']:
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


def extract_alias(expr: str) -> str | None:
    expr = expr.strip()
    if ' as ' in expr.lower():
        return expr.split('AS')[-1].strip().strip('"').strip("'")
    parts = expr.rsplit(' ', 1)
    if len(parts) == 2:
        alias = parts[1].strip('"').strip("'")
        if alias.upper() not in _KEYWORDS:
            try:
                float(alias)
                return None
            except ValueError:
                return alias
    return None


def find_join_key(where_expr: str, table1: str, table2: str) -> tuple | None:
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