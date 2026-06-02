#!/usr/bin/env python3
import json
import subprocess

# Example 1: Basic routing with otherwise
input_data = {
    "pipeline": {
        "steps": [
            {
                "op":"branch",
                "branches":[
                    {"id":"cheap", "when":"price < 20", "steps":[{"op":"map", "as":"tag", "expr":"\"cheap\""}]},
                    {"id":"otherwise", "when":"otherwise", "steps":[{"op":"map", "as":"tag", "expr":"\"premium\""}]}
                ],
                "merge":{"strategy":"concat"}
            }
        ]
    },
    "dataset":[
        {"id":1,"price":10},
        {"id":2,"price":25},
        {"id":3,"price":5}
    ]
}

# Write input to stdin of the script
result = subprocess.run(
    ["python", "etl_pipeline.py", "--execute"],
    input=json.dumps(input_data),
    capture_output=True,
    text=True
)

print("Example 1 Output:")
print(result.stdout)
if result.stderr:
    print("STDERR:", result.stderr)
print("Return code:", result.returncode)
