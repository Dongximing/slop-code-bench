#!/usr/bin/env python3
"""
synth_configs - Layered YAML/JSON config merger for experiment specifications.

Turns layered YAML/JSON inputs into a canonical experiment spec with validation.
Supports single-run mode (Parts 1-2) and manifest-based multi-run mode (Part 3).
"""

import csv
import json
import re
import sys
from pathlib import Path
from typing import Any

import yaml


# Error codes
ERROR_CODES = {
    "MISSING_FILE": "MISSING_FILE",
    "PARSE_ERROR": "PARSE_ERROR",
    "INVALID_STRUCTURE": "INVALID_STRUCTURE",
    "MISSING_KEY": "MISSING_KEY",
    "INVALID_VALUE": "INVALID_VALUE",
    "INVALID_INPUT": "INVALID_INPUT",
    "UNKNOWN_FRAGMENT": "UNKNOWN_FRAGMENT",
    "FRAGMENT_CYCLE": "FRAGMENT_CYCLE",
    "INVALID_TEMPLATE": "INVALID_TEMPLATE",
    "UNKNOWN_ENV_VAR": "UNKNOWN_ENV_VAR",
    "INVALID_MANIFEST": "INVALID_MANIFEST",
    "RUN_FAILURE": "RUN_FAILURE",
}

# Source file mapping for warnings
SOURCE_NAMES = {
    "base": "base.yaml",
    "defaults": "defaults.yaml",
    "run": "run.yaml",
    "flags": "flags.json",
}

# Run ID validation pattern (only alphanumeric, underscore, hyphen)
VALID_RUN_ID_PATTERN = re.compile(r'^[a-zA-Z0-9_-]+$')


class MergeError(Exception):
    """Exception raised for merge-related errors."""
    pass


class ManifestError(Exception):
    """Exception raised for manifest parsing/validation errors."""
    pass


def error_exit(code: str, message: str) -> None:
    """Write error to stderr as JSON and exit with code 1."""
    error_obj = {"error": {"code": code, "message": message}}
    print(json.dumps(error_obj), file=sys.stderr)
    sys.exit(1)


def parse_args(argv):
    """Parse CLI args allowing any order but rejecting unknown flags."""
    valid_flags = {"--base", "--overrides", "--fragments", "--manifest", "--flags", "--env", "--out"}
    args = {
        "base": None,
        "overrides": None,
        "fragments": None,
        "manifest": None,
        "flags": None,
        "env": None,
        "out": None,
    }
    encountered = set()

    i = 0
    while i < len(argv):
        flag = argv[i]
        if flag not in valid_flags:
            error_exit(
                ERROR_CODES["INVALID_INPUT"],
                f"Unknown flag: {flag}"
            )
        if flag in encountered:
            error_exit(
                ERROR_CODES["INVALID_INPUT"],
                f"Duplicate flag: {flag}"
            )
        i += 1
        if i >= len(argv):
            error_exit(
                ERROR_CODES["INVALID_INPUT"],
                f"Missing value for flag: {flag}"
            )
        args[flag[2:]] = argv[i]
        encountered.add(flag)
        i += 1

    return args


def load_yaml(path: str, source_name: str) -> Any:
    """Load a YAML file, returning None for empty files."""
    path = Path(path)
    if not path.exists() or not path.is_file():
        if source_name == "defaults.yaml" or source_name == "base.yaml":
            raise MergeError(ERROR_CODES["MISSING_FILE"], f"Required file missing: {path}")
        else:
            return None

    try:
        content = path.read_text(encoding="utf-8").strip()
        if not content:
            return None
        data = yaml.safe_load(content)
        return data
    except yaml.YAMLError as e:
        raise MergeError(ERROR_CODES["PARSE_ERROR"], f"YAML parse error in {path}: {e}")


