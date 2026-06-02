"""SQL query parser using sqlparse with CTE and subquery support."""

import re
import sqlparse
from sqlparse.sql import Identifier, IdentifierList, Parenthesis, TokenList
from sqlparse.tokens import Keyword, TokenType

_KEYWORDS = {'AND', 'OR', 'NOT', 'SELECT', 'FROM', 'WHERE', 'GROUP', 'HAVING', 'ORDER',
             'INNER', 'LEFT', 'RIGHT', 'FULL', 'JOIN', 'ON', 'BY', 'ASC', 'DESC',
             'OVER', 'PARTITION', 'ROWS', 'RANGE', 'PRECEDING', 'FOLLOWING', 'CURRENT', 'UNBOUNDED',
             'WITH', 'RECURSIVE', 'AS', 'EXISTS', 'FILTER'}

_STOP_WORDS = {'GROUP', 'HAVING', 'ORDER', 'LIMIT', 'OFFSET', 'BY'}


def parse_query(sql: str) -> dict:
    """Parse SQL query with CTE and subquery support."""
    stmt = sqlparse.parse(sql.strip())
    if not stmt:
        raise ValueError("Empty query")
    return _parse_statement(stmt[0])


def _parse_statement(stmt) -> dict:
    """Parse a statement, handling WITH clauses first."""
    # Check for WITH clause
    ctes = []
    recursive = False
    is_recursive = False

    tokens = list(stmt.flatten())

    # Look for WITH at the start
    with_pos = None
    for i, t in enumerate(tokens):
        if t.ttype in Keyword and t.value.upper() == 'WITH':
            with_pos = i
            break

    if with_pos is not None:
        # Extract the WITH clause content
        # Find where WITH ends (before main SELECT or other keywords)
        cte_tokens = []
        brace_depth = 0
        in_string = False
        string_char = None

        for j in range(with_pos + 1, len(tokens)):
            t = tokens[j]

            # Handle string literals
            if not in_string and t.value in ("'", '"'):
                in_string = True
                string_char = t.value
            elif in_string and t.value == string_char:
                in_string = False

            if not in_string:
                if t.value == '(':
                    brace_depth += 1
                elif t.value == ')':
                    brace_depth -= 1

            # Check for RECURSIVE keyword
            if not in_string and brace_depth == 0:
                if t.ttype in Keyword and t.value.upper() == 'RECURSIVE':
                    recursive = True
                    is_recursive = True
                    continue
                # End of WITH clause when we hit SELECT or other main query keywords
                # that are not part of a CTE definition
                if t.ttype in Keyword and t.value.upper() == 'SELECT':
                    break
                # If we hit a keyword that could be the start of main query
                if t.ttype in Keyword and t.value.upper() in _STOP_WORDS:
                    break

            cte_tokens.append(t)

        if cte_tokens:
            ctes = _parse_cte_definitions([t.value for t in cte_tokens], recursive)

    # Parse the main query parts
    return {
        'ctes': ctes,
        'recursive': recursive,
        'select': _parse_select(stmt),
        'from': _parse_from(stmt),
        'where': _extract_tokens(stmt, 'WHERE', _STOP_WORDS),
        'group_by': _parse_token_list(stmt, 'GROUP', _STOP_WORDS),
        'having': _extract_tokens(stmt, 'HAVING', _STOP_WORDS),
        'order_by': _parse_order_by(stmt),
        'limit': _find_number(stmt, 'LIMIT', ['OFFSET']),
        'offset': _find_number(stmt, 'OFFSET', ['LIMIT']),
        'window_functions': _parse_window_functions(stmt),
        'subqueries': _extract_subqueries(stmt),
    }


def _parse_cte_definitions(tokens: list, recursive: bool) -> list:
    """Parse CTE definitions from tokens."""
    ctes = []
    i = 0

    while i < len(tokens):
        token = tokens[i].strip()
        if not token:
            i += 1
            continue

        # Skip RECURSIVE keyword if present after WITH
        if recursive and i == 0 and token.upper() == 'RECURSIVE':
            i += 1
            continue

        # CTE name
        cte_name = token
        i += 1

        # Check for column list in parentheses
        columns = []
        if i < len(tokens) and tokens[i].strip() == '(':
            i += 1
            while i < len(tokens) and tokens[i].strip() != ')':
                col = tokens[i].strip().rstrip(',')
                if col and col != ',':
                    columns.append(col)
                i += 1
            if i < len(tokens) and tokens[i].strip() == ')':
                i += 1

        # AS keyword
        if i < len(tokens) and tokens[i].strip().upper() == 'AS':
            i += 1

        # Subquery in parentheses
        if i < len(tokens) and tokens[i].strip() == '(':
            # Find matching closing parenthesis
            subquery_str, end_i = _extract_parenthesized(tokens, i)
            if subquery_str:
                ctes.append({
                    'name': cte_name,
                    'columns': columns,
                    'query': subquery_str,
                    'recursive': recursive and cte_name in _detect_recursive_refs(subquery_str, cte_name)
                })
                i = end_i
                continue

        i += 1

    return ctes


