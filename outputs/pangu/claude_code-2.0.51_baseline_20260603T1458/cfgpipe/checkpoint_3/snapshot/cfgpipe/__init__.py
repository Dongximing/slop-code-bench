"""cfgpipe - Configuration pipeline with groups and custom types."""

from .core import Config, SchemaError, ResolutionError
from .types import TypeRegistry, register_type

__all__ = ["Config", "SchemaError", "ResolutionError", "TypeRegistry", "register_type"]
