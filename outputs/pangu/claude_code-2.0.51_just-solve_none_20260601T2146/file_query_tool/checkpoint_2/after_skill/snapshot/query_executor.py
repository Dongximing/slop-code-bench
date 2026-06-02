"""SQL query executor for CSV data."""

from typing import Dict, List, Optional, Tuple, Any
import pandas as pd

from database import CSVDatabase
from query_parser import parse_query, extract_alias, find_join_key
from evaluator import SQLExpressionEvaluator


class SQLExecutor:
    """Executes SQL queries on CSV data."""

    def __init__(self, db: CSVDatabase):
        self.db = db

    def execute(self, sql: str) -> pd.DataFrame:
        query_plan = parse_query(sql)
        return self._build_query(query_plan)

    def _build_query(self, plan: Dict[str, Any]) -> pd.DataFrame:
        select_list, distinct = plan['select']
        from_clause = plan['from']
        where_expr = plan['where']
        group_by = plan['group_by']
        having_expr = plan['having']
        order_by = plan['order_by']
        limit = plan['limit']
        offset = plan['offset']

        if not from_clause or not from_clause.get('main_table'):
            raise ValueError("FROM clause is required")

        df = self._load_main_table(from_clause['main_table'])
        select_columns = self._process_select_expressions(select_list, df)

        df = self._apply_joins(df, from_clause, where_expr)

        if where_expr:
            df = self._apply_where(df, where_expr)

        if group_by:
            df = self._apply_group_by(df, group_by, select_columns, having_expr, distinct)
        elif having_expr:
            raise ValueError("HAVING clause requires GROUP BY")

        df = self._apply_distinct(df, select_columns, distinct)
        df = self._apply_order_by(df, order_by, select_columns)
        df = self._apply_limit_offset(df, limit, offset)

        return self._build_result(df, select_columns)

    def _load_main_table(self, table_name: str) -> pd.DataFrame:
        df = self.db.get_table(table_name)
        if df is None:
            raise ValueError(f"Table '{table_name}' not found")
        return df.copy()

    def _process_select_expressions(self, select_list: List[str], df: pd.DataFrame) -> List[Tuple[str, str, Optional[str], str]]:
        select_columns = []

        for expr in select_list:
            expr = expr.strip()
            upper = expr.upper()

            if upper.startswith('COUNT('):
                inner = expr[6:-1].strip()
                col = '__count_all__' if inner == '*' else inner.split('.')[-1]
                alias = extract_alias(expr) or f'count_{len(select_columns)}'
                select_columns.append((alias, expr, 'COUNT', col))

            elif upper.startswith('SUM('):
                col = expr[4:-1].strip().split('.')[-1]
                alias = extract_alias(expr) or f'sum_{len(select_columns)}'
                select_columns.append((alias, expr, 'SUM', col))

            elif upper.startswith('MIN('):
                col = expr[4:-1].strip().split('.')[-1]
                alias = extract_alias(expr) or f'min_{len(select_columns)}'
                select_columns.append((alias, expr, 'MIN', col))

            elif upper.startswith('MAX('):
                col = expr[4:-1].strip().split('.')[-1]
                alias = extract_alias(expr) or f'max_{len(select_columns)}'
                select_columns.append((alias, expr, 'MAX', col))

            else:
                col_name = expr.split('.')[-1] if '.' in expr else expr
                alias = extract_alias(expr) or col_name
                select_columns.append((alias, expr, None, col_name))

        return select_columns

    def _apply_joins(self, df: pd.DataFrame, from_clause: Dict, where_expr: str) -> pd.DataFrame:
        for join_info in from_clause.get('joins', []):
            join_df = self.db.get_table(join_info['table'])
            if join_df is None:
                raise ValueError(f"Table '{join_info['table']}' not found")

            join_key = find_join_key(where_expr, from_clause['main_table'], join_info['table'])
            how = (join_info['type'] or 'INNER').upper()

            if join_key:
                df = pd.merge(df, join_df, left_on=join_key[0], right_on=join_key[1],
                             how=how, suffixes=('_x', '_y'))
            else:
                df = pd.merge(df, join_df, how=how, suffixes=('_x', '_y'))

        return df

    def _apply_where(self, df: pd.DataFrame, where_expr: str) -> pd.DataFrame:
        evaluator = SQLExpressionEvaluator(self.db.tables)
        mask = df.apply(lambda row: evaluator.evaluate(where_expr, row), axis=1)
        return df[mask].copy()

    def _apply_group_by(self, df: pd.DataFrame, group_by: List[str],
                       select_columns: List[Tuple], having_expr: str, distinct: bool) -> pd.DataFrame:
        group_cols = [g.split('.')[-1] for g in group_by]
        existing_group_cols = [c for c in group_cols if c in df.columns]

        if not existing_group_cols:
            raise ValueError("No valid GROUP BY columns found")

        agg_dict = {}
        has_count_all = False

        for _, expr, agg_type, col in select_columns:
            if agg_type == 'COUNT' and col == '__count_all__':
                has_count_all = True
            elif agg_type and col in df.columns:
                agg_dict[col] = agg_type.lower()

        if agg_dict:
            grouped = df.groupby(existing_group_cols, as_index=False).agg(agg_dict)
            new_cols = []
            for col in grouped.columns:
                if col in existing_group_cols:
                    new_cols.append(col)
                else:
                    for alias, _, agg_type, c in select_columns:
                        if c == col:
                            new_cols.append(alias)
                            break
                    else:
                        new_cols.append(col)
            grouped.columns = new_cols
            df = grouped

        elif has_count_all:
            df = df.groupby(existing_group_cols, as_index=False).size()
            df.columns = existing_group_cols + ['count']

        else:
            raise ValueError("No valid aggregate in SELECT with GROUP BY")

        if having_expr and group_by:
            evaluator = SQLExpressionEvaluator(self.db.tables)
            mask = df.apply(lambda row: evaluator.evaluate(having_expr, row), axis=1)
            df = df[mask]

        return df

    def _apply_distinct(self, df: pd.DataFrame, select_columns: List[Tuple], distinct: bool) -> pd.DataFrame:
        if distinct:
            select_cols = [alias for alias, _, agg_type, col in select_columns
                         if alias in df.columns and agg_type is None]
            if select_cols:
                df = df[select_cols].drop_duplicates()
        return df

    def _apply_order_by(self, df: pd.DataFrame, order_by: List[Tuple],
                       select_columns: List[Tuple]) -> pd.DataFrame:
        if not order_by:
            return df

        sort_cols = []
        ascending = []

        for col, desc in order_by:
            col_name = col.split('.')[-1]
            if col_name in df.columns:
                sort_cols.append(col_name)
                ascending.append(not desc)
            else:
                for alias, _, _, c in select_columns:
                    if alias == col_name:
                        sort_cols.append(alias)
                        ascending.append(not desc)
                        break

        if sort_cols:
            df = df.sort_values(by=sort_cols, ascending=ascending)
        return df

    def _apply_limit_offset(self, df: pd.DataFrame, limit: Optional[int],
                           offset: Optional[int]) -> pd.DataFrame:
        if limit is not None:
            if offset is not None:
                df = df.iloc[offset:offset+limit]
            else:
                df = df.head(limit)
        elif offset is not None:
            df = df.iloc[offset:]
        return df

    def _build_result(self, df: pd.DataFrame, select_columns: List[Tuple]) -> pd.DataFrame:
        result_cols = []
        for alias, expr, agg_type, col in select_columns:
            if alias in df.columns:
                result_cols.append(alias)
            elif agg_type == 'COUNT' and col == '__count_all__':
                if 'count' in df.columns:
                    result_cols.append('count')

        if not result_cols:
            result_cols = [c for c in df.columns if not c.endswith(('_x', '_y'))]

        return df[result_cols]
