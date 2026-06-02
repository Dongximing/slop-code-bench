#!/usr/bin/env python3

import argparse
import csv
import json
import re
import sys
from pathlib import Path

import yaml

ERROR_MISSING_FILE = "MISSING_FILE"
ERROR_PARSE = "PARSE_ERROR"
ERROR_INVALID_STRUCTURE = "INVALID_STRUCTURE"
ERROR_MISSING_KEY = "MISSING_KEY"
ERROR_INVALID_VALUE = "INVALID_VALUE"
ERROR_INVALID_INPUT = "INVALID_INPUT"
ERROR_UNKNOWN_FRAGMENT = "UNKNOWN_FRAGMENT"
ERROR_FRAGMENT_CYCLE = "FRAGMENT_CYCLE"
ERROR_INVALID_TEMPLATE = "INVALID_TEMPLATE"
ERROR_UNKNOWN_ENV_VAR = "UNKNOWN_ENV_VAR"
ERROR_INVALID_MANIFEST = "INVALID_MANIFEST"
ERROR_RUN_FAILURE = "RUN_FAILURE"

LAYER_BASE = "base.yaml"
LAYER_DEFAULTS = "defaults.yaml"
LAYER_RUN = "run.yaml"
LAYER_FLAGS = "flags.json"
LAYER_GLOBAL_FLAGS = "global_flags.json"


def error_exit(code: str, message: str):
    print(json.dumps({"error": {"code": code, "message": message}}), file=sys.stderr)
    sys.exit(1)


def _mk_err(run_id, code, message, path, notes=None):
    r = {"run_id": run_id, "code": code, "message": message, "path": path}
    if notes is not None:
        r["notes"] = notes
    return r


def _mk_load_err(path, run_id, err_code, err_msg, notes=None):
    return _mk_err(run_id, err_code, err_msg, str(path), notes)


def parse_env_file(path: Path) -> dict:
    env = {}
    if not path.exists():
        return env
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n")
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            env[key.strip()] = value.strip()
    return env


def parse_yaml_file(path: Path) -> tuple:
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        if not content.strip():
            return None, None, None
        data = yaml.load(content, yaml.SafeLoader)
        return data, None, None
    except yaml.YAMLError as e:
        return None, ERROR_PARSE, f"Failed to parse {path}: {e}"
    except OSError as e:
        return None, ERROR_MISSING_FILE, f"Cannot read {path}: {e}"


def load_fragments(fragments_dir: Path) -> tuple:
    fragments = {}
    if not fragments_dir.exists():
        return fragments, None, None
    for frag_path in fragments_dir.glob("*.yaml"):
        if frag_path.name == "meta.yaml":
            continue
        frag_name = frag_path.stem
        if not re.match(r'^[a-z0-9_]+$', frag_name):
            return None, ERROR_INVALID_TEMPLATE, f"Invalid fragment name: {frag_name}"
        data, err_code, err_msg = parse_yaml_file(frag_path)
        if err_code:
            return None, err_code, err_msg
        fragments[frag_name] = data if data is not None else {}
    return fragments, None, None


def load_fragment_meta(fragments_dir: Path) -> tuple:
    if not fragments_dir.exists():
        return None, None, None
    meta_path = fragments_dir / "meta.yaml"
    if not meta_path.exists():
        return {}, None, None
    return parse_yaml_file(meta_path)


# Fragment placeholder pattern
FRAGMENT_PATTERN = re.compile(r'^\{\{fragment:([a-z0-9_]+)\}\}$')


def is_fragment_placeholder(value) -> bool:
    """Check if a value is a fragment placeholder."""
    if not isinstance(value, str):
        return False
    return bool(FRAGMENT_PATTERN.match(value))


def extract_fragment_name(value: str) -> str:
    """Extract fragment name from placeholder. Assumes is_fragment_placeholder(value) is True."""
    return FRAGMENT_PATTERN.match(value).group(1)


