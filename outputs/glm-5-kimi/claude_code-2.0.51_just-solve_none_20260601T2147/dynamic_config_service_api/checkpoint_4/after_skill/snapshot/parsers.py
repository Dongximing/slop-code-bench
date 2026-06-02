"""Configuration parsing for JSON, YAML, and TOML formats."""

import re
import json
import yaml
import toml


def normalize_value(value):
    if isinstance(value, dict):
        return {k: normalize_value(v) for k, v in sorted(value.items())}
    if isinstance(value, list):
        return [normalize_value(item) for item in value]
    if isinstance(value, float) and value == int(value) and abs(value) < 10**15:
        return float(int(value))
    return value


def canonical_json(obj):
    return json.dumps(normalize_value(obj), separators=(',', ':'), sort_keys=True)


class ParseError(Exception):
    def __init__(self, message, reason=None):
        super().__init__(message)
        self.reason = reason


def parse_json(raw):
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise ParseError(f"Invalid JSON: {e}")


class SafeYamlLoader(yaml.SafeLoader):
    pass


def _yaml_construct_undefined(loader, node):
    raise ParseError("YAML custom tags are not allowed", "yaml_feature_not_allowed")


def _yaml_construct_merge(loader, node):
    raise ParseError("YAML merge keys are not allowed", "yaml_feature_not_allowed")


SafeYamlLoader.add_constructor(None, _yaml_construct_undefined)
SafeYamlLoader.add_constructor('tag:yaml.org,2002:null', lambda l, n: None)
SafeYamlLoader.add_constructor('tag:yaml.org,2002:bool', lambda l, n: l.construct_yaml_bool(n))
SafeYamlLoader.add_constructor('tag:yaml.org,2002:int', lambda l, n: l.construct_yaml_int(n))
SafeYamlLoader.add_constructor('tag:yaml.org,2002:float', lambda l, n: l.construct_yaml_float(n))
SafeYamlLoader.add_constructor('tag:yaml.org,2002:str', lambda l, n: l.construct_yaml_str(n))
SafeYamlLoader.add_constructor('tag:yaml.org,2002:seq', lambda l, n: l.construct_yaml_seq(n))
SafeYamlLoader.add_constructor('tag:yaml.org,2002:map', lambda l, n: l.construct_yaml_map(n))
SafeYamlLoader.add_constructor('tag:yaml.org,2002:binary', lambda l, n: l.construct_yaml_binary(n))
SafeYamlLoader.add_constructor('tag:yaml.org,2002:timestamp', lambda l, n: l.construct_yaml_timestamp(n))
SafeYamlLoader.add_constructor('tag:yaml.org,2002:omap', lambda l, n: l.construct_yaml_omap(n))
SafeYamlLoader.add_constructor('tag:yaml.org,2002:pairs', lambda l, n: l.construct_yaml_pairs(n))
SafeYamlLoader.add_constructor('tag:yaml.org,2002:set', lambda l, n: l.construct_yaml_set(n))


def _strip_quotes(text):
    """Remove quoted strings from text for YAML feature scanning."""
    text = re.sub(r'"[^"]*"', '', text)
    return re.sub(r"'[^']*'", '', text)


def parse_yaml(raw):
    try:
        if '<<:' in raw or '<< :' in raw:
            raise ParseError("YAML merge keys are not allowed", "yaml_feature_not_allowed")

        if '&' in raw or '*' in raw:
            for line in raw.split('\n'):
                stripped = _strip_quotes(line)
                if re.search(r'&\w+', stripped):
                    raise ParseError("YAML anchors are not allowed", "yaml_feature_not_allowed")
                if re.search(r'\*\w+', stripped):
                    raise ParseError("YAML aliases are not allowed", "yaml_feature_not_allowed")

        if re.search(r'!\w+', raw):
            for line in raw.split('\n'):
                stripped = _strip_quotes(line)
                if re.search(r'(?<!!)!\w+', stripped):
                    raise ParseError("YAML custom tags are not allowed", "yaml_feature_not_allowed")

        result = yaml.load(raw, Loader=SafeYamlLoader)

        def check_keys(obj):
            if isinstance(obj, dict):
                for k in obj.keys():
                    if not isinstance(k, str):
                        raise ParseError("YAML mapping keys must be strings", "yaml_feature_not_allowed")
                    check_keys(obj[k])
            elif isinstance(obj, list):
                for item in obj:
                    check_keys(item)

        check_keys(result)
        return result
    except yaml.YAMLError as e:
        raise ParseError(f"Invalid YAML: {e}")


def parse_toml(raw):
    try:
        result = toml.loads(raw)

        def check_json_types(obj, path=""):
            if isinstance(obj, dict):
                for k, v in obj.items():
                    check_json_types(v, f"{path}/{k}")
            elif isinstance(obj, list):
                for i, item in enumerate(obj):
                    check_json_types(item, f"{path}/{i}")
            elif hasattr(obj, 'isoformat'):
                raise ParseError(f"Non-JSON type at {path}: datetime not allowed", "non_json_type")

        check_json_types(result)
        return result
    except toml.TomlDecodeError as e:
        raise ParseError(f"Invalid TOML: {e}")


PARSERS = {'json': parse_json, 'yaml': parse_yaml, 'toml': parse_toml}


def parse_raw_config(raw, fmt):
    fmt = fmt.lower()
    if fmt not in PARSERS:
        raise ParseError(f"Unsupported format: {fmt}")
    return PARSERS[fmt](raw)
