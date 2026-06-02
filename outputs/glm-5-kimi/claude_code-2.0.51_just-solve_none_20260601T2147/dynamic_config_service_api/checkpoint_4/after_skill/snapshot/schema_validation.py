"""JSON Schema validation."""

import jsonschema
from jsonschema import validate


class SchemaValidationError(Exception):
    def __init__(self, path, rule, expected, actual):
        self.path = path
        self.rule = rule
        self.expected = expected
        self.actual = actual
        super().__init__(f"Validation failed at {path}")


def get_json_type(value):
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "unknown"


class ExternalRefError(Exception):
    pass


def check_external_refs(obj):
    if isinstance(obj, dict):
        if '$ref' in obj:
            ref = obj['$ref']
            if ref.startswith(('http://', 'https://', '//')):
                raise ExternalRefError()
            if not ref.startswith('#') and ':' in ref.split('/')[0]:
                raise ExternalRefError()
        for v in obj.values():
            check_external_refs(v)
    elif isinstance(obj, list):
        for item in obj:
            check_external_refs(item)


def _format_validation_error(e):
    path = "/" + "/".join(str(p) for p in e.absolute_path) if e.absolute_path else "/"
    rule = e.validator
    val = e.validator_value

    if rule == "type":
        expected = ", ".join(val) if isinstance(val, list) else val
        actual = get_json_type(e.instance)
    elif rule == "enum":
        expected = ", ".join(repr(v) for v in val)
        actual = get_json_type(e.instance)
    elif rule == "required":
        path = path + "/" + val[0] if val else path
        expected = "property required"
        actual = "missing"
    elif rule == "pattern":
        expected = f"matching pattern {val}"
        actual = get_json_type(e.instance)
    elif rule in ("minimum", "maximum"):
        op = ">=" if rule == "minimum" else "<="
        expected = f"{op} {val}"
        actual = str(e.instance)
    elif rule in ("minLength", "maxLength"):
        op = "length >=" if rule == "minLength" else "length <="
        expected = f"{op} {val}"
        actual = f"length {len(e.instance)}"
    elif rule in ("minItems", "maxItems"):
        op = "items >=" if rule == "minItems" else "items <="
        expected = f"{op} {val}"
        actual = f"items {len(e.instance)}"
    else:
        expected = str(val)
        actual = str(e.instance)[:50]

    return SchemaValidationError(path, rule, expected, actual)


def validate_config_against_schema(config, schema):
    try:
        check_external_refs(schema)
        validate(instance=config, schema=schema)
    except jsonschema.exceptions.ValidationError as e:
        raise _format_validation_error(e)
    except ExternalRefError:
        raise SchemaValidationError("/", "schema", "no external $ref", "external $ref found")
    except Exception as e:
        raise SchemaValidationError("/", "schema", "valid schema", str(e))
