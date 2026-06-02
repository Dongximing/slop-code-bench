"""SQL expression evaluator for WHERE and HAVING clauses with subquery support."""

import re
import pandas as pd
from typing import Callable, Optional, Dict


class SQLExpressionEvaluator:
    """Evaluates boolean expressions for WHERE and HAVING clauses."""

    def __init__(self, tables: Dict):
        self.tables = tables
        self._subquery_handler = None

    def set_subquery_handler(self, handler: Callable):
        """Set a handler for evaluating subqueries."""
        self._subquery_handler = handler

    def evaluate(self, expr: str, row: pd.Series) -> bool:
        if not expr or not expr.strip():
            return True
        expr = self._replace_operators(expr.strip())
        expr = self._handle_like(expr)
        expr = self._handle_in_exists(expr, row)
        expr = self._process_parentheses(expr, row)
        expr = self._process_not(expr, row)
        return self._safe_eval(expr, row)

    def _handle_in_exists(self, expr: str, row: pd.Series) -> str:
        """Handle IN subqueries and EXISTS/NOT EXISTS."""
        # Handle EXISTS/NOT EXISTS
        exists_pattern = r'\b(NOT\s+)?EXISTS\s*\(\s*SELECT'
        matches = list(re.finditer(exists_pattern, expr, re.IGNORECASE))

        for match in reversed(matches):
            not_exists = match.group(1) is not None
            # Find matching closing parenthesis
            start = match.end() - len('SELECT') + 1  # Start of subquery
            subquery, end_pos = self._extract_parenthesis_content(expr, start - 1)
            if subquery and self._subquery_handler:
                result = self._subquery_handler(subquery, row, self.tables)
                # For EXISTS, result is boolean if already evaluated
                if isinstance(result, pd.DataFrame):
                    result = len(result) > 0
                if not_exists:
                    result = not result
                expr = expr[:match.start()] + ('True' if result else 'False') + expr[end_pos:]

        # Handle scalar subqueries in comparisons
        # Pattern: column = (SELECT ...), column != (SELECT ...), column > (SELECT ...), etc.
        scalar_pattern = r'(\w+\.?\w*)\s*(=|!=|>|<|>=|<=)\s*\(\s*SELECT'

        for match in reversed(list(re.finditer(scalar_pattern, expr, re.IGNORECASE))):
            col = match.group(1)
            op = match.group(2)
            start = match.end() - len('SELECT') + 2
            subquery, end_pos = self._extract_parenthesis_content(expr, start - 1)
            if subquery and self._subquery_handler:
                scalar_result = self._subquery_handler(subquery, row, self.tables)
                if scalar_result is None:
                    scalar_val = 'None'
                elif isinstance(scalar_result, (int, float)):
                    scalar_val = str(scalar_result)
                else:
                    scalar_val = f"'{scalar_result}'"
                expr = expr[:match.start()] + f'({scalar_val})' + expr[end_pos:]

        return expr

    def _extract_parenthesis_content(self, expr: str, start: int) -> tuple:
        """Extract content within parentheses."""
        depth = 1
        i = start + 1
        while i < len(expr) and depth > 0:
            if expr[i] == '(':
                depth += 1
            elif expr[i] == ')':
                depth -= 1
            i += 1
        return expr[start+1:i-1].strip(), i

    def _replace_operators(self, expr: str) -> str:
        expr = expr.replace('<>', '!=')
        return (expr.replace('AND', 'and').replace('OR', 'or').replace('NOT', 'not')
                  .replace('TRUE', 'True').replace('FALSE', 'False').replace('NULL', 'None'))

    def _handle_like(self, expr: str) -> str:
        def convert_like(m):
            left = m.group(1).strip("'\"")
            pattern = m.group(2).strip("'\"").replace('%', '.*').replace('_', '.')
            return 'True' if re.match(pattern + '$', left, re.IGNORECASE) else 'False'
        return re.sub(r'([^\"\']*?)\s+LIKE\s+([^\"\']*)', convert_like, expr, flags=re.IGNORECASE)

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