def resolve_fragments(doc: dict, fragments: dict, warnings: list, meta: dict, run_id: str = None,
                      resolution_stack: list = None) -> tuple:
    """
    Recursively resolve {{fragment:name}} placeholders in the document.
    Returns (resolved_doc, err_code, err_msg).
    """
    if resolution_stack is None:
        resolution_stack = []

    deprecated_warned = set()

    def _resolve(val) -> tuple:
        if not isinstance(val, str) or not is_fragment_placeholder(val):
            if isinstance(val, dict):
                result = {}
                for k, v in val.items():
                    resolved, err_code, err_msg = _resolve(v)
                    if err_code:
                        return None, err_code, err_msg
                    result[k] = resolved
                return result, None, None

            if isinstance(val, list):
                result = []
                for item in val:
                    resolved, err_code, err_msg = _resolve(item)
                    if err_code:
                        return None, err_code, err_msg
                    result.append(resolved)
                return result, None, None
            return val, None, None

        frag_name = extract_fragment_name(val)

        if frag_name in resolution_stack:
            cycle = " -> ".join(resolution_stack[resolution_stack.index(frag_name):] + [frag_name])
            return None, ERROR_FRAGMENT_CYCLE, f"Fragment cycle detected: {cycle}"

        if frag_name not in fragments:
            return None, ERROR_UNKNOWN_FRAGMENT, f"Fragment '{frag_name}' not found"

        frag_content = fragments[frag_name]

        if frag_name in meta and "deprecated_reason" in meta[frag_name]:
            if frag_name not in deprecated_warned:
                prefix = f"FRAGMENT_DEPRECATED {run_id} " if run_id else "FRAGMENT_DEPRECATED "
                warnings.append(f"{prefix}{frag_name} {meta[frag_name]['deprecated_reason']}")
                deprecated_warned.add(frag_name)

        if frag_content is None:
            frag_content = {}

        if isinstance(frag_content, dict) and "_meta" in frag_content:
            frag_content = {k: v for k, v in frag_content.items() if k != "_meta"}

        resolved, err_code, err_msg = _resolve(frag_content)
        if err_code:
            return None, err_code, err_msg
        return resolved, None, None

    resolved, err_code, err_msg = _resolve(doc)
    return resolved, err_code, err_msg


def parse_json_file(path: Path) -> tuple:
    """Parse a JSON file, returning (data, error_code, error_message)."""
    try:
        if not path.exists() or path.stat().st_size == 0:
            return None, None, None
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return None, ERROR_PARSE, f"{path} must contain a JSON object, got {type(data).__name__}"
        return data, None, None
    except json.JSONDecodeError as e:
        return None, ERROR_PARSE, f"Failed to parse {path}: {e}"
    except OSError as e:
        return None, ERROR_MISSING_FILE, f"Cannot read {path}: {e}"


# Environment variable pattern: ${VAR_NAME}
ENV_PATTERN = re.compile(r'\$\{([a-zA-Z_][a-zA-Z0-9_]*)\}')


def interpolate_env_vars(obj: dict, env: dict, path: str = "") -> tuple:
    if isinstance(obj, dict):
        result = {}
        for key, val in obj.items():
            new_path = f"{path}/{key}" if path else f"/{key}"
            resolved, err_code, err_msg = interpolate_env_vars(val, env, new_path)
            if err_code:
                return None, err_code, err_msg
            result[key] = resolved
        return result, None, None
    if isinstance(obj, list):
        result = []
        for i, item in enumerate(obj):
            new_path = f"{path}/{i}" if path else f"/{i}"
            resolved, err_code, err_msg = interpolate_env_vars(item, env, new_path)
            if err_code:
                return None, err_code, err_msg
            result.append(resolved)
        return result, None, None
    if isinstance(obj, str):
        matches = list(ENV_PATTERN.finditer(obj))
        if not matches:
            return obj, None, None
        result = obj
        for match in matches:
            var_name = match.group(1)
            if var_name not in env:
                return None, ERROR_UNKNOWN_ENV_VAR, f"{var_name} is not defined"
            result = result[:match.start()] + env[var_name] + result[match.end():]
        return result, None, None
    return obj, None, None


def _merge_layers_with_sources(layers: list, warnings: list, path: str = "", run_id: str = None) -> tuple:
    """Deep merge layers tracking source of each value."""
    result = {}
    sources = {}
    for layer_name, layer_dict in layers:
        if not layer_dict:
            continue
        for key, val in layer_dict.items():
            current_path = f"{path}/{key}" if path else f"/{key}"
            if key not in result:
                result[key] = val
                sources[key] = layer_name
                continue

            base = result[key]
            if type(base) is not type(val):
                prefix = f"CONFLICT {run_id} " if run_id else "CONFLICT "
                warnings.append(f"{prefix}{current_path} {sources[key]} -> {layer_name}")

            if isinstance(base, dict) and isinstance(val, dict):
                remaining = [(lname, ldict.get(key, {})) for lname, ldict in layers]
                result[key], _ = _merge_layers_with_sources(remaining, warnings, current_path, run_id)
            else:
                result[key] = val
            sources[key] = layer_name
    return result, sources


