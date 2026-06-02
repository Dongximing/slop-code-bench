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
