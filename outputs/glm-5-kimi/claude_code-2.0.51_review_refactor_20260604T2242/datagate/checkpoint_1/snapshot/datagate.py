#!/usr/bin/env python3
"""datagate – fetch a remote CSV and serve it as JSON."""

import argparse
import asyncio
import csv
import hashlib
import io
import json
import re
import time
from csv import Sniffer
from urllib.parse import urlparse

import aiohttp
from aiohttp import web
import chardet


# ---------------------------------------------------------------------------
# Global in-memory store:  id -> {"columns": [...], "rows": [...]}
# ---------------------------------------------------------------------------
DATASETS: dict[str, dict] = {}

# Map source URL -> dataset id (ensures determinism)
SOURCE_TO_ID: dict[str, str] = {}

# Maximum rows returned by /datasets/<id>
ROW_LIMIT = 100

# Time-like pattern: HH:MM, H:MM, HH:MM:SS, H:MM:SS etc.
TIME_RE = re.compile(r"^\d{1,2}:\d{2}(:\d{2})?$")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_time(value: str) -> bool:
    """Return True if *value* looks like a time (e.g. 08:30, 9:15)."""
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

    # Keep time-like strings as text
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
    # Detect delimiter via csv.Sniffer
    sample = text[:8192]
    try:
        dialect = Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        # Fallback: try comma
        dialect = csv.excel

    reader = csv.reader(io.StringIO(text), dialect)
    rows_raw = list(reader)

    # Filter out completely empty rows
    rows_raw = [r for r in rows_raw if any(cell.strip() for cell in r)]

    if len(rows_raw) < 2:
        raise ValueError("Non-tabular content: need at least one header row and one data row")

    columns = [c.strip() for c in rows_raw[0]]
    rows = []
    for row in rows_raw[1:]:
        # Pad or truncate row to match header length
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

    # Auto-detect
    detected = chardet.detect(data)
    encoding = detected.get("encoding") or "utf-8"
    return data.decode(encoding, errors="replace")


def _source_to_id(source: str) -> str:
    """Deterministically map a source URL to a dataset id."""
    return hashlib.sha256(source.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# HTTP handlers
# ---------------------------------------------------------------------------

async def handle_convert(request: web.Request) -> web.Response:
    """GET /convert  –  fetch a remote CSV and store it."""
    source = request.query.get("source")
    if not source:
        return web.json_response(
            {"ok": False, "error": "Missing required parameter: source"}, status=400
        )

    # Validate URL
    parsed = urlparse(source)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return web.json_response(
            {"ok": False, "error": f"Invalid URL: {source}"}, status=400
        )

    # Check charset parameter validity early
    charset = request.query.get("charset")
    if charset is not None:
        try:
            "".encode(charset)
        except LookupError:
            return web.json_response(
                {"ok": False, "error": f"Unsupported or malformed charset: {charset}"}, status=400
            )

    # Check if we already have this source
    dataset_id = _source_to_id(source)
    if dataset_id in DATASETS:
        return web.json_response(
            {"ok": True, "endpoint": f"/datasets/{dataset_id}"}, status=200
        )

    # Fetch remote CSV
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

    # Decode
    try:
        text = _decode_bytes(data, charset)
    except ValueError as exc:
        return web.json_response(
            {"ok": False, "error": str(exc)}, status=400
        )

    # Parse
    try:
        columns, rows = _parse_csv_text(text)
    except ValueError as exc:
        return web.json_response(
            {"ok": False, "error": str(exc)}, status=400
        )

    # Store
    SOURCE_TO_ID[source] = dataset_id
    DATASETS[dataset_id] = {"columns": columns, "rows": rows}

    return web.json_response(
        {"ok": True, "endpoint": f"/datasets/{dataset_id}"}, status=200
    )


async def handle_dataset(request: web.Request) -> web.Response:
    """GET /datasets/<id>  –  return stored dataset."""
    start = time.monotonic()

    dataset_id = request.match_info["id"]
    if dataset_id not in DATASETS:
        return web.json_response(
            {"ok": False, "error": f"Unknown dataset: {dataset_id}"}, status=404
        )

    ds = DATASETS[dataset_id]
    elapsed_ms = (time.monotonic() - start) * 1000

    returned_rows = ds["rows"][:ROW_LIMIT]

    return web.json_response(
        {
            "ok": True,
            "columns": ds["columns"],
            "rows": returned_rows,
            "query_ms": round(elapsed_ms, 1),
        },
        status=200,
    )


async def handle_not_found(request: web.Request) -> web.Response:
    """Catch-all for unknown routes."""
    return web.json_response(
        {"ok": False, "error": "Not found"}, status=404
    )


# ---------------------------------------------------------------------------
# Middleware: CORS
# ---------------------------------------------------------------------------

@web.middleware
async def cors_middleware(request: web.Request, handler):
    resp = await handler(request)
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "*"
    return resp


# ---------------------------------------------------------------------------
# App factory & CLI
# ---------------------------------------------------------------------------

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
