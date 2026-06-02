#!/usr/bin/env python3
"""Parser utilities for CSV, TSV, JSONL, and JSON files."""

import json
from pathlib import Path
from typing import Any, Dict, List, Tuple


def parse_value(value: str) -> Any:
    """Parse a string value into appropriate Python type."""
    if value == '':
        return None
    if value.lower() == 'true':
        return True
    if value.lower() == 'false':
        return False
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        pass
    return value


def parse_csv(filepath: str, delimiter: str = ',') -> List[Dict[str, Any]]:
    """Parse a CSV/TSV file and return list of row dicts."""
    with open(filepath, 'r', encoding='utf-8') as f:
        lines = f.read().strip().split('\n')
    if not lines:
        return []
    headers = lines[0].split(delimiter)
    rows = []
    for line in lines[1:]:
        if not line.strip():
            continue
        values = line.split(delimiter)
        rows.append({h: parse_value(values[i]) if i < len(values) else None
                      for i, h in enumerate(headers)})
    return rows


def parse_tsv(filepath: str) -> List[Dict[str, Any]]:
    """Parse a TSV file."""
    return parse_csv(filepath, delimiter='\t')


def parse_jsonl(filepath: str) -> List[Dict[str, Any]]:
    """Parse a JSONL file."""
    with open(filepath, 'r', encoding='utf-8') as f:
        return [json.loads(line) for line in f if line.strip()]


def parse_json_file(filepath: str) -> List[Dict[str, Any]]:
    """Parse a JSON file containing an array."""
    with open(filepath, 'r', encoding='utf-8') as f:
        return json.load(f)


def get_file_extension(filepath: str) -> str:
    """Get the file extension without the dot."""
    return Path(filepath).suffix.lstrip('.').lower()


def parse_file(filepath: str) -> Tuple[List[Dict[str, Any]], str]:
    """Parse a data file and return rows and extension."""
    ext = get_file_extension(filepath)
    parsers = {
        'csv': parse_csv,
        'tsv': parse_tsv,
        'jsonl': parse_jsonl,
        'json': parse_json_file,
    }
    if ext not in parsers:
        raise ValueError(f"Unsupported file extension: {ext}")
    return parsers[ext](filepath), ext
