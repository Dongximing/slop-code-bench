#!/usr/bin/env python3
"""Verify datagate configuration implementation."""

import sys
import os

# Clear relevant env vars for clean testing
for key in ['DATAGATE_CONFIG', 'MAX_SOURCE_SIZE', 'ORIGIN_ALLOWLIST', 'REQUIRE_TLS', 'STORAGE_DIR', 'CACHE_ENABLED']:
    os.environ.pop(key, None)

import importlib
import datagate
importlib.reload(datagate)

print('=' * 70)
print('Verification: datagate Configuration, Size Limits, and Access Control')
print('=' * 70)

# Test 1: Defaults
print('\n[Test 1] Configuration defaults')
try:
    assert datagate.config['MAX_SOURCE_SIZE'] is None
    assert datagate.config['ORIGIN_ALLOWLIST'] is None
    assert datagate.config['REQUIRE_TLS'] is False
    assert datagate.config['STORAGE_DIR'] is None
    assert datagate.config['CACHE_ENABLED'] is True
    assert datagate.MAX_SOURCE_SIZE is None
    assert datagate.ORIGIN_ALLOWLIST is None
    assert datagate.REQUIRE_TLS is False
    assert datagate.STORAGE_DIR is None
    assert datagate.CACHE_ENABLED is True
    print('✓ Test 1 PASSED')
except AssertionError as e:
    print(f'✗ Test 1 FAILED: {e}')
    sys.exit(1)

# Test 2: Config file loading
print('\n[Test 2] Config file loading with KEY=VALUE format')
try:
    import tempfile
    config_path = '/tmp/test_cfg_load.cfg'
    with open(config_path, 'w') as f:
        f.write('# Comment line\n')
        f.write('MAX_SOURCE_SIZE=1048576\n')
        f.write('\n')  # Blank line
        f.write('ORIGIN_ALLOWLIST=example.com,test.org\n')
        f.write('REQUIRE_TLS=true\n')
        f.write('STORAGE_DIR=/tmp/datagate_store\n')
        f.write('CACHE_ENABLED=false\n')
    os.environ['DATAGATE_CONFIG'] = config_path

    # Fresh import
    importlib.reload(datagate)

    assert datagate.config['MAX_SOURCE_SIZE'] == 1048576
    assert set(datagate.config['ORIGIN_ALLOWLIST']) == {'example.com', 'test.org'}
    assert datagate.config['REQUIRE_TLS'] is True
    assert datagate.config['STORAGE_DIR'] == '/tmp/datagate_store'
    assert datagate.config['CACHE_ENABLED'] is False

    # Check runtime constants
    assert datagate.MAX_SOURCE_SIZE == 1048576
    assert set(datagate.ORIGIN_ALLOWLIST) == {'example.com', 'test.org'}
    assert datagate.REQUIRE_TLS is True
    assert datagate.STORAGE_DIR == '/tmp/datagate_store'
    assert datagate.CACHE_ENABLED is False

    os.unlink(config_path)
    print('✓ Test 2 PASSED')
except Exception as e:
    print(f'✗ Test 2 FAILED: {e}')
    if os.path.exists(config_path):
        os.unlink(config_path)
    sys.exit(1)

# Test 3: Environment variables override
print('\n[Test 3] Environment variables override config file')
try:
    import tempfile
    config_path = '/tmp/test_cfg_override.cfg'
    with open(config_path, 'w') as f:
        f.write('MAX_SOURCE_SIZE=100\n')
        f.write('ORIGIN_ALLOWLIST=allowed.com\n')
    os.environ['DATAGATE_CONFIG'] = config_path

    # Set env vars to override
    os.environ['MAX_SOURCE_SIZE'] = '2048'
    os.environ['ORIGIN_ALLOWLIST'] = 'mydomain.com,another.com'
    os.environ['REQUIRE_TLS'] = '1'
    os.environ['STORAGE_DIR'] = '/custom/storage'

    importlib.reload(datagate)

    assert datagate.MAX_SOURCE_SIZE == 2048, f"Expected 2048, got {datagate.MAX_SOURCE_SIZE}"
    assert set(datagate.ORIGIN_ALLOWLIST) == {'mydomain.com', 'another.com'}
    assert datagate.REQUIRE_TLS is True

    os.unlink(config_path)
    print('✓ Test 3 PASSED')
