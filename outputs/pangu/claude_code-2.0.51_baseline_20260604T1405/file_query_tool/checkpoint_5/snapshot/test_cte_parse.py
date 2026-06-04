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


# Test Example 1
test_sql = '''dept_totals AS (
    SELECT department, SUM(salary) as total_salary, COUNT(*) as employee_count
    FROM employees
    GROUP BY department
)
SELECT department, total_salary, employee_count
FROM dept_totals
ORDER BY total_salary DESC'''

print("=== Test 1: Basic CTE (Example 1) ===")
try:
    ctes, main = _parse_cte_blocks(test_sql)
    print("CTEs parsed:", len(ctes))
    for c in ctes:
        print(f"  Name: {c['name']}")
        print(f"  Query: {c['query'][:50]}...")
    print("Main query starts with:", main[:50], "...")
    print("Main query contains SELECT:", "SELECT" in main.upper())
    print("Main query has FROM:", "FROM" in main.upper())
except Exception as e:
    print(f"Error: {e}")

# Test Example 2: Multiple CTEs
test_sql2 = '''high_earners AS (
    SELECT * FROM employees WHERE salary > 85000
),
managers AS (
    SELECT DISTINCT manager_id FROM employees WHERE manager_id IS NOT NULL
)
SELECT e.name, e.department, e.salary
FROM high_earners e
WHERE e.id IN (SELECT manager_id FROM managers)
ORDER BY e.salary DESC'''

print("\n=== Test 2: Multiple CTEs (Example 2) ===")
try:
    ctes, main = _parse_cte_blocks(test_sql2)
    print(f"CTEs parsed: {len(ctes)}")
    for c in ctes:
        print(f"  Name: {c['name']}")
except Exception as e:
    print(f"Error: {e}")

# Test Example 3: Recursive CTE
test_sql3 = '''employee_hierarchy AS (
    SELECT id, name, manager_id, department, salary, 0 as level
    FROM employees
    WHERE manager_id IS NULL
    UNION ALL
    SELECT e.id, e.name, e.manager_id, e.department, e.salary, eh.level + 1
    FROM employees e
    JOIN employee_hierarchy eh ON e.manager_id = eh.id
)
SELECT name, level, department, salary
FROM employee_hierarchy
ORDER BY department, level, name'''

print("\n=== Test 3: Recursive CTE (Example 3) ===")
try:
    ctes, main = _parse_cte_blocks(test_sql3)
    print(f"CTEs parsed: {len(ctes)}")
    for c in ctes:
        print(f"  Name: {c['name']}")
        print(f"  Query has UNION ALL: {'UNION ALL' in c['query'].upper()}")
except Exception as e:
    print(f"Error: {e}")

# Test Example 4: Scalar subqueries - not a CTE, just a regular query
test_sql4 = '''SELECT
    name,
    salary,
    (SELECT AVG(salary) FROM employees) as company_avg,
    (SELECT COUNT(*) FROM employees e2 WHERE e2.salary > e1.salary) as people_earning_more
FROM employees e1
ORDER BY salary DESC'''

print("\n=== Test 4: Query without CTE (Example 4) ===")
try:
    # This should NOT be parsed as a CTE
    ctes, main = _parse_cte_blocks(test_sql4)
    print(f"CTEs parsed: {len(ctes)}")
except Exception as e:
    print(f"Error: {e}")
