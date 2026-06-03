#!/usr/bin/env python3
"""ETL Pipeline Executor - Reads JSON from STDIN, executes ETL pipeline."""

import argparse
import json
import sys
from typing import Any, Callable


def is_valid_expr(expr: str) -> bool:
    """Check for obvious syntax errors in expressions.

    Rejects consecutive operators (e.g., '^^', '+-', '*/', etc.).
    """
    # Common operators that should not appear consecutively (except allowed pairs)
    operators = set('+-*/%<>=!&|^?')
    # Allowed consecutive pairs
    allowed_pairs = {
        ('>', '='), ('<', '='), ('!', '='),  # >=, <=, !=
        ('*', '/'), ('/', '*'),  # */, /* (division/multiplication adjacent)
        ('<', '<'), ('>', '>'),  # <<, >>
        ('|', '|'), ('&', '&'),  # ||, &&
        ('=', '='),  # ==
    }

    i = 0
    while i < len(expr) - 1:
        curr = expr[i]
        next_char = expr[i + 1]

        # Check if current and next are both operators
        if curr in operators and next_char in operators:
            # Special case: unary minus at start or after operator/open paren/brace/bracket/comma
            if curr == '-' and i == 0:
                i += 1
                continue
            if curr == '-' and expr[i-1] in '+-*/%<>=!&|^?({[,' and next_char not in operators:
                i += 1
                continue

            # Check if this is an allowed pair
            if {curr, next_char} in allowed_pairs:
                i += 1
                continue

            # Check for specific forbidden consecutive operators
            # e.g., ^^, ##, @@, ~~, $$(, $$, etc.
            if (curr == '^' and next_char == '^'):
                return False  # ^^
            if (curr == '#' and next_char == '#'):
                return False  # ##
            if (curr == '@' and next_char == '@'):
                return False  # @@
            if (curr == '~' and next_char == '~'):
                return False  # ~~
            if (curr == '?' and next_char == '?'):
                return False  # ??

            # General case: if both are arithmetic/bitwise operators and not a known comparison
            if curr in '+-*/%&|^?' and next_char in '+-*/%&|^?':
                return False

        i += 1

    return True


