#!/usr/bin/env python3
"""Error handling utilities."""

import json
import sys


def error(code, message, path):
    """Output error JSON and exit."""
    print(json.dumps({"status": "error", "error_code": code, "message": f"ETL_ERROR: {message}", "path": path}))
    sys.exit(1)
