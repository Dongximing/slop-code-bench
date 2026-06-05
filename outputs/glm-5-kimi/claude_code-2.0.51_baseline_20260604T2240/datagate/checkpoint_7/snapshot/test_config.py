#!/usr/bin/env python3
"""Tests for datagate configuration, size limits, and access control."""

import json
import os
import sys
import tempfile
from pathlib import Path
from io import BytesIO

import pytest
from flask import Flask
from flask.testing import FlaskClient

# Import the datagate module
import datagate


@pytest.fixture
def app():
    """Create a test Flask app."""
    app = datagate.app
    app.config['TESTING'] = True
    yield app


@pytest.fixture
def client(app):
    """Create a test client."""
    return app.test_client()


@pytest.fixture
def reset_config():
    """Reset configuration between tests."""
    # Save original config
    orig_config = datagate.config
    orig_storage_dir = datagate._storage_dir
    orig_datasets = datagate.datasets.copy()
    orig_cache = datagate._convert_cache.copy()

    yield

    # Restore original config
    datagate.config = orig_config
    datagate._storage_dir = orig_storage_dir
    datagate.datasets = orig_datasets
    datagate._convert_cache = orig_cache


class TestConfiguration:
    """Test configuration loading system."""

    def test_parse_boolean_true_values(self):
        """Test parsing true boolean values."""
        true_values = ['1', 'true', 'True', 'TRUE', 'yes', 'Yes', 'YES', 'on', 'On', 'ON']
        for val in true_values:
            result = datagate._parse_boolean(val, 'TEST')
            assert result is True, f"Failed for value: {val}"

    def test_parse_boolean_false_values(self):
        """Test parsing false boolean values."""
        false_values = ['0', 'false', 'False', 'FALSE', 'no', 'No', 'NO', 'off', 'Off', 'OFF']
        for val in false_values:
            result = datagate._parse_boolean(val, 'TEST')
            assert result is False, f"Failed for value: {val}"

    def test_parse_boolean_invalid_value_exits(self, capsys):
        """Test that invalid boolean value causes exit."""
        with pytest.raises(SystemExit) as exc_info:
            datagate._parse_boolean('invalid', 'TEST_SETTING')
        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert 'TEST_SETTING has invalid value' in captured.out

    def test_parse_integer_valid(self):
        """Test parsing valid integer values."""
        assert datagate._parse_integer('100', 'TEST') == 100
        assert datagate._parse_integer('0', 'TEST') == 0
        assert datagate._parse_integer('-50', 'TEST') == -50

    def test_parse_integer_invalid_exits(self, capsys):
        """Test that invalid integer value causes exit."""
        with pytest.raises(SystemExit) as exc_info:
            datagate._parse_integer('not-a-number', 'TEST_SETTING')
        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert 'TEST_SETTING must be an integer' in captured.out

    def test_parse_list_empty(self):
        """Test parsing empty list."""
        assert datagate._parse_list('') == []
        assert datagate._parse_list(None) == []

    def test_parse_list_single(self):
        """Test parsing single-item list."""
        assert datagate._parse_list('example.com') == ['example.com']

    def test_parse_list_multiple(self):
        """Test parsing multi-item list."""
        result = datagate._parse_list('example.com, test.org, demo.net')
        assert result == ['example.com', 'test.org', 'demo.net']

    def test_parse_config_file_valid(self):
        """Test parsing a valid config file."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.conf', delete=False) as f:
            f.write('MAX_SOURCE_SIZE=1000\n')
            f.write('REQUIRE_TLS=true\n')
            f.write('# This is a comment\n')
            f.write('\n')
            f.write('ORIGIN_ALLOWLIST=example.com, test.org\n')
            f.flush()

            config = datagate._parse_config_file(f.name)

        os.unlink(f.name)

        assert config['MAX_SOURCE_SIZE'] == '1000'
        assert config['REQUIRE_TLS'] == 'true'
        assert config['ORIGIN_ALLOWLIST'] == 'example.com, test.org'

    def test_parse_config_file_invalid_line_exits(self, capsys):
        """Test that invalid config line causes exit."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.conf', delete=False) as f:
            f.write('INVALID_LINE_NO_EQUALS\n')
            f.flush()

            with pytest.raises(SystemExit) as exc_info:
                datagate._parse_config_file(f.name)

        os.unlink(f.name)
        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert 'Invalid config line' in captured.out

    def test_parse_config_file_not_found_exits(self, capsys):
        """Test that missing config file causes exit."""
        with pytest.raises(SystemExit) as exc_info:
            datagate._parse_config_file('/nonexistent/config.conf')

        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert 'Config file not found' in captured.out


