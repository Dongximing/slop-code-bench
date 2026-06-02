#!/usr/bin/env python3
"""Dynamic Buffer - Multi-language code generator for streaming data processors.

This is a thin orchestrator that delegates to specialized modules:
- parsers.py: File parsing utilities
- inference.py: Transformation inference from sample data
- generators/: Language-specific code generators
"""

import argparse
from pathlib import Path
from typing import Tuple

from parsers import parse_file, get_file_extension
from inference import TransformationInferrer
from generators import (
    PythonCodeGenerator,
    JavaScriptCodeGenerator,
    CppCodeGenerator,
    RustCodeGenerator,
)


def find_sample_files(sample_dir: str) -> Tuple[str, str]:
    """Find input and output files in sample directory."""
    sample_path = Path(sample_dir)
    for ext in ['csv', 'tsv', 'jsonl', 'json']:
        potential_input = sample_path / ('input.' + ext)
        potential_output = sample_path / ('output.' + ext)
        if potential_input.exists() and potential_output.exists():
            return str(potential_input), str(potential_output)
    raise ValueError("Could not find matching input/output pair in " + sample_dir)


def main():
    parser = argparse.ArgumentParser(description='Dynamic Buffer Code Generator')
    parser.add_argument('module_name', help='Name of the generated module')
    parser.add_argument('--output', required=True, help='Output directory')
    parser.add_argument('--sample', required=True, help='Sample directory containing input/output files')
    parser.add_argument('--python', action='store_true', help='Generate Python module')
    parser.add_argument('--javascript', action='store_true', help='Generate JavaScript module')
    parser.add_argument('--cpp', action='store_true', help='Generate C++ module')
    parser.add_argument('--rust', action='store_true', help='Generate Rust crate')

    args = parser.parse_args()

    lang_flags = [args.python, args.javascript, args.cpp, args.rust]
    if sum(lang_flags) != 1:
        parser.error("Exactly one of --python, --javascript, --cpp, or --rust must be specified")

    input_file, output_file = find_sample_files(args.sample)
    file_ext = get_file_extension(input_file)
    input_rows, _ = parse_file(input_file)
    output_rows, _ = parse_file(output_file)

    inferrer = TransformationInferrer(input_rows, output_rows)
    config = inferrer.infer()

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    if args.python:
        generator = PythonCodeGenerator(args.module_name, config, file_ext)
        files = generator.generate()
        package_dir = output_dir / args.module_name
        package_dir.mkdir(parents=True, exist_ok=True)
        for filename, content in files.items():
            with open(package_dir / filename, 'w', encoding='utf-8') as f:
                f.write(content)
        print("Generated Python package:", package_dir)
    elif args.javascript:
        generator = JavaScriptCodeGenerator(args.module_name, config, file_ext)
        code = generator.generate()
        module_dir = output_dir / args.module_name
        module_dir.mkdir(parents=True, exist_ok=True)
        with open(module_dir / 'index.js', 'w', encoding='utf-8') as f:
            f.write(code)
        print("Generated JavaScript module:", module_dir)
    elif args.cpp:
        generator = CppCodeGenerator(args.module_name, config, file_ext)
        files = generator.generate()
        module_dir = output_dir / args.module_name
        module_dir.mkdir(parents=True, exist_ok=True)
        for filename, content in files.items():
            with open(module_dir / filename, 'w', encoding='utf-8') as f:
                f.write(content)
        print("Generated C++ module:", module_dir)
    elif args.rust:
        generator = RustCodeGenerator(args.module_name, config, file_ext)
        files = generator.generate()
        crate_dir = output_dir / args.module_name
        crate_dir.mkdir(parents=True, exist_ok=True)
        src_dir = crate_dir / 'src'
        src_dir.mkdir(parents=True, exist_ok=True)
        for filename, content in files.items():
            if filename == 'Cargo.toml':
                with open(crate_dir / filename, 'w', encoding='utf-8') as f:
                    f.write(content)
            else:
                clean_name = filename
                if clean_name.startswith('src/'):
                    clean_name = clean_name[4:]
                with open(src_dir / clean_name, 'w', encoding='utf-8') as f:
                    f.write(content)
        print("Generated Rust crate:", crate_dir)


if __name__ == '__main__':
    main()
