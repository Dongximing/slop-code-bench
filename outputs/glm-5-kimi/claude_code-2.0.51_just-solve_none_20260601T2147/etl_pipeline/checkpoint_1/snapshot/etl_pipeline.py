#!/usr/bin/env python3
"""ETL Pipeline Parser - validates and normalizes pipeline specifications."""

import json
import re
import sys
from typing import Any, Dict, List, Optional, Tuple


class ETLError(Exception):
    """Custom exception for ETL pipeline errors."""
    def __init__(self, error_code: str, message: str, path: str):
        self.error_code = error_code
        self.message = message
        self.path = path
        super().__init__(message)


# Valid operators for expressions
VALID_OPERATORS = {'+', '-', '*', '/', '>', '>=', '<', '<=', '==', '!=', 'and', 'or', 'not'}


def validate_expression(expr: str, path: str) -> None:
    """
    Validate expression syntax.
    Supports arithmetic, comparison, boolean operators, and parentheses.
    Rejects obvious syntax errors like consecutive operators.
    """
    if not expr or not expr.strip():
        raise ETLError('BAD_EXPR', 'ETL_ERROR: empty expression', path)

    expr = expr.strip()

    # Check for consecutive operators (basic syntax check)
    # This regex looks for patterns like ^^, ++, **, etc. but allows valid sequences
    # We need to be careful: >= is valid, but >> is not

    # Remove valid multi-char operators first
    temp = expr
    temp = temp.replace('>=', ' ')
    temp = temp.replace('<=', ' ')
    temp = temp.replace('==', ' ')
    temp = temp.replace('!=', ' ')
    temp = temp.replace('and', ' ')
    temp = temp.replace('or', ' ')
    temp = temp.replace('not', ' ')

    # Now check for consecutive operator characters
    # Valid single-char operators: + - * / > < = !
    # After removing multi-char ops, we shouldn't have consecutive operator chars
    # except for specific cases

    # Pattern to detect invalid consecutive operators
    # After substitution, check for sequences like ++, --, **, //, >>, <<, == (remaining)
    consecutive_pattern = r'[\+\-\*\/\^\&\|\!\~\%\<\>\=]{2,}'

    # But we need to be more nuanced - things like '--' could be in identifiers
    # Let's tokenize instead

    try:
        _tokenize_expression(expr)
    except ValueError as e:
        raise ETLError('BAD_EXPR', f'ETL_ERROR: {str(e)}', path)


def _is_binary_operator(token: str) -> bool:
    """Check if a token is a binary operator."""
    return token in ('+', '-', '*', '/', '>', '>=', '<', '<=', '==', '!=', 'and', 'or')


def _is_unary_operator(token: str) -> bool:
    """Check if a token can be a unary operator."""
    return token in ('not', '-')


def _is_operator(token: str) -> bool:
    """Check if a token is an operator (binary or unary)."""
    return _is_binary_operator(token) or _is_unary_operator(token)


def _tokenize_expression(expr: str) -> List[str]:
    """
    Tokenize an expression and validate syntax.
    Returns list of tokens.
    """
    tokens = []
    i = 0

    while i < len(expr):
        # Skip whitespace
        if expr[i].isspace():
            i += 1
            continue

        # Parentheses
        if expr[i] in '()':
            tokens.append(expr[i])
            i += 1
            continue

        # Numbers (including decimals)
        if expr[i].isdigit():
            j = i
            while j < len(expr) and (expr[j].isdigit() or expr[j] == '.'):
                j += 1
            tokens.append(expr[i:j])
            i = j
            continue

        # Identifiers and keywords
        if expr[i].isalpha() or expr[i] == '_':
            j = i
            while j < len(expr) and (expr[j].isalnum() or expr[j] == '_'):
                j += 1
            token = expr[i:j]
            tokens.append(token)
            i = j
            continue

        # Multi-char operators
        if i + 1 < len(expr):
            two_char = expr[i:i+2]
            if two_char in ('>=', '<=', '==', '!='):
                tokens.append(two_char)
                i += 2
                continue

        # Single-char operators
        if expr[i] in '+-*/><':
            tokens.append(expr[i])
            i += 1
            continue

        # Unknown character
        raise ValueError(f"invalid character '{expr[i]}' in expression")

    # Validate token sequence
    # Two consecutive binary operators is invalid (e.g., "and or", "> <")
    # But unary operators like 'not' can follow binary operators
    for i in range(len(tokens) - 1):
        curr, next_tok = tokens[i], tokens[i + 1]

        # Skip parentheses in check
        if curr in '()' or next_tok in '()':
            continue

        # Two consecutive binary operators is invalid
        if _is_binary_operator(curr) and _is_binary_operator(next_tok):
            raise ValueError(f"consecutive operators '{curr}' and '{next_tok}'")

    return tokens