def load_json(path: str) -> Any:
    """Load a JSON file, returning None if missing or empty."""
    path = Path(path)
    if not path.exists() or not path.is_file():
        return None

    try:
        content = path.read_text(encoding="utf-8").strip()
        if not content:
            return None
        data = json.loads(content)
        return data
    except json.JSONDecodeError as e:
        raise MergeError(ERROR_CODES["PARSE_ERROR"], f"JSON parse error in {path}: {e}")


def load_env(path: str) -> dict:
    """Load env.list file as a dictionary of key-value pairs."""
    path = Path(path)
    env_vars = {}
    if path.exists() and path.is_file():
        try:
            content = path.read_text(encoding="utf-8")
            for line in content.splitlines():
                line = line.strip()
                if line and "=" in line and not line.startswith("#"):
                    key, value = line.split("=", 1)
                    key = key.strip()
                    value = value.strip()
                    if key:
                        env_vars[key] = value
        except Exception as e:
            raise MergeError(ERROR_CODES["INVALID_INPUT"], f"Error parsing env file: {e}")
    return env_vars


FRAGMENT_PLACEHOLDER_PATTERN = "{{fragment:{}}}"
FRAGMENT_NAME_PATTERN = re.compile(r'^[a-z][a-z0-9_]*$')


def load_fragments(fragments_dir: str) -> dict:
    """Load all YAML fragment files from the fragments directory."""
    fragments_path = Path(fragments_dir)
    if not fragments_path.exists() or not fragments_path.is_dir():
        return {}

    fragments = {}
    for yaml_file in fragments_path.glob("*.yaml"):
        fragment_name = yaml_file.stem
        if not re.match(r'^[a-z][a-z0-9_]*$', fragment_name):
            raise MergeError(ERROR_CODES["INVALID_TEMPLATE"], f"Invalid fragment name: {fragment_name}")
        content = yaml_file.read_text(encoding="utf-8").strip()
        if content:
            data = yaml.safe_load(content)
            fragments[fragment_name] = data
    return fragments


def load_fragment_metadata(fragments_dir: str) -> dict:
    """Load fragment deprecation metadata from fragments/meta.yaml."""
    meta_path = Path(fragments_dir) / "meta.yaml"
    if not meta_path.exists() or not meta_path.is_file():
        return {}

    content = meta_path.read_text(encoding="utf-8").strip()
    if not content:
        return {}

    try:
        data = yaml.safe_load(content)
        if not isinstance(data, dict):
            return {}
        result = {}
        for name, meta in data.items():
            if isinstance(meta, dict) and "deprecated_reason" in meta:
                result[name] = meta["deprecated_reason"]
        return result
    except yaml.YAMLError:
        return {}


def interpolate_environment(data: dict, env_vars: dict) -> dict:
    """Replace ${VAR} tokens in string values with environment variable values."""
    def _interpolate(obj, path=""):
        if isinstance(obj, str):
            pattern = r'\$\{([A-Za-z_][A-Za-z0-9_]*)\}'
            result = obj
            matches = list(re.finditer(pattern, obj))
            for match in matches:
                var_name = match.group(1)
                if var_name not in env_vars:
                    raise MergeError(ERROR_CODES["UNKNOWN_ENV_VAR"], f"{var_name} is not defined")
                result = result.replace(match.group(0), env_vars[var_name])
            return result
        elif isinstance(obj, dict):
            return {k: _interpolate(v, f"{path}/{k}" if path else f"/{k}") for k, v in obj.items()}
        elif isinstance(obj, list):
            return [_interpolate(item, f"{path}/{i}") for i, item in enumerate(obj)]
        return obj

    return _interpolate(data)


def deep_merge(base: Any, override: Any, path: str = "", warnings: list = None) -> Any:
    """Deep merge override into base. Records type conflicts as warnings."""
    if warnings is None:
        warnings = []

    if base is None:
        return override
    if override is None:
        return base

    if type(base) != type(override):
        if warnings is not None:
            warnings.append(path)
        return override

    if isinstance(base, dict) and isinstance(override, dict):
        result = base.copy()
        for key, value in override.items():
            new_path = f"{path}/{key}" if path else f"/{key}"
            if key in result:
                result[key] = deep_merge(result[key], value, new_path, warnings)
            else:
                result[key] = value
        return result

    if isinstance(base, list) and isinstance(override, list):
        return override

    return override


