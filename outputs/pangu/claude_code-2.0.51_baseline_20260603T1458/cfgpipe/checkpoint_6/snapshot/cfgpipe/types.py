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
        """Render a typed value back to a string for change events."""
        pass

    def json_value(self, value: Any) -> Any:
        """Convert a typed value to a JSON-serializable representation.

        Default implementation returns the value as-is for types that
        already produce JSON-serializable values from parse().
        Override for types that need special JSON serialization.
        """
        return value


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


class DurationType(Type):
    """Duration type - non-negative time span with units h, m, s."""

    SECONDS_PER_HOUR = 3600
    SECONDS_PER_MINUTE = 60

    def parse(self, value: str, path: str) -> int:
        """Parse duration string, return total seconds as integer."""
        if not value:
            raise ValueError(f"invalid duration: empty string")

        total_seconds = 0
        i = 0
        length = len(value)

        while i < length:
            # Parse number
            num_str = ''
            while i < length and value[i].isdigit():
                num_str += value[i]
                i += 1

            if not num_str:
                raise ValueError(f"invalid duration: expected number at position {i}: {value!r}")

            # Parse unit
            if i >= length:
                # Bare number without unit - fatal error
                raise ValueError(f"invalid duration: bare number without unit suffix: {value!r}")

            unit = value[i]
            i += 1

            if unit == 'h':
                total_seconds += int(num_str) * self.SECONDS_PER_HOUR
            elif unit == 'm':
                total_seconds += int(num_str) * self.SECONDS_PER_MINUTE
            elif unit == 's':
                total_seconds += int(num_str)
            else:
                raise ValueError(f"invalid duration: unknown unit {unit!r}")

        if total_seconds < 0:
            raise ValueError(f"invalid duration: negative duration: {value!r}")

        return total_seconds

    def render(self, value: Any) -> str:
        """Render duration as normalized string."""
        total_seconds = int(value)
        if total_seconds == 0:
            return "0s"

        hours = total_seconds // self.SECONDS_PER_HOUR
        remaining = total_seconds % self.SECONDS_PER_HOUR
        minutes = remaining // self.SECONDS_PER_MINUTE
        seconds = remaining % self.SECONDS_PER_MINUTE

        parts = []
        if hours > 0:
            parts.append(f"{hours}h")
        if minutes > 0:
            parts.append(f"{minutes}m")
        if seconds > 0:
            parts.append(f"{seconds}s")

        return ''.join(parts)

    def json_value(self, value: Any) -> str:
        """JSON serialization is the normalized string form."""
        return self.render(value)


class PatternType(Type):
    """Pattern type - validated regular-expression pattern string."""

    def parse(self, value: str, path: str) -> str:
        """Validate regex pattern, return original pattern string."""
        try:
            re.compile(value)
        except re.error as e:
            raise ValueError(f"invalid pattern: {e}")
        return value

    def render(self, value: Any) -> str:
        """Render pattern as original string."""
        return str(value)

    def json_value(self, value: Any) -> str:
        """JSON serialization is the original pattern string."""
        return str(value)


class MapType(Type):
    """Map type - string key-value pairs."""

    def parse(self, value: str, path: str) -> dict[str, str]:
        """Parse comma-separated key:value pairs into a dict."""
        result = {}

        if not value:
            return result

        parts = value.split(',')
        for part in parts:
            if ':' not in part:
                raise ValueError(f"invalid map entry: missing colon in {part!r}")
            key, sep, val = part.partition(':')
            # Take everything after the first colon as the value
            # Key is before the first colon, value is after
            result[key] = val

        return result

    def render(self, value: Any) -> str:
        """Render map as lexicographically sorted key:value pairs."""
        d = value
        if isinstance(d, dict):
            parts = [f"{k}:{v}" for k, v in sorted(d.items())]
            return ','.join(parts)
        return str(d)

    def json_value(self, value: Any) -> dict[str, str]:
        """JSON serialization is a JSON object."""
        return dict(value) if isinstance(value, dict) else {}


class ListType(Type):
    """List type - ordered sequence of strings."""

    def parse(self, value: str, path: str) -> list[str]:
        """Parse comma-separated segments into a list of strings."""
        if not value:
            return []
        return value.split(',')

    def render(self, value: Any) -> str:
        """Render list as comma-separated string in insertion order."""
        lst = value
        if isinstance(lst, list):
            return ','.join(lst)
        return str(lst)

    def json_value(self, value: Any) -> list[str]:
        """JSON serialization is a JSON array of strings."""
        return list(value) if isinstance(value, list) else []


class RedactedType(Type):
    """Redacted type - value is hidden, uses masked representation."""

    # Use a consistent masked value
    MASKED = "<masked>"

    def parse(self, value: str, path: str) -> str:
        """Redacted type accepts any string input."""
        return value

    def render(self, value: Any) -> str:
        """Render as masked value."""
        return self.MASKED

    def json_value(self, value: Any) -> str:
        """JSON serialization is the masked value."""
        return self.MASKED


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
        self.register("duration", DurationType())
        self.register("pattern", PatternType())
        self.register("map", MapType())
        self.register("list", ListType())
        self.register("redacted", RedactedType())

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
    DURATION = _builtin_registry.get("duration")
    PATTERN = _builtin_registry.get("pattern")
    MAP = _builtin_registry.get("map")
    LIST = _builtin_registry.get("list")
    REDACTED = _builtin_registry.get("redacted")

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