class ExpressionEvaluator:
    """Evaluates ETL expressions with proper null handling and type semantics."""

    # Supported operators and their priorities (higher = higher precedence)
    OPERATOR_PRECEDENCE = {
        '||': 1,
        '&&': 2,
        '==': 3, '!=': 3,
        '<': 4, '<=': 4, '>': 4, '>=': 4,
        '+': 5, '-': 5,
        '*': 6, '/': 6,
        '!': 7,  # unary
        '-': 7,  # unary minus
    }

    # Two-character operators (check before single-character)
    TWO_CHAR_OPS = {'==', '!=', '<=', '>=', '||', '&&'}

    def __init__(self):
        self.tokens = []
        self.pos = 0

    def tokenize(self, expr: str) -> list:
        """Convert expression string into tokens."""
        tokens = []
        i = 0
        while i < len(expr):
            c = expr[i]

            # Skip whitespace
            if c.isspace():
                i += 1
                continue

            # Check for two-character operators
            if i + 1 < len(expr):
                two_char = expr[i:i+2]
                if two_char in self.TWO_CHAR_OPS:
                    tokens.append(two_char)
                    i += 2
                    continue

            # Single-character operators and punctuation
            if c in '()':
                tokens.append(c)
                i += 1
                continue

            # Numbers (including decimals)
            if c.isdigit() or (c == '.' and i + 1 < len(expr) and expr[i+1].isdigit()):
                j = i
                has_dot = c == '.'
                j += 1
                while j < len(expr):
                    if expr[j].isdigit():
                        j += 1
                    elif expr[j] == '.' and not has_dot:
                        has_dot = True
                        j += 1
                    else:
                        break
                num_str = expr[i:j]
                if '.' in num_str:
                    tokens.append(('NUMBER', float(num_str)))
                else:
                    tokens.append(('NUMBER', int(num_str)))
                i = j
                continue

            # Strings (double-quoted)
            if c == '"':
                j = i + 1
                while j < len(expr) and expr[j] != '"':
                    j += 1
                if j < len(expr):  # Found closing quote
                    tokens.append(('STRING', expr[i+1:j]))
                    i = j + 1
                else:
                    raise ValueError(f"Unterminated string: {expr[i:]}")
                continue

            # Identifiers (field names, boolean literals)
            if c.isalpha() or c == '_':
                j = i
                while j < len(expr) and (expr[j].isalnum() or expr[j] == '_'):
                    j += 1
                ident = expr[i:j]
                if ident in ('true', 'false'):
                    tokens.append(('BOOL', ident == 'true'))
                elif ident == 'null':
                    tokens.append(('NULL', None))
                else:
                    tokens.append(('IDENTIFIER', ident))
                i = j
                continue

            # Single-character operators
            if c in '+-*/%<>=!&|^?':
                tokens.append(c)
                i += 1
                continue

            raise ValueError(f"Unexpected character: {c}")

        return tokens

    def parse_expression(self, tokens: list) -> Any:
        """Parse tokens into an AST."""
        self.tokens = tokens
        self.pos = 0
        result = self.parse_or()
        if self.pos < len(self.tokens):
            raise ValueError(f"Unexpected token at end: {self.tokens[self.pos]}")
        return result

    def peek(self) -> Any:
        """Look at current token without consuming."""
        if self.pos < len(self.tokens):
            return self.tokens[self.pos]
        return None

    def consume(self) -> Any:
        """Consume and return current token."""
        tok = self.peek()
        self.pos += 1
        return tok

    def parse_or(self) -> Any:
        """Parse || (logical OR)."""
        left = self.parse_and()
        while self.peek() == '||':
            self.consume()
            right = self.parse_and()
            left = ('OR', left, right)
        return left

    def parse_and(self) -> Any:
        """Parse && (logical AND)."""
        left = self.parse_equality()
        while self.peek() == '&&':
            self.consume()
            right = self.parse_equality()
            left = ('AND', left, right)
        return left

    def parse_equality(self) -> Any:
        """Parse == and !=."""
        left = self.parse_comparison()
        while self.peek() in ('==', '!='):
            op = self.consume()
            right = self.parse_comparison()
            left = (op, left, right)
        return left

    def parse_comparison(self) -> Any:
        """Parse <, <=, >, >=."""
        left = self.parse_additive()
        while self.peek() in ('<', '<=', '>', '>='):
            op = self.consume()
            right = self.parse_additive()
            left = (op, left, right)
        return left

    def parse_additive(self) -> Any:
        """Parse + and -."""
        left = self.parse_multiplicative()
        while self.peek() in ('+', '-'):
            op = self.consume()
            right = self.parse_multiplicative()
            left = (op, left, right)
        return left

    def parse_multiplicative(self) -> Any:
        """Parse * and /."""
        left = self.parse_unary()
        while self.peek() in ('*', '/'):
            op = self.consume()
            right = self.parse_unary()
            left = (op, left, right)
        return left

    def parse_unary(self) -> Any:
        """Parse unary ! and -."""
        if self.peek() == '!':
            self.consume()
            return ('!', self.parse_unary())
        if self.peek() == '-':
            self.consume()
            return ('NEG', self.parse_unary())
        return self.parse_primary()

    def parse_primary(self) -> Any:
        """Parse primary expressions: literals, identifiers, parenthesized expressions."""
        tok = self.peek()
        if tok is None:
            raise ValueError("Unexpected end of expression")

        if tok == '(':
            self.consume()
            expr = self.parse_or()
            if self.peek() != ')':
                raise ValueError("Expected ')'")
            self.consume()
            return expr

        if isinstance(tok, tuple):
            token_type, value = tok
            self.consume()
            if token_type in ('NUMBER', 'STRING', 'BOOL'):
                return ('LITERAL', value)
            elif token_type == 'NULL':
                return ('LITERAL', None)
            elif token_type == 'IDENTIFIER':
                return ('IDENTIFIER', value)

        raise ValueError(f"Unexpected token: {tok}")

    def evaluate(self, ast: Any, row: dict) -> Any:
        """Evaluate AST against a row."""
        if isinstance(ast, tuple):
            op = ast[0]

            if op == 'LITERAL':
                return ast[1]  # Return the literal value

            if op == 'IDENTIFIER':
                ident = ast[1]
                return row.get(ident)  # Missing identifiers resolve to null

            if op in ('+', '-', '*', '/', '==', '!=', '<', '<=', '>', '>='):
                left = self.evaluate(ast[1], row)
                right = self.evaluate(ast[2], row)

                # Null handling for arithmetic
                if op in ('+', '-', '*', '/'):
                    if left is None or right is None:
                        return None

                # Null handling for comparisons
                if op in ('==', '!=', '<', '<=', '>', '>='):
                    if left is None or right is None:
                        if op in ('==', '!='):
                            return False  # Type mismatch or null involvement returns false
                        return False

                if op == '+':
                    return left + right
                if op == '-':
                    return left - right
                if op == '*':
                    return left * right
                if op == '/':
                    if right == 0:
                        return None  # Division by zero returns null
                    return left / right
                if op == '==':
                    return left == right
                if op == '!=':
                    return left != right
                if op == '<':
                    if not isinstance(left, (int, float)) or not isinstance(right, (int, float)):
                        return False
                    return left < right
                if op == '<=':
                    if not isinstance(left, (int, float)) or not isinstance(right, (int, float)):
                        return False
                    return left <= right
                if op == '>':
                    if not isinstance(left, (int, float)) or not isinstance(right, (int, float)):
                        return False
                    return left > right
                if op == '>=':
                    if not isinstance(left, (int, float)) or not isinstance(right, (int, float)):
                        return False
                    return left >= right

            if op == 'AND':
                left = self.evaluate(ast[1], row)
                right = self.evaluate(ast[2], row)
                # Null values are falsy
                if left is False or right is False:
                    return False
                if left is None or right is None:
                    return None
                return True

            if op == 'OR':
                left = self.evaluate(ast[1], row)
                right = self.evaluate(ast[2], row)
                # Null values are falsy
                if left is True or right is True:
                    return True
                if left is None and right is None:
                    return None
                if left is None:
                    return right if right is not False else None
                if right is None:
                    return left if left is not False else None
                return False

            if op == '!':
                val = self.evaluate(ast[1], row)
                if val is None:
                    return False  # Null values are falsy, so !null = false
                return not val

            if op == 'NEG':
                val = self.evaluate(ast[1], row)
                if val is None:
                    return None
                return -val

        # Handle literal values (not tuples)
        if ast is True or ast is False:
            return ast
        if isinstance(ast, (int, float)):
            return ast
        return ast

    def compile_and_evaluate(self, expr: str, row: dict) -> Any:
        """Compile expression and evaluate against row."""
        tokens = self.tokenize(expr)
        ast = self.parse_expression(tokens)
        return self.evaluate(ast, row)