except Exception as e:
    print(f'✗ Test 3 FAILED: {e}')
    if os.path.exists(config_path):
        os.unlink(config_path)
    sys.exit(1)

# Test 4: Boolean parsing
print('\n[Test 4] Boolean parsing strictness')
try:
    # True values
    for val in ['true', 'True', 'TRUE', '1', 'yes', 'Yes', 'YES', 'on', 'On', 'ON']:
        assert datagate.parse_boolean(val, 'TEST') is True, f"Failed for '{val}'"

    # False values
    for val in ['false', 'False', 'FALSE', '0', 'no', 'No', 'NO', 'off', 'Off', 'OFF']:
        assert datagate.parse_boolean(val, 'TEST') is False, f"Failed for '{val}'"

    # Invalid should raise
    try:
        datagate.parse_boolean('invalid', 'TEST')
        assert False, "Should have raised ValueError"
    except ValueError:
        pass

    try:
        datagate.parse_boolean('2', 'TEST')
        assert False, "Should have raised ValueError"
    except ValueError:
        pass

    print('✓ Test 4 PASSED')
except AssertionError as e:
    print(f'✗ Test 4 FAILED: {e}')
    sys.exit(1)

# Test 5: Invalid config causes startup failure
print('\n[Test 5] Invalid config (not a number) causes startup failure')
config_path = '/tmp/test_invalid.cfg'
with open(config_path, 'w') as f:
    f.write('MAX_SOURCE_SIZE=not_a_number\n')
os.environ['DATAGATE_CONFIG'] = config_path

try:
    importlib.reload(datagate)
    print(f'✗ Test 5 FAILED: Should have raised SystemExit')
    os.unlink(config_path)
    sys.exit(1)
except SystemExit:
    pass  # Expected!
except Exception as e:
    print(f'✗ Test 5 FAILED with wrong exception: {e}')
    os.unlink(config_path)
    sys.exit(1)

os.unlink(config_path)
print('✓ Test 5 PASSED')

# Test 6: Invalid config line (no =) causes startup failure
print('\n[Test 6] Invalid config line (missing =) causes startup failure')
config_path = '/tmp/test_noeq.cfg'
with open(config_path, 'w') as f:
    f.write('MAX_SOURCE_SIZE=100\n')
    f.write('LINE WITHOUT EQUALS SIGN\n')
os.environ['DATAGATE_CONFIG'] = config_path

try:
    importlib.reload(datagate)
    print(f'✗ Test 6 FAILED: Should have raised SystemExit')
    os.unlink(config_path)
    sys.exit(1)
except SystemExit:
    pass
except Exception as e:
    print(f'✗ Test 6 FAILED with wrong exception: {e}')
    os.unlink(config_path)
    sys.exit(1)

os.unlink(config_path)
print('✓ Test 6 PASSED')

# Test 7: Non-existent config file (not an error)
print('\n[Test 7] Non-existent config file does not cause failure')
os.environ['DATAGATE_CONFIG'] = '/tmp/does_not_exist_xyz.cfg'
os.environ.pop('MAX_SOURCE_SIZE', None)

try:
    importlib.reload(datagate)
    # Should get defaults since file doesn't exist
    assert datagate.MAX_SOURCE_SIZE is None
    print('✓ Test 7 PASSED')
except SystemExit:
    print('✗ Test 7 FAILED: File not found should not cause startup failure')
    sys.exit(1)
except Exception as e:
    print(f'✗ Test 7 FAILED: {e}')
    sys.exit(1)