def _extract_parenthesized(tokens: list, start: int) -> tuple:
    """Extract content within parentheses, handling nested parentheses."""
    depth = 1
    i = start + 1
    start_content = i

    while i < len(tokens) and depth > 0:
        if tokens[i].strip() == '(':
            depth += 1
        elif tokens[i].strip() == ')':
            depth -= 1
        i += 1

    # Return the content inside parentheses and the position after closing
    content = ''.join(tokens[start_content:i-1])
    return content.strip(), i


def _detect_recursive_refs(subquery: str, cte_name: str) -> bool:
    """Check if a CTE definition contains a self-reference (for recursive CTEs)."""
    # Simple check: see if the CTE name appears in the recursive member
    # For proper parsing, we need to identify the UNION/UNION ALL
    subquery_upper = subquery.upper()

    # Look for UNION or UNION ALL
    union_pos = -1
    for keyword in ['UNION ALL', 'UNION']:
        pos = subquery_upper.find(keyword)
        if pos != -1:
            union_pos = pos
            break

    if union_pos == -1:
        return False

    # Check if the recursive member references the CTE name
    recursive_part = subquery[union_pos:]
    # Look for FROM cte_name in the recursive part
    pattern = r'\b' + re.escape(cte_name) + r'\b'
    return bool(re.search(pattern, recursive_part, re.IGNORECASE))


def _extract_tokens(stmt, keyword: str, stop: set) -> str:
    """Extract tokens after a keyword until a stop word."""
    in_clause = False
    parts = []
    brace_depth = 0
    in_string = False
    string_char = None

    for t in stmt.flatten():
        val = t.value.strip()

        # Handle string literals
        if not in_string and val in ("'", '"'):
            in_string = True
            string_char = val
        elif in_string and val == string_char:
            in_string = False

        if not in_string:
            if t.ttype in Keyword:
                k = t.value.upper()
                if k == keyword:
                    in_clause = True
                    continue
                elif k in stop:
                    in_clause = False
            if val in ('(', ')'):
                brace_depth += 1 if val == '(' else -1

        if in_clause and t.value.strip():
            parts.append(t.value)

    return ' '.join(parts).strip()


def _parse_token_list(stmt, keyword: str, stop: set) -> list:
    """Parse a list of tokens after a keyword."""
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
    """Find a number after a keyword."""
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
            try:
                return int(val)
            except ValueError:
                pass
    return None


def _parse_select(stmt) -> tuple:
    """Parse SELECT clause, handling subqueries."""
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
                selects.append(t.value)

    # Process each select item to handle subqueries and extract aliases
    processed = []
    for expr in selects:
        expr = expr.strip().rstrip(',')
        if expr:
            processed.append(expr)

    return processed, distinct


def _parse_window_functions(stmt) -> list:
    """Parse window functions, including those with FILTER clauses."""
    window_funcs = []
    current_expr = []
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
    """Extract window specification, including FILTER clause."""
    expr = expr.strip()

    # Check for FILTER clause
    filter_match = re.search(r'\bFILTER\s*\(\s*WHERE\s', expr, re.IGNORECASE)
    filter_where = None

    if filter_match:
        # Extract the FILTER (WHERE ...) part
        start = filter_match.end() - len('WHERE ')
        depth = 1
        i = start + len('WHERE ') - 1
        while i < len(expr) and depth > 0:
            if expr[i] == '(':
                depth += 1
            elif expr[i] == ')':
                depth -= 1
            i += 1
        filter_where = expr[start + len('WHERE '):i-1].strip()
        # Remove FILTER clause from expr for further processing
        expr = expr[:filter_match.start()].strip() + expr[i-1:]

    over_empty = re.search(r'\bOVER\s*\(\s*\)', expr, re.IGNORECASE)
    if over_empty:
        func_m = re.match(r'^([^(]+)\s+OVER\s*\(\s*\)', expr, re.IGNORECASE)
        if func_m:
            return {'function': func_m.group(1).strip().upper(), 'partition_by': [], 'order_by': [], 'frame_clause': None, 'filter_where': filter_where}

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

        return _parse_window_body(body, func, filter_where)

    return None


def _parse_window_body(window_body: str, func: str, filter_where: str = None) -> dict:
    """Parse window body with FILTER clause support."""
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

    return {'function': func.upper(), 'partition_by': partition_by, 'order_by': order_by,
            'frame_clause': frame_clause, 'filter_where': filter_where}


