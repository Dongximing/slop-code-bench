#!/usr/bin/env python3
"""datagate – fetch a remote CSV and serve it as JSON."""

import argparse
import csv
import hashlib
import io
import os
import re
import time
from urllib.parse import urlparse

import aiohttp
from aiohttp import web
import async_timeout
import chardet


DATASETS: dict[str, dict] = {}


def _parse_cache_enabled() -> bool:
    """Parse CACHE_ENABLED environment variable with strict validation.

    True values (case-insensitive): 1, true, yes, on
    False values (case-insensitive): 0, false, no, off
    Invalid values raise SystemExit at startup.
    """
    val = os.environ.get("CACHE_ENABLED")
    if val is None:
        return True  # Default is enabled

    val_lower = val.lower()
    if val_lower in ("1", "true", "yes", "on"):
        return True
    if val_lower in ("0", "false", "no", "off"):
        return False
    print(f"Error: Invalid CACHE_ENABLED value: {val!r}. Must be one of: 1, true, yes, on, 0, false, no, off", flush=True)
    raise SystemExit(1)


CACHE_ENABLED = _parse_cache_enabled()

# Time-like pattern: HH:MM, H:MM, HH:MM:SS, H:MM:SS etc.
TIME_RE = re.compile(r"^\d{1,2}:\d{2}(:\d{2})?$")


def _is_time(value: str) -> bool:
    return bool(TIME_RE.match(value.strip()))


def _infer_value(raw: str):
    """Coerce a CSV cell to int/float when possible; keep time-like strings as-is."""
    if raw == "":
        return raw

    stripped = raw.strip()

    if _is_time(stripped):
        return raw

    try:
        return int(stripped)
    except ValueError:
        pass

    try:
        return float(stripped)
    except ValueError:
        pass

    return raw


def _parse_csv_text(text: str):
    """Parse *text* as CSV with auto-detected delimiter.

    Returns (columns, rows). Raises ValueError for non-tabular content.
    """
    sample = text[:8192]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel

    reader = csv.reader(io.StringIO(text), dialect)
    rows_raw = list(reader)
    rows_raw = [r for r in rows_raw if any(cell.strip() for cell in r)]

    if len(rows_raw) < 2:
        raise ValueError("Non-tabular content: need at least one header row and one data row")

    columns = [c.strip() for c in rows_raw[0]]
    rows = []
    for row in rows_raw[1:]:
        padded = row + [""] * (len(columns) - len(row))
        padded = padded[: len(columns)]
        rows.append([_infer_value(c) for c in padded])

    return columns, rows


def _decode_bytes(data: bytes, charset: str | None = None) -> str:
    """Decode bytes to string, auto-detecting encoding if charset is None."""
    if charset is not None:
        try:
            return data.decode(charset)
        except (LookupError, UnicodeDecodeError) as exc:
            raise ValueError(f"Unsupported or malformed charset: {charset}") from exc

    detected = chardet.detect(data)
    encoding = detected.get("encoding") or "utf-8"
    return data.decode(encoding, errors="replace")


def _source_to_id(source: str) -> str:
    return hashlib.sha256(source.encode()).hexdigest()[:16]


async def handle_convert(request: web.Request) -> web.Response:
    """Fetch a remote CSV and store it. Returns dataset endpoint."""
    # Validate force parameter: presence flag, at most one occurrence
    force_values = request.query.getall("force")
    if len(force_values) > 1:
        return web.json_response(
            {"ok": False, "error": "Duplicate parameter: force"}, status=400
        )
    force = len(force_values) == 1

    source = request.query.get("source")
    if not source:
        return web.json_response(
            {"ok": False, "error": "Missing required parameter: source"}, status=400
        )

    parsed = urlparse(source)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return web.json_response(
            {"ok": False, "error": f"Invalid URL: {source}"}, status=400
        )

    charset = request.query.get("charset")
    if charset is not None:
        try:
            "".encode(charset)
        except LookupError:
            return web.json_response(
                {"ok": False, "error": f"Unsupported or malformed charset: {charset}"}, status=400
            )

    dataset_id = _source_to_id(source)

    # Cache hit: return cached result if caching enabled and not forced
    if CACHE_ENABLED and not force and dataset_id in DATASETS:
        return web.json_response(
            {"ok": True, "endpoint": f"/datasets/{dataset_id}"}, status=200
        )

    # Fetch and parse the remote CSV
    try:
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(source) as resp:
                if resp.status != 200:
                    return web.json_response(
                        {"ok": False, "error": f"Remote server returned HTTP {resp.status}"}, status=404
                    )
                data = await resp.read()
    except Exception as exc:
        return web.json_response(
            {"ok": False, "error": f"Source unreachable: {exc}"}, status=404
        )

    try:
        text = _decode_bytes(data, charset)
    except ValueError as exc:
        return web.json_response(
            {"ok": False, "error": str(exc)}, status=400
        )

    try:
        columns, rows = _parse_csv_text(text)
    except ValueError as exc:
        return web.json_response(
            {"ok": False, "error": str(exc)}, status=400
        )

    DATASETS[dataset_id] = {"columns": columns, "rows": rows}

    return web.json_response(
        {"ok": True, "endpoint": f"/datasets/{dataset_id}"}, status=200
    )


