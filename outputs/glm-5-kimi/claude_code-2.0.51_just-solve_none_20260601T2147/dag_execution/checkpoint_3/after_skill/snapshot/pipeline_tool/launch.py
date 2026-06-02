#!/usr/bin/env python3
import argparse
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from executor import PipelineExecutor


def main():
    parser = argparse.ArgumentParser(description='Execute pipeline from file')
    parser.add_argument('pipeline', help='Path to the pipeline file')
    parser.add_argument('--workspace', required=True, help='Working directory')
    parser.add_argument('--output', required=True, help='Output directory')
    parser.add_argument('--config', help='Path to TOML config file', default=None)

    args = parser.parse_args()

    executor = PipelineExecutor(
        pipeline_path=args.pipeline,
        workspace=args.workspace,
        output_dir=args.output,
        config_path=args.config
    )

    exit_code = executor.run()
    sys.exit(exit_code)


if __name__ == '__main__':
    main()