def merge_layers(layers: list, warnings: list, path: str = "", run_id: str = None) -> dict:
    """Deep merge layers. Layers is [(name, dict), ...] lowest to highest precedence."""
    result, _ = _merge_layers_with_sources(layers, warnings, path, run_id)
    return result


def validate_document(doc: dict) -> tuple:
    if not isinstance(doc, dict):
        return False, ERROR_INVALID_STRUCTURE, "Merged document is not an object"

    for key, typ, msg in [
        ("experiment_name", str, "experiment_name must be a non-empty string with visible characters"),
        ("training", dict, "training must be an object"),
        ("resources", dict, "resources must be an object"),
    ]:
        if key not in doc or not isinstance(doc[key], typ) or (key == "experiment_name" and not doc[key].strip()):
            return False, ERROR_MISSING_KEY if key not in doc else ERROR_INVALID_VALUE, f"{key} is required" if key not in doc else msg

    resources = doc["resources"]

    if "world_size" in resources:
        if not isinstance(resources["world_size"], int) or resources["world_size"] <= 0:
            return False, ERROR_INVALID_VALUE, "resources.world_size must be a positive integer"

    if "accelerators" in resources:
        acc = resources["accelerators"]
        if not isinstance(acc, list):
            return False, ERROR_INVALID_VALUE, "resources.accelerators must be an array"
        for i, item in enumerate(acc):
            if not isinstance(item, dict):
                return False, ERROR_INVALID_VALUE, f"resources.accelerators[{i}] must be an object"
            if "type" not in item or not isinstance(item["type"], str):
                return False, ERROR_INVALID_VALUE, f"resources.accelerators[{i}].type must be a string"
            if "count" not in item or not isinstance(item["count"], int) or item["count"] <= 0:
                return False, ERROR_INVALID_VALUE, f"resources.accelerators[{i}].count must be a positive integer"

    if not ("accelerators" in resources or "profile" in resources):
        return False, ERROR_MISSING_KEY, "resources must include either accelerators or profile"

    return True, None, None


def sort_json(obj):
    if isinstance(obj, dict):
        return {k: sort_json(v) for k, v in sorted(obj.items())}
    if isinstance(obj, list):
        return [sort_json(item) for item in obj]
    return obj


def parse_manifest(manifest_path: Path, manifest_dir: Path) -> tuple:
    """
    Parse manifest CSV file. Returns list of run entries or error.
    """
    try:
        with open(manifest_path, 'r', encoding='utf-8') as f:
            content = f.read()
    except OSError as e:
        return None, ERROR_MISSING_FILE, f"Cannot read manifest: {e}"

    # Remove BOM if present
    if content.startswith('\ufeff'):
        content = content[1:]

    lines = content.splitlines()
    # Skip empty lines and comment lines
    data_lines = [line for line in lines if line.strip() and not line.strip().startswith('#')]

    if not data_lines:
        return None, ERROR_INVALID_MANIFEST, "Manifest has no data rows"

    # Parse CSV
    reader = csv.DictReader(data_lines)
    if reader.fieldnames is None:
        return None, ERROR_INVALID_MANIFEST, "Manifest has no header row"

    # Check for required columns
    required_cols = {'run_id', 'override_path'}
    actual_cols = set(reader.fieldnames)
    if not required_cols.issubset(actual_cols):
        missing = required_cols - actual_cols
        return None, ERROR_INVALID_MANIFEST, f"Missing required columns: {', '.join(missing)}"

    runs = []
    seen_ids = set()
    valid_id_pattern = re.compile(r'^[a-zA-Z0-9_-]+$')

    for row_num, row in enumerate(reader, start=2):  # start=2 because header is line 1
        run_id = row.get('run_id', '').strip()

        # Validate run_id
        if not run_id:
            return None, ERROR_INVALID_MANIFEST, f"Empty run_id in row {row_num}"
        if run_id in seen_ids:
            return None, ERROR_INVALID_MANIFEST, f"Duplicate run_id: {run_id}"
        if not valid_id_pattern.match(run_id):
            return None, ERROR_INVALID_MANIFEST, f"Invalid run_id characters: {run_id}"

        seen_ids.add(run_id)

        override_path = row.get('override_path', '').strip()
        if not override_path:
            return None, ERROR_INVALID_MANIFEST, f"Empty override_path for run_id {run_id}"

        flags_path = row.get('flags_path', '').strip() or None
        notes = row.get('notes', '').strip() or None

        runs.append({
            'run_id': run_id,
            'override_path': override_path,
            'flags_path': flags_path,
            'notes': notes,
            'row_num': row_num
        })

    return runs, None, None