def get_source_name(source_key: str) -> str:
    """Get the source name for a given source key."""
    return SOURCE_NAMES.get(source_key, source_key)


def inject_defaults(merged: dict, defaults_data: dict, warnings: list) -> dict:
    """Inject missing keys from defaults.yaml without overriding existing keys."""
    if not isinstance(merged, dict) or not isinstance(defaults_data, dict):
        return merged

    result = merged.copy()

    for key, default_value in defaults_data.items():
        if key not in result:
            result[key] = default_value
        elif isinstance(default_value, dict) and isinstance(result[key], dict):
            result[key] = inject_defaults(result[key], default_value, warnings)

    return result


def canonicalize(obj: Any) -> Any:
    """Convert object to canonical form with sorted keys."""
    if isinstance(obj, dict):
        return {k: canonicalize(v) for k, v in sorted(obj.items())}
    elif isinstance(obj, list):
        return [canonicalize(item) for item in obj]
    else:
        return obj


def validate_spec(data: dict) -> list:
    """Validate the merged specification. Returns list of error messages."""
    errors = []
    if not isinstance(data, dict):
        errors.append("INVALID_STRUCTURE: Merged document must be an object")
        return errors

    if "experiment_name" not in data:
        errors.append("experiment_name is required")
    elif not isinstance(data["experiment_name"], str) or not data["experiment_name"].strip():
        errors.append("experiment_name must be a non-empty string")

    if "training" not in data:
        errors.append("training is required")
    elif not isinstance(data["training"], dict):
        errors.append("training must be an object")

    if "resources" not in data:
        errors.append("resources is required")
    elif not isinstance(data["resources"], dict):
        errors.append("resources must be an object")
    else:
        resources = data["resources"]
        has_accelerators = "accelerators" in resources and isinstance(resources["accelerators"], list)
        has_profile = "profile" in resources and isinstance(resources["profile"], str)
        if not has_accelerators and not has_profile:
            errors.append("resources must include either accelerators (array) or profile (string)")

        if "world_size" in resources:
            ws = resources["world_size"]
            if not isinstance(ws, int) or ws <= 0:
                errors.append("resources.world_size must be a positive integer")

        if "accelerators" in resources and resources["accelerators"] is not None:
            accels = resources["accelerators"]
            if not isinstance(accels, list):
                errors.append("resources.accelerators must be an array")
            else:
                for i, acc in enumerate(accels):
                    if not isinstance(acc, dict):
                        errors.append(f"resources.accelerators[{i}] must be an object")
                    else:
                        if "type" not in acc or not isinstance(acc["type"], str):
                            errors.append(f"resources.accelerators[{i}].type must be a string")
                        if "count" not in acc or not isinstance(acc["count"], int) or acc["count"] <= 0:
                            errors.append(f"resources.accelerators[{i}].count must be a positive integer")

    return errors


def filter_metadata(obj):
    """Filter out metadata keys (starting with _) from fragment content."""
    if isinstance(obj, dict):
        return {k: filter_metadata(v) for k, v in obj.items() if not k.startswith('_')}
    elif isinstance(obj, list):
        return [filter_metadata(item) for item in obj]
    return obj


def format_warning(warning, run_id: str = None) -> str:
    """Format a warning for output, including run_id for FRAGMENT_DEPRECATED."""
    if isinstance(warning, tuple):
        json_ptr = warning[0] if warning[0] != "" else "/"
        loser = get_source_name(warning[1]) if len(warning) > 1 else "unknown"
        winner = get_source_name(warning[2]) if len(warning) > 2 else "unknown"
        return f"CONFLICT {json_ptr} {loser} -> {winner}"
    elif isinstance(warning, str):
        if warning.startswith("FRAGMENT_DEPRECATED") and run_id:
            # Parse the fragment deprecation to include run_id
            parts = warning.split(" ", 2)
            if len(parts) >= 3:
                return f"{parts[0]} {run_id} {parts[2]}"
        return warning
    return str(warning)


