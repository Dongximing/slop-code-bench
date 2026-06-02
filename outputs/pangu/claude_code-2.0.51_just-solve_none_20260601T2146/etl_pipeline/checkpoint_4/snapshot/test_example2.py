#!/usr/bin/env python3
import json
import subprocess

# Example 2: Error path inside a branch
input_data = {
    "pipeline": {
        "steps": [
            {
                "op":"branch",
                "branches":[
                    {"id":"s1", "when":"qty >= 1", "steps":[{"op":"select", "columns":["id","missing"]}]},
                    {"id":"otherwise", "when":"otherwise", "steps":[]}
                ],
                "merge":{"strategy":"concat"}
            }
        ]
    },
    "dataset": [ {"id":1,"qty":2} ]
}

# Write input to stdin of the script
result = subprocess.run(
    ["python", "etl_pipeline.py", "--execute"],
    input=json.dumps(input_data),
    capture_output=True,
    text=True
)

print("Example 2 Output:")
print(result.stdout)
if result.stderr:
    print("STDERR:", result.stderr)
print("Return code:", result.returncode)
