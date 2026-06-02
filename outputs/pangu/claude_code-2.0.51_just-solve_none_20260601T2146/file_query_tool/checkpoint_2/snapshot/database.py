#!/usr/bin/env python3
"""Database layer for SQL query CLI supporting multiple file formats."""

import sys
from pathlib import Path
from typing import Dict, Optional, Set
import pandas as pd
import gzip
import bz2


class MultiFormatDatabase:
    """Manages multiple file format files as database tables."""

    SUPPORTED_FORMATS = {'.csv', '.parquet', '.tsv', '.json', '.jsonl'}
    COMPRESSED_EXTENSIONS = {'.gz', '.bz2'}

    def __init__(self, data_dir: str):
        self.data_dir = Path(data_dir)
        self.tables: Dict[str, pd.DataFrame] = {}
        self._column_registry: Dict[str, Set[str]] = {}
        self._load_tables()

    def _get_base_filename(self, filepath: Path) -> str:
        """Get the base filename without any extension."""
        name = filepath.name

        # Remove compression suffix if present
        for comp_ext in self.COMPRESSED_EXTENSIONS:
            if name.endswith(comp_ext):
                name = name[:-len(comp_ext)]
                break

        # Remove data format suffix
        for fmt in self.SUPPORTED_FORMATS:
            if name.endswith(fmt):
                name = name[:-len(fmt)]
                break

        return name

    def _load_tables(self):
        """Load all supported files from the data directory."""
        # First pass: load all files
        file_groups: Dict[str, list] = {}

        for ext in self.SUPPORTED_FORMATS:
            # Handle uncompressed files
            for filepath in self.data_dir.rglob(f"*{ext}"):
                base_name = self._get_base_filename(filepath)
                if base_name not in file_groups:
                    file_groups[base_name] = []
                file_groups[base_name].append((filepath, ext, None))

            # Handle compressed files
            for comp_ext in self.COMPRESSED_EXTENSIONS:
                for filepath in self.data_dir.rglob(f"*{ext}{comp_ext}"):
                    base_name = self._get_base_filename(filepath)
                    if base_name not in file_groups:
                        file_groups[base_name] = []
                    file_groups[base_name].append((filepath, ext, comp_ext))

        # Load each group of files with the same base name
        for base_name, files in file_groups.items():
            dataframes = []
            table_name = base_name.replace('.', '_')

            for filepath, fmt, comp in files:
                try:
                    if fmt == '.csv':
                        df = self._read_csv_file(filepath, comp)
                    elif fmt == '.parquet':
                        df = self._read_parquet_file(filepath, comp)
                    elif fmt == '.tsv':
                        df = self._read_tsv_file(filepath, comp)
                    elif fmt == '.json':
                        df = self._read_json_file(filepath, comp, lines=False)
                    elif fmt == '.jsonl':
                        df = self._read_json_file(filepath, comp, lines=True)

                    if df is not None and not df.empty and len(df.columns) > 0:
                        dataframes.append(df)

                except Exception as e:
                    print(f"Warning: Could not load {filepath}: {e}", file=sys.stderr)

            # Check that all files with the same base name have the same columns
            if dataframes:
                # Store the columns for validation
                first_cols = set(dataframes[0].columns)
                self._column_registry[table_name] = first_cols

                for i, df in enumerate(dataframes[1:], 1):
                    if set(df.columns) != first_cols:
                        # Keep the first file and warn about mismatches
                        print(f"Warning: File {files[i][0].name} has different columns than {files[0][0].name}", file=sys.stderr)
                    else:
                        # Concatenate if columns match
                        dataframes[0] = pd.concat([dataframes[0], df], ignore_index=True)

                if dataframes[0] is not None:
                    self.tables[table_name] = dataframes[0]

    def _open_compressed(self, filepath: Path, comp_ext: Optional[str]):
        """Open a compressed file and return a file-like object."""
        if comp_ext == '.gz':
            return gzip.open(filepath, 'rt', encoding='utf-8')
        elif comp_ext == '.bz2':
            return bz2.open(filepath, 'rt', encoding='utf-8')
        else:
            return open(filepath, 'r', encoding='utf-8')

    def _read_csv_file(self, filepath: Path, comp_ext: Optional[str]) -> Optional[pd.DataFrame]:
        """Read a CSV file (optionally compressed)."""
        with self._open_compressed(filepath, comp_ext) as f:
            return pd.read_csv(f)

    def _read_parquet_file(self, filepath: Path, comp_ext: Optional[str]) -> Optional[pd.DataFrame]:
        """Read a Parquet file (optionally compressed)."""
        # Note: PyArrow handles compression transparently for parquet
        return pd.read_parquet(filepath)

    def _read_tsv_file(self, filepath: Path, comp_ext: Optional[str]) -> Optional[pd.DataFrame]:
        """Read a TSV file (optionally compressed)."""
        with self._open_compressed(filepath, comp_ext) as f:
            return pd.read_csv(f, sep='\t')

    def _read_json_file(self, filepath: Path, comp_ext: Optional[str], lines: bool) -> Optional[pd.DataFrame]:
        """Read a JSON file (optionally compressed)."""
        with self._open_compressed(filepath, comp_ext) as f:
            return pd.read_json(f, lines=lines)

    def get_table(self, name: str) -> Optional[pd.DataFrame]:
        """Get a table by name."""
        return self.tables.get(name)

    def get_table_columns(self, name: str) -> Optional[Set[str]]:
        """Get the columns for a table."""
        return self._column_registry.get(name)
