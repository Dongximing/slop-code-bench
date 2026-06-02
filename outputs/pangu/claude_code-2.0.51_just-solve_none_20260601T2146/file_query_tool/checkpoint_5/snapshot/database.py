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


def _read_file(filepath: Path, fmt: str, comp_ext: Optional[str]) -> pd.DataFrame:
    """Read a file based on its format and compression."""
    opener = {
        '.gz': gzip.open,
        '.bz2': bz2.open,
        None: open,
    }[comp_ext]

    read_kwargs = {'encoding': 'utf-8'} if comp_ext else {}

    if fmt == '.csv':
        with opener(filepath, 'rt', **read_kwargs) as f:
            return pd.read_csv(f)
    elif fmt == '.parquet':
        return pd.read_parquet(filepath)
    elif fmt == '.tsv':
        with opener(filepath, 'rt', **read_kwargs) as f:
            return pd.read_csv(f, sep='\t')
    elif fmt == '.json':
        with opener(filepath, 'rt', **read_kwargs) as f:
            return pd.read_json(f, lines=False)
    elif fmt == '.jsonl':
        with opener(filepath, 'rt', **read_kwargs) as f:
            return pd.read_json(f, lines=True)
    return pd.DataFrame()


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


def _extract_base_name(filepath: Path) -> str:
    """Extract base name from filepath, removing format and compression extensions."""
    name = filepath.name
    for ext in COMPRESSED_EXTENSIONS:
        if name.endswith(ext):
            name = name[:-(len(ext))]
            break
    for fmt in SUPPORTED_FORMATS:
        if name.endswith(fmt):
            name = name[:-(len(fmt))]
            break
    return name


def _load_files_from_paths(filepaths: list) -> Dict[str, pd.DataFrame]:
    """Load multiple files, grouping by base name."""
    file_groups: Dict[str, list] = {}

    for fp in map(Path, filepaths):
        fmt, comp = _get_format_from_path(fp)
        if fmt:
            base = _extract_base_name(fp)
            file_groups.setdefault(base, []).append((fp, fmt, comp))

    tables = {}
    for base_name, files in file_groups.items():
        table_name = base_name.replace('.', '_')
        dataframes = []

        for fp, fmt, comp in files:
            try:
                df = _read_file(fp, fmt, comp)
                if not df.empty:
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
            filepaths.extend(self.data_dir.rglob(f"*{ext}"))
            for comp_ext in COMPRESSED_EXTENSIONS:
                filepaths.extend(self.data_dir.rglob(f"*{ext}{comp_ext}"))
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
            self.tables[table_name] = list(tables.values())[0] if tables else pd.DataFrame()