def evaluate_expression(expr: str, row: dict) -> Any:
    """Evaluate an expression against a row. Returns None on parse error."""
    try:
        evaluator = ExpressionEvaluator()
        return evaluator.compile_and_evaluate(expr, row)
    except (ValueError, Exception):
        return None


def normalize_step(step: dict[str, Any], index: int) -> tuple[dict[str, Any] | None, str | None, str | None]:
    """Normalize a single step. Returns (normalized_step, error_code, path) or (None, error_code, path) on error."""
    if not isinstance(step, dict):
        return None, "SCHEMA_VALIDATION_FAILED", f"pipeline.steps[{index}]"

    # Get and validate op field
    if "op" not in step:
        return None, "SCHEMA_VALIDATION_FAILED", f"pipeline.steps[{index}]"

    op_raw = step["op"]
    if not isinstance(op_raw, str):
        return None, "SCHEMA_VALIDATION_FAILED", f"pipeline.steps[{index}].op"

    # Normalize op: trim and lowercase
    op_normalized = op_raw.strip().lower()

    if not op_normalized:
        return None, "SCHEMA_VALIDATION_FAILED", f"pipeline.steps[{index}].op"

    # Supported operations
    supported_ops = {"select", "filter", "map", "rename", "limit"}

    if op_normalized not in supported_ops:
        return None, "UNKNOWN_OP", f"pipeline.steps[{index}].op"

    # Build normalized step with op first, then other fields alphabetically
    normalized: dict[str, Any] = {"op": op_normalized}

    # Validate and normalize based on operation
    if op_normalized == "select":
        if "columns" not in step:
            return None, "SCHEMA_VALIDATION_FAILED", f"pipeline.steps[{index}]"
        columns = step["columns"]
        if not isinstance(columns, list):
            return None, "SCHEMA_VALIDATION_FAILED", f"pipeline.steps[{index}].columns"
        # Preserve column names exactly as-is (spaces are significant)
        for col in columns:
            if not isinstance(col, str):
                return None, "SCHEMA_VALIDATION_FAILED", f"pipeline.steps[{index}].columns"
        normalized["columns"] = columns

    elif op_normalized == "filter":
        if "where" not in step:
            return None, "SCHEMA_VALIDATION_FAILED", f"pipeline.steps[{index}]"
        where = step["where"]
        if not isinstance(where, str):
            return None, "SCHEMA_VALIDATION_FAILED", f"pipeline.steps[{index}].where"
        where_trimmed = where.strip()
        if not where_trimmed:
            return None, "SCHEMA_VALIDATION_FAILED", f"pipeline.steps[{index}].where"
        if not is_valid_expr(where_trimmed):
            return None, "BAD_EXPR", f"pipeline.steps[{index}].where"
        normalized["where"] = where_trimmed

    elif op_normalized == "map":
        if "as" not in step or "expr" not in step:
            return None, "SCHEMA_VALIDATION_FAILED", f"pipeline.steps[{index}]"
        as_field = step["as"]
        expr = step["expr"]
        if not isinstance(as_field, str) or not isinstance(expr, str):
            return None, "SCHEMA_VALIDATION_FAILED", f"pipeline.steps[{index}]"
        # Trim both fields
        as_trimmed = as_field.strip()
        expr_trimmed = expr.strip()
        if not as_trimmed or not expr_trimmed:
            return None, "SCHEMA_VALIDATION_FAILED", f"pipeline.steps[{index}]"
        # Validate expression
        if not is_valid_expr(expr_trimmed):
            return None, "BAD_EXPR", f"pipeline.steps[{index}].expr"
        normalized["as"] = as_trimmed
        normalized["expr"] = expr_trimmed

    elif op_normalized == "rename":
        # Check for from/to or mapping
        has_from_to = "from" in step and "to" in step
        has_mapping = "mapping" in step

        if not has_from_to and not has_mapping:
            return None, "SCHEMA_VALIDATION_FAILED", f"pipeline.steps[{index}]"

        if has_from_to:
            from_val = step["from"]
            to_val = step["to"]
            if not isinstance(from_val, str) or not isinstance(to_val, str):
                return None, "SCHEMA_VALIDATION_FAILED", f"pipeline.steps[{index}]"
            from_trimmed = from_val.strip()
            to_trimmed = to_val.strip()
            if not from_trimmed or not to_trimmed:
                return None, "SCHEMA_VALIDATION_FAILED", f"pipeline.steps[{index}]"
            # Convert to mapping format
            normalized["mapping"] = {from_trimmed: to_trimmed}

        if has_mapping:
            mapping = step["mapping"]
            if not isinstance(mapping, dict):
                return None, "SCHEMA_VALIDATION_FAILED", f"pipeline.steps[{index}].mapping"
            # Preserve keys and values exactly
            normalized_mapping = {}
            for k, v in mapping.items():
                if not isinstance(k, str) or not isinstance(v, str):
                    return None, "SCHEMA_VALIDATION_FAILED", f"pipeline.steps[{index}].mapping"
                normalized_mapping[k] = v
            normalized["mapping"] = normalized_mapping

    elif op_normalized == "limit":
        if "n" not in step:
            return None, "SCHEMA_VALIDATION_FAILED", f"pipeline.steps[{index}]"
        n_val = step["n"]
        if not isinstance(n_val, int):
            return None, "SCHEMA_VALIDATION_FAILED", f"pipeline.steps[{index}].n"
        if n_val < 0:
            return None, "SCHEMA_VALIDATION_FAILED", f"pipeline.steps[{index}].n"
        normalized["n"] = n_val

    # Drop unknown keys (already handled by only adding known fields to normalized)

    return normalized, None, None


