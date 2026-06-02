"""SQL expression evaluator for WHERE and HAVING clauses."""

import re
from typing import Dict
import pandas as pd


class SQLExpressionEvaluator:
    """Evaluates boolean expressions for WHERE and HAVING clauses."""

    def __init__(self, tables: Dict[str, pd.DataFrame]):
        self.tables = tables

    def evaluate(self, expr: str, row: pd.Series) -> bool:
        if not expr or not expr.strip():
            return True

        expr = expr.strip()
        expr = self._process_parentheses(expr, row)
        expr = self._process_not(expr, row)
        expr = self._replace_operators(expr)
        expr = self._handle_like(expr)
        return self._safe_eval(expr, row)

    def _process_parentheses(self, expr: str, row: pd.Series) -> str:
        while '(' in expr:
            start = expr.rfind('(')
            end = expr.find(')', start)
            if end == -1:
                break
            inner = expr[start+1:end]
            result = self.evaluate(inner, row)
            expr = expr[:start] + ('True' if result else 'False') + expr[end+1:]
        return expr

    def _process_not(self, expr: str, row: pd.Series) -> str:
        for match in re.finditer(r'\bNOT\s+(.+?)(?:\s|AND|OR|$)', expr, re.IGNORECASE):
            inner = match.group(1)
            result = self.evaluate(inner, row)
            expr = expr.replace(match.group(0), 'False' if result else 'True')
        return expr

    def _replace_operators(self, expr: str) -> str:
        expr = expr.replace('<>', '!=')
        return expr.replace('AND', 'and').replace('OR', 'or').replace('NOT', 'not') \
                   .replace('TRUE', 'True').replace('FALSE', 'False').replace('NULL', 'None')

    def _handle_like(self, expr: str) -> str:
        def convert_like(match):
            left = match.group(1).strip("'\"")
            pattern = match.group(2).strip("'\"").replace('%', '.*').replace('_', '.')
            return 'True' if re.match(pattern + '$', left, re.IGNORECASE) else 'False'

        return re.sub(
            r'(["\'\']?[^"\'\']*?)["\'\']?\s+LIKE\s+(["\'\']?[^"\'\']*?)["\'\']?',
            convert_like, expr, flags=re.IGNORECASE
        )

    def _safe_eval(self, expr: str, row: pd.Series) -> bool:
        def replace_column(match):
            col = match.group(1)
            if '.' in col:
                col = col.split('.')[-1]
            if col in row.index:
                val = row[col]
                if pd.isna(val):
                    return 'None'
                if isinstance(val, str):
                    return f"'{val}'"
                if isinstance(val, (int, float)):
                    return str(val)
            return 'None'

        expr = re.sub(r'\b(\w+)\b', replace_column, expr)
        try:
            return bool(eval(expr, {"__builtins__": {}}, {}))
        except:
            return False