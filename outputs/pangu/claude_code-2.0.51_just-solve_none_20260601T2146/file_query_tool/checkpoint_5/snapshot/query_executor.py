"""SQL query executor for CSV data with CTE and subquery support."""

import pandas as pd
import re
from database import CSVDatabase
from query_parser import parse_query, extract_alias, find_join_key
from evaluator import SQLExpressionEvaluator
from window_functions import WindowSpec, compute_window_function, validate_window_spec, WindowFunctionError
from errors import (
    CTENotFoundError, CyclicReferenceError, RecursiveDepthExceededError,
    ScalarSubqueryMultipleRowsError, InvalidSubqueryReferenceError,
    InvalidCTESyntaxError
)


class SQLExecutor:
    """Executes SQL queries on CSV data with CTE and subquery support."""

    MAX_RECURSIVE_DEPTH = 1000

    def __init__(self, db: CSVDatabase):
        self.db = db

    def execute(self, sql: str) -> pd.DataFrame:
        return self._build_query(parse_query(sql))

    def _build_query(self, plan: dict) -> pd.DataFrame:
        # Create a working context with base tables
        working_tables = dict(self.db.tables)
        outer_tables = dict(self.db.tables)  # Keep track of outer query tables

        # Process CTEs first
        if plan.get('ctes'):
            for cte in plan['ctes']:
                if cte.get('recursive'):
                    result_df = self._execute_recursive_cte(cte, working_tables)
                else:
                    result_df = self._execute_cte(cte, working_tables)
                working_tables[cte['name'].lower()] = result_df
                outer_tables[cte['name'].lower()] = result_df

        # Execute the main query
        df = self._load_main_table(plan['from'], working_tables)
        outer_tables[plan['from'].get('main_table', 'derived').lower()] = df

        select_cols = self._process_select_expressions(plan['select'][0], df)

        # Apply subqueries in SELECT, WHERE, etc.
        df = self._apply_subqueries_in_select(df, plan['select'][0], working_tables, outer_tables)
        df = self._apply_joins(df, plan['from'], plan['where'])
        if plan['where']:
            df = self._apply_where(df, plan['where'], working_tables, outer_tables)

        group_by = plan['group_by']
        having = plan['having']
        if group_by or having:
            df = self._apply_group_by(df, group_by, select_cols, having, plan['select'][1])

        if plan['select'][1]:
            df = self._apply_distinct(df, select_cols)

        df = self._apply_window_functions(df, plan.get('window_functions', []), select_cols)
        df = self._apply_order_by(df, plan['order_by'], select_cols)
        df = self._apply_limit_offset(df, plan['limit'], plan['offset'])
        return self._build_result(df, select_cols)

    def _execute_cte(self, cte: dict, working_tables: dict) -> pd.DataFrame:
        """Execute a non-recursive CTE."""
        temp_tables = dict(working_tables)
        temp_executor = SQLExecutor(MockDatabase(temp_tables))
        return temp_executor._build_query(parse_query(cte['query']))

    def _execute_recursive_cte(self, cte: dict, working_tables: dict) -> pd.DataFrame:
        """Execute a recursive CTE."""
        query = cte['query']
        cte_name = cte['name']

        query_upper = query.upper()

        union_pos = -1
        for keyword in ['UNION ALL', 'UNION']:
            pos = query_upper.find(keyword)
            if pos != -1:
                union_pos = pos
                union_keyword = keyword
                break

        if union_pos == -1:
            return self._execute_cte(cte, working_tables)

        anchor_sql = query[:union_pos].strip()
        recursive_sql = query[union_pos + len(union_keyword):].strip()

        anchor_result = self._execute_simple_query(anchor_sql, working_tables)
        if anchor_result.empty:
            return anchor_result

        working_tables[cte_name.lower()] = anchor_result.copy()

        visited = set()
        depth = 0

        while depth < self.MAX_RECURSIVE_DEPTH:
            for _, row in anchor_result.iterrows():
                visited.add(tuple(row))

            recursive_result = self._execute_simple_query(recursive_sql, working_tables)

            new_rows = []
            for _, row in recursive_result.iterrows():
                row_tuple = tuple(row)
                if row_tuple not in visited:
                    new_rows.append(row)
                    visited.add(row_tuple)

            if not new_rows:
                break

            new_df = pd.DataFrame(new_rows)
            for col in anchor_result.columns:
                if col not in new_df.columns:
                    new_df[col] = None

            anchor_result = pd.concat([anchor_result, new_df], ignore_index=True)
            working_tables[cte_name.lower()] = anchor_result.copy()
            depth += 1

        if depth >= self.MAX_RECURSIVE_DEPTH:
            raise RecursiveDepthExceededError()

        return anchor_result

    def _execute_simple_query(self, sql: str, working_tables: dict) -> pd.DataFrame:
        """Execute a simple query without CTEs for use in recursive CTEs."""
        temp_db = MockDatabase(working_tables)
        return SQLExecutor(temp_db)._build_query(parse_query(sql))

    def _load_main_table(self, from_clause: dict, working_tables: dict) -> pd.DataFrame:
        """Load the main table, handling derived tables (subqueries)."""
        if from_clause.get('derived_tables'):
            dt = from_clause['derived_tables'][0]
            temp_db = MockDatabase(working_tables)
            subquery_result = SQLExecutor(temp_db)._build_query(parse_query(dt['subquery']))
            return subquery_result

        table_name = from_clause.get('main_table')
        if table_name:
            if table_name.lower() in working_tables:
                return working_tables[table_name.lower()].copy()
            df = self.db.get_table(table_name)
            if df is None:
                raise ValueError(f"Table '{table_name}' not found")
            return df.copy()

        raise ValueError("No FROM clause found")

    def _process_select_expressions(self, select_list: list, df: pd.DataFrame) -> list:
        """Process SELECT expressions, handling FILTER clauses."""
        select_cols = []
        for expr in select_list:
            expr = expr.strip()
            upper = expr.upper()
            col = expr.split('.')[-1]

            filter_where = None
            if 'FILTER' in upper:
                filter_match = re.search(r'\bFILTER\s*\(\s*WHERE\s', expr, re.IGNORECASE)
                if filter_match:
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
                    expr = expr[:filter_match.start()].strip() + expr[i-1:]
                    upper = expr.upper()

            if upper.startswith('COUNT('):
                inner = expr[6:-1].strip().rstrip(')')
                col = '__count_all__' if inner == '*' else inner.split('.')[-1]
                alias = extract_alias(expr) or f'count_{len(select_cols)}'
            elif upper.startswith(('SUM(', 'MIN(', 'MAX(')):
                alias = extract_alias(expr) or f'{upper[:3].lower()}_{len(select_cols)}'
            elif 'OVER' in upper:
                alias = extract_alias(expr) or f'window_{len(select_cols)}'
                col = None
            else:
                alias = extract_alias(expr) or col

            agg_type_map = {'SUM': 'sum', 'MIN': 'min', 'MAX': 'max', 'COUNT': 'count', 'AVG': 'avg'}
            agg_type = next((agg_type_map[f] for f in agg_type_map if upper.startswith(f'{f}(')), None)
            select_cols.append((alias, expr, agg_type, col, filter_where))
        return select_cols

    def _apply_joins(self, df: pd.DataFrame, from_clause: dict, where_expr: str) -> pd.DataFrame:
        """Apply joins including subquery joins."""
        for join_info in from_clause.get('joins', []):
            if join_info.get('is_subquery'):
                working = dict(self.db.tables)
                table_name = from_clause.get('main_table') or 'derived'
                working[table_name.lower()] = df
                join_df = self._evaluate_subquery(join_info['subquery'], None, working)
                if join_info.get('alias'):
                    join_df = join_df.rename(columns={c: f"{join_info['alias']}.{c}" if '.' not in c else c for c in join_df.columns})
            else:
                join_df = self.db.get_table(join_info['table'])
                if join_df is None:
                    raise ValueError(f"Table '{join_info['table']}' not found")

            join_key = find_join_key(where_expr, from_clause['main_table'] or 'derived', join_info['table'])
            how = join_info.get('type') or 'INNER'
            kwargs = {'left_on': join_key[0], 'right_on': join_key[1]} if join_key else {}
            df = pd.merge(df, join_df, how=how, suffixes=('_x', '_y'), **kwargs)
        return df

    def _apply_where(self, df: pd.DataFrame, where_expr: str, working_tables: dict, outer_tables: dict) -> pd.DataFrame:
        """Apply WHERE clause with subquery support."""
        evaluator = SQLExpressionEvaluator(working_tables)
        evaluator.set_subquery_handler(lambda sq, row, _: self._evaluate_subquery_for_row(sq, row, outer_tables))
        mask = df.apply(lambda row: evaluator.evaluate(where_expr, row), axis=1)
        return df[mask].copy()

    def _apply_subqueries_in_select(self, df: pd.DataFrame, select_list: list,
                                    working_tables: dict, outer_tables: dict) -> pd.DataFrame:
        """Evaluate scalar and other subqueries in SELECT clause."""
        for expr in select_list:
            expr = expr.strip()
            if '(' in expr and 'SELECT' in expr.upper():
                alias = extract_alias(expr) or f'subquery_{len([c for c in df.columns if c.startswith(\"subquery_\")])}'
                if re.search(r'\(\s*SELECT', expr, re.IGNORECASE):
                    df[alias] = df.apply(lambda row: self._evaluate_scalar_subquery(expr, row, outer_tables), axis=1)
        return df

    def _evaluate_scalar_subquery(self, expr: str, row: pd.Series,
                                  outer_tables: dict) -> any:
        """Evaluate a scalar subquery in SELECT clause."""
        match = re.search(r'\(\s*(SELECT[^)]+)\s*\)', expr, re.IGNORECASE | re.DOTALL)
        if not match:
            match = re.search(r'\(\s*SELECT.*?\)', expr, re.IGNORECASE | re.DOTALL)
        if not match:
            return None

        subquery_sql = match.group(1).strip()
        return self._evaluate_subquery_for_row(subquery_sql, row, outer_tables)

    def _evaluate_subquery(self, subquery_sql: str, row: pd.Series = None,
                          working_tables: dict = None) -> pd.DataFrame:
        """Evaluate a subquery. Returns DataFrame result."""
        if working_tables is None:
            working_tables = dict(self.db.tables)

        temp_db = MockDatabase(working_tables)
        executor = SQLExecutor(temp_db)
        return executor._build_query(parse_query(subquery_sql))

    def _evaluate_subquery_for_row(self, subquery_sql: str, row: pd.Series,
                                   outer_tables: dict) -> any:
        """Evaluate a subquery in the context of a row (for correlated subqueries)."""
        working_tables = dict(outer_tables)

        temp_db = MockDatabase(working_tables)
        executor = SQLExecutor(temp_db)

        try:
            result = executor._build_query(parse_query(subquery_sql))
        except Exception:
            return None

        sq_type = self._classify_subquery(subquery_sql)

        if sq_type == 'scalar':
            if len(result) == 0:
                return None
            return result.iloc[0, 0] if len(result.columns) > 0 else None

        elif sq_type == 'exists':
            return len(result) > 0

        elif sq_type in ('derived', 'derived_table'):
            return result

        return result

    def _classify_subquery(self, sql: str) -> str:
        """Classify subquery type."""
        sql = sql.strip()
        sql_upper = sql.upper()

        if 'EXISTS' in sql_upper and sql_upper.startswith('SELECT'):
            if re.match(r'\s*SELECT\s+[*1]', sql, re.IGNORECASE):
                return 'exists'

        if re.match(r'\s*SELECT\s+', sql, re.IGNORECASE):
            if not re.search(r'\s+FROM\s+', sql, re.IGNORECASE):
                return 'scalar'

            if 'EXISTS' in sql_upper:
                exists_match = re.search(r'\bEXISTS\s*\(', sql_upper)
                if exists_match:
                    pos = exists_match.start()
                    before_exists = sql_upper[:pos]
                    if re.match(r'\s*SELECT\s*$', before_exists):
                        return 'exists'

        return 'derived'

    def _apply_group_by(self, df: pd.DataFrame, group_by: list, select_cols: list,
                       having: str, distinct: bool) -> pd.DataFrame:
        """Apply GROUP BY with FILTER clause support for aggregates."""
        if not group_by:
            return df

        group_cols = [g.split('.')[-1] for g in group_by]
        existing = [c for c in group_cols if c in df.columns]
        if not existing:
            raise ValueError("No valid GROUP BY columns found")

        agg_dict = {}
        filter_clauses = {}
        has_count_all = False

        for alias, expr, agg_type, col, filter_where in select_cols:
            if agg_type:
                if agg_type == 'count' and col == '__count_all__':
                    has_count_all = True
                elif col and col in df.columns:
                    if filter_where:
                        filter_clauses[alias] = (col, filter_where)
                    else:
                        agg_dict[col] = agg_type

        if agg_dict:
            grouped = df.groupby(existing, as_index=False).agg(agg_dict)
            new_cols = []
            for col in grouped.columns:
                if col in existing:
                    new_cols.append(col)
                else:
                    for alias, _, _, c, _ in select_cols:
                        if c == col:
                            new_cols.append(alias)
                            break
                    else:
                        new_cols.append(col)
            grouped.columns = new_cols
            df = grouped
        elif has_count_all:
            df = df.groupby(existing, as_index=False).size()
            df.columns = existing + ['count']
        else:
            raise ValueError("No valid aggregate in SELECT with GROUP BY")

        if filter_clauses:
            for alias, (col, filter_where) in filter_clauses.items():
                evaluator = SQLExpressionEvaluator(dict(self.db.tables))
                grouped_filtered = df.groupby(existing).apply(
                    lambda g: g[g.apply(lambda row: evaluator.evaluate(filter_where, row), axis=1)][col].count()
                )
                df[alias] = grouped_filtered.reindex(df.index).values

        if having:
            evaluator = SQLExpressionEvaluator(dict(self.db.tables))
            df = df[df.apply(lambda row: evaluator.evaluate(having, row), axis=1)]

        return df

    def _apply_distinct(self, df: pd.DataFrame, select_cols: list) -> pd.DataFrame:
        """Apply DISTINCT."""
        cols = [alias for alias, _, agg_type, col, _ in select_cols
                if alias in df.columns and agg_type is None]
        return df[cols].drop_duplicates() if cols else df

    def _apply_window_functions(self, df: pd.DataFrame, window_funcs: list,
                                select_cols: list) -> pd.DataFrame:
        """Apply window functions."""
        if not window_funcs:
            return df

        for wf in window_funcs:
            filter_where = wf.get('filter_where')
            if filter_where:
                evaluator = SQLExpressionEvaluator(dict(self.db.tables))
                mask = df.apply(lambda row: evaluator.evaluate(filter_where, row), axis=1)
                filtered_df = df[mask].copy()
            else:
                filtered_df = df

            spec = WindowSpec(wf['function'], wf['partition_by'], wf['order_by'], wf.get('frame_clause'))
            validate_window_spec(spec)
            result = compute_window_function(filtered_df, spec)

            if filter_where:
                result_full = pd.Series([None] * len(df), index=df.index)
                result_full[mask] = result.values
                result = result_full

            alias = next((a for a, e, _, _, _ in select_cols if wf['function'] in e.upper() and 'OVER' in e.upper()),
                        f"window_{len([c for c in df.columns if c.startswith('window_')])}")
            df[alias] = result

        return df

    def _apply_order_by(self, df: pd.DataFrame, order_by: list, select_cols: list) -> pd.DataFrame:
        """Apply ORDER BY."""
        if not order_by:
            return df

        sort_cols = []
        ascending = []
        for col, desc in order_by:
            name = col.split('.')[-1]
            if name in df.columns:
                sort_cols.append(name)
                ascending.append(not desc)
            else:
                for alias, _, _, _, _ in select_cols:
                    if alias == name:
                        sort_cols.append(alias)
                        ascending.append(not desc)
                        break

        if sort_cols:
            df = df.sort_values(by=sort_cols, ascending=ascending)
        return df

    def _apply_limit_offset(self, df: pd.DataFrame, limit: int, offset: int) -> pd.DataFrame:
        """Apply LIMIT and OFFSET."""
        if limit is not None:
            return df.iloc[offset:offset+limit] if offset else df.head(limit)
        if offset is not None:
            return df.iloc[offset:]
        return df

    def _build_result(self, df: pd.DataFrame, select_cols: list) -> pd.DataFrame:
        """Build the final result DataFrame."""
        result_cols = []
        for alias, _, agg_type, col, _ in select_cols:
            if alias in df.columns:
                result_cols.append(alias)
            elif agg_type == 'count' and col == '__count_all__' and 'count' in df.columns:
                result_cols.append('count')

        if not result_cols:
            result_cols = [c for c in df.columns if not c.endswith(('_x', '_y'))]

        return df[result_cols] if result_cols else pd.DataFrame()


class MockDatabase(CSVDatabase):
    """Mock database that uses provided tables instead of loading from files."""

    def __init__(self, tables: dict):
        self.tables = tables

    def get_table(self, name: str):
        return self.tables.get(name.lower())
