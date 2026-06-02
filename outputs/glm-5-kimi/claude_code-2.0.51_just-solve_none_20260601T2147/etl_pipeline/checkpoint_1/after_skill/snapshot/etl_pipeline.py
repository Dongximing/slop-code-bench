#!/usr/bin/env python3
"""ETL Pipeline Parser - validates and normalizes pipeline specifications."""

import json
import sys
from typing import Any, Dict, List

BINARY_OPS = frozenset({'+', '-', '*', '/', '>', '>=', '<', '<=', '==', '!=', 'and', 'or'})


class ETLError(Exception):
    def __init__(self, error_code: str, message: str, path: str):
        self.error_code = error_code
        self.message = message
        self.path = path
        super().__init__(message)


def _require_string_field(step: Dict[str, Any], field: str, path: str) -> str:
    """Require a non-empty trimmed string field from step."""
    if field not in step:
        raise ETLError('MISSING_COLUMN',
                       f'ETL_ERROR: missing required field "{field}"', f'{path}.{field}')
    value = step[field]
    if not isinstance(value, str):
        raise ETLError('SCHEMA_VALIDATION_FAILED',
                       f'ETL_ERROR: {field} must be a string', f'{path}.{field}')
    trimmed = value.strip()
    if not trimmed:
        raise ETLError('SCHEMA_VALIDATION_FAILED',
                       f'ETL_ERROR: {field} cannot be empty', f'{path}.{field}')
    return trimmed


def _tokenize_expression(expr: str) -> List[str]:
    tokens = []
    i = 0
    while i < len(expr):
        if expr[i].isspace():
            i += 1
        elif expr[i] in '()':
            tokens.append(expr[i])
            i += 1
        elif expr[i].isdigit():
            j = i
            while j < len(expr) and (expr[j].isdigit() or expr[j] == '.'):
                j += 1
            tokens.append(expr[i:j])
            i = j
        elif expr[i].isalpha() or expr[i] == '_':
            j = i
            while j < len(expr) and (expr[j].isalnum() or expr[j] == '_'):
                j += 1
            tokens.append(expr[i:j])
            i = j
        elif i + 1 < len(expr) and expr[i:i+2] in ('>=', '<=', '==', '!='):
            tokens.append(expr[i:i+2])
            i += 2
        elif expr[i] in '+-*/><':
            tokens.append(expr[i])
            i += 1
        else:
            raise ValueError(f"invalid character '{expr[i]}' in expression")

    for i in range(len(tokens) - 1):
        curr, next_tok = tokens[i], tokens[i + 1]
        if curr in '()' or next_tok in '()':
            continue
        if curr in BINARY_OPS and next_tok in BINARY_OPS:
            raise ValueError(f"consecutive operators '{curr}' and '{next_tok}'")

    return tokens


def validate_expression(expr: str, path: str) -> None:
    if not expr or not expr.strip():
        raise ETLError('BAD_EXPR', 'ETL_ERROR: empty expression', path)
    try:
        _tokenize_expression(expr.strip())
    except ValueError as e:
        raise ETLError('BAD_EXPR', f'ETL_ERROR: {e}', path)


def _normalize_select(step: Dict[str, Any], path: str) -> Dict[str, Any]:
    if 'columns' not in step:
        raise ETLError('MISSING_COLUMN',
                       'ETL_ERROR: missing required field "columns"', f'{path}.columns')
    columns = step['columns']
    if not isinstance(columns, list):
        raise ETLError('SCHEMA_VALIDATION_FAILED',
                       'ETL_ERROR: columns must be an array', f'{path}.columns')
    for i, col in enumerate(columns):
        if not isinstance(col, str):
            raise ETLError('SCHEMA_VALIDATION_FAILED',
                           'ETL_ERROR: column names must be strings', f'{path}.columns[{i}]')
    return {'columns': columns}


def _normalize_filter(step: Dict[str, Any], path: str) -> Dict[str, Any]:
    where = _require_string_field(step, 'where', path)
    validate_expression(where, f'{path}.where')
    return {'where': where}