def execute_pipeline(pipeline: dict, dataset: list[dict]) -> tuple[list[dict], str | None, str | None]:
    """Execute the full pipeline on the dataset. Returns (result_rows, error_code, path)."""
    steps = pipeline["steps"]
    current_rows = dataset

    for i, step in enumerate(steps):
        op = step["op"]

        # Store step index for error messages
        global step_index
        step_index = i

        if op == "select":
            # Validate columns exist
            columns = step["columns"]
            if current_rows:
                first_row_keys = set(current_rows[0].keys())
                for col in columns:
                    if col not in first_row_keys:
                        return None, "MISSING_COLUMN", f"pipeline.steps[{i}].columns[{columns.index(col)}]"

            current_rows = [{col: row[col] for col in columns} for row in current_rows]

        elif op == "filter":
            where_expr = step["where"]
            filtered = []
            for row in current_rows:
                try:
                    result_val = evaluate_expression(where_expr, row)
                    if result_val is True:
                        filtered.append(row)
                except Exception as e:
                    return None, "EXECUTION_FAILED", f"pipeline.steps[{i}].where"
            current_rows = filtered

        elif op == "map":
            as_field = step["as"]
            expr = step["expr"]
            mapped = []
            for row in current_rows:
                new_row = row.copy()
                try:
                    eval_result = evaluate_expression(expr, row)
                    new_row[as_field] = eval_result
                    mapped.append(new_row)
                except Exception as e:
                    return None, "EXECUTION_FAILED", f"pipeline.steps[{i}].expr"
            current_rows = mapped

        elif op == "rename":
            mapping = step["mapping"]
            renamed = []
            for row in current_rows:
                new_row = row.copy()
                for src_col, target_col in mapping.items():
                    if src_col in new_row:
                        val = new_row.pop(src_col)
                        new_row[target_col] = val
                renamed.append(new_row)
            current_rows = renamed

        elif op == "limit":
            n = step["n"]
            current_rows = current_rows[:n]

    return current_rows, None, None


