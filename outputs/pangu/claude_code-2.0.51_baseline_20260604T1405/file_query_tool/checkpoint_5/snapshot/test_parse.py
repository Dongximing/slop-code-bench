#!/usr/bin/env python3
import re

def _parse_cte_blocks(sql):
    """Parse CTE blocks from WITH clause content. Returns (ctes_list, remaining_sql)."""
    ctes = []
    pos = 0
    sql_len = len(sql)

    while pos < sql_len:
        sql = sql[pos:].strip()
        if not sql:
            break

        # Find AS keyword followed by (
        as_match = re.search(r'\bAS\s*\(', sql, re.IGNORECASE)
        if not as_match:
            break

        name_part = sql[:as_match.start()].strip()

        # Parse name and optional column list
        col_match = re.match(r'([\w]+)\s*\(([^)]+)\)', name_part)
        if col_match:
            cte_name = col_match.group(1)
            columns = [c.strip() for c in col_match.group(2).split(',')]
        else:
            cte_name = name_part
            columns = None

        # Find matching closing parenthesis for AS (...)
        paren_start = as_match.end() - 1  # Position of '('
        paren_count = 1
        j = paren_start + 1

        while j < len(sql) and paren_count > 0:
            if sql[j] == '(':
                paren_count += 1
            elif sql[j] == ')':
                paren_count -= 1
            j += 1

        if paren_count != 0:
            raise Exception(f"Unmatched parentheses in CTE '{cte_name}'")

        query_str = sql[paren_start + 1:j - 1].strip()

        cte = {
            'name': cte_name,
            'columns': columns,
            'query': query_str
        }
        ctes.append(cte)

        pos = j

        if pos < sql_len and sql[pos:pos+1] == ',':
            pos += 1
        else:
            break

    remaining = sql[pos:].strip() if pos < sql_len else ""
    return ctes, remaining


# Test with Example 1
test_sql = '''dept_totals AS (
    SELECT department, SUM(salary) as total_salary, COUNT(*) as employee_count
    FROM employees
    GROUP BY department
)
SELECT department, total_salary, employee_count
FROM dept_totals
ORDER BY total_salary DESC'''

try:
    ctes, main = _parse_cte_blocks(test_sql)
    print("CTEs parsed:", ctes)
    print("Main query:", main[:100], "...")
except Exception as e:
    print(f"Error: {e}")