def normalize_step(step: Dict[str, Any], index: int) -> Dict[str, Any]:
    """
    Normalize a pipeline step according to rules.
    Returns normalized step or raises ETLError.
    """
    path = f'pipeline.steps[{index}]'

    # Validate step is an object
    if not isinstance(step, dict):
        raise ETLError('SCHEMA_VALIDATION_FAILED',
                      'ETL_ERROR: step must be an object', path)

    # Get and validate op field
    if 'op' not in step:
        raise ETLError('SCHEMA_VALIDATION_FAILED',
                      'ETL_ERROR: missing required field "op"', path)

    op_value = step.get('op')
    if not isinstance(op_value, str):
        raise ETLError('SCHEMA_VALIDATION_FAILED',
                      'ETL_ERROR: op must be a string', f'{path}.op')

    # Normalize op: trim and lowercase
    op = op_value.strip().lower()

    # Check if empty after trim
    if not op:
        raise ETLError('SCHEMA_VALIDATION_FAILED',
                      'ETL_ERROR: op cannot be empty', f'{path}.op')

    # Validate known operations
    valid_ops = {'select', 'filter', 'map', 'rename', 'limit'}
    if op not in valid_ops:
        raise ETLError('UNKNOWN_OP',
                      f'ETL_ERROR: unsupported op \'{op}\'', f'{path}.op')

    # Build normalized step
    normalized: Dict[str, Any] = {'op': op}

    if op == 'select':
        # Requires columns (array of strings)
        if 'columns' not in step:
            raise ETLError('MISSING_COLUMN',
                          'ETL_ERROR: missing required field "columns"', f'{path}.columns')

        columns = step['columns']
        if not isinstance(columns, list):
            raise ETLError('SCHEMA_VALIDATION_FAILED',
                          'ETL_ERROR: columns must be an array', f'{path}.columns')

        normalized_columns = []
        for i, col in enumerate(columns):
            if not isinstance(col, str):
                raise ETLError('SCHEMA_VALIDATION_FAILED',
                              'ETL_ERROR: column names must be strings', f'{path}.columns[{i}]')
            # Column names preserved exactly (not trimmed)
            normalized_columns.append(col)

        normalized['columns'] = normalized_columns

    elif op == 'filter':
        # Requires where (string expression)
        if 'where' not in step:
            raise ETLError('MISSING_COLUMN',
                          'ETL_ERROR: missing required field "where"', f'{path}.where')

        where = step['where']
        if not isinstance(where, str):
            raise ETLError('SCHEMA_VALIDATION_FAILED',
                          'ETL_ERROR: where must be a string', f'{path}.where')

        # Trim expression
        where_trimmed = where.strip()
        if not where_trimmed:
            raise ETLError('BAD_EXPR',
                          'ETL_ERROR: where expression cannot be empty', f'{path}.where')

        # Validate expression
        validate_expression(where_trimmed, f'{path}.where')

        normalized['where'] = where_trimmed

    elif op == 'map':
        # Requires as (string) and expr (string expression)
        if 'as' not in step:
            raise ETLError('MISSING_COLUMN',
                          'ETL_ERROR: missing required field "as"', f'{path}.as')

        if 'expr' not in step:
            raise ETLError('MISSING_COLUMN',
                          'ETL_ERROR: missing required field "expr"', f'{path}.expr')

        as_value = step['as']
        if not isinstance(as_value, str):
            raise ETLError('SCHEMA_VALIDATION_FAILED',
                          'ETL_ERROR: as must be a string', f'{path}.as')

        expr_value = step['expr']
        if not isinstance(expr_value, str):
            raise ETLError('SCHEMA_VALIDATION_FAILED',
                          'ETL_ERROR: expr must be a string', f'{path}.expr')

        # Trim as field
        as_trimmed = as_value.strip()
        if not as_trimmed:
            raise ETLError('SCHEMA_VALIDATION_FAILED',
                          'ETL_ERROR: as field cannot be empty', f'{path}.as')

        # Trim expr field
        expr_trimmed = expr_value.strip()
        if not expr_trimmed:
            raise ETLError('BAD_EXPR',
                          'ETL_ERROR: expr expression cannot be empty', f'{path}.expr')

        # Validate expression
        validate_expression(expr_trimmed, f'{path}.expr')

        normalized['as'] = as_trimmed
        normalized['expr'] = expr_trimmed

    elif op == 'rename':
        # Requires either from/to OR mapping
        has_from_to = 'from' in step and 'to' in step
        has_mapping = 'mapping' in step

        if not has_from_to and not has_mapping:
            raise ETLError('MISSING_COLUMN',
                          'ETL_ERROR: rename requires either from/to or mapping', path)

        if has_mapping:
            mapping = step['mapping']
            if not isinstance(mapping, dict):
                raise ETLError('SCHEMA_VALIDATION_FAILED',
                              'ETL_ERROR: mapping must be an object', f'{path}.mapping')

            # Validate all keys and values are strings
            normalized_mapping = {}
            for k, v in mapping.items():
                if not isinstance(k, str) or not isinstance(v, str):
                    raise ETLError('SCHEMA_VALIDATION_FAILED',
                                  'ETL_ERROR: mapping keys and values must be strings', f'{path}.mapping')
                # Preserve exactly
                normalized_mapping[k] = v

            normalized['mapping'] = normalized_mapping
        else:
            # Convert from/to to mapping
            from_val = step['from']
            to_val = step['to']

            if not isinstance(from_val, str):
                raise ETLError('SCHEMA_VALIDATION_FAILED',
                              'ETL_ERROR: from must be a string', f'{path}.from')

            if not isinstance(to_val, str):
                raise ETLError('SCHEMA_VALIDATION_FAILED',
                              'ETL_ERROR: to must be a string', f'{path}.to')

            # Trim from/to before conversion
            from_trimmed = from_val.strip()
            to_trimmed = to_val.strip()

            if not from_trimmed:
                raise ETLError('SCHEMA_VALIDATION_FAILED',
                              'ETL_ERROR: from field cannot be empty', f'{path}.from')

            if not to_trimmed:
                raise ETLError('SCHEMA_VALIDATION_FAILED',
                              'ETL_ERROR: to field cannot be empty', f'{path}.to')

            normalized['mapping'] = {from_trimmed: to_trimmed}

    elif op == 'limit':
        # Requires n (integer >= 0)
        if 'n' not in step:
            raise ETLError('MISSING_COLUMN',
                          'ETL_ERROR: missing required field "n"', f'{path}.n')

        n = step['n']
        if not isinstance(n, int) or isinstance(n, bool):
            raise ETLError('SCHEMA_VALIDATION_FAILED',
                          'ETL_ERROR: n must be an integer', f'{path}.n')

        if n < 0:
            raise ETLError('SCHEMA_VALIDATION_FAILED',
                          'ETL_ERROR: n must be >= 0', f'{path}.n')

        normalized['n'] = n

    # Reorder keys: op first, then alphabetically
    result = {'op': normalized['op']}
    for key in sorted(k for k in normalized.keys() if k != 'op'):
        result[key] = normalized[key]

    return result


