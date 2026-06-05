#!/usr/bin/env python3
"""Test script to verify multi-language support."""

import sys
import os
import json
import subprocess
import tempfile

def run_test():
    # Create a temporary directory with test files
    with tempfile.TemporaryDirectory() as tmpdir:
        # Create test files
        go_file = os.path.join(tmpdir, 'main.go')
        with open(go_file, 'w') as f:
            f.write('package main\nimport "log"\nfunc main() { log.Println("hi", 42) }\n')

        rs_file = os.path.join(tmpdir, 'main.rs')
        with open(rs_file, 'w') as f:
            f.write('fn main() { println!("hi {}", 42); }\n')

        java_file = os.path.join(tmpdir, 'Main.java')
        with open(java_file, 'w') as f:
            f.write('class Main { static void main(String[] a){ System.out.println("hi"); } }\n')

        hs_file = os.path.join(tmpdir, 'Main.hs')
        with open(hs_file, 'w') as f:
            f.write('module Main where\nmain = putStrLn "hi"\n')

        # Create rules file
        rules_file = os.path.join(tmpdir, 'rules.json')
        with open(rules_file, 'w') as f:
            json.dump([
                {
                    "id": "go-no-log-println",
                    "kind": "exact",
                    "languages": ["go"],
                    "pattern": 'log.Println("hi", 42)'
                },
                {
                    "id": "rust-no-println-macro",
                    "kind": "exact",
                    "languages": ["rust"],
                    "pattern": 'println!("hi {}", 42)'
                },
                {
                    "id": "java-no-system-out",
                    "kind": "exact",
                    "languages": ["java"],
                    "pattern": 'System.out.println("hi")'
                },
                {
                    "id": "hs-avoid-putStrLn",
                    "kind": "exact",
                    "languages": ["haskell"],
                    "pattern": 'putStrLn "hi"'
                }
            ], f)

        # Run code_search.py
        result = subprocess.run(
            [sys.executable, '/workspace/code_search.py', tmpdir, '--rules', rules_file],
            capture_output=True,
            text=True
        )

        print("Exit code:", result.returncode)
        print("\nOutput:")
        for line in result.stdout.strip().split('\n'):
            if line:
                obj = json.loads(line)
                print(f"  {obj.get('rule_id')}: {obj.get('file')} ({obj.get('language')})")

        if result.stderr:
            print("\nErrors:")
            print(result.stderr)

        # Parse output and verify
        lines = [l for l in result.stdout.strip().split('\n') if l]
        assert len(lines) == 4, f"Expected 4 output lines, got {len(lines)}"

        languages_found = set()
        for line in lines:
            obj = json.loads(line)
            languages_found.add(obj['language'])
            assert obj['language'] in ['go', 'rust', 'java', 'haskell'], f"Unexpected language: {obj['language']}"

        assert languages_found == {'go', 'rust', 'java', 'haskell'}, f"Missing languages: {languages_found}"

        print("\n✓ All tests passed!")
        return True

if __name__ == '__main__':
    try:
        run_test()
    except AssertionError as e:
        print(f"\n✗ Test failed: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"\n✗ Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
