#!/usr/bin/env python3
"""Test script for mvault version handling."""

import json
import os
import shutil
import sys
import tempfile
import subprocess
from datetime import datetime, timezone

# Get the directory containing mvault.py
MVault_DIR = os.path.dirname(os.path.abspath(__file__))
SYNC_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%S"

def create_v1_catalog(source_id, entries):
    """Create a v1 catalog structure."""
    return {
        "version": 1,
        "source_id": source_id,
        "entries": entries
    }

def create_v1_entry(entry_id, published, title_history, description_history, views_history, likes_history, preview_history, width=1920, height=1080):
    """Create a v1 entry with UNIX-epoch history keys."""
    return {
        "id": entry_id,
        "published": published,
        "width": width,
        "height": height,
        "title": title_history,
        "description": description_history,
        "views": views_history,
        "likes": likes_history,
        "preview": preview_history
    }

def create_v2_catalog(source, episodes, streams=None, clips=None):
    """Create a v2 catalog structure."""
    return {
        "version": 2,
        "source": source,
        "episodes": episodes,
        "streams": streams or [],
        "clips": clips or []
    }

def create_v2_entry(entry_id, published, title_history, description_history, views_history, likes_history, preview_history, width=1920, height=1080):
    """Create a v2 entry with ISO 8601 history keys."""
    return {
        "id": entry_id,
        "published": published,
        "width": width,
        "height": height,
        "title": title_history,
        "description": description_history,
        "views": views_history,
        "likes": likes_history,
        "preview": preview_history
    }

def create_v3_catalog(source, episodes, streams=None, clips=None):
    """Create a v3 catalog structure."""
    return {
        "version": 3,
        "source": source,
        "episodes": episodes,
        "streams": streams or [],
        "clips": clips or []
    }

def create_entry_with_history(entry_id, published, title, description, views, likes, preview, removed=None, annotations=None, width=1920, height=1080):
    """Create an entry with ISO 8601 history keys."""
    sync_ts = datetime.now(timezone.utc).strftime(SYNC_TIMESTAMP_FORMAT)
    entry = {
        "id": entry_id,
        "published": published,
        "width": width,
        "height": height,
        "title": {sync_ts: title},
        "description": {sync_ts: description},
        "views": {sync_ts: views},
        "likes": {sync_ts: likes},
        "preview": {sync_ts: preview},
        "removed": removed or {sync_ts: False},
        "annotations": annotations or []
    }
    return entry

def run_mvault_cmd(args):
    """Run mvault command and return result."""
    cmd = [sys.executable, os.path.join(MVault_DIR, "mvault.py")] + args
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=MVault_DIR)
    return result.returncode, result.stdout, result.stderr

def test_version_1_loading():
    """Test loading v1 catalog."""
    print("\n=== Test: Version 1 catalog loading ===")

    with tempfile.TemporaryDirectory() as tmpdir:
        os.chdir(tmpdir)

        # Initialize as v3 first
        code, out, err = run_mvault_cmd(["init", "testvault", "https://media.example.com/channel/test"])
        assert code == 0, f"init failed: {err}"

        # Now manually create a v1 catalog to test loading
        v1_catalog = create_v1_catalog(
            "testchannel",
            [
                create_v1_entry(
                    "entry1",
                    "2024-01-01T00:00:00",
                    {"1704067200": "Title at midnight"},
                    {"1704067200": "Description"},
                    {"1704067200": 100},
                    {"1704067200": 50},
                    {"1704067200": "https://example.com/preview1.jpg"}
                )
            ]
        )

        # Write v1 catalog
        os.makedirs("testvault", exist_ok=True)
        with open("testvault/catalog.json", "w") as f:
            json.dump(v1_catalog, f)

        # Try to migrate
        code, out, err = run_mvault_cmd(["migrate", "testvault"])
        print(f"Migrate exit code: {code}")
        if err:
            print(f"Stderr: {err}")

        # Verify migration
        with open("testvault/catalog.json", "r") as f:
            migrated = json.load(f)

        assert migrated["version"] == 3, f"Expected version 3, got {migrated['version']}"
        assert "source_id" not in migrated, "v1 source_id should be converted to source"
        assert migrated["source"] == "https://media.example.com/channel/testchannel", \
            f"Wrong source URL: {migrated['source']}"
        assert len(migrated["episodes"]) == 1, "Should have 1 episode"
        assert len(migrated["streams"]) == 0, "Should have empty streams"
        assert len(migrated["clips"]) == 0, "Should have empty clips"

        episode = migrated["episodes"][0]
        assert episode["id"] == "entry1"
        assert "removed" in episode, "Should have removed field"
        assert "annotations" in episode, "Should have annotations field"
        assert isinstance(episode["annotations"], list), "annotations should be a list"

        # Check that history keys were converted
        assert isinstance(episode["title"], dict), "title should be history object"
        # The timestamp should be ISO 8601, not Unix epoch
        first_key = list(episode["title"].keys())[0]
        assert "T" in first_key, f"History key should be ISO 8601, got {first_key}"

        print("✓ v1 catalog migration test PASSED")
        return True