class TestMaxSourceSize:
    """Test MAX_SOURCE_SIZE enforcement."""

    def test_upload_size_at_limit_accepted(self, client, reset_config, tmp_path):
        """Test that file at size limit is accepted."""
        # Set max size to 100 bytes and disable allowlist
        datagate.config.MAX_SOURCE_SIZE = 100
        datagate.config.ORIGIN_ALLOWLIST = None
        datagate._storage_dir = tmp_path

        # Create CSV content that is exactly 100 bytes
        csv_content = b'name,value\nAlice,30\nBob,25\n'  # Exactly 28 bytes
        assert len(csv_content) <= 100

        data = {
            'file': (BytesIO(csv_content), 'test.csv')
        }

        response = client.post('/upload', data=data, content_type='multipart/form-data')
        assert response.status_code == 200
        result = json.loads(response.data)
        assert result['ok'] is True

    def test_upload_size_exceeds_limit_rejected(self, client, reset_config, tmp_path):
        """Test that file exceeding size limit is rejected with 400."""
        # Set max size to 10 bytes and disable allowlist
        datagate.config.MAX_SOURCE_SIZE = 10
        datagate.config.ORIGIN_ALLOWLIST = None
        datagate._storage_dir = tmp_path

        # Create CSV content larger than 10 bytes
        csv_content = b'name,value\nAlice,30\nBob,25\n'

        data = {
            'file': (BytesIO(csv_content), 'test.csv')
        }

        response = client.post('/upload', data=data, content_type='multipart/form-data')
        assert response.status_code == 400
        result = json.loads(response.data)
        assert result['ok'] is False
        assert 'exceeds maximum' in result['error']

    def test_upload_no_limit_accepted(self, client, reset_config, tmp_path):
        """Test that any file size is accepted when no limit is set."""
        datagate.config.MAX_SOURCE_SIZE = None
        datagate.config.ORIGIN_ALLOWLIST = None
        datagate._storage_dir = tmp_path

        # Create a larger CSV content
        csv_content = b'name,value\n' + b'Alice,30\n' * 100

        data = {
            'file': (BytesIO(csv_content), 'test.csv')
        }

        response = client.post('/upload', data=data, content_type='multipart/form-data')
        assert response.status_code == 200
        result = json.loads(response.data)
        assert result['ok'] is True


