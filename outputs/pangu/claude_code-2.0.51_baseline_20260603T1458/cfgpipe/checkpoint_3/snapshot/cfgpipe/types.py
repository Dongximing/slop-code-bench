"""Custom parameter types and type registry."""

from abc import ABC, abstractmethod
from typing import Any
import re


class Type(ABC):
    """Base class for parameter types."""

    @abstractmethod
    def parse(self, value: str, path: str) -> Any:
        """Parse a string value into a typed value. Returns the parsed value.

        Must raise ValueError if the value doesn't match the type.
        """
        pass

    @abstractmethod
    def render(self, value: Any) -> str:
        """Render a typed value back to a string."""
        pass


class StringType(Type):
    """String type - no conversion needed."""

    def parse(self, value: str, path: str) -> str:
        return value

    def render(self, value: Any) -> str:
        return str(value)


class IntegerType(Type):
    """Integer type."""

    def parse(self, value: str, path: str) -> int:
        try:
            return int(value)
        except ValueError:
            raise ValueError(f"invalid integer: {value!r}")

    def render(self, value: Any) -> str:
        return str(int(value))


class FloatType(Type):
    """Float type."""

    def parse(self, value: str, path: str) -> float:
        try:
            return float(value)
        except ValueError:
            raise ValueError(f"invalid float: {value!r}")

    def render(self, value: Any) -> str:
        return str(float(value))


class BooleanType(Type):
    """Boolean type - accepts 'true'/'false', '1'/'0', 'yes'/'no', 'on'/'off' (case insensitive)."""

    TRUE_VALUES = {"true", "1", "yes", "on"}
    FALSE_VALUES = {"false", "0", "no", "off"}

    def parse(self, value: str, path: str) -> bool:
        lower = value.lower()
        if lower in self.TRUE_VALUES:
            return True
        if lower in self.FALSE_VALUES:
            return False
        raise ValueError(f"invalid boolean: {value!r}")

    def render(self, value: Any) -> str:
        return "true" if bool(value) else "false"


class PortType(Type):
    """Port type - integer 0-65535, rendered as plain decimal with no leading zeros except '0'."""

    def parse(self, value: str, path: str) -> int:
        try:
            port = int(value)
        except ValueError:
            raise ValueError(f"invalid port (not an integer): {value!r}")
        if port < 0 or port > 65535:
            raise ValueError(f"port out of range (0-65535): {port}")
        # Check for leading zeros
        if value != "0" and value.startswith("0"):
            raise ValueError(f"port has leading zeros: {value!r}")
        return port

    def render(self, value: Any) -> str:
        return str(int(value))


class TypeRegistry:
    """Registry for parameter types."""

    def __init__(self):
        self._types: dict[str, Type] = {}
        self._register_builtins()

    def _register_builtins(self):
        """Register built-in types."""
        self.register("string", StringType())
        self.register("integer", IntegerType())
        self.register("float", FloatType())
        self.register("boolean", BooleanType())
        self.register("port", PortType())

    def register(self, name: str, type_obj: Type) -> None:
        """Register a custom type."""
        self._types[name] = type_obj

    def get(self, name: str) -> Type | None:
        """Get a type by name."""
        return self._types.get(name)

    def has(self, name: str) -> bool:
        """Check if a type exists."""
        return name in self._types

    def list_types(self) -> list[str]:
        """List all registered type names."""
        return list(self._types.keys())


# Global type registry instance
_builtin_registry = TypeRegistry()


class BuiltinTypes:
    """Access to built-in types."""

    STRING = _builtin_registry.get("string")
    INTEGER = _builtin_registry.get("integer")
    FLOAT = _builtin_registry.get("float")
    BOOLEAN = _builtin_registry.get("boolean")
    PORT = _builtin_registry.get("port")

    @staticmethod
    def register(name: str, type_obj: Type) -> None:
        """Register a type with the global registry."""
        _builtin_registry.register(name, type_obj)

    @staticmethod
    def get(name: str) -> Type | None:
        """Get a type from the global registry."""
        return _builtin_registry.get(name)

    @staticmethod
    def has(name: str) -> bool:
        """Check if a type exists in the global registry."""
        return _builtin_registry.has(name)


def register_type(name: str, type_obj: Type) -> None:
    """Register a custom type."""
    BuiltinTypes.register(name, type_obj)