# Global variable for step index (used in error messages)
step_index = 0


def validate_and_normalize_pipeline(data: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None, str | None]:
    """Validate and normalize the entire pipeline.

    Returns (result_dict, error_code, path) or (None, error_code, path) on error.
    """
    # Check top-level structure
    if "pipeline" not in data:
        return None, "SCHEMA_VALIDATION_FAILED", "pipeline"

    if "dataset" not in data:
        return None, "SCHEMA_VALIDATION_FAILED", "dataset"

    pipeline = data["pipeline"]
    dataset = data["dataset"]

    if not isinstance(pipeline, dict):
        return None, "SCHEMA_VALIDATION_FAILED", "pipeline"

    if not isinstance(dataset, list):
        return None, "SCHEMA_VALIDATION_FAILED", "dataset"

    # Validate each element in dataset must be a JSON object
    for i, row in enumerate(dataset):
        if not isinstance(row, dict):
            return None, "SCHEMA_VALIDATION_FAILED", f"dataset[{i}]"

    # Check steps
    if "steps" not in pipeline:
        return None, "SCHEMA_VALIDATION_FAILED", "pipeline.steps"

    steps = pipeline["steps"]
    if not isinstance(steps, list):
        return None, "SCHEMA_VALIDATION_FAILED", "pipeline.steps"

    # Validate and normalize each step
    normalized_steps = []
    for i, step in enumerate(steps):
        normalized_step, error_code, path = normalize_step(step, i)
        if error_code:
            return None, error_code, path
        normalized_steps.append(normalized_step)

    result = {
        "status": "ok",
        "normalized": {
            "steps": normalized_steps
        }
    }
    return result, None, None


def format_error(error_code: str, path: str) -> str:
    """Format error message based on error code."""
    if error_code == "SCHEMA_VALIDATION_FAILED":
        return f"ETL_ERROR: schema validation failed at {path}"
    elif error_code == "BAD_EXPR":
        return f"ETL_ERROR: invalid expression at {path}"
    elif error_code == "MISSING_COLUMN":
        return f"ETL_ERROR: column not found in row"
    elif error_code == "EXECUTION_FAILED":
        return f"ETL_ERROR: execution failed at {path}"
    else:
        return f"ETL_ERROR: {error_code}"


