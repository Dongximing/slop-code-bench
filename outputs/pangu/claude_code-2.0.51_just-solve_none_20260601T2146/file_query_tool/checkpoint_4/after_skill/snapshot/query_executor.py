"""SQL query executor for CSV data."""

import pandas as pd
from database import CSVDatabase
from query_parser import parse_query, extract_alias, find_join_key
from evaluator import SQLExpressionEvaluator
from window_functions import WindowSpec, compute_window_function, validate_window_spec, WindowFunctionError


class SQLExecutor:
    """Executes SQL queries on CSV data."""

    def __init__(self, db: CSVDatabase):
        self.db = db

    def execute(self, sql: str) -> pd.DataFrame:
        return self._build_query(parse_query(sql))

    def _build_query(self, plan: dict) -> pd.DataFrame:
        df = self._load_main_table(plan['from']['main_table'])
        select_cols = self._process_select_expressions(plan['select'][0], df)

        df = self._apply_joins(df, plan['from'], plan['where'])
        if plan['where']:
            df = self._apply_where(df, plan['where'])

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

    def _load_main_table(self, table_name: str) -> pd.DataFrame:
        df = self.db.get_table(table_name)
        if df is None:
            raise ValueError(f"Table '{table_name}' not found")
        return df.copy()

    def _process_select_expressions(self, select_list: list, df: pd.DataFrame) -> list:
        select_cols = []
        for expr in select_list:
            expr = expr.strip()
            upper = expr.upper()
            col = expr.split('.')[-1]

            if upper.startswith('COUNT('):
                inner = expr[6:-1].strip()
                col = '__count_all__' if inner == '*' else inner.split('.')[-1]
                alias = extract_alias(expr) or f'count_{len(select_cols)}'
            elif upper.startswith(('SUM(', 'MIN(', 'MAX(')):
                alias = extract_alias(expr) or f'{upper[:3].lower()}_{len(select_cols)}'
            elif 'OVER' in upper:
                alias = extract_alias(expr) or f'window_{len(select_cols)}'
                col = None
            else:
                alias = extract_alias(expr) or col

            agg_type_map = {'SUM': 'sum', 'MIN': 'min', 'MAX': 'max', 'COUNT': 'count'}
            agg_type = next((agg_type_map[f] for f in agg_type_map if upper.startswith(f'{f}(')), None)
            select_cols.append((alias, expr, agg_type, col))
        return select_cols

    def _apply_joins(self, df: pd.DataFrame, from_clause: dict, where_expr: str) -> pd.DataFrame:
        for join_info in from_clause.get('joins', []):
            join_df = self.db.get_table(join_info['table'])
            if join_df is None:
                raise ValueError(f"Table '{join_info['table']}' not found")
            join_key = find_join_key(where_expr, from_clause['main_table'], join_info['table'])
            how = join_info.get('type') or 'INNER'
            kwargs = {'left_on': join_key[0], 'right_on': join_key[1]} if join_key else {}
            df = pd.merge(df, join_df, how=how, suffixes=('_x', '_y'), **kwargs)
        return df

    def _apply_where(self, df: pd.DataFrame, where_expr: str) -> pd.DataFrame:
        evaluator = SQLExpressionEvaluator(self.db.tables)
        mask = df.apply(lambda row: evaluator.evaluate(where_expr, row), axis=1)
        return df[mask].copy()

    def _apply_group_by(self, df: pd.DataFrame, group_by: list, select_cols: list,
                       having: str, distinct: bool) -> pd.DataFrame:
        group_cols = [g.split('.')[-1] for g in group_by]
        existing = [c for c in group_cols if c in df.columns]
        if not existing:
            raise ValueError("No valid GROUP BY columns found")

        agg_dict = {}
        has_count_all = False
        for _, _, agg_type, col in select_cols:
            if agg_type == 'count' and col == '__count_all__':
                has_count_all = True
            elif agg_type and col in df.columns:
                agg_dict[col] = agg_type

        if agg_dict:
            grouped = df.groupby(existing, as_index=False).agg(agg_dict)
            new_cols = []
            for col in grouped.columns:
                if col in existing:
                    new_cols.append(col)
                else:
                    for alias, _, _, c in select_cols:
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

        if having and group_by:
            evaluator = SQLExpressionEvaluator(self.db.tables)
            df = df[df.apply(lambda row: evaluator.evaluate(having, row), axis=1)]

        return df

    def _apply_distinct(self, df: pd.DataFrame, select_cols: list) -> pd.DataFrame:
        cols = [alias for alias, _, agg_type, col in select_cols
                if alias in df.columns and agg_type is None]
        return df[cols].drop_duplicates() if cols else df

    def _apply_window_functions(self, df: pd.DataFrame, window_funcs: list,
                                select_cols: list) -> pd.DataFrame:
        if not window_funcs:
            return df

        for wf in window_funcs:
            spec = WindowSpec(wf['function'], wf['partition_by'], wf['order_by'], wf.get('frame_clause'))
            validate_window_spec(spec)
            result = compute_window_function(df, spec)

            alias = next((a for a, e, _, _ in select_cols if wf['function'] in e.upper() and 'OVER' in e.upper()),
                        f"window_{len([c for c in df.columns if c.startswith('window_')])}")
            df[alias] = result

        return df

    def _apply_order_by(self, df: pd.DataFrame, order_by: list, select_cols: list) -> pd.DataFrame:
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
                for alias, _, _, c in select_cols:
                    if alias == name:
                        sort_cols.append(alias)
                        ascending.append(not desc)
                        break

        if sort_cols:
            df = df.sort_values(by=sort_cols, ascending=ascending)
        return df

    def _apply_limit_offset(self, df: pd.DataFrame, limit: int, offset: int) -> pd.DataFrame:
        if limit is not None:
            return df.iloc[offset:offset+limit] if offset else df.head(limit)
        if offset is not None:
            return df.iloc[offset:]
        return df

    def _build_result(self, df: pd.DataFrame, select_cols: list) -> pd.DataFrame:
        result_cols = []
        for alias, _, agg_type, col in select_cols:
            if alias in df.columns:
                result_cols.append(alias)
            elif agg_type == 'count' and col == '__count_all__' and 'count' in df.columns:
                result_cols.append('count')

        if not result_cols:
            result_cols = [c for c in df.columns if not c.endswith(('_x', '_y'))]

        return df[result_cols]