def resolve_path(path_str: str, base_dir: Path) -> Path:
    """Resolve a path relative to base_dir if it's relative."""
    path = Path(path_str)
    if path.is_absolute():
        return path
    return base_dir / path


def process_run(run_entry: dict, base_path: Path, overrides_dir: Path, fragments_dir: Path,
                global_flags_data: dict, env: dict, manifest_dir: Path) -> dict:
    run_id = run_entry['run_id']
    override_path_str = run_entry['override_path']
    flags_path_str = run_entry['flags_path']
    notes = run_entry['notes']
    warnings = []

    def mk_viol(code, msg, path=None):
        return {'run_id': run_id, 'code': code, 'message': msg, 'path': str(path or run_id)}

    def ok(res):
        return {'run_id': run_id, 'status': 'ok', 'doc': res, 'warnings': warnings, 'notes': notes}

    def err(code, msg, path=None):
        return {'run_id': run_id, 'status': 'error', 'violation': mk_viol(code, msg, path), 'notes': notes}

    # Load base.yaml
    if not base_path.exists():
        return err(ERROR_MISSING_FILE, f"Base file not found: {base_path}", base_path)
    base_data, e, m = parse_yaml_file(base_path)
    if e:
        return err(e, m, base_path)
    if base_data is None or not isinstance(base_data, dict):
        return err(ERROR_PARSE, f"base.yaml must be a YAML object, got {type(base_data).__name__}", base_path)

    # Load defaults.yaml
    defaults_path = overrides_dir / "defaults.yaml"
    if not defaults_path.exists():
        return err(ERROR_MISSING_FILE, f"Defaults file not found: {defaults_path}", defaults_path)
    defaults_data, e, m = parse_yaml_file(defaults_path)
    if e:
        return err(e, m, defaults_path)
    if defaults_data is None or not isinstance(defaults_data, dict):
        return err(ERROR_PARSE, f"defaults.yaml must be a YAML object, got {type(defaults_data).__name__}", defaults_path)

    # Load run.yaml (optional)
    run_path = overrides_dir / "run.yaml"
    run_data = None
    if run_path.exists():
        run_data, e, m = parse_yaml_file(run_path)
        if e:
            return err(e, m, run_path)
        if not isinstance(run_data, dict):
            return err(ERROR_PARSE, f"run.yaml must be a YAML object, got {type(run_data).__name__}", run_path)

    # Load per-run override file
    override_path = resolve_path(override_path_str, manifest_dir)
    if not override_path.exists():
        return err(ERROR_MISSING_FILE, f"{override_path_str} not found", override_path_str)
    override_data, e, m = parse_yaml_file(override_path)
    if e:
        return err(e, m, override_path)
    if override_data is None or not isinstance(override_data, dict):
        return err(ERROR_PARSE, f"Override file must be a YAML object, got {type(override_data).__name__}", override_path)

    # 5. Load per-run flags file (optional)
    flags_data = {}
    if flags_path_str:
        flags_path = resolve_path(flags_path_str, manifest_dir)
        if flags_path.exists():
            flags_data, err_code, err_msg = parse_json_file(flags_path)
            if err_code:
                return {
                    'run_id': run_id,
                    'status': 'error',
                    'violation': {
                        'run_id': run_id,
                        'code': err_code,
                        'message': err_msg,
                        'path': str(flags_path)
                    },
                    'notes': notes
                }
            if flags_data is None:
                flags_data = {}
            if not isinstance(flags_data, dict):
                return {
                    'run_id': run_id,
                    'status': 'error',
                    'violation': {
                        'run_id': run_id,
                        'code': ERROR_PARSE,
                        'message': f"Flags file must be a JSON object, got {type(flags_data).__name__}",
                        'path': str(flags_path)
                    },
                    'notes': notes
                }

    # 6. Load fragments
    fragments, err_code, err_msg = load_fragments(fragments_dir)
    if err_code:
        return {
            'run_id': run_id,
            'status': 'error',
            'violation': {
                'run_id': run_id,
                'code': err_code,
                'message': err_msg,
                'path': str(fragments_dir)
            },
            'notes': notes
        }

    # 7. Load fragment meta
    fragment_meta = {}
    fragment_meta_data, err_code, err_msg = load_fragment_meta(fragments_dir)
    if err_code:
        return {'run_id': run_id, 'status': 'error', 'violation': _mk_load_err(fragments_dir / "meta.yaml", run_id, err_code, err_msg, notes)}
    if fragment_meta_data is not None:
        fragment_meta = fragment_meta_data

    # 8. Merge layers in order: base -> defaults -> run -> override -> flags -> global_flags
    layers = [
        (LAYER_BASE, base_data if base_data else {}),
        (LAYER_DEFAULTS, defaults_data if defaults_data else {}),
    ]
    if run_data is not None:
        layers.append((LAYER_RUN, run_data if run_data else {}))
    layers.append((str(override_path), override_data if override_data else {}))
    layers.append((str(flags_path) if flags_path_str else "flags.json", flags_data if flags_data else {}))
    if global_flags_data:
        layers.append((LAYER_GLOBAL_FLAGS, global_flags_data))

    merged = merge_layers(layers, warnings, run_id=run_id)

    # 9. Inject defaults for missing required sections from defaults.yaml
    for section in ["experiment_name", "training", "resources"]:
        if section not in merged or merged[section] is None:
            if section in defaults_data and defaults_data[section] is not None:
                merged[section] = defaults_data[section]

    # 10. Resolve fragment placeholders
    resolved, err_code, err_msg = resolve_fragments(merged, fragments, warnings, fragment_meta, run_id=run_id)
    if err_code:
        return {
            'run_id': run_id,
            'status': 'error',
            'violation': {
                'run_id': run_id,
                'code': err_code,
                'message': err_msg,
                'path': '<merge>'
            },
            'notes': notes,
            'warnings': warnings
        }
    merged = resolved

    # 11. Interpolate environment variables
    interpolated, err_code, err_msg = interpolate_env_vars(merged, env)
    if err_code:
        return {
            'run_id': run_id,
            'status': 'error',
            'violation': {
                'run_id': run_id,
                'code': err_code,
                'message': err_msg,
                'path': '<interpolate>'
            },
            'notes': notes,
            'warnings': warnings
        }
    merged = interpolated

    # 12. Validate (after interpolation)
    is_valid, err_code, err_msg = validate_document(merged)
    if not is_valid:
        return {
            'run_id': run_id,
            'status': 'error',
            'violation': {
                'run_id': run_id,
                'code': err_code,
                'message': err_msg,
                'path': '<validate>'
            },
            'notes': notes,
            'warnings': warnings
        }

    return {
        'run_id': run_id,
        'status': 'ok',
        'doc': merged,
        'warnings': warnings,
        'notes': notes
    }


def parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="synth_configs",
        description="Merge layered YAML/JSON configs into canonical experiment spec with manifest support.",
        allow_abbrev=False
    )
    parser.add_argument("--base", required=True, help="Path to base.yaml")
    parser.add_argument("--overrides", required=True, help="Path to overrides directory")
    parser.add_argument("--fragments", required=True, help="Path to fragments directory")
    parser.add_argument("--manifest", required=True, help="Path to runs_manifest.csv")
    parser.add_argument("--flags", required=False, default="", help="Path to global flags.json (optional)")
    parser.add_argument("--env", required=True, help="Path to env.list")
    parser.add_argument("--out", required=True, help="Path to output directory")

    try:
        return parser.parse_args(argv)
    except SystemExit:
        error_exit(ERROR_INVALID_INPUT, "Invalid CLI invocation")


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]

    args = parse_args(argv)

    base_path = Path(args.base)
    overrides_dir = Path(args.overrides)
    fragments_dir = Path(args.fragments)
    manifest_path = Path(args.manifest)
    flags_path = Path(args.flags) if args.flags else None
    env_path = Path(args.env)
    out_dir = Path(args.out)

    # Validate output directory exists
    if not out_dir.exists() or not out_dir.is_dir():
        error_exit(ERROR_INVALID_INPUT, f"Output directory does not exist: {out_dir}")

    # 1. Parse env file
    env = parse_env_file(env_path)

    # 2. Load global flags (optional)
    global_flags_data = {}
    if flags_path and flags_path.exists():
        global_flags_data, err_code, err_msg = parse_json_file(flags_path)
        if err_code:
            error_exit(err_code, err_msg)
        if global_flags_data is None:
            global_flags_data = {}
        if not isinstance(global_flags_data, dict):
            error_exit(ERROR_PARSE, f"Global flags must be a JSON object, got {type(global_flags_data).__name__}")

    # 3. Parse manifest
    manifest_dir = manifest_path.parent if manifest_path.parent else Path('.')
    runs, err_code, err_msg = parse_manifest(manifest_path, manifest_dir)
    if err_code:
        error_exit(ERROR_INVALID_MANIFEST, err_msg)

    # 4. Create output subdirectory
    out_subdir = out_dir / "out"
    out_subdir.mkdir(parents=True, exist_ok=True)

    # 5. Process each run
    run_results = []
    all_warnings = []  # For warnings.txt
    violations = []  # For violations.ndjson
    succeeded_count = 0
    failed_count = 0

    for run_entry in runs:
        result = process_run(
            run_entry,
            base_path=base_path,
            overrides_dir=overrides_dir,
            fragments_dir=fragments_dir,
            global_flags_data=global_flags_data,
            env=env,
            manifest_dir=manifest_dir
        )

        run_id = result['run_id']
        status = result['status']

        if status == 'ok':
            # Write per-run spec
            doc = sort_json(result['doc'])
            spec_path = out_subdir / f"{run_id}.json"
            with open(spec_path, 'w', encoding='utf-8') as f:
                json.dump(doc, f, separators=(',', ':'))
                f.write('\n')

            # Collect warnings
            if result.get('warnings'):
                all_warnings.extend(result['warnings'])

            succeeded_count += 1
        else:
            # Add to violations
            violations.append(result['violation'])
            failed_count += 1

        # Build run summary entry
        run_summary = {
            'run_id': run_id,
            'status': status,
            'warnings': len(result.get('warnings', [])),
            'conflicts': sum(1 for w in result.get('warnings', []) if w.startswith('CONFLICT ')),
            'notes': result.get('notes')
        }
        run_results.append(run_summary)

    # 6. Write summary.json
    summary = {
        'total_runs': len(runs),
        'succeeded': succeeded_count,
        'failed': failed_count,
        'runs': run_results
    }
    with open(out_dir / "summary.json", 'w', encoding='utf-8') as f:
        json.dump(summary, f, indent=2)

    # 7. Write warnings.txt (sorted by run_id, then by JSON pointer)
    # Group warnings by run_id for sorting
    if all_warnings:
        # Sort warnings: by run_id first, then lexicographically
        all_warnings.sort(key=lambda w: (w.split(' ', 1)[1] if ' ' in w and w.split(' ', 1)[0] in ('CONFLICT', 'FRAGMENT_DEPRECATED') else w, w))
        with open(out_dir / "warnings.txt", 'w', encoding='utf-8') as f:
            for w in all_warnings:
                f.write(w + '\n')
    else:
        # Write empty file
        with open(out_dir / "warnings.txt", 'w', encoding='utf-8') as f:
            pass

    # 8. Write violations.ndjson
    with open(out_dir / "violations.ndjson", 'w', encoding='utf-8') as f:
        for v in violations:
            f.write(json.dumps(v) + '\n')

    # 9. Determine exit code
    # Exit 0 if at least one run succeeds and all required outputs exist
    # Exit 1 if manifest parsing fails or if every run fails
    if succeeded_count > 0:
        sys.exit(0)
    else:
        # All runs failed or no runs at all
        if len(runs) == 0:
            # This is actually an INVALID_MANIFEST situation
            error_exit(ERROR_INVALID_MANIFEST, "No valid runs in manifest")
        else:
            # All runs failed after manifest parsing succeeded
            sys.exit(1)


if __name__ == "__main__":
    main()