def _parse_from(stmt) -> dict:
    """Parse FROM clause, handling derived tables (subqueries)."""
    result = {'main_table': None, 'joins': [], 'derived_tables': []}
    in_from, in_join, join_type = False, False, 'INNER'

    tokens = list(stmt.tokens)

    for idx, token in enumerate(tokens):
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
        elif isinstance(token, Parenthesis):
            # This is a subquery in FROM clause (derived table)
            if in_from and result['main_table'] is None:
                subquery_sql = token.value.strip()
                result['main_table'] = None  # We'll use derived table
                result['derived_tables'].append({
                    'alias': _get_parenthesis_alias(token),
                    'subquery': subquery_sql
                })
            elif in_join:
                subquery_sql = token.value.strip()
                result['joins'].append({
                    'table': None,
                    'type': join_type,
                    'alias': _get_parenthesis_alias(token),
                    'on_cols': [],
                    'is_subquery': True,
                    'subquery': subquery_sql
                })
        elif isinstance(token, Identifier):
            table = token.get_name()
            if in_join:
                result['joins'].append({'table': table, 'type': join_type, 'alias': token.get_alias(), 'on_cols': []})
            elif result['main_table'] is None:
                result['main_table'] = table

    return result


def _get_parenthesis_clause(token) -> str:
    """Extract alias from a Parenthesis token."""
    # Look at the token after the parenthesis for alias
    return None


def _get_parenthesis_alias(token) -> str | None:
    """Get alias from a parenthesis token."""
    # Check token's parent for alias
    if hasattr(token, '__parent__') and token.__parent__:
        parent = token.__parent__
        if isinstance(parent, Identifier):
            return parent.get_alias()
    return None


def _parse_order_by(stmt) -> list:
    """Parse ORDER BY clause."""
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
    """Extract alias from an expression."""
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
    """Find join key from WHERE expression."""
    if not where_expr:
        return None
    for pattern in [
        rf'{table1}\.(([\w]+))\s*=\s*{table2}\.(([\w]+))',
        rf'{table2}\.(([\w]+))\s*=\s*{table1}\.(([\w]+))',
    ]:
        match = re.search(pattern, where_expr, re.IGNORECASE)
        if match:
            return (match.group(1), match.group(3))
    return None


def _extract_subqueries(stmt) -> list:
    """Extract subqueries from the statement."""
    subqueries = []
    tokens = list(stmt.flatten())

    for i, token in enumerate(tokens):
        if token.value.strip() == '(':
            # Check if this is preceded by SELECT
            if i > 0 and tokens[i-1].value.strip().upper() == 'SELECT':
                # This is likely a subquery
                subquery_str, end_i = _extract_balanced_parentheses(tokens, i)
                if subquery_str:
                    # Determine subquery type
                    sq_type = _classify_subquery(subquery_str)
                    subqueries.append({
                        'sql': subquery_str,
                        'type': sq_type,
                        'position': i
                    })

    return subqueries


def _extract_balanced_parentheses(tokens: list, start: int) -> tuple:
    """Extract content between balanced parentheses."""
    depth = 0
    i = start
    start_content = start + 1

    while i < len(tokens):
        if tokens[i].value == '(':
            depth += 1
        elif tokens[i].value == ')':
            depth -= 1
            if depth == 0:
                break
        i += 1

    content = ''.join(tokens[start_content:i])
    return content.strip(), i


def _classify_subquery(sql: str) -> str:
    """Classify subquery type: scalar, exists, derived, correlated."""
    sql_upper = sql.strip().upper()

    # Check for EXISTS/NOT EXISTS
    if sql_upper.startswith('SELECT') and 'EXISTS' in sql_upper:
        # Check if it starts with SELECT 1 or SELECT * in EXISTS context
        if re.match(r'\s*SELECT\s+(\*|1)', sql, re.IGNORECASE):
            return 'exists'

    # Check if it starts with SELECT and has no FROM (scalar)
    if re.match(r'\s*SELECT\s+', sql, re.IGNORECASE):
        # Simple check: if it's just aggregate, it's scalar
        if not re.search(r'\s+FROM\s+', sql, re.IGNORECASE):
            return 'scalar'

    # Check for EXISTS in WHERE clause context
    if 'EXISTS' in sql_upper:
        return 'exists'

    # Default to derived table
    return 'derived'


def _detect_correlated(subquery: str, outer_alias: str) -> bool:
    """Detect if a subquery references outer query columns."""
    # Simple heuristic: check for table.column patterns that might be outer refs
    for match in re.finditer(r'\w+\.\w+', subquery):
        ref = match.group()
        if outer_alias and ref.startswith(outer_alias + '.'):
            return True
    return False