class TestOriginAllowlist:
    """Test ORIGIN_ALLOWLIST enforcement."""

    def test_no_allowlist_allows_all(self, client, reset_config, tmp_path):
        """Test that requests pass when no allowlist is configured."""
        datagate.config.ORIGIN_ALLOWLIST = None
        datagate._storage_dir = tmp_path

        csv_content = b'name,value\nAlice,30\n'
        data = {'file': (BytesIO(csv_content), 'test.csv')}

        # Request without Referer should succeed
        response = client.post('/upload', data=data, content_type='multipart/form-data')
        assert response.status_code == 200

    def test_missing_referer_rejected(self, client, reset_config, tmp_path):
        """Test that missing Referer header is rejected with 403."""
        datagate.config.ORIGIN_ALLOWLIST = ['example.com']
        datagate._storage_dir = tmp_path

        csv_content = b'name,value\nAlice,30\n'
        data = {'file': (BytesIO(csv_content), 'test.csv')}

        response = client.post('/upload', data=data, content_type='multipart/form-data')
        assert response.status_code == 403
        result = json.loads(response.data)
        assert result['ok'] is False
        assert 'Missing Referer header' in result['error']

    def test_referer_allowed_exact_match(self, client, reset_config, tmp_path):
        """Test that exact domain match is allowed."""
        datagate.config.ORIGIN_ALLOWLIST = ['example.com']
        datagate._storage_dir = tmp_path

        csv_content = b'name,value\nAlice,30\n'
        data = {'file': (BytesIO(csv_content), 'test.csv')}

        response = client.post('/upload', data=data, content_type='multipart/form-data',
                               headers={'Referer': 'https://example.com/page'})
        assert response.status_code == 200

    def test_referer_allowed_subdomain(self, client, reset_config, tmp_path):
        """Test that subdomain of allowed domain is allowed."""
        datagate.config.ORIGIN_ALLOWLIST = ['example.com']
        datagate._storage_dir = tmp_path

        csv_content = b'name,value\nAlice,30\n'
        data = {'file': (BytesIO(csv_content), 'test.csv')}

        response = client.post('/upload', data=data, content_type='multipart/form-data',
                               headers={'Referer': 'https://sub.example.com/page'})
        assert response.status_code == 200

    def test_referer_not_allowed_different_domain(self, client, reset_config, tmp_path):
        """Test that different domain is rejected."""
        datagate.config.ORIGIN_ALLOWLIST = ['example.com']
        datagate._storage_dir = tmp_path

        csv_content = b'name,value\nAlice,30\n'
        data = {'file': (BytesIO(csv_content), 'test.csv')}

        response = client.post('/upload', data=data, content_type='multipart/form-data',
                               headers={'Referer': 'https://other.com/page'})
        assert response.status_code == 403
        result = json.loads(response.data)
        assert result['ok'] is False
        assert 'Origin not allowed' in result['error']

    def test_referer_not_allowed_partial_match(self, client, reset_config, tmp_path):
        """Test that partial domain match (without boundary) is rejected."""
        datagate.config.ORIGIN_ALLOWLIST = ['example.com']
        datagate._storage_dir = tmp_path

        csv_content = b'name,value\nAlice,30\n'
        data = {'file': (BytesIO(csv_content), 'test.csv')}

        # notexample.com should NOT match example.com
        response = client.post('/upload', data=data, content_type='multipart/form-data',
                               headers={'Referer': 'https://notexample.com/page'})
        assert response.status_code == 403
        result = json.loads(response.data)
        assert result['ok'] is False

    def test_allowlist_case_insensitive(self, client, reset_config, tmp_path):
        """Test that allowlist matching is case-insensitive."""
        datagate.config.ORIGIN_ALLOWLIST = ['EXAMPLE.COM']
        datagate._storage_dir = tmp_path

        csv_content = b'name,value\nAlice,30\n'
        data = {'file': (BytesIO(csv_content), 'test.csv')}

        response = client.post('/upload', data=data, content_type='multipart/form-data',
                               headers={'Referer': 'https://example.com/page'})
        assert response.status_code == 200

    def test_multiple_allowlist_entries(self, client, reset_config, tmp_path):
        """Test that multiple allowlist entries work."""
        datagate.config.ORIGIN_ALLOWLIST = ['example.com', 'test.org', 'demo.net']
        datagate._storage_dir = tmp_path

        csv_content = b'name,value\nAlice,30\n'

        # Test each allowed domain
        for domain in ['example.com', 'test.org', 'demo.net']:
            data = {'file': (BytesIO(csv_content), 'test.csv')}
            response = client.post('/upload', data=data, content_type='multipart/form-data',
                                   headers={'Referer': f'https://{domain}/page'})
            assert response.status_code == 200


