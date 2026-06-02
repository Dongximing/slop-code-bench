#!/usr/bin/env python3
"""
CLI launcher for pipeline execution.
Usage: python launch.py <pipeline> --workspace <dir> --output <dir> [--config <TOML config file>]
"""
import sys
import os
from pathlib import Path

def main():
    """Main entry point for the CLI."""
    # Parse arguments manually for now
    args = sys.argv[1:]

    if len(args) < 4:
        print("Usage: python launch.py <pipeline> --workspace <dir> --output <dir> [--config <TOML config file>]")
        sys.exit(2)

    # Find pipeline file
    pipeline_file = None
    workspace = None
    output = None
    config_file = None

    i = 0
    while i < len(args):
        if args[i] == '--workspace':
            workspace = args[i + 1] if i + 1 < len(args) else None
            i += 2
        elif args[i] == '--output':
            output = args[i + 1] if i + 1 < len(args) else None
            i += 2
        elif args[i] == '--config':
            config_file = args[i + 1] if i + 1 < len(args) else None
            i += 2
        elif not args[i].startswith('--'):
            pipeline_file = args[i]
            i += 1
        else:
            i += 1

    if not pipeline_file:
        print("Error: Pipeline file is required")
        sys.exit(2)

    if not workspace:
        print("Error: --workspace is required")
        sys.exit(2)

    if not output:
        print("Error: --output is required")
        sys.exit(2)

    # Convert to Path objects
    pipeline_path = Path(pipeline_file)
    workspace_path = Path(workspace)
    output_path = Path(output)

    # Validate paths
    if not pipeline_path.exists():
        print(f"Error: Pipeline file not found: {pipeline_file}")
        sys.exit(3)

    # Ensure output directory exists
    output_path.mkdir(parents=True, exist_ok=True)

    # Import modules after path validation to give early error feedback
    from pipeline_parser import parse_pipeline_file, ParseError
    from config_parser import parse_config_file, parse_entry_task
    from orchestrator import Orchestrator

    try:
        # Parse config if provided
        config = None
        entry_spec = None
        env = {}

        if config_file:
            try:
                config = parse_config_file(config_file)
                entry_spec = config.entry
                env = config.env
            except ParseError as e:
                print(f"SYNTAX_ERROR:{e.message}", file=sys.stderr)
                sys.exit(2)

        # Parse pipeline file
        try:
            with open(pipeline_path, 'r') as f:
                pipeline_content = f.read()

            parsed_tasks = parse_pipeline_file(pipeline_content)
        except ParseError as e:
            print(f"{e}", file=sys.stderr)
            sys.exit(2)
        except Exception as e:
            print(f"SYNTAX_ERROR:Error parsing pipeline file: {e}", file=sys.stderr)
            sys.exit(2)

        # Validate tasks
        task_names = {t.name for t in parsed_tasks}

        # Check for circular dependencies (simplified - just check basic validity)
        # TODO: Implement full circular dependency check

        # Create orchestrator
        orchestrator = Orchestrator(workspace_path, output_path, config, env)

        # Register tasks
        orchestrator.register_tasks(parsed_tasks)

        # Execute pipeline
        exit_code = orchestrator.execute(entry_spec)

        sys.exit(exit_code)

    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
