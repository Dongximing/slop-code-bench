#!/usr/bin/env python3
"""Test script for module and tag functionality"""
import subprocess
import json
import os
import tempfile

DATA_DIR = tempfile.mkdtemp()

def run_forge(cmd_parts, input_json=None):
    """Run forge command and return result"""
    full_cmd = ["python3", "forge.py", "--data-dir", DATA_DIR] + cmd_parts
    result = subprocess.run(
        full_cmd,
        input=input_json,
        capture_output=True,
        text=True
    )
    try:
        return json.loads(result.stdout) if result.stdout.strip() else None, result.returncode
    except json.JSONDecodeError:
        return result.stdout.strip(), result.returncode

# Test
print("=" * 60)
print("Testing Module and Tag Operations")
print("=" * 60)

# 1. Create a unit first
print("\n1. Creating a unit...")
unit = {"category": "storage", "manufacturer": "Acme", "host": "host1", "capacity": 100}
result, code = run_forge(["unit", "create"], json.dumps(unit))
print(f"   Result: {result['uuid'] if result else result}, Code: {code}")
unit_uuid = result['uuid']

# 2. Test module create - valid
print("\n2. Creating a module (valid)...")
module_data = {"unit_uuid": unit_uuid, "component_type": "nvme"}
result, code = run_forge(["module", "create"], json.dumps(module_data))
print(f"   Result: {result['uuid'] if result else result}, Code: {code}")
module_uuid = result['uuid']

# 3. Test module create with firmware_image
print("\n3. Creating a module with firmware image...")
fw_uuid = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
module_data = {"unit_uuid": unit_uuid, "component_type": "sata", "firmware_image": fw_uuid}
result, code = run_forge(["module", "create"], json.dumps(module_data))
print(f"   Result: {result['uuid'] if result else result}, Code: {code}")
module2_uuid = result['uuid']

# 4. Test module create - invalid component_type
print("\n4. Creating module with empty component_type...")
module_data = {"unit_uuid": unit_uuid, "component_type": ""}
result, code = run_forge(["module", "create"], json.dumps(module_data))
print(f"   Result: {result}, Code: {code}")

# 5. Test module create - invalid unit_uuid
print("\n5. Creating module with non-existent unit_uuid...")
module_data = {"unit_uuid": "nonexistent", "component_type": "nvme"}
result, code = run_forge(["module", "create"], json.dumps(module_data))
print(f"   Result: {result}, Code: {code}")

# 6. Test module create - invalid firmware_image format
print("\n6. Creating module with invalid firmware_image format...")
module_data = {"unit_uuid": unit_uuid, "component_type": "nvme", "firmware_image": "invalid"}
result, code = run_forge(["module", "create"], json.dumps(module_data))
print(f"   Result: {result}, Code: {code}")

# 7. Test module list
print("\n7. Listing all modules...")
result, code = run_forge(["module", "list"])
print(f"   Found {len(result)} modules")

# 8. Test module get
print("\n8. Getting a module by UUID...")
result, code = run_forge(["module", "get", module_uuid])
print(f"   Module: {result['uuid']}, component_type: {result['component_type']}")

# 9. Test module get - not found
print("\n9. Getting non-existent module...")
result, code = run_forge(["module", "get", "nonexistent"])
print(f"   Result: {result}, Code: {code}")

# 10. Test module flash - invalid image format
print("\n10. Flashing module with invalid image format...")
result, code = run_forge(["module", "flash", module_uuid, "--image", "invalid"])
print(f"    Result: {result}, Code: {code}")

# 11. Test module flash - module not found
print("\n11. Flashing non-existent module...")
result, code = run_forge(["module", "flash", "nonexistent", "--image", fw_uuid])
print(f"    Result: {result}, Code: {code}")

# 12. Test module flash - image not found (valid format, but not existing)
print("\n12. Flashing module with non-existent firmware image...")
result, code = run_forge(["module", "flash", module_uuid, "--image", "f00dcafe-1234-5678-9012-34567890abcd"])
print(f"    Result: {result}, Code: {code}")

# 13. Test module flash - valid flash
print("\n13. Flashing module with valid firmware image...")
result, code = run_forge(["module", "flash", module_uuid, "--image", fw_uuid])
print(f"    Result: {result['uuid']}, firmware_image: {result['firmware_image']}, Code: {code}")

# Now create tags
print("\n14. Creating a tag...")
tag_data = {"module_uuid": module_uuid, "key": "env", "value": "production"}
result, code = run_forge(["tag", "create"], json.dumps(tag_data))
print(f"   Tag: {result['uuid']}, key: {result['key']}, value: {result['value']}, Code: {code}")
tag_uuid = result['uuid']

# 15. Test tag create - missing field
print("\n15. Creating tag with missing key...")
tag_data = {"module_uuid": module_uuid, "value": "test"}
result, code = run_forge(["tag", "create"], json.dumps(tag_data))
print(f"   Result: {result}, Code: {code}")

# 16. Test tag create - empty field
print("\n16. Creating tag with empty key...")
tag_data = {"module_uuid": module_uuid, "key": "", "value": "test"}
result, code = run_forge(["tag", "create"], json.dumps(tag_data))
print(f"   Result: {result}, Code: {code}")

# 17. Test tag create - non-existent module
print("\n17. Creating tag with non-existent module UUID...")
tag_data = {"module_uuid": "nonexistent", "key": "env", "value": "test"}
result, code = run_forge(["tag", "create"], json.dumps(tag_data))
print(f"   Result: {result}, Code: {code}")

# 18. Test tag create - duplicate (module_uuid, key)
print("\n18. Creating duplicate tag (same module_uuid and key)...")
tag_data = {"module_uuid": module_uuid, "key": "env", "value": "test2"}
result, code = run_forge(["tag", "create"], json.dumps(tag_data))
print(f"   Result: {result}, Code: {code}")

# 19. Test tag list
print("\n19. Listing all tags...")
result, code = run_forge(["tag", "list"])
print(f"   Found {len(result)} tags: {result}")

# 20. Test tag list with --module filter
print("\n20. Listing tags with --module filter...")
result, code = run_forge(["tag", "list", "--module", module_uuid])
print(f"   Found {len(result)} tags for module")

# 21. Test tag list with --key filter
print("\n21. Listing tags with --key filter...")
result, code = run_forge(["tag", "list", "--key", "env"])
print(f"   Found {len(result)} tags with key 'env'")

# 22. Test tag get
print("\n22. Getting a tag by UUID...")
result, code = run_forge(["tag", "get", tag_uuid])
print(f"   Tag: {result['uuid']}, key: {result['key']}, value: {result['value']}")

# 23. Test tag get - not found
print("\n23. Getting non-existent tag...")
result, code = run_forge(["tag", "get", "nonexistent"])
print(f"   Result: {result}, Code: {code}")

# 24. Test tag delete
print("\n24. Deleting a tag...")
result, code = run_forge(["tag", "delete", tag_uuid])
print(f"   Result: {result}, Code: {code}")

# 25. Test tag delete - not found
print("\n25. Deleting non-existent tag...")
result, code = run_forge(["tag", "delete", "nonexistent"])
print(f"   Result: {result}, Code: {code}")

# 26. Test tag update (should return unknown_command)
print("\n26. Testing tag update (should fail with unknown_command)...")
result, code = run_forge(["tag", "update"])
print(f"   Result: {result}, Code: {code}")

# Clean up
import shutil
shutil.rmtree(DATA_DIR)
print("\n" + "=" * 60)
print("All tests completed!")
print("=" * 60)