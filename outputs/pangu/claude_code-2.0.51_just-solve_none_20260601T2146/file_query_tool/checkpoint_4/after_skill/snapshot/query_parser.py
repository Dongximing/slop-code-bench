"""SQL query parser using sqlparse."""

import re
import sqlparse
from sqlparse.sql import Identifier
from sqlparse.tokens import Keyword

_KEYWORDS = {'AND', 'OR', 'NOT', 'SELECT', 'FROM', 'WHERE', 'GROUP', 'HAVING', 'ORDER',
             'INNER', 'LEFT', 'RIGHT', 'FULL', 'JOIN', 'ON', 'BY', 'ASC', 'DESC',
             'OVER', 'PARTITION', 'ROWS', 'RANGE', 'PRECEDING', 'FOLLOWING', 'CURRENT', 'UNBOUNDED'}

_STOP_WORDS = {'GROUP', 'HAVING', 'ORDER', 'LIMIT', 'OFFSET', 'BY'}


def parse_query(sql: str) -> dict:
    stmt = sqlparse.parse(sql.strip())[0]
    return {
        'select': _parse_select(stmt),
        'from': _parse_from(stmt),
        'where': _extract_tokens(stmt, 'WHERE', _STOP_WORDS),
        'group_by': _parse_token_list(stmt, 'GROUP', _STOP_WORDS),
        'having': _extract_tokens(stmt, 'HAVING', _STOP_WORDS),
        'order_by': _parse_order_by(stmt),
        'limit': _find_number(stmt, 'LIMIT', ['OFFSET']),
        'offset': _find_number(stmt, 'OFFSET', ['LIMIT']),
        'window_functions': _parse_window_functions(stmt),
    }


def _extract_tokens(stmt, keyword: str, stop: set) -> str:
    in_clause = False
    parts = []
    for t in stmt.flatten():
        if t.ttype in Keyword:
            k = t.value.upper()
            if k == keyword:
                in_clause = True
                continue
            elif k in stop:
                in_clause = False
        if in_clause and t.value.strip():
            parts.append(t.value)
    return ' '.join(parts).strip()


def _parse_token_list(stmt, keyword: str, stop: set) -> list:
    items = []
    for t in stmt.flatten():
        if t.ttype in Keyword:
            if t.value.upper() == keyword:
                continue
            if t.value.upper() in stop:
                break
        v = t.value.strip()
        if v and v != ',' and v not in stop:
            items.append(v)
    return items


def _find_number(stmt, keyword: str, stop: set) -> int | None:
    in_clause = False
    for t in stmt.flatten():
        if t.ttype in Keyword:
            k = t.value.upper()
            if k == keyword:
                in_clause = True
                continue
            if k in stop:
                in_clause = False
        if in_clause and t.value.strip():
            val = t.value.strip()
            if val.isdigit() or (val.startswith('-') and val[1:].isdigit()):
                return int(val)
    return None


def _parse_select(stmt) -> tuple:
    selects, distinct = [], False
    in_select = False
    for t in stmt.flatten():
        v = t.value.strip()
        if t.ttype in Keyword:
            if v.upper() == 'SELECT':
                in_select = True
                continue
            elif v.upper() in _STOP_WORDS:
                in_select = False
                continue
        if in_select and v:
            if v.upper() == 'DISTINCT':
                distinct = True
            else:
                selects.append(v)
    return selects, distinct


def _parse_window_functions(stmt) -> list:
    window_funcs, current_expr = [], []
    in_select = False

    for token in stmt.flatten():
        v = token.value.strip()
        if token.ttype in Keyword:
            if v.upper() == 'SELECT':
                in_select = True
                continue
            elif v.upper() in _STOP_WORDS:
                in_select = False
                if current_expr:
                    wf = _extract_window_spec(''.join(current_expr).strip())
                    if wf:
                        window_funcs.append(wf)
                    current_expr = []
                continue

        if in_select:
            if v == ',':
                if current_expr:
                    wf = _extract_window_spec(''.join(current_expr).strip())
                    if wf:
                        window_funcs.append(wf)
                    current_expr = []
            else:
                current_expr.append(v + ' ')

    if current_expr:
        wf = _extract_window_spec(''.join(current_expr).strip())
        if wf:
            window_funcs.append(wf)

    return window_funcs


