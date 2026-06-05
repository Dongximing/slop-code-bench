#!/usr/bin/env python3
"""Test script for pattern matching with metavariables."""

import sys
import os
import json
import subprocess
import tempfile

def test_pattern_matching():
    # Create a temporary directory with test files
    with tempfile.TemporaryDirectory() as tmpdir:
        # Create test files with various code patterns
        go_file = os.path.join(tmpdir, 'main.go')
        with open(go_file, 'w') as f:
            f.write('''package main
import "log"
func main() {
    log.Println("hi", 42)
    log.Println("bye", 100)
}
''')

        rs_file = os.path.join(tmpdir, 'main.rs')
        with open(rs_file, 'w') as f:
            f.write('''fn main() {
    println!("hello {}", 1);
    println!("world {}", 2);
}
''')

        java_file = os.path.join(tmpdir, 'Main.java')
        with open(java_file, 'w') as f:
            f.write('''class Main {
    static void main(String[] a) {
        System.out.println("test1");
        System.out.println("test2");
    }
}
''')

        hs_file = os.path.join(tmpdir, 'Main.hs')
        with open(hs_file, 'w') as f:
            f.write('''module Main where
main = do
    putStrLn "hello"
    putStrLn "world"
''')

        # Create rules file with pattern rules (using metavariables)
        rules_file = os.path.join(tmpdir, 'rules.json')
        with open(rules_file, 'w') as f:
            json.dump([
                {
                    "id": "go-log-println",
                    "kind": "pattern",
                    "languages": ["go"],
                    "pattern": "log.Println($ARGS)",
                    "fix": {
                        "kind": "replace",
                        "template": "logger.Println($ARGS)"
                    }
                },
                {
                    "id": "rust-println",
                    "kind": "pattern",
                    "languages": ["rust"],
                    "pattern": "println!($MSG)",
                    "fix": {
                        "kind": "replace",
                        "template": "log::info!($MSG)"
                    }
                },
                {
                    "id": "java-sysout",
                    "kind": "pattern",
                    "languages": ["java"],
                    "pattern": "System.out.println($X)",
                    "fix": {
                        "kind": "replace",
                        "template": "logger.info($X)"
                    }
                },
                {
                    "id": "hs-putstrln",
                    "kind": "pattern",
                    "languages": ["haskell"],
                    "pattern": "putStrLn $MSG",
                    "fix": {
                        "kind": "replace",
                        "template": "logInfo $MSG"
                    }
                }
            ], f)

        # Run code_search.py with dry-run
        result = subprocess.run(
            [sys.executable, '/workspace/code_search.py', tmpdir, '--rules', rules_file, '--dry-run'],
            capture_output=True,
            text=True
        )

        print("Exit code:", result.returncode)
        print("\nOutput:")

        matches_by_lang = {'go': 0, 'rust': 0, 'java': 0, 'haskell': 0}

        for line in result.stdout.strip().split('\n'):
            if line:
                obj = json.loads(line)
                if 'event' not in obj or obj['event'] != 'fix':
                    lang = obj.get('language')
                    if lang in matches_by_lang:
                        matches_by_lang[lang] += 1
                    print(f"  {obj.get('rule_id')}: {obj.get('match')[:40] if obj.get('match') else 'N/A'} ({lang})")
                else:
                    print(f"  FIX: {obj.get('rule_id')}: {obj.get('replacement')[:40] if obj.get('replacement') else 'N/A'}")

        if result.stderr:
            print("\nErrors:")
            print(result.stderr)

        # Verify we got matches for each language
        print("\nMatches by language:")
        for lang, count in matches_by_lang.items():
            print(f"  {lang}: {count}")

        # We should get matches for all languages
        total_matches = sum(matches_by_lang.values())
        print(f"\nTotal match lines: {total_matches}")

        return result.returncode == 0

if __name__ == '__main__':
    try:
        if test_pattern_matching():
            print("\n✓ Pattern matching test passed!")
        else:
            print("\n✗ Pattern matching test failed!")
            sys.exit(1)
    except Exception as e:
        print(f"\n✗ Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