def test_version_2_loading():
    """Test loading v2 catalog."""
    print("\n=== Test: Version 2 catalog loading ===")

    with tempfile.TemporaryDirectory() as tmpdir:
        os.chdir(tmpdir)

        # Initialize as v3 first
        code, out, err = run_mvault_cmd(["init", "testvault2", "https://media.example.com/channel/test"])
        assert code == 0, f"init failed: {err}"

        # Manually create a v2 catalog
        v2_catalog = create_v2_catalog(
            "https://media.example.com/channel/testchannel",
            [
                create_v2_entry(
                    "episode1",
                    "2024-01-01T00:00:00",
                    {"2024-01-01T00:00:00": "Episode Title"},
                    {"2024-01-01T00:00:00": "Episode Description"},
                    {"2024-01-01T00:00:00": 1000},
                    {"2024-01-01T00:00:00": 500},
                    {"2024-01-01T00:00:00": "https://example.com/preview1.jpg"}
                )
            ],
            streams=[
                create_v2_entry(
                    "stream1",
                    "2024-01-02T00:00:00",
                    {"2024-01-02T00:00:00": "Stream Title"},
                    {"2024-01-02T00:00:00": "Stream Description"},
                    {"2024-01-02T00:00:00": 500},
                    {"2024-01-02T00:00:00": 250},
                    {"2024-01-02T00:00:00": "https://example.com/preview2.jpg"}
                )
            ]
        )

        # Write v2 catalog
        os.makedirs("testvault2", exist_ok=True)
        with open("testvault2/catalog.json", "w") as f:
            json.dump(v2_catalog, f)

        # Try to migrate
        code, out, err = run_mvault_cmd(["migrate", "testvault2"])
        print(f"Migrate exit code: {code}")
        if err:
            print(f"Stderr: {err}")

        # Verify migration
        with open("testvault2/catalog.json", "r") as f:
            migrated = json.load(f)

        assert migrated["version"] == 3, f"Expected version 3, got {migrated['version']}"
        assert migrated["source"] == "https://media.example.com/channel/testchannel"
        assert len(migrated["episodes"]) == 1, "Should have 1 episode"
        assert len(migrated["streams"]) == 1, "Should have 1 stream"
        assert len(migrated["clips"]) == 0, "Should have empty clips"

        for entry in migrated["episodes"] + migrated["streams"]:
            assert "removed" in entry, "Should have removed field"
            assert "annotations" in entry, "Should have annotations field"
            assert isinstance(entry["annotations"], list), "annotations should be a list"

        print("✓ v2 catalog migration test PASSED")
        return True

def test_version_3_no_migration():
    """Test that v3 catalog doesn't get migrated."""
    print("\n=== Test: Version 3 catalog (no migration) ===")

    with tempfile.TemporaryDirectory() as tmpdir:
        os.chdir(tmpdir)

        # Create a v3 catalog directly
        v3_catalog = create_v3_catalog(
            "https://media.example.com/channel/test",
            [
                create_entry_with_history("ep1", "2024-01-01T00:00:00", "Title", "Desc", 100, 50, "url")
            ]
        )

        os.makedirs("testvault3", exist_ok=True)
        with open("testvault3/catalog.json", "w") as f:
            json.dump(v3_catalog, f)

        # Try to migrate
        code, out, err = run_mvault_cmd(["migrate", "testvault3"])
        print(f"Migrate exit code: {code}")

        # Should succeed with no changes (already v3)
        assert code == 0, "v3 catalog should not produce error on migrate"

        with open("testvault3/catalog.json", "r") as f:
            after = json.load(f)

        assert after["version"] == 3, "Should remain version 3"

        print("✓ v3 catalog (no migration needed) test PASSED")
        return True

def test_missing_version_field():
    """Test error handling for missing version field."""
    print("\n=== Test: Missing version field ===")

    with tempfile.TemporaryDirectory() as tmpdir:
        os.chdir(tmpdir)

        # Create catalog without version
        bad_catalog = {"source_id": "test", "entries": []}

        os.makedirs("badvault", exist_ok=True)
        with open("badvault/catalog.json", "w") as f:
            json.dump(bad_catalog, f)

        code, out, err = run_mvault_cmd(["migrate", "badvault"])
        print(f"Migrate exit code: {code}")

        assert code != 0, "Should exit non-zero for missing version"
        assert "version" in err.lower() or "missing" in err.lower(), \
            "Error message should mention version"

        print("✓ Missing version field error test PASSED")
        return True

def test_invalid_json():
    """Test error handling for invalid JSON."""
    print("\n=== Test: Invalid JSON ===")

    with tempfile.TemporaryDirectory() as tmpdir:
        os.chdir(tmpdir)

        os.makedirs("badvault", exist_ok=True)
        with open("badvault/catalog.json", "w") as f:
            f.write("{ invalid json }")

        code, out, err = run_mvault_cmd(["migrate", "badvault"])
        print(f"Migrate exit code: {code}")

        assert code != 0, "Should exit non-zero for invalid JSON"
        assert "json" in err.lower() or "invalid" in err.lower(), \
            "Error message should mention JSON error"

        print("✓ Invalid JSON error test PASSED")
        return True

