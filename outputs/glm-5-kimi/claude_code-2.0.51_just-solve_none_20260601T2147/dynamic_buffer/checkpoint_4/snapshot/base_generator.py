#!/usr/bin/env python3
"""Base code generator with shared utilities for multi-language code generation."""

from typing import Any, Dict, List, Optional


def val_str(val: Any, lang: str = 'py') -> str:
    """Format a value as a string for the target language.

    Args:
        val: The value to format (bool, str, number, etc.)
        lang: Target language - 'py', 'js', 'cpp', or 'rust'

    Returns:
        String representation appropriate for the target language
    """
    if isinstance(val, bool):
        if lang == 'py':
            return 'True' if val else 'False'
        return 'true' if val else 'false'
    if isinstance(val, str):
        return "'" + val + "'"
    return str(val)


def compute_median(values: List[float]) -> float:
    """Compute median using lower-middle rule for even count.

    For an odd number of values: middle value in sorted order.
    For an even number: element at index floor((k-1)/2) in 0-based sorted order.
    """
    if not values:
        return 0.0
    sorted_vals = sorted(values)
    k = len(sorted_vals)
    idx = (k - 1) // 2
    return sorted_vals[idx]


class CodeGenerator:
    """Base class for language-specific code generators.

    Provides common data access and utility methods. Subclasses implement
    language-specific code generation logic.
    """

    def __init__(self, module_name: str, config: Dict, file_ext: str):
        self.module_name = module_name
        self.config = config
        self.file_ext = file_ext

    @property
    def has_stateful(self) -> bool:
        """Check if config has stateful transforms."""
        return bool(self.config.get('stateful_transforms'))

    @property
    def has_neighbor_filters(self) -> bool:
        """Check if config has neighbor-based filters."""
        return bool(self.config.get('neighbor_filters'))

    @property
    def has_centered_windows(self) -> bool:
        """Check if config has centered window transforms."""
        for st in self.get_stateful_transforms():
            if st.get('type') == 'centered_window':
                return True
        return False

    @property
    def has_partitioned_state(self) -> bool:
        """Check if config has partitioned state."""
        for st in self.get_stateful_transforms():
            if st.get('partition_by') or st.get('type') in ('partitioned_window', 'partitioned_row_number'):
                return True
        return False

    @property
    def delimiter(self) -> str:
        """Get the delimiter for delimited file formats."""
        if self.file_ext == 'csv':
            return ','
        elif self.file_ext == 'tsv':
            return '\t'
        return None

    def get_stateful_transforms(self) -> List[Dict]:
        """Get list of stateful transforms from config."""
        return self.config.get('stateful_transforms', [])

    def get_column_transforms(self) -> Dict:
        """Get column transforms from config."""
        return self.config.get('column_transforms', {})

    def get_filter_conditions(self) -> List[Dict]:
        """Get filter conditions from config."""
        return self.config.get('filter_conditions', [])

    def get_neighbor_filters(self) -> List[Dict]:
        """Get neighbor filters from config."""
        return self.config.get('neighbor_filters', [])

    def get_output_columns(self) -> List[str]:
        """Get output column names from config."""
        return self.config.get('output_columns', [])

    def get_stateful_output_columns(self) -> set:
        """Get set of column names produced by stateful transforms."""
        return {st.get('output_column') for st in self.get_stateful_transforms()}

    def get_partition_columns(self) -> List[str]:
        """Get partition columns from config."""
        return self.config.get('partition_columns', [])

    def get_max_lookahead(self) -> int:
        """Get maximum lookahead required for centered windows."""
        max_lookahead = 0
        for st in self.get_stateful_transforms():
            if st.get('type') == 'centered_window':
                lookahead = st.get('lookahead', 0)
                max_lookahead = max(max_lookahead, lookahead)
        return max_lookahead

    def get_max_window_size(self) -> int:
        """Get maximum window size across all window transforms."""
        max_size = 0
        for st in self.get_stateful_transforms():
            st_type = st.get('type')
            if st_type == 'sliding_window':
                max_size = max(max_size, st.get('window_size', 1))
            elif st_type == 'centered_window':
                size = st.get('lookbehind', 0) + 1 + st.get('lookahead', 0)
                max_size = max(max_size, size)
            elif st_type == 'partitioned_window':
                max_size = max(max_size, st.get('window_size', 1))
        return max_size

    def get_centered_window_transforms(self) -> List[Dict]:
        """Get list of centered window transforms."""
        return [st for st in self.get_stateful_transforms()
                if st.get('type') == 'centered_window']

    def get_partitioned_transforms(self) -> List[Dict]:
        """Get list of partitioned transforms."""
        return [st for st in self.get_stateful_transforms()
                if st.get('partition_by') or st.get('type') in ('partitioned_window', 'partitioned_row_number')]

    def get_ranking_transforms(self) -> List[Dict]:
        """Get list of ranking transforms."""
        return [st for st in self.get_stateful_transforms()
                if st.get('type') in ('rank', 'dense_rank', 'row_number', 'partitioned_row_number')]

    def generate(self) -> Dict[str, str]:
        """Generate code files. Subclasses must implement."""
        raise NotImplementedError("Subclasses must implement generate()")
