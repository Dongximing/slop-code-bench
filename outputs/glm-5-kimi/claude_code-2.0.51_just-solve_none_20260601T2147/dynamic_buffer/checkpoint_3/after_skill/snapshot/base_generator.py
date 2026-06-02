#!/usr/bin/env python3
"""Base code generator with shared utilities for multi-language code generation."""

from typing import Any, Dict, List


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

    def generate(self) -> Dict[str, str]:
        """Generate code files. Subclasses must implement."""
        raise NotImplementedError("Subclasses must implement generate()")