def _error_response(message: str) -> web.Response:
    """Return a 400 JSON error response."""
    return web.json_response(
        {"ok": False, "error": message}, status=400
    )


def _parse_filter_params(request: web.Request, columns: list[str]) -> list[dict]:
    """Parse and validate filter parameters.

    Returns a list of filter dicts with keys: column, comparator, value
    Raises ValueError on invalid input.
    """
    valid_comparators = {"exact", "contains", "less", "greater"}
    filters = []

    for key in request.query:
        # Skip control parameters (starting with _)
        if key.startswith("_"):
            continue

        # Check if this is a filter parameter (contains __)
        if "__" not in key:
            continue

        # Parse column and comparator
        parts = key.split("__", 1)
        if len(parts) != 2:
            continue

        column, comparator = parts

        # Check for duplicate parameters via getall
        values = request.query.getall(key)
        if len(values) > 1:
            raise ValueError(f"Duplicate filter key: {key}")

        # Validate column exists (case-sensitive, exact match)
        if column not in columns:
            raise ValueError(f"Unknown filter column: {column}")

        # Validate comparator
        if comparator not in valid_comparators:
            raise ValueError(f"Invalid comparator: {comparator}")

        filter_value = request.query[key]

        # For less/greater, validate that filter value is numeric
        if comparator in ("less", "greater"):
            try:
                float(filter_value)
            except (ValueError, TypeError):
                raise ValueError(f"Comparator target not numeric: {filter_value}")

        filters.append({
            "column": column,
            "comparator": comparator,
            "value": filter_value,
            "key": key
        })

    return filters


def _apply_filter(row: list, columns: list[str], filter_spec: dict) -> bool:
    """Apply a single filter to a row. Returns True if row matches."""
    col_idx = columns.index(filter_spec["column"])
    stored_value = row[col_idx]
    filter_value = filter_spec["value"]
    comparator = filter_spec["comparator"]

    # Convert empty string stored values to empty string for comparison
    if stored_value is None:
        stored_value = ""

    if comparator == "exact":
        # Case-sensitive string equality
        return str(stored_value) == filter_value

    if comparator == "contains":
        # Case-sensitive substring
        return filter_value in str(stored_value)

    if comparator == "less":
        # Numeric strict less - non-numeric stored values don't match
        try:
            stored_num = float(stored_value)
            filter_num = float(filter_value)
            return stored_num < filter_num
        except (ValueError, TypeError):
            return False

    # comparator == "greater"
    # Numeric strict greater - non-numeric stored values don't match
    try:
        stored_num = float(stored_value)
        filter_num = float(filter_value)
        return stored_num > filter_num
    except (ValueError, TypeError):
        return False


def _apply_filters(rows: list, columns: list[str], filters: list[dict]) -> list:
    """Apply all filters to rows (AND logic). Returns filtered rows."""
    if not filters:
        return rows

    result = []
    for row in rows:
        if all(_apply_filter(row, columns, f) for f in filters):
            result.append(row)
    return result


def _parse_control_params(request: web.Request, columns: list[str]) -> dict:
    """Parse and validate control parameters. Raises ValueError on invalid input."""
    result = {
        "size": 100,
        "offset": 0,
        "sort_col": None,
        "sort_desc": False,
        "shape": "lists",
        "show_rowid": True,
        "show_total": True,
        "filters": [],
        "timeout": None,
    }

    control_params = ["_size", "_offset", "_shape", "_sort", "_sort_desc", "_rowid", "_total", "_timeout"]

    # Check for duplicate control parameters
    for param in control_params:
        if param in request.query:
            values = request.query.getall(param)
            if len(values) > 1:
                raise ValueError(f"Duplicate parameter: {param}")

    # Parse _size
    if "_size" in request.query:
        size_str = request.query["_size"]
        try:
            size = int(size_str)
            if size <= 0:
                raise ValueError("_size must be a positive integer")
            result["size"] = size
        except ValueError:
            raise ValueError("_size must be a positive integer")

    # Parse _offset
    if "_offset" in request.query:
        offset_str = request.query["_offset"]
        try:
            offset = int(offset_str)
            if offset < 0:
                raise ValueError("_offset must be a non-negative integer")
            result["offset"] = offset
        except ValueError:
            raise ValueError("_offset must be a non-negative integer")

    # Parse _shape
    if "_shape" in request.query:
        shape = request.query["_shape"]
        if shape not in ("lists", "objects"):
            raise ValueError("_shape must be 'lists' or 'objects'")
        result["shape"] = shape

    # Parse _sort and _sort_desc
    sort_col = request.query.get("_sort")
    sort_desc_col = request.query.get("_sort_desc")

    col_name = sort_desc_col if sort_desc_col is not None else sort_col
    if col_name is not None:
        col = col_name.strip()
        if col == "" or col not in columns:
            raise ValueError(f"Unknown column: {col_name}")
        result["sort_col"] = col
        result["sort_desc"] = sort_desc_col is not None

    if "_rowid" in request.query:
        rowid_val = request.query["_rowid"]
        if rowid_val != "hide":
            raise ValueError("_rowid must be 'hide'")
        result["show_rowid"] = False

    if "_total" in request.query:
        total_val = request.query["_total"]
        if total_val != "hide":
            raise ValueError("_total must be 'hide'")
        result["show_total"] = False

    # Parse _timeout
    if "_timeout" in request.query:
        timeout_str = request.query["_timeout"]
        try:
            timeout = float(timeout_str)
            if timeout <= 0:
                raise ValueError("_timeout must be a positive number")
            result["timeout"] = timeout
        except ValueError:
            raise ValueError("_timeout must be a positive number")

    # Parse filter parameters
    result["filters"] = _parse_filter_params(request, columns)

    return result