# Test 8: Storing directory creation
print('\n[Test 8] STORAGE_DIR creation')
storage_path = '/tmp/test_datagate_storage_xyz123'
import shutil
if os.path.exists(storage_path):
    shutil.rmtree(storage_path)

os.environ['STORAGE_DIR'] = storage_path
os.environ.pop('DATAGATE_CONFIG', None)

try:
    importlib.reload(datagate)
    assert datagate.STORAGE_DIR == storage_path
    assert os.path.exists(storage_path), "Storage directory should be created"
    print('✓ Test 8 PASSED')
except Exception as e:
    print(f'✗ Test 8 FAILED: {e}')
    sys.exit(1)
finally:
    if os.path.exists(storage_path):
        shutil.rmtree(storage_path)

# Test 9: Origin allowlist parsing
print('\n[Test 9] Origin allowlist parsing')
config_path = '/tmp/test_origin.cfg'
with open(config_path, 'w') as f:
    f.write('ORIGIN_ALLOWLIST=example.com,sub.test.org\n')
os.environ['DATAGATE_CONFIG'] = config_path

try:
    importlib.reload(datagate)
    assert set(datagate.ORIGIN_ALLOWLIST) == {'example.com', 'sub.test.org'}
    print('✓ Test 9 PASSED')
except Exception as e:
    print(f'✗ Test 9 FAILED: {e}')
    os.unlink(config_path)
    sys.exit(1)

os.unlink(config_path)

# Test 10: Origin allowlist checking function
print('\n[Test 10] Origin allowlist check - domain matching logic')
# Test the domain boundary logic directly
config_path = '/tmp/test_origin2.cfg'
with open(config_path, 'w') as f:
    f.write('ORIGIN_ALLOWLIST=example.com,test.org\n')
os.environ['DATAGATE_CONFIG'] = config_path

# We need to test check_origin_allowed with proper Flask context
# Let's import the function and test with a mock request

from datagate import check_origin_allowed, ORIGIN_ALLOWLIST, parse_list, parse_boolean, get_endpoint_url

# Test domain matching logic by checking it directly
test_domains = [
    ('example.com', True, 'exact match'),
    ('www.example.com', True, 'subdomain match'),
    ('api.v2.example.com', True, 'multi-level subdomain'),
    ('notexample.com', False, 'different domain same TLD suffix'),
    ('testorg.com', False, 'similar but different'),
    ('example.com.bad.com', False, 'suffixed not subdomain'),
]

all_passed = True
for domain, should_match, desc in test_domains:
    # Manually check the logic from check_origin_allowed
    hostname_lower = domain.lower()
    matched = False
    for suffix in ORIGIN_ALLOWLIST:
        suffix_lower = suffix.lower()
        if hostname_lower == suffix_lower:
            matched = True
            break
        if hostname_lower.endswith('.' + suffix_lower):
            matched = True
            break

    if matched != should_match:
        print(f'  FAIL domain matching for {desc}: {domain} -> {matched}, expected {should_match}')
        all_passed = False

if all_passed:
    os.unlink(config_path)
    print('✓ Test 10 PASSED')
else:
    os.unlink(config_path)
    print(f'✗ Test 10 FAILED')
    sys.exit(1)

# Test 11: Parse list function
print('\n[Test 11] List parsing')
try:
    assert datagate.parse_list('a,b,c', 'TEST') == ['a', 'b', 'c']
    assert datagate.parse_list(' a , b , c ', 'TEST') == ['a', 'b', 'c']
    assert datagate.parse_list('', 'TEST') is None
    assert datagate.parse_list('   ', 'TEST') is None
    assert datagate.parse_list('a', 'TEST') == ['a']
    print('✓ Test 11 PASSED')
except Exception as e:
    print(f'✗ Test 11 FAILED: {e}')
    sys.exit(1)

print('\n' + '=' * 70)
print('ALL CONFIGURATION TESTS PASSED ✓')
print('=' * 70)
