"""Database layer for SQL query CLI supporting multiple file formats."""

import sys
from pathlib import Path
from typing import Dict, Optional, Set
import pandas as pd
import gzip
import bz2


class CSVDatabase:
    """Manages multiple file format files as database tables."""

    SUPPORTED_FORMATS = {'.csv', '.parquet', '.tsv', '.json', '.jsonl'}
    COMPRESSED_EXTENSIONS = {'.gz', '.bz2'}

    def __init__(self, data_dir: str):
        self.data_dir = Path(data_dir)
        self.tables: Dict[str, pd.DataFrame] = {}
        self._column_registry: Dict[str, Set[str]] = {}
        self._load_tables()

    def _get_base_filename(self, filepath: Path) -> str:
        name = filepath.name
        for ext in self.COMPRESSED_EXTENSIONS:
            if name.endswith(ext):
                name = name[:-(len(ext))]
                break
        for fmt in self.SUPPORTED_FORMATS:
            if name.endswith(fmt):
                name = name[:-(len(fmt))]
                break
        return name

    def _load_tables(self):
        file_groups: Dict[str, list] = {}

        for ext in self.SUPPORTED_FORMATS:
            for filepath in self.data_dir.rglob(f"*{ext}"):
                base_name = self._get_base_filename(filepath)
                file_groups.setdefault(base_name, []).append((filepath, ext, None))

            for comp_ext in self.COMPRESSED_EXTENSIONS:
                for filepath in self.data_dir.rglob(f"*{ext}{comp_ext}"):
                    base_name = self._get_base_filename(filepath)
                    file_groups.setdefault(base_name, []).append((filepath, ext, comp_ext))

        readers = {
            '.csv': self._read_csv,
            '.parquet': self._read_parquet,
            '.tsv': self._read_tsv,
            '.json': self._read_json,
            '.jsonl': self._read_jsonl,
        }

        for base_name, files in file_groups.items():
            dataframes = []
            table_name = base_name.replace('.', '_')

            for filepath, fmt, comp in files:
                try:
                    df = readers[fmt](filepath, comp)
                    if df is not None and not df.empty and len(df.columns) > 0:
                        dataframes.append(df)
                except Exception as e:
                    print(f"Warning: Could not load {filepath}: {e}", file=sys.stderr)

            if not dataframes:
                continue

            first_cols = set(dataframes[0].columns)
            self._column_registry[table_name] = first_cols

            for i, df in enumerate(dataframes[1:], 1):
                if set(df.columns) != first_cols:
                    print(f"Warning: File {files[i][0].name} has different columns than {files[0][0].name}", file=sys.stderr)
                else:
                    dataframes[0] = pd.concat([dataframes[0], df], ignore_index=True)

            if dataframes[0] is not None:
                self.tables[table_name] = dataframes[0]

    def _open_compressed(self, filepath: Path, comp_ext: Optional[str]):
        if comp_ext == '.gz':
            return gzip.open(filepath, 'rt', encoding='utf-8')
        if comp_ext == '.bz2':
            return bz2.open(filepath, 'rt', encoding='utf-8')
        return open(filepath, 'r', encoding='utf-8')

    def _read_csv(self, filepath: Path, comp_ext: Optional[str]) -> pd.DataFrame:
        with self._open_compressed(filepath, comp_ext) as f:
            return pd.read_csv(f)

    def _read_parquet(self, filepath: Path, comp_ext: Optional[str]) -> pd.DataFrame:
        return pd.read_parquet(filepath)

    def _read_tsv(self, filepath: Path, comp_ext: Optional[str]) -> pd.DataFrame:
        with self._open_compressed(filepath, comp_ext) as f:
            return pd.read_csv(f, sep='\t')

    def _read_json(self, filepath: Path, comp_ext: Optional[str]) -> pd.DataFrame:
        with self._open_compressed(filepath, comp_ext) as f:
            return pd.read_json(f, lines=False)

    def _read_jsonl(self, filepath: Path, comp_ext: Optional[str]) -> pd.DataFrame:
        with self._open_compressed(filepath, comp_ext) as f:
            return pd.read_json(f, lines=True)

    def get_table(self, name: str) -> Optional[pd.DataFrame]:
        return self.tables.get(name)

    def get_table_columns(self, name: str) -> Optional[Set[str]]:
        return self._column_registry.get(name)

    def load_sharded_tables(self, sharded_configs: list):
        """Load sharded tables from glob patterns."""
        from glob import glob

        for table_name, pattern in sharded_configs:
            # Find all files matching the glob pattern, sorted lexicographically
            matched_files = sorted(glob(pattern))

            if not matched_files:
                # Create empty table with no schema
                self.tables[table_name] = pd.DataFrame()
                self._column_registry[table_name] = set()
                continue

            # Determine file formats from extensions
            files_with_format = []
            for filepath in matched_files:
                fp = Path(filepath)
                ext = None
                comp_ext = None

                # Check for compression first
                for comp in self.COMPRESSED_EXTENSIONS:
                    if fp.name.endswith(comp):
                        comp_ext = comp
                        # Remove compression extension and check format
                        name_without_comp = fp.name[:-(len(comp))]
                        for fmt in self.SUPPORTED_FORMATS:
                            if name_without_comp.endswith(fmt):
                                ext = fmt
                                break
                        break

                if not comp_ext:
                    for fmt in self.SUPPORTED_FORMATS:
                        if fp.name.endswith(fmt):
                            ext = fmt
                            break

                if ext:
                    files_with_format.append((fp, ext, comp_ext))

            if not files_with_format:
                # No files match supported format
                self.tables[table_name] = pd.DataFrame()
                self._column_registry[table_name] = set()
                continue

            # Read all matching files
            readers = {
                '.csv': self._read_csv,
                '.parquet': self._read_parquet,
                '.tsv': self._read_tsv,
                '.json': self._read_json,
                '.jsonl': self._read_jsonl,
            }

            dataframes = []
            for filepath, fmt, comp in files_with_format:
                try:
                    df = readers[fmt](filepath, comp)
                    if df is not None and len(df.columns) > 0:
                        dataframes.append(df)
                except Exception as e:
                    print(f"Warning: Could not load {filepath}: {e}", file=sys.stderr)

            if not dataframes:
                self.tables[table_name] = pd.DataFrame()
                self._column_registry[table_name] = set()
                continue

            # Set column registry from first file with columns
            first_cols = set(dataframes[0].columns)
            self._column_registry[table_name] = first_cols

            # Check schema alignment and concatenate
            for i, df in enumerate(dataframes[1:], 1):
                if set(df.columns) != first_cols:
                    print(f"Warning: File {files_with_format[i][0].name} has different columns than {files_with_format[0][0].name}", file=sys.stderr)
                    # Remove misaligned dataframe
                    dataframes[i] = None

            # Filter out None values and concatenate
            valid_dataframes = [df for df in dataframes if df is not None]
            if valid_dataframes:
                self.tables[table_name] = pd.concat(valid_dataframes, ignore_index=True)
