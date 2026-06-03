#!/usr/bin/env python3
"""ETL Pipeline CLI - Parses, validates, and normalizes an ETL pipeline specification."""

import sys
import json

# --- Expression Syntax Validation ---

import re

class ExpressionError(Exception):
    def __init__(self, message: str):
        super().__init__()
        self.message = message


class UnknownOpError(Exception):
    def __init__(self, op_name: str):
        super().__init__()
        self.op_name = op_name


class ValidationError(Exception):
    def __init__(self, message: str, field: str = None):
        super().__init__()
        self.message = message
        self.field = field


def validate_expression(expr: str) -> None:
    """Reject obvious syntax errors in arithmetic/comparison expressions."""
    stripped = expr.strip()
    if not stripped:
        raise ExpressionError("empty expression")

    # Consecutive operators (e.g., '^^', '+-', '**', '//', '&&', '||')
    if re.search(r'[+\-*/&|]{2,}', stripped):
        raise ExpressionError("consecutive operators")

    # Operators at start/end
    if re.match(r'^[+\-*/]', stripped) or re.search(r'[+\-*/]$', stripped):
        # Single - at start is allowed (negative number)
        if not re.match(r'^[-+*/]', stripped) and re.search(r'[+*/]', stripped):
            # Actually check more carefully
            if stripped[0] in '+*/' or stripped[-1] in '+-*/':
                raise ExpressionError("operator in invalid position")

    # Parentheses balance
    stack = 0
    for ch in stripped:
        if ch == '(':
            stack += 1
        elif ch == ')':
            stack -= 1
            if stack < 0:
                raise ExpressionError("unmatched closing parenthesis")
    if stack != 0:
        raise ExpressionError("unmatched parentheses")

    # Adjacent parens or parens next to operators are fine;
    # we only want to catch obvious syntax errors.
    # Ensure two literals or identifiers aren't directly adjacent.
    # Tokenize simply: replace '(' and ')' with spaces, then split.
    spaced = re.sub(r'([()])', r' \1 ', stripped)
    tokens = spaced.split()
    if len(tokens) == 0:
        raise ExpressionError("empty expression")

    # Reject consecutive identifiers without operator
    prev_type = None  # 'ident', 'number', 'op', 'paren'

    i = 0
    while i < len(tokens):
        tok = tokens[i]

        # Check for malformed tokens (multiple dots, etc.)
        if tok.isdigit():
            cur_type = 'number'
        elif re.fullmatch(r'\d+\.\d+([eE][+-]?\d+)?', tok):
            cur_type = 'number'
        elif re.fullmatch(r'\d+\.?\d*[eE][+-]?\d+', tok):
            cur_type = 'number'
        elif tok in ('and', 'or'):
            cur_type = 'op_word'
        elif tok in ('(', ')'):
            cur_type = 'paren'
        else:
            # identifier / column name
            cur_type = 'ident'

        if prev_type == 'ident' and cur_type == 'ident':
            raise ExpressionError("consecutive identifiers without operator")
        if prev_type == 'number' and cur_type == 'number':
            raise ExpressionError("consecutive numbers without operator")

        # operator detection
        if tok in ('+', '-', '*', '/', '>', '>=', '<', '<=', '==', '!='):
            cur_type = 'op'
        elif tok in ('and', 'or', 'not'):
            cur_type = 'op'

        if cur_type == 'op' and prev_type == 'op' and tok not in ('not',):
            # 'not' is a unary keyword but can follow another word op
            raise ExpressionError("consecutive operators")
        if tok == 'not' and prev_type == 'op_word' and tokens[i - 1] not in ('and', 'or'):
            # actually 'not' is tricky; but consecutive 'not not' already caught above
            pass

        prev_type = cur_type
        i += 1


# --- Step Normalization ---

def normalize_op(op: str) -> str:
    """Normalize operation name: trim and lower."""
    stripped = op.strip()
    if not stripped:
        raise ValueError("empty op")
    lower = stripped.lower()
    # Validate against known ops (used for error messages)
    return lower