def parse_manifest(manifest_path: str) -> list:
    """Parse the runs manifest CSV file. Returns list of run dicts."""
    path = Path(manifest_path)
    if not path.exists() or not path.is_file():
        raise ManifestError(ERROR_CODES["INVALID_MANIFEST"], f"Manifest file not found: {path}")

    runs = []
    manifest_dir = path.parent if path.parent else Path(".")

    with open(path, 'r', encoding='utf-8') as f:
        # Read header line
        first_line = f.readline().strip()
        if not first_line or not first_line.startswith('run_id'):
            raise ManifestError(ERROR_CODES["INVALID_MANIFEST"], "Missing required header column 'run_id'")

        # Parse header (RFC 4180)
        headers = []
        in_quote = False
        current = ""
        for char in first_line:
            if char == '"':
                if in_quote and current and current[-1] == '"':
                    current = current[:-1]
                    headers.append(current)
                    current = ""
                    in_quote = False
                else:
                    in_quote = not in_quote
            elif char == ',' and not in_quote:
                headers.append(current)
                current = ""
            else:
                current += char
        if current:
            headers.append(current)

        # Check for required columns
        if "run_id" not in headers:
            raise ManifestError(ERROR_CODES["INVALID_MANIFEST"], "Missing required header column 'run_id'")
        if "override_path" not in headers:
            raise ManifestError(ERROR_CODES["INVALID_MANIFEST"], "Missing required header column 'override_path'")

        # Read remaining lines
        for line_num, line in enumerate(f, start=2):
            line = line.strip()
            if not line:
                continue  # Skip empty lines
            if line.startswith('#'):
                continue  # Skip comment lines

            # Parse CSV line (simple implementation)
            values = []
            in_quote = False
            current = ""
            i = 0
            while i < len(line):
                char = line[i]
                if char == '"':
                    if in_quote and i + 1 < len(line) and line[i + 1] == '"':
                        current += '"'
                        i += 2
                    else:
                        in_quote = not in_quote
                        i += 1
                elif char == ',' and not in_quote:
                    values.append(current)
                    current = ""
                    i += 1
                else:
                    current += char
                    i += 1
            values.append(current)

            # Pad or truncate to match headers
            while len(values) < len(headers):
                values.append("")
            if len(values) > len(headers):
                values = values[:len(headers)]

            row = dict(zip(headers, values))

            # Clean up values (strip whitespace)
            row = {k: v.strip() for k, v in row.items()}

            # Resolve absolute paths for override_path and flags_path
            if row["override_path"]:
                op = Path(row["override_path"])
                if not op.is_absolute():
                    row["override_path"] = str(manifest_dir / op)

            if row.get("flags_path"):
                fp = Path(row["flags_path"])
                if not fp.is_absolute():
                    row["flags_path"] = str(manifest_dir / fp)

            runs.append(row)

    return runs