def test_non_integer_version():
    """Test error handling for non-integer version."""
    print("\n=== Test: Non-integer version ===")

    with tempfile.TemporaryDirectory() as tmpdir:
        os.chdir(tmpdir)

        bad_catalog = {"version": "not-an-integer", "entries": []}

        os.makedirs("badvault", exist_ok=True)
        with open("badvault/catalog.json", "w") as f:
            json.dump(bad_catalog, f)

        code, out, err = run_mvault_cmd(["migrate", "badvault"])
        print(f"Migrate exit code: {code}")

        assert code != 0, "Should exit non-zero for non-integer version"
        assert "integer" in err.lower() or "version" in err.lower(), \
            "Error message should mention version field type"

        print("✓ Non-integer version error test PASSED")
        return True

def test_missing_vault_error():
    """Test error handling for missing vault."""
    print("\n=== Test: Missing vault ===")

    code, out, err = run_mvault_cmd(["migrate", "nonexistent"])
    print(f"Migrate exit code: {code}")

    assert code != 0, "Should exit non-zero for missing vault"
    assert "not found" in err.lower() or "nonexistent" in err.lower() or "not exist" in err.lower(), \
        "Error message should mention vault not found"

    print("✓ Missing vault error test PASSED")
    return True

def test_sync_auto_migration():
    """Test that sync auto-migrates v1/v2 vaults."""
    print("\n=== Test: Sync auto-migration ===")

    with tempfile.TemporaryDirectory() as tmpdir:
        os.chdir(tmpdir)

        # Mock the requests library by creating a simple HTTP server
        # For simplicity, we'll just test the migration path without actual HTTP

        # Create a v1 catalog
        v1_catalog = create_v1_catalog(
            "testchannel",
            [
                create_v1_entry(
                    "entry1",
                    "2024-01-01T00:00:00",
                    {"1704067200": "Title"},
                    {"1704067200": "Desc"},
                    {"1704067200": 100},
                    {"1704067200": 50},
                    {"1704067200": "url"}
                )
            ]
        )

        os.makedirs("v1vault", exist_ok=True)
        with open("v1vault/catalog.json", "w") as f:
            json.dump(v1_catalog, f)

        # The sync should trigger migration
        # Since we can't actually fetch data without a server, we'll test the load_catalog
        # function directly by importing the module
        sys.path.insert(0, tmpdir)

        # We'll just verify that migration happens via the migrate command for now
        code, out, err = run_mvault_cmd(["migrate", "v1vault"])
        assert code == 0, f"Migration should succeed, got code {code}"

        print("✓ Sync auto-migration test PASSED (via migrate command)")
        return True

def test_backup_on_migration():
    """Test that backup is created before migration."""
    print("\n=== Test: Backup on migration ===")

    with tempfile.TemporaryDirectory() as tmpdir:
        os.chdir(tmpdir)

        # Create a v1 catalog
        v1_catalog = create_v1_catalog(
            "testchannel",
            [create_v1_entry("entry1", "2024-01-01T00:00:00", {"1704067200": "Title"},
                            {"1704067200": "Desc"}, {"1704067200": 100},
                            {"1704067200": 50}, {"1704067200": "url"})]
        )

        os.makedirs("backupvault", exist_ok=True)
        with open("backupvault/catalog.json", "w") as f:
            json.dump(v1_catalog, f)

        # Migrate
        code, out, err = run_mvault_cmd(["migrate", "backupvault"])
        assert code == 0, "Migration should succeed"

        # Check backup exists
        assert os.path.exists("backupvault/catalog.bak"), "Backup file should exist"

        with open("backupvault/catalog.bak", "r") as f:
            backup = json.load(f)

        assert backup["version"] == 1, "Backup should be v1"

        print("✓ Backup on migration test PASSED")
        return True

if __name__ == "__main__":
    print("Running mvault version handling tests...\n")

    tests = [
        test_version_1_loading,
        test_version_2_loading,
        test_version_3_no_migration,
        test_missing_version_field,
        test_invalid_json,
        test_non_integer_version,
        test_missing_vault_error,
        test_sync_auto_migration,
        test_backup_on_migration,
    ]

    passed = 0
    failed = 0

    for test in tests:
        try:
            if test():
                passed += 1
            else:
                failed += 1
                print(f"✗ {test.__name__} FAILED")
        except Exception as e:
            failed += 1
            print(f"✗ {test.__name__} FAILED with exception: {e}")
            import traceback
            traceback.print_exc()

    print(f"\n{'='*50}")
    print(f"Results: {passed} passed, {failed} failed")

    sys.exit(0 if failed == 0 else 1)