def process_pipeline(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Process the pipeline specification.
    Returns success response or raises ETLError.
    """
    # Validate top-level structure
    if not isinstance(data, dict):
        raise ETLError('SCHEMA_VALIDATION_FAILED',
                      'ETL_ERROR: input must be a JSON object', '')

    # Check required fields (dataset is required, pipeline is required based on spec)
    if 'pipeline' not in data:
        raise ETLError('SCHEMA_VALIDATION_FAILED',
                      'ETL_ERROR: missing required field "pipeline"', 'pipeline')

    if 'dataset' not in data:
        raise ETLError('SCHEMA_VALIDATION_FAILED',
                      'ETL_ERROR: missing required field "dataset"', 'dataset')

    pipeline = data['pipeline']
    if not isinstance(pipeline, dict):
        raise ETLError('SCHEMA_VALIDATION_FAILED',
                      'ETL_ERROR: pipeline must be an object', 'pipeline')

    if 'steps' not in pipeline:
        raise ETLError('SCHEMA_VALIDATION_FAILED',
                      'ETL_ERROR: missing required field "steps"', 'pipeline.steps')

    steps = pipeline['steps']
    if not isinstance(steps, list):
        raise ETLError('SCHEMA_VALIDATION_FAILED',
                      'ETL_ERROR: steps must be an array', 'pipeline.steps')

    # Validate dataset
    dataset = data['dataset']
    if not isinstance(dataset, list):
        raise ETLError('SCHEMA_VALIDATION_FAILED',
                      'ETL_ERROR: dataset must be an array', 'dataset')

    for i, item in enumerate(dataset):
        if not isinstance(item, dict):
            raise ETLError('SCHEMA_VALIDATION_FAILED',
                          'ETL_ERROR: dataset items must be objects', f'dataset[{i}]')

    # Normalize all steps
    normalized_steps = []
    for i, step in enumerate(steps):
        normalized_steps.append(normalize_step(step, i))

    return {
        'status': 'ok',
        'normalized': {
            'steps': normalized_steps
        }
    }


def main():
    """Main entry point."""
    try:
        # Read JSON from STDIN
        input_data = sys.stdin.read()

        try:
            data = json.loads(input_data)
        except json.JSONDecodeError as e:
            error_response = {
                'status': 'error',
                'error_code': 'SCHEMA_VALIDATION_FAILED',
                'message': f'ETL_ERROR: invalid JSON: {str(e)}',
                'path': ''
            }
            print(json.dumps(error_response))
            sys.exit(1)

        # Process pipeline
        result = process_pipeline(data)
        print(json.dumps(result))
        sys.exit(0)

    except ETLError as e:
        error_response = {
            'status': 'error',
            'error_code': e.error_code,
            'message': e.message,
            'path': e.path
        }
        print(json.dumps(error_response))
        sys.exit(1)


if __name__ == '__main__':
    main()