class RunProcessor:
    """Process a single run with layered configuration merging."""

    def __init__(self, run_id: str, run_data: dict, base_data: dict, overrides_dir: Path,
                 fragments: dict, deprecations: dict, global_flags: dict = None, env_vars: dict = None):
        self.run_id = run_id
        self.run_data = run_data
        self.base_data = base_data
        self.overrides_dir = overrides_dir
        self.fragments = fragments
        self.deprecations = deprecations
        self.global_flags = global_flags or {}
        self.env_vars = env_vars or {}
        self.warnings = []
        self.visited_deprecations = set()

    def validate_run_id(self) -> None:
        """Validate the run ID."""
        if not self.run_id:
            raise MergeError(ERROR_CODES["INVALID_MANIFEST"], "run_id cannot be empty")
        if not VALID_RUN_ID_PATTERN.match(self.run_id):
            raise MergeError(ERROR_CODES["INVALID_MANIFEST"],
                           f"run_id '{self.run_id}' contains invalid characters (only a-zA-Z0-9_- allowed)")

    def resolve_fragments(self, data: Any, detection_stack: list = None) -> Any:
        """Recursively resolve fragment placeholders in the data."""
        if detection_stack is None:
            detection_stack = []

        if isinstance(data, str):
            match = re.match(r'^\{\{fragment:([a-z][a-z0-9_]*)\}\}$', data)
            if match:
                fragment_name = match.group(1)

                if fragment_name in detection_stack:
                    cycle_path = " -> ".join(detection_stack[detection_stack.index(fragment_name):] + [fragment_name])
                    raise MergeError(ERROR_CODES["FRAGMENT_CYCLE"], f"Fragment cycle detected: {cycle_path}")

                if fragment_name not in self.fragments:
                    raise MergeError(ERROR_CODES["UNKNOWN_FRAGMENT"], f"Fragment '{fragment_name}' not found")

                if fragment_name in self.deprecations and fragment_name not in self.visited_deprecations:
                    self.visited_deprecations.add(fragment_name)
                    self.warnings.append(f"FRAGMENT_DEPRECATED {fragment_name} {self.deprecations[fragment_name]}")

                fragment_content = self.fragments[fragment_name]
                if fragment_content is None:
                    return None

                filtered_content = filter_metadata(fragment_content)
                new_detection_stack = detection_stack + [fragment_name]
                return self.resolve_fragments(filtered_content, new_detection_stack)
            return data
        elif isinstance(data, dict):
            result = {}
            for key, value in data.items():
                result[key] = self.resolve_fragments(value, detection_stack)
            return result
        elif isinstance(data, list):
            return [self.resolve_fragments(item, detection_stack) for item in data]
        return data

    def process(self) -> tuple:
        """Process the run and return (data, warnings, error_info)."""
        self.validate_run_id()

        try:
            # Start with resolved base
            merged = self.resolve_fragments(self.base_data.copy())

            # Load and merge defaults.yaml
            defaults_path = self.overrides_dir / "defaults.yaml"
            if defaults_path.exists() and defaults_path.is_file():
                defaults_data = load_yaml(str(defaults_path), "defaults.yaml")
                if defaults_data:
                    defaults_data = self.resolve_fragments(defaults_data)
                    temp_warnings = []
                    merged = deep_merge(merged, defaults_data, "", temp_warnings)
                    for wp in temp_warnings:
                        self.warnings.append((wp, "base", "defaults"))

            # Merge run-specific override
            override_path = self.run_data.get("override_path", "")
            if override_path:
                override_path_obj = Path(override_path)
                if not override_path_obj.exists() or not override_path_obj.is_file():
                    raise MergeError(ERROR_CODES["MISSING_FILE"], f"{override_path} not found")

                override_data = load_yaml(str(override_path), "override.yaml")
                if override_data:
                    override_data = self.resolve_fragments(override_data)
                    temp_warnings = []
                    merged = deep_merge(merged, override_data, "", temp_warnings)
                    for wp in temp_warnings:
                        self.warnings.append((wp, "defaults", "run_override"))

            # Merge run-specific flags if provided
            flags_path = self.run_data.get("flags_path")
            if flags_path:
                flags_path_obj = Path(flags_path)
                if not flags_path_obj.exists() or not flags_path_obj.is_file():
                    raise MergeError(ERROR_CODES["MISSING_FILE"], f"{flags_path} not found")

                flags_data = load_json(flags_path)
                if flags_data:
                    flags_data = self.resolve_fragments(flags_data)
                    temp_warnings = []
                    merged = deep_merge(merged, flags_data, "", temp_warnings)
                    for wp in temp_warnings:
                        self.warnings.append((wp, "run_override", "run_flags"))

            # Merge global flags
            if self.global_flags:
                temp_warnings = []
                merged = deep_merge(merged, self.global_flags, "", temp_warnings)
                for wp in temp_warnings:
                    self.warnings.append((wp, "run_override", "global_flags"))

            # Inject defaults for missing keys
            if defaults_path.exists() and defaults_path.is_file():
                defaults_data = load_yaml(str(defaults_path), "defaults.yaml")
                if defaults_data:
                    merged = inject_defaults(merged, defaults_data, self.warnings)

            # Environment interpolation
            merged = interpolate_environment(merged, self.env_vars)

            # Validate
            validation_errors = validate_spec(merged)
            if validation_errors:
                raise MergeError(ERROR_CODES["INVALID_VALUE"], "; ".join(validation_errors))

            return merged, self.warnings, None

        except MergeError as e:
            return None, [], {"run_id": self.run_id, "code": e.args[0], "message": e.args[1], "path": str(override_path if 'override_path' in dir() else "")}
        except Exception as e:
            return None, [], {"run_id": self.run_id, "code": ERROR_CODES["PARSE_ERROR"], "message": str(e), "path": ""}


