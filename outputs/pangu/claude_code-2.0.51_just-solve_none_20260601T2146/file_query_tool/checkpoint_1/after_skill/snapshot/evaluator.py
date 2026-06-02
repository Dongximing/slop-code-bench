"""SQL expression evaluator for WHERE and HAVING clauses."""

import re
from typing import Dict, Optional
import pandas as pd


class SQLExpressionEvaluator:
    """Evaluates boolean expressions for WHERE and HAVING clauses."""

    def __init__(self, tables: Dict[str, pd.DataFrame]):
        self.tables = tables

    def evaluate(self, expr: str, row: pd.Series) -> bool:
        """Evaluate a boolean expression against a row."""
        if not expr or not expr.strip():
            return True

        expr = self._process_parentheses(expr.strip(), row)
        expr = self._handle_not(expr, row)
        expr = self._replace_operators(expr)
        expr = self._handle_like(expr)
        return self._safe_eval(expr, row)

    def _process_parentheses(self, expr: str, row: pd.Series) -> str:
        """Recursively resolve parenthesized subexpressions."""
        while '(' in expr:
            start = expr.rfind('(')
            end = expr.find(')', start)
            if end == -1:
                break
            inner = expr[start+1:end]
            result = self.evaluate(inner, row)
            expr = expr[:start] + ('TRUE' if result else 'FALSE') + expr[end+1:]
        return expr

    def _handle_not(self, expr: str, row: pd.Series) -> str:
        """Handle NOT operator by evaluating subexpressions."""
        for match in re.finditer(r'\bNOT\s+(.+?)(?:\s|AND|OR|$)', expr, re.IGNORECASE):
            inner = match.group(1)
            # Check if it's a complete subexpression
            result = self.evaluate(inner, row)
            expr = expr.replace(match.group(0), 'FALSE' if result else 'TRUE')
        return expr

    def _replace_operators(self, expr: str) -> str:
        """Replace SQL operators with Python equivalents."""
        expr = expr.replace('<>', '!=')
        expr = expr.replace('AND', 'and').replace('OR', 'or').replace('NOT', 'not')
        expr = expr.replace('TRUE', 'True').replace('FALSE', 'False').replace('NULL', 'None')
        return expr

    def _handle_like(self, expr: str) -> str:
        """Convert LIKE pattern matching to boolean results."""
        def convert_like(match):
            left = match.group(1).strip("'\"")
            pattern = match.group(2).strip("'\"").replace('%', '.*').replace('_', '.')
            result = bool(re.match(pattern + '$', left, re.IGNORECASE))
            return 'True' if result else 'False'

        return re.sub(
            r"([\'\"]?[^\'\"]*?)[\'\"]?\s+LIKE\s+([\'\"]?[^\'\"]*?)[\'\"]?",
            convert_like, expr, flags=re.IGNORECASE
        )

    def _safe_eval(self, expr: str, row: pd.Series) -> bool:
        """Safely evaluate expression using row data."""
        def replace_column(match):
            col = match.group(1)
            # Handle table-qualified column names
            if '.' in col:
                col = col.split('.')[-1]
            if col in row.index:
                val = row[col]
                if pd.isna(val):
                    return 'None'
                elif isinstance(val, str):
                    return f"'{val}'"
                elif isinstance(val, (int, float)):
                    return str(val)
            return 'None'

        expr = re.sub(r'\b(\w+)\b', replace_column, expr)
        return eval(expr, {"__builtins__": {}}, {}) or False
