"""Database layer for SQL query CLI supporting multiple file formats."""

import sys
from glob import glob
from pathlib import Path
from typing import Dict, Optional
import pandas as pd
import gzip
import bz2


SUPPORTED_FORMATS = {'.csv', '.parquet', '.tsv', '.json', '.jsonl'}
COMPRESSED_EXTENSIONS = {'.gz', '.bz2'}


def _open_compressed(filepath: Path, comp_ext: Optional[str]):
    if comp_ext == '.gz':
        return gzip.open(filepath, 'rt', encoding='utf-8')
    if comp_ext == '.bz2':
        return bz2.open(filepath, 'rt', encoding='utf-8')
    return open(filepath, 'r', encoding='utf-8')


def _read_csv(filepath, comp_ext):
    with _open_compressed(filepath, comp_ext) as f:
        return pd.read_csv(f)


def _read_parquet(filepath, comp_ext):
    return pd.read_parquet(filepath)


def _read_tsv(filepath, comp_ext):
    with _open_compressed(filepath, comp_ext) as f:
        return pd.read_csv(f, sep='\t')


def _read_json(filepath, comp_ext):
    with _open_compressed(filepath, comp_ext) as f:
        return pd.read_json(f, lines=False)


def _read_jsonl(filepath, comp_ext):
    with _open_compressed(filepath, comp_ext) as f:
        return pd.read_json(f, lines=True)


READERS = {
    '.csv': _read_csv,
    '.parquet': _read_parquet,
    '.tsv': _read_tsv,
    '.json': _read_json,
    '.jsonl': _read_jsonl,
}


def _get_format_from_path(filepath: Path) -> tuple:
    for comp in COMPRESSED_EXTENSIONS:
        if filepath.name.endswith(comp):
            name_without_comp = filepath.name[:-(len(comp))]
            for fmt in SUPPORTED_FORMATS:
                if name_without_comp.endswith(fmt):
                    return fmt, comp
            return None, None
    for fmt in SUPPORTED_FORMATS:
        if filepath.name.endswith(fmt):
            return fmt, None
    return None, None


def _load_files_from_paths(filepaths: list) -> Dict[str, pd.DataFrame]:
    """Load multiple files, grouping by base name."""
    file_groups: Dict[str, list] = {}

    for fp in map(Path, filepaths):
        fmt, comp = _get_format_from_path(fp)
        if fmt and comp:
            base = fp.name
            for c in COMPRESSED_EXTENSIONS:
                if base.endswith(c):
                    base = base[:-(len(c))]
                    break
            for f in SUPPORTED_FORMATS:
                if base.endswith(f):
                    base = base[:-(len(f))]
                    break
            file_groups.setdefault(base, []).append((fp, fmt, comp))
        elif fmt:
            base = fp.name
            for f in SUPPORTED_FORMATS:
                if base.endswith(f):
                    base = base[:-(len(f))]
                    break
            file_groups.setdefault(base, []).append((fp, fmt, None))

    tables = {}
    for base_name, files in file_groups.items():
        table_name = base_name.replace('.', '_')
        dataframes = []

        for fp, fmt, comp in files:
            try:
                df = READERS[fmt](fp, comp)
                if df is not None and len(df.columns) > 0:
                    dataframes.append(df)
            except Exception as e:
                print(f"Warning: Could not load {fp}: {e}", file=sys.stderr)

        if not dataframes:
            continue

        first = dataframes[0]
        for df in dataframes[1:]:
            if list(df.columns) != list(first.columns):
                print(f"Warning: File {fp.name} has different columns", file=sys.stderr)
            else:
                first = pd.concat([first, df], ignore_index=True)

        tables[table_name] = first

    return tables


class CSVDatabase:
    """Manages multiple file format files as database tables."""

    def __init__(self, data_dir: str):
        self.data_dir = Path(data_dir)
        self.tables: Dict[str, pd.DataFrame] = {}
        self._load_tables()

    def _load_tables(self):
        """Load all supported files from data directory."""
        filepaths = []
        for ext in SUPPORTED_FORMATS:
            for fp in self.data_dir.rglob(f"*{ext}"):
                filepaths.append(fp)
            for comp_ext in COMPRESSED_EXTENSIONS:
                for fp in self.data_dir.rglob(f"*{ext}{comp_ext}"):
                    filepaths.append(fp)

        self.tables = _load_files_from_paths(filepaths)

    def get_table(self, name: str) -> Optional[pd.DataFrame]:
        return self.tables.get(name)

    def load_sharded_tables(self, sharded_configs: list):
        """Load sharded tables from glob patterns."""
        for table_name, pattern in sharded_configs:
            filepaths = sorted(glob(pattern))
            if not filepaths:
                self.tables[table_name] = pd.DataFrame()
                continue

            tables = _load_files_from_paths(filepaths)
            if tables:
                self.tables[table_name] = list(tables.values())[0]
            else:
                self.tables[table_name] = pd.DataFrame()