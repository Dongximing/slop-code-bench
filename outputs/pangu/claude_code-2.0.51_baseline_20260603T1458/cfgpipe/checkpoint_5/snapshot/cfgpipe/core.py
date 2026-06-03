"""cfgpipe - Configuration pipeline core implementation."""

import os
import json
from typing import Any
from .types import TypeRegistry, Type


class SchemaError(Exception):
    """Raised when schema validation fails."""

    def __init__(self, message: str, path: str | None = None, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.path = path
        self.details = details or {}


class ResolutionError(Exception):
    """Raised when parameter resolution fails."""

    def __init__(self, message: str, path: str, source: str, detail: str):
        super().__init__(message)
        self.path = path
        self.source = source
        self.detail = detail


class _Parameter:
    """Internal representation of a parameter declaration."""

    __slots__ = ['type', 'default', 'env', 'file', 'arg', 'primary_store']

    def __init__(
        self,
        type: str,
        default: str | None = None,
        env: str | None = None,
        file: str | None = None,
        arg: str | None = None,
        primary_store: str | None = None
    ):
        self.type = type
        self.default = default
        self.env = env
        self.file = file
        self.arg = arg
        self.primary_store = primary_store


class _SchemaValidator:
    """Validates schema structure and collects parameter declarations."""

    SOURCE_KEYS = {'default', 'env', 'file', 'arg', 'primary-store'}

    def __init__(self, registry: TypeRegistry):
        self.registry = registry
        self.errors: list[SchemaError] = []

    def validate(self, schema: dict[str, Any]) -> dict[str, _Parameter]:
        """Validate schema and return parameter declarations keyed by path."""
        if not isinstance(schema, dict):
            raise SchemaError("schema root must be an object")
        if not schema:
            raise SchemaError("schema root must be non-empty")

        params: dict[str, _Parameter] = {}
        primary_stores: dict[str, str] = {}  # primary-store key -> path

        self._validate_group(schema, '', params, primary_stores)

        if self.errors:
            raise self.errors[0]

        return params

    def _validate_group(
        self,
        group: dict[str, Any],
        path: str,
        params: dict[str, _Parameter],
        primary_stores: dict[str, str]
    ) -> None:
        """Recursively validate a group and its contents."""
        if not isinstance(group, dict):
            self.errors.append(SchemaError(
                f"group at '{path}' must be an object",
                path if path else None
            ))
            return

        # Check if group has type field - that's invalid
        if 'type' in group:
            self.errors.append(SchemaError(
                f"group '{path}' must not define a 'type' field for itself",
                path if path else None
            ))
            return

        # Check for source annotations on group
        for key in self.SOURCE_KEYS:
            if key in group:
                val = group[key]
                if isinstance(val, str):
                    self.errors.append(SchemaError(
                        f"group '{path}' must not contain source-annotation '{key}' with non-object value",
                        path if path else None
                    ))
                    return

        for name, value in group.items():
            if not isinstance(name, str):
                self.errors.append(SchemaError(
                    f"schema key must be a string, got {type(name).__name__}",
                    path if path else None
                ))
                continue

            # Build the full path for this entry
            entry_path = f"{path}.{name}" if path else name

            # Must be object-valued
            if not isinstance(value, dict):
                self.errors.append(SchemaError(
                    f"entry '{entry_path}' must be object-valued",
                    entry_path
                ))
                continue

            # Check if this is a parameter (has type field)
            if 'type' in value:
                self._validate_parameter(value, entry_path, params, primary_stores)
            else:
                # This is a nested group
                self._validate_group(value, entry_path, params, primary_stores)

    def _validate_parameter(
        self,
        param: dict[str, Any],
        path: str,
        params: dict[str, _Parameter],
        primary_stores: dict[str, str]
    ) -> None:
        """Validate a parameter declaration."""
        # Check type field
        if 'type' not in param or not isinstance(param['type'], str):
            self.errors.append(SchemaError(
                f"parameter '{path}' must have a string-valued 'type' field",
                path
            ))
            return

        type_name = param['type']

        # Check if type is recognized
        if not self.registry.has(type_name):
            self.errors.append(SchemaError(
                f"parameter '{path}' has unrecognized type '{type_name}'",
                path
            ))
            return

        # Validate source keys
        default = param.get('default')
        env = param.get('env')
        file = param.get('file')
        arg = param.get('arg')
        primary_store = param.get('primary-store')

        # Validate types of source values
        for key, val in [('default', default), ('env', env), ('file', file), ('arg', arg), ('primary-store', primary_store)]:
            if val is not None and not isinstance(val, str):
                self.errors.append(SchemaError(
                    f"parameter '{path}' source-annotation '{key}' must have a string value",
                    path
                ))
                return

        # Check duplicate primary-store
        if primary_store is not None:
            if primary_store in primary_stores:
                self.errors.append(SchemaError(
                    f"duplicate primary-store key '{primary_store}' used by '{primary_stores[primary_store]}' and '{path}'",
                    path
                ))
                return
            primary_stores[primary_store] = path

        # Store parameter
        params[path] = _Parameter(
            type=type_name,
            default=default,
            env=env,
            file=file,
            arg=arg,
            primary_store=primary_store
        )


class _SourceResolver:
    """Resolves parameter values from various sources."""

    def __init__(self, registry: TypeRegistry):
        self.registry = registry

    def resolve(
        self,
        params: dict[str, _Parameter],
        cli_args: dict[str, str] | None = None,
        env: dict[str, str] | None = None,
        file_contents: dict[str, str] | None = None
    ) -> dict[str, Any]:
        """Resolve all parameters and return the configuration dict with type-native values."""
        result: dict[str, Any] = {}
        errors: list[tuple[str, str, str]] = []  # (path, source, detail)

        for path, param in params.items():
            type_obj = self.registry.get(param.type)
            if type_obj is None:
                errors.append((path, 'schema', f"type '{param.type}' not found"))
                continue

            value, source = self._resolve_parameter(param, cli_args, env, file_contents)

            if value is not None:
                try:
                    # Parse the value into a typed representation
                    parsed = type_obj.parse(value, path)
                    # Use parsed value
                    result[path] = parsed
                except ValueError as e:
                    errors.append((path, source, str(e)))
            elif param.default is not None:
                try:
                    # Parse the default value
                    parsed = type_obj.parse(param.default, path)
                    # Use parsed value
                    result[path] = parsed
                except ValueError as e:
                    errors.append((path, 'default', str(e)))

        if errors:
            # Raise first error
            path, source, detail = errors[0]
            raise ResolutionError(
                f"failed to resolve parameter '{path}' from {source}",
                path=path,
                source=source,
                detail=detail
            )

        return result

    def _resolve_parameter(
        self,
        param: _Parameter,
        cli_args: dict[str, str] | None,
        env: dict[str, str] | None,
        file_contents: dict[str, str] | None
    ) -> tuple[str | None, str | None]:
        """Resolve a single parameter value, return (value, source) or (None, None)."""
        # Priority: arg > env > file > default
        if param.arg and cli_args is not None and param.arg in cli_args:
            return (cli_args[param.arg], 'arg')
        if param.env and env is not None and param.env in env:
            return (env[param.env], 'env')
        if param.file and file_contents is not None and param.file in file_contents:
            return (file_contents[param.file].strip(), 'file')
        return (None, None)


def _build_output(params: dict[str, _Parameter], resolved: dict[str, Any], schema: dict[str, Any], registry: TypeRegistry) -> dict[str, Any]:
    """Build nested output dict from resolved parameters, preserving group structure.

    Uses type-native JSON serialization for resolved values.
    """
    # First, build the full structure from schema
    def copy_structure(schema_node: dict[str, Any]) -> dict[str, Any]:
        """Copy the schema structure without values."""
        node = {}
        for key, value in schema_node.items():
            if 'type' in value:
                # This is a leaf parameter
                continue
            else:
                # This is a group
                node[key] = copy_structure(value)
        return node

    result = copy_structure(schema)

    # Fill in resolved values with type-native JSON serialization
    for path, value in resolved.items():
        parts = path.split('.')
        current = result
        for part in parts[:-1]:
            if part not in current:
                # Create intermediate structure
                current[part] = {}
            current = current[part]

        # Get the type object for JSON serialization
        param = params.get(path)
        if param and param.type:
            type_obj = registry.get(param.type)
            if type_obj:
                value = type_obj.json_value(value)

        current[parts[-1]] = value

    return result


class Config:
    """Configuration manager with hierarchical schema and type resolution."""

    def __init__(self, schema: dict[str, Any], registry: TypeRegistry | None = None):
        """Initialize with a schema.

        Args:
            schema: The configuration schema as a nested dict
            registry: Optional custom type registry (defaults to global)
        """
        from .types import BuiltinTypes
        self.registry = registry or TypeRegistry()
        self.schema = self._validate_schema(schema)
        self.params = self._collect_parameters(schema)

    def _validate_schema(self, schema: dict[str, Any]) -> dict[str, Any]:
        """Validate the schema structure."""
        validator = _SchemaValidator(self.registry)
        return schema  # Return validated schema

    def _collect_parameters(self, schema: dict[str, Any]) -> dict[str, _Parameter]:
        """Collect parameter declarations from schema."""
        validator = _SchemaValidator(self.registry)
        return validator.validate(schema)

    def resolve(
        self,
        cli_args: dict[str, str] | None = None,
        env: dict[str, str] | None = None,
        file_contents: dict[str, str] | None = None
    ) -> dict[str, Any]:
        """Resolve configuration from various sources.

        Args:
            cli_args: Command-line arguments as dict
            env: Environment variables as dict
            file_contents: File contents keyed by filename

        Returns:
            Nested dict with resolved type-native values, mirroring schema hierarchy
        """
        resolver = _SourceResolver(self.registry)
        resolved = resolver.resolve(self.params, cli_args, env, file_contents)
        return _build_output(self.params, resolved, self.schema, self.registry)

    def get_parameter_names(self) -> list[str]:
        """Get list of all parameter paths."""
        return list(self.params.keys())

    def get_group_names(self) -> list[str]:
        """Get list of all group paths."""
        groups = set()
        for path in self.params.keys():
            parts = path.split('.')
            for i in range(1, len(parts)):
                groups.add('.'.join(parts[:i]))
        return sorted(groups)


def load_schema_file(path: str) -> dict[str, Any]:
    """Load a schema from a JSON file."""
    with open(path, 'r') as f:
        return json.load(f)


def save_config(config: dict[str, Any], path: str) -> None:
    """Save configuration to a JSON file."""
    with open(path, 'w') as f:
        json.dump(config, f, indent=2)
