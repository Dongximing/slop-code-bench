"""SQL expression evaluator for WHERE and HAVING clauses."""

import re
import pandas as pd


class SQLExpressionEvaluator:
    """Evaluates boolean expressions for WHERE and HAVING clauses."""

    def __init__(self, tables):
        self.tables = tables

    def evaluate(self, expr: str, row: pd.Series) -> bool:
        if not expr or not expr.strip():
            return True
        expr = self._replace_operators(expr.strip())
        expr = self._handle_like(expr)
        expr = self._process_parentheses(expr, row)
        expr = self._process_not(expr, row)
        return self._safe_eval(expr, row)

    def _replace_operators(self, expr: str) -> str:
        expr = expr.replace('<>', '!=')
        return (expr.replace('AND', 'and').replace('OR', 'or').replace('NOT', 'not')
                  .replace('TRUE', 'True').replace('FALSE', 'False').replace('NULL', 'None'))

    def _handle_like(self, expr: str) -> str:
        def convert_like(m):
            left = m.group(1).strip("'\"")
            pattern = m.group(2).strip("'\"").replace('%', '.*').replace('_', '.')
            return 'True' if re.match(pattern + '$', left, re.IGNORECASE) else 'False'
        return re.sub(r'([^"\']*?)\s+LIKE\s+([^"\']*)', convert_like, expr, flags=re.IGNORECASE)

    def _process_parentheses(self, expr: str, row: pd.Series) -> str:
        while '(' in expr:
            start = expr.rfind('(')
            end = expr.find(')', start)
            if end == -1:
                break
            result = self.evaluate(expr[start+1:end], row)
            expr = expr[:start] + ('True' if result else 'False') + expr[end+1:]
        return expr

    def _process_not(self, expr: str, row: pd.Series) -> str:
        for m in re.finditer(r'\bNOT\s+(.+?)(?:\s|AND|OR|$)', expr, re.IGNORECASE):
            result = self.evaluate(m.group(1), row)
            expr = expr.replace(m.group(0), 'False' if result else 'True')
        return expr

    def _safe_eval(self, expr: str, row: pd.Series) -> bool:
        def replace_column(m):
            col = m.group(1).split('.')[-1]
            if col not in row.index:
                return 'None'
            val = row[col]
            if pd.isna(val):
                return 'None'
            if isinstance(val, str):
                return f"'{val}'"
            return str(val) if isinstance(val, (int, float)) else 'None'
        expr = re.sub(r'\b(\w+)\b', replace_column, expr)
        try:
            return bool(eval(expr, {"__builtins__": {}}, {}))
        except:
            return False