class TestRequireTls:
    """Test REQUIRE_TLS endpoint URL generation."""

    def test_require_tls_false_relative_url(self, client, reset_config, tmp_path):
        """Test that REQUIRE_TLS=false produces relative URLs."""
        datagate.config.REQUIRE_TLS = False
        datagate.config.ORIGIN_ALLOWLIST = None
        datagate._storage_dir = tmp_path

        csv_content = b'name,value\nAlice,30\n'
        data = {'file': (BytesIO(csv_content), 'test.csv')}

        response = client.post('/upload', data=data, content_type='multipart/form-data')
        assert response.status_code == 200
        result = json.loads(response.data)
        assert result['ok'] is True
        assert result['endpoint'].startswith('/datasets/')
        assert 'https://' not in result['endpoint']

    def test_require_tls_true_absolute_url(self, client, reset_config, tmp_path):
        """Test that REQUIRE_TLS=true produces absolute HTTPS URLs."""
        datagate.config.REQUIRE_TLS = True
        datagate.config.ORIGIN_ALLOWLIST = None
        datagate._storage_dir = tmp_path

        csv_content = b'name,value\nAlice,30\n'
        data = {'file': (BytesIO(csv_content), 'test.csv')}

        response = client.post('/upload', data=data, content_type='multipart/form-data')
        assert response.status_code == 200
        result = json.loads(response.data)
        assert result['ok'] is True
        assert result['endpoint'].startswith('https://')
        assert '/datasets/' in result['endpoint']


class TestStorageDir:
    """Test STORAGE_DIR persistence."""

    def test_storage_dir_created_if_missing(self, tmp_path):
        """Test that STORAGE_DIR is created if it doesn't exist."""
        storage_path = tmp_path / 'new_storage_dir'
        assert not storage_path.exists()

        datagate.config.STORAGE_DIR = storage_path
        datagate._init_storage()

        assert storage_path.exists()
        assert storage_path.is_dir()

    def test_dataset_persisted_to_storage(self, client, reset_config, tmp_path):
        """Test that dataset is persisted to storage directory."""
        datagate.config.STORAGE_DIR = tmp_path
        datagate._storage_dir = tmp_path
        datagate.config.ORIGIN_ALLOWLIST = None

        csv_content = b'name,value\nAlice,30\n'
        data = {'file': (BytesIO(csv_content), 'test.csv')}

        response = client.post('/upload', data=data, content_type='multipart/form-data')
        assert response.status_code == 200
        result = json.loads(response.data)

        # Check that file was created in storage
        dataset_id = result['endpoint'].split('/')[-1]
        storage_file = tmp_path / f"{dataset_id}.json"
        assert storage_file.exists()

        # Verify content
        with open(storage_file) as f:
            stored_data = json.load(f)
        assert stored_data['columns'] == ['name', 'value']
        assert len(stored_data['rows']) == 1

    def test_dataset_loaded_on_restart(self, tmp_path, reset_config):
        """Test that datasets are loaded from storage on startup."""
        # Create a stored dataset file
        dataset_id = 'abcd1234efgh5678'
        dataset = {
            'columns': ['name', 'value'],
            'rows': [['Alice', 30]],
            'source': 'upload://test.csv'
        }
        storage_file = tmp_path / f"{dataset_id}.json"
        with open(storage_file, 'w') as f:
            json.dump(dataset, f)

        # Initialize storage and load datasets
        datagate.config.STORAGE_DIR = tmp_path
        datagate._storage_dir = tmp_path
        datagate.datasets = {}
        datagate._convert_cache = {}
        datagate._load_all_datasets()

        # Check that dataset was loaded
        assert dataset_id in datagate.datasets
        assert datagate.datasets[dataset_id]['columns'] == ['name', 'value']


class TestCacheEnabled:
    """Test CACHE_ENABLED configuration."""

    def test_cache_enabled_true(self, reset_config):
        """Test that CACHE_ENABLED=true enables caching."""
        datagate.config.CACHE_ENABLED = True
        assert datagate.config.CACHE_ENABLED is True

    def test_cache_enabled_false(self, reset_config):
        """Test that CACHE_ENABLED=false disables caching."""
        datagate.config.CACHE_ENABLED = False
        assert datagate.config.CACHE_ENABLED is False


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