def main() -> None:
    """Main entry point."""
    parser = argparse.ArgumentParser(description="ETL Pipeline Executor")
    parser.add_argument(
        "--execute",
        action="store_true",
        default=False,
        help="Execute the pipeline and return data and metrics (default: return normalized)"
    )
    args = parser.parse_args()

    try:
        # Read JSON from STDIN
        input_data = json.load(sys.stdin)
    except json.JSONDecodeError as e:
        error_response = {
            "status": "error",
            "error_code": "SCHEMA_VALIDATION_FAILED",
            "message": f"ETL_ERROR: Invalid JSON - {e}",
            "path": ""
        }
        print(json.dumps(error_response))
        sys.exit(1)

    # Validate input structure
    if "pipeline" not in input_data or "dataset" not in input_data:
        error_response = {
            "status": "error",
            "error_code": "SCHEMA_VALIDATION_FAILED",
            "message": "ETL_ERROR: Missing required fields 'pipeline' and 'dataset'",
            "path": ""
        }
        print(json.dumps(error_response))
        sys.exit(1)

    pipeline = input_data["pipeline"]
    dataset = input_data["dataset"]

    # Validate pipeline structure
    if not isinstance(pipeline, dict) or "steps" not in pipeline:
        error_response = {
            "status": "error",
            "error_code": "SCHEMA_VALIDATION_FAILED",
            "message": "ETL_ERROR: pipeline must have 'steps' field",
            "path": "pipeline"
        }
        print(json.dumps(error_response))
        sys.exit(1)

    if not isinstance(dataset, list):
        error_response = {
            "status": "error",
            "error_code": "SCHEMA_VALIDATION_FAILED",
            "message": "ETL_ERROR: dataset must be a list",
            "path": "dataset"
        }
        print(json.dumps(error_response))
        sys.exit(1)

    # Validate each row in dataset
    for i, row in enumerate(dataset):
        if not isinstance(row, dict):
            error_response = {
                "status": "error",
                "error_code": "SCHEMA_VALIDATION_FAILED",
                "message": f"ETL_ERROR: dataset[{i}] must be a JSON object",
                "path": f"dataset[{i}]"
            }
            print(json.dumps(error_response))
            sys.exit(1)

    steps = pipeline["steps"]
    if not isinstance(steps, list):
        error_response = {
            "status": "error",
            "error_code": "SCHEMA_VALIDATION_FAILED",
            "message": "ETL_ERROR: pipeline.steps must be a list",
            "path": "pipeline.steps"
        }
        print(json.dumps(error_response))
        sys.exit(1)

    # Validate and normalize each step
    normalized_steps = []
    for i, step in enumerate(steps):
        normalized_step, error_code, path = normalize_step(step, i)
        if error_code:
            error_response = {
                "status": "error",
                "error_code": error_code,
                "message": format_error(error_code, path),
                "path": path
            }
            if error_code == "UNKNOWN_OP":
                try:
                    op_value = steps[i]["op"]
                    error_response["message"] = f"ETL_ERROR: unsupported op '{op_value.lower()}'"
                except (IndexError, KeyError):
                    pass
            print(json.dumps(error_response))
            sys.exit(1)
        normalized_steps.append(normalized_step)

    # If --execute flag is used, execute the pipeline
    if args.execute:
        # Build normalized pipeline for execution
        normalized_pipeline = {"steps": normalized_steps}

        # Execute the pipeline
        result_rows, error_code, path = execute_pipeline(normalized_pipeline, dataset)

        if error_code:
            error_response = {
                "status": "error",
                "error_code": error_code,
                "message": format_error(error_code, path),
                "path": path
            }
            print(json.dumps(error_response))
            sys.exit(1)

        # Success response with data and metrics
        success_response = {
            "status": "ok",
            "data": result_rows,
            "metrics": {
                "rows_in": len(dataset),
                "rows_out": len(result_rows)
            }
        }
        print(json.dumps(success_response))
        sys.exit(0)
    else:
        # Return normalized (backward compatible with checkpoint 1)
        result = {
            "status": "ok",
            "normalized": {
                "steps": normalized_steps
            }
        }
        print(json.dumps(result))
        sys.exit(0)


if __name__ == "__main__":
    main()
