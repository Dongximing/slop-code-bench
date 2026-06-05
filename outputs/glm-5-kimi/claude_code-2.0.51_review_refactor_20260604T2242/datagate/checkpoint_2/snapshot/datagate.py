#!/usr/bin/env python3
"""datagate – fetch a remote CSV and serve it as JSON."""

import argparse
import csv
import hashlib
import io
import re
import time
from urllib.parse import urlparse

import aiohttp
from aiohttp import web
import chardet


DATASETS: dict[str, dict] = {}

ROW_LIMIT = 100

# Time-like pattern: HH:MM, H:MM, HH:MM:SS, H:MM:SS etc.
TIME_RE = re.compile(r"^\d{1,2}:\d{2}(:\d{2})?$")


def _is_time(value: str) -> bool:
    return bool(TIME_RE.match(value.strip()))


def _infer_value(raw: str):
    """Return a Python object for a single CSV cell.

    * strings remain text
    * integers → int
    * decimals → float
    * time-like values → str  (kept as-is)
    """
    if raw == "":
        return raw

    stripped = raw.strip()

    if _is_time(stripped):
        return raw

    # Try integer
    try:
        return int(stripped)
    except ValueError:
        pass

    # Try float
    try:
        return float(stripped)
    except ValueError:
        pass

    return raw


def _parse_csv_text(text: str):
    """Parse *text* as CSV, auto-detecting the delimiter.

    Returns (columns, rows) where columns is a list of header strings and
    rows is a list of lists of inferred values.

    Raises ValueError if the content cannot be parsed as tabular data.
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
    """Decode *data* using *charset*, or auto-detect encoding."""
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
    """GET /convert  –  fetch a remote CSV and store it."""
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
    if dataset_id in DATASETS:
        return web.json_response(
            {"ok": True, "endpoint": f"/datasets/{dataset_id}"}, status=200
        )

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
    """Return a 400 error response."""
    return web.json_response(
        {"ok": False, "error": message}, status=400
    )


def _parse_control_params(request: web.Request, columns: list[str]) -> dict:
    """Parse and validate all control parameters.

    Returns a dict with parsed values, or raises ValueError with error message.
    """
    result = {
        "size": 100,
        "offset": 0,
        "sort_col": None,
        "sort_desc": False,
        "shape": "lists",
        "show_rowid": True,
        "show_total": True,
    }

    control_params = ["_size", "_offset", "_shape", "_sort", "_sort_desc", "_rowid", "_total"]

    # Check for duplicate parameters
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
            if size_str == "" or not size_str.lstrip("-").isdigit():
                raise ValueError("_size must be a positive integer")
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
            if offset_str == "" or not offset_str.lstrip("-").isdigit():
                raise ValueError("_offset must be a non-negative integer")
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

    if sort_desc_col is not None:
        # _sort_desc wins if both are present
        col = sort_desc_col.strip()
        if col == "" or col not in columns:
            raise ValueError(f"Unknown column: {sort_desc_col}")
        result["sort_col"] = col
        result["sort_desc"] = True
    elif sort_col is not None:
        col = sort_col.strip()
        if col == "" or col not in columns:
            raise ValueError(f"Unknown column: {sort_col}")
        result["sort_col"] = col
        result["sort_desc"] = False

    # Parse _rowid
    if "_rowid" in request.query:
        rowid_val = request.query["_rowid"]
        if rowid_val != "hide":
            raise ValueError("_rowid must be 'hide'")
        result["show_rowid"] = False

    # Parse _total
    if "_total" in request.query:
        total_val = request.query["_total"]
        if total_val != "hide":
            raise ValueError("_total must be 'hide'")
        result["show_total"] = False

    return result


async def handle_dataset(request: web.Request) -> web.Response:
    """GET /datasets/<id>  –  return stored dataset."""
    start = time.monotonic()

    dataset_id = request.match_info["id"]
    if dataset_id not in DATASETS:
        return web.json_response(
            {"ok": False, "error": f"Unknown dataset: {dataset_id}"}, status=404
        )

    ds = DATASETS[dataset_id]
    columns = ds["columns"]
    original_rows = ds["rows"]

    # Parse control parameters
    try:
        params = _parse_control_params(request, columns)
    except ValueError as e:
        return _error_response(str(e))

    # Get total before pagination
    total = len(original_rows)

    # Track original indices for rowid calculation
    # Each row is (original_index, row_data)
    indexed_rows = list(enumerate(original_rows))

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