async def handle_dataset(request: web.Request) -> web.Response:
    """Return stored dataset with pagination, sorting, and format options."""
    start = time.monotonic()

    dataset_id = request.match_info["id"]
    if dataset_id not in DATASETS:
        return web.json_response(
            {"ok": False, "error": f"Unknown dataset: {dataset_id}"}, status=404
        )

    ds = DATASETS[dataset_id]
    columns = ds["columns"]
    original_rows = ds["rows"]

    # Parse control parameters and filters
    try:
        params = _parse_control_params(request, columns)
    except ValueError as e:
        return _error_response(str(e))

    # Apply timeout if specified
    timeout_seconds = params["timeout"]
    if timeout_seconds is not None:
        try:
            async with async_timeout.timeout(timeout_seconds):
                return await _process_dataset_request(params, columns, original_rows, start)
        except TimeoutError:
            return _error_response("Query timeout")

    return await _process_dataset_request(params, columns, original_rows, start)


async def _process_dataset_request(params: dict, columns: list[str], original_rows: list, start: float) -> web.Response:
    """Process the dataset request with filters, sorting, and pagination."""
    filtered_rows = _apply_filters(original_rows, columns, params["filters"])

    # Get total after filtering
    total = len(filtered_rows)

    # Build a mapping from filtered rows to their original indices
    row_to_orig_idx = {id(row): idx for idx, row in enumerate(original_rows)}
    indexed_rows = [(row_to_orig_idx[id(row)], row) for row in filtered_rows]

    # Apply sorting (stable sort, before pagination)
    if params["sort_col"] is not None:
        col_idx = columns.index(params["sort_col"])
        # Stable sort by column value, original index preserves stability
        indexed_rows = sorted(
            indexed_rows,
            key=lambda x: (x[1][col_idx] is None, x[1][col_idx] if x[1][col_idx] is not None else ""),
            reverse=params["sort_desc"]
        )

    # Apply pagination
    offset = params["offset"]
    size = params["size"]

    if offset >= len(indexed_rows):
        paginated_indexed_rows = []
    else:
        paginated_indexed_rows = indexed_rows[offset:offset + size]

    # Build response
    response_data = {"ok": True}

    # Add total if not hidden
    if params["show_total"]:
        response_data["total"] = total

    # Add columns
    response_data["columns"] = columns

    # Build rows based on shape
    if params["shape"] == "lists":
        response_data["rows"] = [row for _, row in paginated_indexed_rows]
    else:  # objects
        objects = []
        for i, (orig_idx, row) in enumerate(paginated_indexed_rows):
            obj = {"rowid": orig_idx + 1}  # 1-based source-file row number
            for j, col in enumerate(columns):
                obj[col] = row[j] if j < len(row) else ""
            objects.append(obj)
        response_data["rows"] = objects

    # Handle _rowid=hide for objects shape
    if params["shape"] == "objects" and not params["show_rowid"]:
        for obj in response_data["rows"]:
            del obj["rowid"]

    elapsed_ms = (time.monotonic() - start) * 1000
    response_data["query_ms"] = round(elapsed_ms, 1)

    return web.json_response(response_data, status=200)


async def handle_not_found(request: web.Request) -> web.Response:
    return web.json_response(
        {"ok": False, "error": "Not found"}, status=404
    )


@web.middleware
async def cors_middleware(request: web.Request, handler):
    resp = await handler(request)
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "*"
    return resp


def create_app() -> web.Application:
    app = web.Application(middlewares=[cors_middleware])
    app.router.add_get("/convert", handle_convert)
    app.router.add_get("/datasets/{id}", handle_dataset)
    # Catch-all for unknown routes – must be last
    app.router.add_route("*", "/{path:.*}", handle_not_found)
    return app


def main():
    parser = argparse.ArgumentParser(description="datagate – CSV-to-JSON gateway")
    sub = parser.add_subparsers(dest="command")
    start_p = sub.add_parser("start", help="Start the datagate server")
    start_p.add_argument("--port", type=int, default=8001)
    start_p.add_argument("--address", default="127.0.0.1")
    args = parser.parse_args()

    if args.command != "start":
        parser.print_help()
        return

    app = create_app()
    web.run_app(app, host=args.address, port=args.port)


if __name__ == "__main__":
    main()