def main() -> None:
    """Main entry point."""
    args = parse_args(sys.argv[1:])

    # Determine mode: single-run or manifest-based
    manifest_path = args.get("manifest")
    use_manifest = manifest_path is not None

    # Load shared resources
    fragments_dir = args["fragments"]
    fragments = load_fragments(fragments_dir)
    deprecations = load_fragment_metadata(fragments_dir)

    env_vars = load_env(args["env"])

    # Load global flags if present
    global_flags = load_json(args["flags"])

    # Load base.yaml (required)
    try:
        base_data = load_yaml(args["base"], "base.yaml")
        if base_data is None:
            if use_manifest:
                error_exit(ERROR_CODES["MISSING_FILE"], f"Required file missing: {args['base']}")
            else:
                raise MergeError(ERROR_CODES["MISSING_FILE"], f"Required file missing: {args['base']}")
    except MergeError as e:
        if use_manifest:
            raise
        else:
            error_exit(e.args[0], e.args[1])

    # Load overrides directory
    overrides_dir = Path(args["overrides"])
    if not overrides_dir.exists() or not overrides_dir.is_dir():
        error_exit(ERROR_CODES["MISSING_FILE"], f"Overrides directory missing: {args['overrides']}")

    defaults_path = overrides_dir / "defaults.yaml"
    if not defaults_path.exists() or not defaults_path.is_file():
        error_exit(ERROR_CODES["MISSING_FILE"], f"Required file missing: {defaults_path}")

    if use_manifest:
        # Manifest mode
        try:
            runs = parse_manifest(manifest_path)
        except ManifestError as e:
            error_exit(e.args[0], e.args[1])

        # Validate run IDs are unique
        seen_ids = set()
        for run_data in runs:
            run_id = run_data.get("run_id", "")
            if run_id in seen_ids:
                error_exit(ERROR_CODES["INVALID_MANIFEST"], f"Duplicate run_id: {run_id}")
            if not run_id or not VALID_RUN_ID_PATTERN.match(run_id):
                error_exit(ERROR_CODES["INVALID_MANIFEST"], f"Invalid run_id: {run_id}")
            seen_ids.add(run_id)

        # Ensure output directory exists
        out_path = Path(args["out"])
        out_path.mkdir(parents=True, exist_ok=True)
        out_subdir = out_path / "out"
        out_subdir.mkdir(parents=True, exist_ok=True)

        # Process each run
        summary = {
            "total_runs": len(runs),
            "succeeded": 0,
            "failed": 0,
            "runs": []
        }
        all_violations = []
        all_warnings = []
        run_warnings_count = {}  # Track warnings per run

        for run_data in runs:
            run_id = run_data["run_id"]
            processor = RunProcessor(
                run_id=run_id,
                run_data=run_data,
                base_data=base_data,
                overrides_dir=overrides_dir,
                fragments=fragments,
                deprecations=deprecations,
                global_flags=global_flags,
                env_vars=env_vars
            )

            result, warnings, violation = processor.process()

            if violation:
                # Run failed
                summary["failed"] += 1
                summary["runs"].append({
                    "run_id": run_id,
                    "status": "error",
                    "warnings": 0,
                    "conflicts": 0,
                    "notes": run_data.get("notes") or None
                })
                all_violations.append(violation)
                run_warnings_count[run_id] = {"warnings": 0, "conflicts": 0}
            else:
                # Run succeeded
                summary["succeeded"] += 1
                canonical = canonicalize(result)

                # Write per-run JSON spec
                spec_json = json.dumps(canonical, separators=(",", ":")) + "\n"
                (out_subdir / f"{run_id}.json").write_text(spec_json, encoding="utf-8")

                # Count warnings and conflicts
                warning_count = len(warnings)
                conflict_count = sum(1 for w in warnings if isinstance(w, tuple))

                summary["runs"].append({
                    "run_id": run_id,
                    "status": "ok",
                    "warnings": warning_count,
                    "conflicts": conflict_count,
                    "notes": run_data.get("notes") or None
                })

                # Store warnings for output
                for w in warnings:
                    all_warnings.append((run_id, w))

                run_warnings_count[run_id] = {"warnings": warning_count, "conflicts": conflict_count}

        # Write summary.json
        summary_content = json.dumps(summary, separators=(",", ":")) + "\n"
        (out_path / "summary.json").write_text(summary_content, encoding="utf-8")

        # Write warnings.txt (ordered by manifest row, then lexicographically by JSON pointer within a run)
        warning_lines = []
        for run_id, warning in all_warnings:
            warning_lines.append(format_warning(warning, run_id))

        # Sort: within each run, lexicographically by JSON pointer
        # Group by run first to maintain manifest order
        run_warnings = {}
        for run_id, warning in all_warnings:
            if run_id not in run_warnings:
                run_warnings[run_id] = []
            run_warnings[run_id].append(warning)

        # Sort warnings within each run
        for run_id in run_warnings:
            run_warnings[run_id].sort(key=lambda w: w[0] if isinstance(w, tuple) else "")

        # Build final warning lines in manifest order
        final_warning_lines = []
        for run_data in runs:
            run_id = run_data["run_id"]
            if run_id in run_warnings:
                for w in run_warnings[run_id]:
                    final_warning_lines.append(format_warning(w, run_id))

        warnings_content = "\n".join(final_warning_lines)
        if warnings_content:
            warnings_content += "\n"
        else:
            warnings_content = "\n"

        (out_path / "warnings.txt").write_text(warnings_content, encoding="utf-8")

        # Write violations.ndjson
        violations_content = "\n".join(json.dumps(v, separators=(",", ":")) for v in all_violations)
        if violations_content:
            violations_content += "\n"

        (out_path / "violations.ndjson").write_text(violations_content, encoding="utf-8")

        # Exit code: 0 if at least one run succeeded, 1 otherwise
        if summary["succeeded"] == 0:
            error_exit(ERROR_CODES["RUN_FAILURE"], "All runs failed")
        else:
            sys.exit(0)

    else:
        # Single-run mode (original behavior for backward compatibility)
        try:
            # Load run.yaml (optional)
            run_data = None
            run_path = overrides_dir / "run.yaml"
            if run_path.exists() and run_path.is_file():
                run_data = load_yaml(str(run_path), "run.yaml")

            all_warnings = []
            visited_deprecations = set()

            def resolve_with_fragments(data, detection_stack=None):
                if detection_stack is None:
                    detection_stack = []

                if isinstance(data, str):
                    match = re.match(r'^\{\{fragment:([a-z][a-z0-9_]*)\}\}$', data)
                    if match:
                        fragment_name = match.group(1)

                        if fragment_name in detection_stack:
                            cycle_path = " -> ".join(detection_stack[detection_stack.index(fragment_name):] + [fragment_name])
                            raise MergeError(ERROR_CODES["FRAGMENT_CYCLE"], f"Fragment cycle detected: {cycle_path}")

                        if fragment_name not in fragments:
                            raise MergeError(ERROR_CODES["UNKNOWN_FRAGMENT"], f"Fragment '{fragment_name}' not found")

                        if fragment_name in deprecations and fragment_name not in visited_deprecations:
                            visited_deprecations.add(fragment_name)
                            all_warnings.append(f"FRAGMENT_DEPRECATED {fragment_name} {deprecations[fragment_name]}")

                        fragment_content = fragments[fragment_name]
                        if fragment_content is None:
                            return None

                        filtered_content = filter_metadata(fragment_content)
                        new_detection_stack = detection_stack + [fragment_name]
                        return resolve_with_fragments(filtered_content, new_detection_stack)
                    return data
                elif isinstance(data, dict):
                    result = {}
                    for key, value in data.items():
                        result[key] = resolve_with_fragments(value, detection_stack)
                    return result
                elif isinstance(data, list):
                    return [resolve_with_fragments(item, detection_stack) for item in data]
                return data

            # Apply fragments to base data
            if base_data is not None:
                base_data = resolve_with_fragments(base_data)

            merged = base_data

            # Merge with defaults
            temp_warnings = []
            defaults_data = load_yaml(str(defaults_path), "defaults.yaml")
            if defaults_data:
                defaults_data = resolve_with_fragments(defaults_data)
                merged = deep_merge(merged, defaults_data, "", temp_warnings)
                for wp in temp_warnings:
                    all_warnings.append((wp, "base", "defaults"))

            # Merge with run.yaml if present
            if run_data is not None:
                run_data = resolve_with_fragments(run_data)
                temp_warnings = []
                merged = deep_merge(merged, run_data, "", temp_warnings)
                for wp in temp_warnings:
                    all_warnings.append((wp, "defaults", "run"))

            # Merge with flags.json if present
            if global_flags:
                flags_data = global_flags
                flags_data = resolve_with_fragments(flags_data)
                temp_warnings = []
                merged = deep_merge(merged, flags_data, "", temp_warnings)
                for wp in temp_warnings:
                    all_warnings.append((wp, "run", "flags"))

            # Inject defaults for missing keys
            if defaults_data:
                merged = inject_defaults(merged, defaults_data, all_warnings)

            # Step 2: Environment variable interpolation (post-merge)
            merged = interpolate_environment(merged, env_vars)

            # Validate (after interpolation)
            validation_errors = validate_spec(merged)
            if validation_errors:
                error_exit(
                    ERROR_CODES["MISSING_KEY"] if "required" in " ".join(validation_errors).lower()
                    else ERROR_CODES["INVALID_VALUE"],
                    "; ".join(validation_errors)
                )

            # Write outputs
            write_outputs(args["out"], merged, all_warnings)

            # Success - exit 0
            sys.exit(0)

        except MergeError as e:
            error_exit(e.args[0], e.args[1])


def write_outputs(out_dir: str, final_spec: dict, warnings: list) -> None:
    """Write final_spec.json and warnings.txt to output directory."""
    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    canonical_spec = canonicalize(final_spec)
    spec_json = json.dumps(canonical_spec, separators=(",", ":")) + "\n"
    (out_path / "final_spec.json").write_text(spec_json, encoding="utf-8")

    warning_lines = []
    for warning in warnings:
        if isinstance(warning, tuple):
            json_ptr = warning[0] if warning[0] != "" else "/"
            loser = get_source_name(warning[1]) if len(warning) > 1 else "unknown"
            winner = get_source_name(warning[2]) if len(warning) > 2 else "unknown"
            warning_lines.append(f"CONFLICT {json_ptr} {loser} -> {winner}")
        elif isinstance(warning, str):
            warning_lines.append(warning)

    warnings_content = "\n".join(warning_lines)
    if warnings_content:
        warnings_content += "\n"
    else:
        warnings_content = "\n"

    (out_path / "warnings.txt").write_text(warnings_content, encoding="utf-8")


if __name__ == "__main__":
    main()
