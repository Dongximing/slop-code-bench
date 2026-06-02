#!/usr/bin/env python3
"""CSV database layer for SQL query CLI."""

import sys
from pathlib import Path
from typing import Dict
import pandas as pd


class CSVDatabase:
    """Manages CSV files as database tables."""

    def __init__(self, data_dir: str):
        self.data_dir = Path(data_dir)
        self.tables: Dict[str, pd.DataFrame] = {}
        self._load_tables()

    def _load_tables(self):
        """Load all CSV files from the data directory."""
        for csv_path in self.data_dir.rglob("*.csv"):
            rel_path = csv_path.relative_to(self.data_dir)
            parts = list(rel_path.parts)

            filename = parts[-1]
            name = filename[:-4].replace('.', '_')
            parts[-1] = name
            table_name = '.'.join(parts)

            try:
                df = pd.read_csv(csv_path)
                if df.empty or len(df.columns) == 0:
                    print(f"Warning: {csv_path} has no columns or is empty", file=sys.stderr)
                    continue
                self.tables[table_name] = df
            except Exception as e:
                print(f"Warning: Could not load {csv_path}: {e}", file=sys.stderr)

    def get_table(self, name: str):
        """Get a table by name."""
        return self.tables.get(name)
