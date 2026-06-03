#!/usr/bin/env python3

import subprocess
import json
import os

# Test case 1: Simple print pattern
test1_rules = [
    {
        "id": "if-statement",
        "kind": "pattern",
        "pattern": "print($GREETING)",
        "languages": ["python"]
    }
]

# Test file
test_content = '''def run():
    print("hello")
    # print(  name  )  # comment: not code
    print(greeting())
'''

os.makedirs("/tmp/test_repo", exist_ok=True)
with open("/tmp/test_repo/test.py", "w") as f:
    f.write(test_content)

with open("/tmp/test_rules.json", "w") as f:
    json.dump(test1_rules, f)

# Run code_search
result = subprocess.run(
    ["python", "code_search.py", "/tmp/test_repo", "--rules", "/tmp/test_rules.json"],
    capture_output=True,
    text=True
)

print("Test 1: Simple print pattern")
print("STDOUT:")
for line in result.stdout.strip().split('\n'):
    if line:
        print(line)
print("STDERR:", result.stderr if result.stderr else "None")

# Test case 2: JavaScript console.log pattern
test2_rules = [
    {
        "id": "log-with-same-tag",
        "kind": "pattern",
        "pattern": "console.log($TAG, $TAG)",
        "languages": ["javascript"]
    }
]

js_content = '''console.log("user", "user");
console.log("user", id);
'''

os.makedirs("/tmp/test_js_repo", exist_ok=True)
with open("/tmp/test_js_repo/test.js", "w") as f:
    f.write(js_content)

with open("/tmp/test_js_rules.json", "w") as f:
    json.dump(test2_rules, f)

result = subprocess.run(
    ["python", "code_search.py", "/tmp/test_js_repo", "--rules", "/tmp/test_js_rules.json"],
    capture_output=True,
    text=True
)

print("\n\nTest 2: JavaScript console.log pattern")
print("STDOUT:")
for line in result.stdout.strip().split('\n'):
    if line:
        print(line)
print("STDERR:", result.stderr if result.stderr else "None")

# Test case 3: Nested function calls
test3_rules = [
    {
        "id": "func-call",
        "kind": "pattern",
        "pattern": "$X($Y)",
        "languages": ["python"]
    }
]

nested_content = '''f(g(h(z)))
'''

os.makedirs("/tmp/test_nested_repo", exist_ok=True)
with open("/tmp/test_nested_repo/nested.py", "w") as f:
    f.write(nested_content)

with open("/tmp/test_nested_rules.json", "w") as f:
    json.dump(test3_rules, f)

result = subprocess.run(
    ["python", "code_search.py", "/tmp/test_nested_repo", "--rules", "/tmp/test_nested_rules.json"],
    capture_output=True,
    text=True
)

print("\n\nTest 3: Nested function calls")
print("STDOUT:")
for line in result.stdout.strip().split('\n'):
    if line:
        print(line)
print("STDERR:", result.stderr if result.stderr else "None")