def normalize_select(step: dict, index: int) -> dict:
    """Select step: columns must be a non-empty list of strings."""
    if 'columns' not in step:
        raise KeyError("columns")
    cols = step['columns']
    if not isinstance(cols, list) or len(cols) == 0:
        raise TypeError("columns must be a non-empty array")
    if not all(isinstance(c, str) for c in cols):
        raise TypeError("columns elements must be strings")
    # Column names preserved exactly (spaces significant)
    result = {'op': 'select', 'columns': cols}
    # Known extra keys none (all others dropped)
    return result


def normalize_filter(step: dict, index: int) -> dict:
    """Filter step: 'where' must be a non-empty string expression."""
    if 'where' not in step:
        raise KeyError("where")
    where_expr = step['where']
    if not isinstance(where_expr, str):
        raise TypeError("where must be a string")
    trimmed = where_expr.strip()
    if not trimmed:
        raise ValueError("where expression is empty after trimming")
    validate_expression(trimmed)
    result = {'op': 'filter', 'where': trimmed}
    return result


def normalize_map(step: dict, index: int) -> dict:
    """Map step: 'as' and 'expr' required."""
    if 'as' not in step:
        raise KeyError("as")
    if 'expr' not in step:
        raise KeyError("expr")
    as_name = step['as']
    expr = step['expr']
    if not isinstance(as_name, str):
        raise TypeError("as must be a string")
    if not isinstance(expr, str):
        raise TypeError("expr must be a string")
    as_trimmed = as_name.strip()
    if not as_trimmed:
        raise ValueError("as is empty after trimming")
    expr_trimmed = expr.strip()
    if not expr_trimmed:
        raise ValueError("expr is empty after trimming")
    validate_expression(expr_trimmed)
    result = {'op': 'map', 'as': as_trimmed, 'expr': expr_trimmed}
    return result


def normalize_rename(step: dict, index: int) -> dict:
    """Rename step: either 'from'/'to' or 'mapping'."""
    has_from_to = ('from' in step) and ('to' in step)
    has_mapping = 'mapping' in step

    if not (has_from_to or has_mapping):
        # Determine which is missing
        if 'from' not in step:
            raise KeyError("from")
        elif 'to' not in step:
            raise KeyError("to")
        else:
            raise KeyError("mapping")

    if has_from_to:
        from_val = step['from']
        to_val = step['to']
        if not isinstance(from_val, str):
            raise TypeError("from must be a string")
        if not isinstance(to_val, str):
            raise TypeError("to must be a string")
        from_trimmed = from_val.strip()
        to_trimmed = to_val.strip()
        if not from_trimmed:
            raise ValueError("from is empty after trimming")
        if not to_trimmed:
            raise ValueError("to is empty after trimming")
        mapping = {from_trimmed: to_trimmed}
    else:
        mapping_obj = step['mapping']
        if not isinstance(mapping_obj, dict):
            raise TypeError("mapping must be an object")
        # Preserve keys/values exactly (spaces significant)
        mapping = mapping_obj

    return {'op': 'rename', 'mapping': mapping}


def normalize_limit(step: dict, index: int) -> dict:
    """Limit step: 'n' must be a non-negative integer."""
    if 'n' not in step:
        raise KeyError("n")
    n = step['n']
    if not isinstance(n, int):
        raise TypeError("n must be an integer")
    if n < 0:
        raise ValueError("n must be >= 0")
    return {'op': 'limit', 'n': n}


SUPPORTED_OPS = {
    'select': normalize_select,
    'filter': normalize_filter,
    'map': normalize_map,
    'rename': normalize_rename,
    'limit': normalize_limit,
}