def _normalize_map(step: Dict[str, Any], path: str) -> Dict[str, Any]:
    as_val = _require_string_field(step, 'as', path)
    expr = _require_string_field(step, 'expr', path)
    validate_expression(expr, f'{path}.expr')
    return {'as': as_val, 'expr': expr}


def _normalize_rename(step: Dict[str, Any], path: str) -> Dict[str, Any]:
    if 'mapping' in step:
        mapping = step['mapping']
        if not isinstance(mapping, dict):
            raise ETLError('SCHEMA_VALIDATION_FAILED',
                           'ETL_ERROR: mapping must be an object', f'{path}.mapping')
        for k, v in mapping.items():
            if not isinstance(k, str) or not isinstance(v, str):
                raise ETLError('SCHEMA_VALIDATION_FAILED',
                               'ETL_ERROR: mapping keys and values must be strings',
                               f'{path}.mapping')
        return {'mapping': mapping}

    if 'from' not in step or 'to' not in step:
        raise ETLError('MISSING_COLUMN',
                       'ETL_ERROR: rename requires either from/to or mapping', path)

    from_val = _require_string_field(step, 'from', path)
    to_val = _require_string_field(step, 'to', path)
    return {'mapping': {from_val: to_val}}


def _normalize_limit(step: Dict[str, Any], path: str) -> Dict[str, Any]:
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
    return {'n': n}


_OP_NORMALIZERS = {
    'select': _normalize_select,
    'filter': _normalize_filter,
    'map': _normalize_map,
    'rename': _normalize_rename,
    'limit': _normalize_limit,
}


def normalize_step(step: Dict[str, Any], index: int) -> Dict[str, Any]:
    path = f'pipeline.steps[{index}]'

    if not isinstance(step, dict):
        raise ETLError('SCHEMA_VALIDATION_FAILED',
                       'ETL_ERROR: step must be an object', path)

    op_raw = step.get('op')
    if op_raw is None:
        raise ETLError('SCHEMA_VALIDATION_FAILED',
                       'ETL_ERROR: missing required field "op"', path)
    if not isinstance(op_raw, str):
        raise ETLError('SCHEMA_VALIDATION_FAILED',
                       'ETL_ERROR: op must be a string', f'{path}.op')

    op = op_raw.strip().lower()
    if not op:
        raise ETLError('SCHEMA_VALIDATION_FAILED',
                       'ETL_ERROR: op cannot be empty', f'{path}.op')

    if op not in _OP_NORMALIZERS:
        raise ETLError('UNKNOWN_OP',
                       f'ETL_ERROR: unsupported op \'{op}\'', f'{path}.op')

    normalized = {'op': op}
    normalized.update(_OP_NORMALIZERS[op](step, path))

    # Reorder keys: op first, then alphabetically
    result = {'op': normalized['op']}
    result.update((k, normalized[k]) for k in sorted(k for k in normalized if k != 'op'))
    return result


def process_pipeline(data: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(data, dict):
        raise ETLError('SCHEMA_VALIDATION_FAILED',
                       'ETL_ERROR: input must be a JSON object', '')

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

    dataset = data['dataset']
    if not isinstance(dataset, list):
        raise ETLError('SCHEMA_VALIDATION_FAILED',
                       'ETL_ERROR: dataset must be an array', 'dataset')
    for i, item in enumerate(dataset):
        if not isinstance(item, dict):
            raise ETLError('SCHEMA_VALIDATION_FAILED',
                           'ETL_ERROR: dataset items must be objects', f'dataset[{i}]')

    normalized_steps = [normalize_step(s, i) for i, s in enumerate(steps)]

    return {'status': 'ok', 'normalized': {'steps': normalized_steps}}


def main():
    try:
        data = json.loads(sys.stdin.read())
    except json.JSONDecodeError as e:
        print(json.dumps({
            'status': 'error',
            'error_code': 'SCHEMA_VALIDATION_FAILED',
            'message': f'ETL_ERROR: invalid JSON: {e}',
            'path': ''
        }))
        sys.exit(1)

    try:
        result = process_pipeline(data)
        print(json.dumps(result))
    except ETLError as e:
        print(json.dumps({
            'status': 'error',
            'error_code': e.error_code,
            'message': e.message,
            'path': e.path
        }))
        sys.exit(1)


if __name__ == '__main__':
    main()