def _extract_window_spec(expr: str) -> dict | None:
    expr = expr.strip()

    over_empty = re.search(r'\bOVER\s*\(\s*\)', expr, re.IGNORECASE)
    if over_empty:
        func_m = re.match(r'^([^(]+)\s+OVER\s*\(\s*\)', expr, re.IGNORECASE)
        if func_m:
            return {'function': func_m.group(1).strip().upper(), 'partition_by': [], 'order_by': [], 'frame_clause': None}

    over_match = re.search(r'\bOVER\s*\(', expr, re.IGNORECASE)
    if over_match:
        start, depth = over_match.end(), 1
        end = start
        for i, c in enumerate(expr[start:]):
            if c == '(':
                depth += 1
            elif c == ')':
                depth -= 1
                if depth == 0:
                    end = start + i
                    break

        body = expr[start:end].strip()
        func_part = expr[:over_match.start()].strip()
        func_name = (re.match(r'^(.*?)(?:\s+OVER)', func_part, re.IGNORECASE) or type('', None, '')())
        func = func_name.group(1).strip() if func_name else func_part

        return _parse_window_body(body, func)

    return None


def _parse_window_body(window_body: str, func: str) -> dict:
    frame_clause = None

    m = re.search(r'(?:(ROWS|RANGE)\b.*)$', window_body, re.IGNORECASE)
    if m:
        frame_clause = m.group(0).strip()
        window_body_for_parse = window_body[:m.start()].strip()
    else:
        window_body_for_parse = window_body

    parsed = sqlparse.parse(f"SELECT {window_body_for_parse}")[0]
    partition_by, order_by = [], []

    in_p = False
    for t in parsed.flatten():
        v = t.value.strip()
        if t.ttype in Keyword:
            if v.upper() == 'PARTITION':
                in_p = True
                continue
            if v.upper() == 'BY':
                continue
            elif v.upper() in ['ORDER', 'ROWS', 'RANGE']:
                in_p = False
        if in_p and v and v != ',':
            partition_by.append(v.rstrip(','))

    in_o = False
    for t in parsed.flatten():
        v = t.value.strip()
        if t.ttype in Keyword:
            if v.upper() == 'ORDER':
                in_o = True
                continue
            if v.upper() == 'BY':
                continue
            elif v.upper() in ['ROWS', 'RANGE', 'PARTITION']:
                in_o = False
        if in_o:
            if v.upper() == 'DESC' and order_by:
                order_by[-1] = (order_by[-1][0], True)
            elif v.upper() == 'ASC' and order_by:
                order_by[-1] = (order_by[-1][0], False)
            elif v and v != ',':
                order_by.append((v.rstrip(','), False))

    return {'function': func.upper(), 'partition_by': partition_by, 'order_by': order_by, 'frame_clause': frame_clause}


def _parse_from(stmt) -> dict:
    result = {'main_table': None, 'joins': []}
    in_from, in_join, join_type = False, False, 'INNER'

    for token in stmt.tokens:
        if token.ttype in Keyword:
            v = token.value.upper()
            if v == 'FROM':
                in_from, in_join = True, False
            elif v in ['INNER', 'LEFT', 'RIGHT', 'FULL']:
                join_type = v
            elif v == 'JOIN':
                in_join = True
            elif v in {'WHERE', 'GROUP', 'HAVING', 'ORDER', 'LIMIT', 'OFFSET'}:
                in_from = in_join = False
        elif (in_from or in_join) and isinstance(token, Identifier):
            table = token.get_name()
            if in_join:
                result['joins'].append({'table': table, 'type': join_type, 'alias': token.get_alias(), 'on_cols': []})
            elif result['main_table'] is None:
                result['main_table'] = table
    return result


def _parse_order_by(stmt) -> list:
    columns = []
    in_order = False
    for t in stmt.flatten():
        v = t.value.strip().upper() if t.value.strip() else ''
        if t.ttype in Keyword:
            if v == 'ORDER':
                in_order = True
                continue
            if v == 'BY':
                continue
            elif v in ['LIMIT', 'OFFSET']:
                in_order = False
        if in_order:
            if v == 'DESC' and columns:
                columns[-1] = (columns[-1][0], True)
            elif v == 'ASC' and columns:
                columns[-1] = (columns[-1][0], False)
            elif v and v != ',':
                columns.append((t.value.strip(), False))
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
    for pattern in [
        rf'{table1}\.((\w+))\s*=\s*{table2}\.((\w+))',
        rf'{table2}\.((\w+))\s*=\s*{table1}\.((\w+))',
    ]:
        match = re.search(pattern, where_expr, re.IGNORECASE)
        if match:
            return (match.group(1), match.group(3))
    return None