def normalize_step(step: dict, index: int) -> dict:
    """Normalize a single pipeline step."""
    if not isinstance(step, dict):
        raise TypeError("step must be an object")
    if 'op' not in step:
        raise ValidationError("missing 'op' in step", field="op")

    op_raw = step['op']
    if not isinstance(op_raw, str):
        raise TypeError("op must be a string")

    normalized_op = normalize_op(op_raw)

    if normalized_op not in SUPPORTED_OPS:
        raise UnknownOpError(normalized_op)

    # Drop unknown keys before passing to normalizer
    known_op_keys = {
        'select': {'columns'},
        'filter': {'where'},
        'map': {'as', 'expr'},
        'rename': {'from', 'to', 'mapping'},
        'limit': {'n'},
    }
    # Also include 'op' so we don't drop it
    allowed_keys = known_op_keys[normalized_op] | {'op'}
    filtered_step = {k: v for k, v in step.items() if k in allowed_keys}

    try:
        return SUPPORTED_OPS[normalized_op](filtered_step, index)
    except (ValidationError, TypeError, ValueError, ExpressionError) as e:
        # Re-raise with context
        raise


# --- Main ---

def main() -> None:
    try:
        data = json.load(sys.stdin)
    except json.JSONDecodeError as e:
        # Not valid JSON
        print(json.dumps({
            "status": "error",
            "error_code": "SCHEMA_VALIDATION_FAILED",
            "message": f"ETL_ERROR: invalid JSON: {e}",
            "path": "",
        }), flush=True)
        sys.exit(1)

    # Validate top-level structure
    if not isinstance(data, dict):
        print(json.dumps({
            "status": "error",
            "error_code": "SCHEMA_VALIDATION_FAILED",
            "message": "ETL_ERROR: root must be an object",
            "path": "",
        }), flush=True)
        sys.exit(1)

    # 'pipeline' is required (though steps can be empty)
    if 'pipeline' not in data:
        print(json.dumps({
            "status": "error",
            "error_code": "SCHEMA_VALIDATION_FAILED",
            "message": "ETL_ERROR: missing 'pipeline' key",
            "path": "",
        }), flush=True)
        sys.exit(1)

    pipeline = data['pipeline']
    if not isinstance(pipeline, dict):
        print(json.dumps({
            "status": "error",
            "error_code": "SCHEMA_VALIDATION_FAILED",
            "message": "ETL_ERROR: 'pipeline' must be an object",
            "path": "pipeline",
        }), flush=True)
        sys.exit(1)

    if 'steps' not in pipeline:
        print(json.dumps({
            "status": "error",
            "error_code": "SCHEMA_VALIDATION_FAILED",
            "message": "ETL_ERROR: missing 'pipeline.steps'",
            "path": "pipeline.steps",
        }), flush=True)
        sys.exit(1)

    steps = pipeline['steps']
    if not isinstance(steps, list):
        print(json.dumps({
            "status": "error",
            "error_code": "SCHEMA_VALIDATION_FAILED",
            "message": "ETL_ERROR: 'pipeline.steps' must be an array",
            "path": "pipeline.steps",
        }), flush=True)
        sys.exit(1)

    # dataset is required; each element must be an object
    if 'dataset' not in data:
        print(json.dumps({
            "status": "error",
            "error_code": "SCHEMA_VALIDATION_FAILED",
            "message": "ETL_ERROR: missing 'dataset' key",
            "path": "dataset",
        }), flush=True)
        sys.exit(1)

    dataset = data['dataset']
    if not isinstance(dataset, list):
        print(json.dumps({
            "status": "error",
            "error_code": "SCHEMA_VALIDATION_FAILED",
            "message": "ETL_ERROR: 'dataset' must be an array",
            "path": "dataset",
        }), flush=True)
        sys.exit(1)

    for i, row in enumerate(dataset):
        if not isinstance(row, dict):
            print(json.dumps({
                "status": "error",
                "error_code": "SCHEMA_VALIDATION_FAILED",
                "message": f"ETL_ERROR: dataset[{i}] must be an object",
                "path": f"dataset[{i}]",
            }), flush=True)
            sys.exit(1)

    # Process steps
    normalized_steps = []
    for i, step in enumerate(steps):
        path_prefix = f"pipeline.steps[{i}]"
        try:
            norm = normalize_step(step, i)
            normalized_steps.append(norm)
        except UnknownOpError as e:
            print(json.dumps({
                "status": "error",
                "error_code": "UNKNOWN_OP",
                "message": f"ETL_ERROR: unsupported op '{e.op_name}'",
                "path": f"{path_prefix}.op",
            }), flush=True)
            sys.exit(1)
        except ValidationError as e:
            field = e.field if e.field else ""
            print(json.dumps({
                "status": "error",
                "error_code": "SCHEMA_VALIDATION_FAILED",
                "message": f"ETL_ERROR: {e.message}",
                "path": f"{path_prefix}.{field}" if field else path_prefix,
            }), flush=True)
            sys.exit(1)
        except TypeError as e:
            msg = str(e)
            # Infer field from message
            if "must be a string" in msg:
                if "op" in msg:
                    print(json.dumps({
                        "status": "error",
                        "error_code": "SCHEMA_VALIDATION_FAILED",
                        "message": f"ETL_ERROR: {msg}",
                        "path": f"{path_prefix}.op",
                    }), flush=True)
                else:
                    print(json.dumps({
                        "status": "error",
                        "error_code": "SCHEMA_VALIDATION_FAILED",
                        "message": f"ETL_ERROR: {msg}",
                        "path": f"{path_prefix}",
                    }), flush=True)
            else:
                print(json.dumps({
                    "status": "error",
                    "error_code": "SCHEMA_VALIDATION_FAILED",
                    "message": f"ETL_ERROR: {msg}",
                    "path": f"{path_prefix}",
                }), flush=True)
            sys.exit(1)
        except ValueError as e:
            msg = str(e)
            if "empty" in msg:
                # Infer which field
                if "where expression" in msg:
                    print(json.dumps({
                        "status": "error",
                        "error_code": "BAD_EXPR",
                        "message": f"ETL_ERROR: {msg}",
                        "path": f"{path_prefix}.where",
                    }), flush=True)
                elif "expr" in msg:
                    print(json.dumps({
                        "status": "error",
                        "error_code": "BAD_EXPR",
                        "message": f"ETL_ERROR: {msg}",
                        "path": f"{path_prefix}.expr",
                    }), flush=True)
                elif "from" in msg:
                    print(json.dumps({
                        "status": "error",
                        "error_code": "SCHEMA_VALIDATION_FAILED",
                        "message": f"ETL_ERROR: {msg}",
                        "path": f"{path_prefix}.from",
                    }), flush=True)
                elif "to" in msg:
                    print(json.dumps({
                        "status": "error",
                        "error_code": "SCHEMA_VALIDATION_FAILED",
                        "message": f"ETL_ERROR: {msg}",
                        "path": f"{path_prefix}.to",
                    }), flush=True)
                elif "as" in msg:
                    print(json.dumps({
                        "status": "error",
                        "error_code": "SCHEMA_VALIDATION_FAILED",
                        "message": f"ETL_ERROR: {msg}",
                        "path": f"{path_prefix}.as",
                    }), flush=True)
                else:
                    print(json.dumps({
                        "status": "error",
                        "error_code": "SCHEMA_VALIDATION_FAILED",
                        "message": f"ETL_ERROR: {msg}",
                        "path": f"{path_prefix}",
                    }), flush=True)
            else:
                print(json.dumps({
                    "status": "error",
                    "error_code": "SCHEMA_VALIDATION_FAILED",
                    "message": f"ETL_ERROR: {msg}",
                    "path": f"{path_prefix}",
                }), flush=True)
            sys.exit(1)
        except ExpressionError as e:
            # Determine if this is a filter or map expr
            step_fields = list(step.keys())
            if 'where' in step_fields:
                print(json.dumps({
                    "status": "error",
                    "error_code": "BAD_EXPR",
                    "message": f"ETL_ERROR: {e.message}",
                    "path": f"{path_prefix}.where",
                }), flush=True)
            else:
                print(json.dumps({
                    "status": "error",
                    "error_code": "BAD_EXPR",
                    "message": f"ETL_ERROR: {e.message}",
                    "path": f"{path_prefix}.expr",
                }), flush=True)
            sys.exit(1)

    # Build final output
    output = {
        "status": "ok",
        "normalized": {
            "steps": normalized_steps
        }
    }
    print(json.dumps(output), flush=True)
    sys.exit(0)


if __name__ == "__main__":
    main()
