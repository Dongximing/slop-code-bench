#!/usr/bin/env python3
"""
Pipeline CLI Tool - Launch pipeline execution from command line.

Usage:
    python launch.py <pipeline> --workspace <dir> --output <dir> [--config <TOML config file>]
"""

import os
import sys
import argparse

# Add parent directory to path to import pipeline module
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pipeline.parser import Parser, ParseError
from pipeline.config import parse_config, parse_entry_call, ConfigParseError, Config
from pipeline.executor import TaskExecutor


def main():
    parser = argparse.ArgumentParser(description='Execute pipeline from a pipeline file')
    parser.add_argument('pipeline', help='Path to the pipeline file (.pipe)')
    parser.add_argument('--workspace', required=True, help='Working directory')
    parser.add_argument('--output', required=True, help='Output directory for results')
    parser.add_argument('--config', help='Optional TOML configuration file')

    args = parser.parse_args()

    # Validate workspace exists
    if not os.path.isdir(args.workspace):
        print(f"INVALID_PIPE: Workspace directory does not exist: {args.workspace}", file=sys.stderr)
        sys.exit(3)

    # Validate pipeline file exists
    if not os.path.isfile(args.pipeline):
        print(f"SYNTAX_ERROR: Pipeline file not found: {args.pipeline}", file=sys.stderr)
        sys.exit(2)

    # Parse config if provided
    config = None
    if args.config:
        try:
            config = parse_config(args.config)
        except ConfigParseError as e:
            print(str(e), file=sys.stderr)
            sys.exit(2)
    else:
        config = Config()

    # Parse pipeline file
    try:
        with open(args.pipeline, 'r') as f:
            source = f.read()
        pipeline_parser = Parser(source)
        pipeline = pipeline_parser.parse()
    except ParseError as e:
        print(str(e), file=sys.stderr)
        sys.exit(2)
    except SyntaxError as e:
        print(f"SYNTAX_ERROR: {e}", file=sys.stderr)
        sys.exit(2)
    except Exception as e:
        print(f"SYNTAX_ERROR: {e}", file=sys.stderr)
        sys.exit(2)

    # Determine entry point
    entry_name = config.entry
    positional_args = []
    named_args = {}

    if entry_name:
        try:
            entry_name, positional_args, named_args = parse_entry_call(entry_name)
        except ConfigParseError as e:
            print(str(e), file=sys.stderr)
            sys.exit(2)
    else:
        # Use 'main' as default entry point
        entry_name = 'main'

    # Validate entry task exists
    if entry_name not in pipeline.tasks:
        print(f"INVALID_PIPE: Entry task '{entry_name}' not defined", file=sys.stderr)
        sys.exit(3)

    # Create executor
    try:
        executor = TaskExecutor(
            pipeline=pipeline,
            workspace=args.workspace,
            output_dir=args.output,
            env_vars=config.env or {},
            clean_cwd=config.clean_cwd
        )
    except Exception as e:
        print(f"INVALID_PIPE: {e}", file=sys.stderr)
        sys.exit(3)

    # Execute entry task
    entry_task = pipeline.tasks[entry_name]

    try:
        result = executor.execute_task(entry_task, positional_args, named_args)
    except ValueError as e:
        error_msg = str(e)
        if 'SYNTAX_ERROR' in error_msg:
            print(error_msg, file=sys.stderr)
            sys.exit(2)
        elif 'INVALID_PIPE' in error_msg:
            print(error_msg, file=sys.stderr)
            sys.exit(3)
        else:
            print(f"INVALID_PIPE: {e}", file=sys.stderr)
            sys.exit(3)
    except Exception as e:
        print(f"INVALID_PIPE: {e}", file=sys.stderr)
        sys.exit(3)

    # Write results
    executor.write_results()

    # Exit with appropriate code
    if result.exit_code != 0:
        sys.exit(1)
    elif result.success and not all(result.success.values()):
        sys.exit(1)
    else:
        sys.exit(0)


if __name__ == '__main__':
    main()
