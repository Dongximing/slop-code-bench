#!/usr/bin/env python3
"""Test pagination, sorting, and response controls using test client."""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import json

# Mock the CSV server responses
import unittest
from unittest.mock import patch, MagicMock
import requests

# We need to import datagate after mocking requests
import datagate
datagate.app.config['TESTING'] = True
client = datagate.app.test_client()


class TestPaginationSortingControls(unittest.TestCase):
    """Test pagination, sorting, and response controls."""

    def setUp(self):
        """Set up test data by converting a CSV."""
        # Mock the requests.get to return valid CSV
        self.csv_content = """name,age,city,salary
John,25,NYC,50000.50
Jane,30,LA,60000.75
Bob,35,Chicago,70000
Alice,28,Boston,55000.25
Charlie,40,Miami,80000.00
Dave,22,Seattle,45000
Eve,33,Denver,58000.50
Frank,27,Austin,52000.75
Grace,29,Phoenix,54000.25
Henry,31,Dallas,57000.10
Ivan,44,Portland,65000.50
Judy,26,Sacramento,48000.25"""

        with patch('datagate.requests.get') as mock_get:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.content = self.csv_content.encode('utf-8')
            mock_get.return_value = mock_response

            # Convert the CSV
            response = client.get('/convert?source=http://example.com/test.csv')
            self.assertEqual(response.status_code, 200)
            result = response.get_json()
            self.assertTrue(result['ok'])
            self.dataset_id = result['endpoint'].split('/')[-1]

    def test_pagination_size(self):
        """Test _size parameter."""
        response = client.get(f'/datasets/{self.dataset_id}?_size=5')
        self.assertEqual(response.status_code, 200)
        result = response.get_json()
        self.assertEqual(len(result['rows']), 5)
        self.assertIn('total', result)
        self.assertEqual(result['total'], 12)

    def test_pagination_offset(self):
        """Test _offset parameter."""
        response = client.get(f'/datasets/{self.dataset_id}?_size=3&_offset=5')
        self.assertEqual(response.status_code, 200)
        result = response.get_json()
        self.assertEqual(len(result['rows']), 3)
        # Row at offset 5 should be Dave (index 5 in 0-based)
        self.assertEqual(result['rows'][0][0], 'Dave')

    def test_size_larger_than_rows(self):
        """Test _size larger than available rows."""
        response = client.get(f'/datasets/{self.dataset_id}?_size=1000')
        self.assertEqual(response.status_code, 200)
        result = response.get_json()
        self.assertEqual(len(result['rows']), 12)

    def test_invalid_size_not_number(self):
        """Test invalid _size (not a number)."""
        response = client.get(f'/datasets/{self.dataset_id}?_size=abc')
        self.assertEqual(response.status_code, 400)
        result = response.get_json()
        self.assertFalse(result['ok'])

    def test_invalid_size_negative(self):
        """Test invalid _size (negative)."""
        response = client.get(f'/datasets/{self.dataset_id}?_size=-5')
        self.assertEqual(response.status_code, 400)

    def test_invalid_size_zero(self):
        """Test invalid _size (zero)."""
        response = client.get(f'/datasets/{self.dataset_id}?_size=0')
        self.assertEqual(response.status_code, 400)

    def test_invalid_offset_negative(self):
        """Test invalid _offset (negative)."""
        response = client.get(f'/datasets/{self.dataset_id}?_offset=-5')
        self.assertEqual(response.status_code, 400)

    def test_invalid_offset_not_number(self):
        """Test invalid _offset (not a number)."""
        response = client.get(f'/datasets/{self.dataset_id}?_offset=abc')
        self.assertEqual(response.status_code, 400)

    def test_offset_beyond_rows(self):
        """Test offset beyond available rows."""
        response = client.get(f'/datasets/{self.dataset_id}?_offset=100')
        self.assertEqual(response.status_code, 200)
        result = response.get_json()
        self.assertEqual(len(result['rows']), 0)

    def test_sort_ascending(self):
        """Test _sort ascending."""
        response = client.get(f'/datasets/{self.dataset_id}?_sort=name')
        self.assertEqual(response.status_code, 200)
        result = response.get_json()
        # Sorted ascending: Alice should be first
        self.assertEqual(result['rows'][0][0], 'Alice')
        # John should be last
        self.assertEqual(result['rows'][-1][0], 'John')

    def test_sort_descending(self):
        """Test _sort_desc descending."""
        response = client.get(f'/datasets/{self.dataset_id}?_sort_desc=name')
        self.assertEqual(response.status_code, 200)
        result = response.get_json()
        # Sorted descending: John should be first
        self.assertEqual(result['rows'][0][0], 'John')
        # Alice should be last
        self.assertEqual(result['rows'][-1][0], 'Alice')

    def test_sort_desc_wins_both_present(self):
        """Test that _sort_desc wins when both present."""
        response = client.get(f'/datasets/{self.dataset_id}?_sort=name&_sort_desc=name')
        self.assertEqual(response.status_code, 200)
        result = response.get_json()
        # Should be descending because _sort_desc wins
        self.assertEqual(result['rows'][0][0], 'John')

    def test_sort_numeric_column(self):
        """Test sorting on numeric column."""
        response = client.get(f'/datasets/{self.dataset_id}?_sort=age')
        self.assertEqual(response.status_code, 200)
        result = response.get_json()
        # Dave with age 22 should be first
        self.assertEqual(result['rows'][0][1], 22)
        # Ivan with age 44 should be last
        self.assertEqual(result['rows'][-1][1], 44)

    def test_invalid_sort_unknown_column(self):
        """Test invalid _sort (unknown column)."""
        response = client.get(f'/datasets/{self.dataset_id}?_sort=unknown')
        self.assertEqual(response.status_code, 400)

    def test_invalid_sort_desc_unknown_column(self):
        """Test invalid _sort_desc (unknown column)."""
        response = client.get(f'/datasets/{self.dataset_id}?_sort_desc=unknown')
        self.assertEqual(response.status_code, 400)

    def test_empty_sort_column(self):
        """Test empty sort column."""
        response = client.get(f'/datasets/{self.dataset_id}?_sort=')
        self.assertEqual(response.status_code, 400)

    def test_shape_lists(self):
        """Test _shape=lists."""
        response = client.get(f'/datasets/{self.dataset_id}?_shape=lists')
        self.assertEqual(response.status_code, 200)
        result = response.get_json()
        self.assertIsInstance(result['rows'][0], list)
        self.assertNotIn('rowid', result['rows'][0])

    def test_shape_objects(self):
        """Test _shape=objects."""
        response = client.get(f'/datasets/{self.dataset_id}?_shape=objects')
        self.assertEqual(response.status_code, 200)
        result = response.get_json()
        self.assertIsInstance(result['rows'][0], dict)
        self.assertIn('rowid', result['rows'][0])
        self.assertIn('name', result['rows'][0])

    def test_shape_objects_rowid_correctness(self):
        """Test rowid in objects shape is 1-based original row number."""
        response = client.get(f'/datasets/{self.dataset_id}?_shape=objects')
        self.assertEqual(response.status_code, 200)
        result = response.get_json()
        # First row should have rowid=1
        self.assertEqual(result['rows'][0]['rowid'], 1)
        # Last row should have rowid=12
        self.assertEqual(result['rows'][11]['rowid'], 12)

    def test_invalid_shape(self):
        """Test invalid _shape."""
        response = client.get(f'/datasets/{self.dataset_id}?_shape=invalid')
        self.assertEqual(response.status_code, 400)

    def test_rowid_hide_with_objects(self):
        """Test _rowid=hide with objects shape."""
        response = client.get(f'/datasets/{self.dataset_id}?_shape=objects&_rowid=hide')
        self.assertEqual(response.status_code, 200)
        result = response.get_json()
        self.assertNotIn('rowid', result['rows'][0])
        self.assertIn('name', result['rows'][0])

    def test_invalid_rowid_value(self):
        """Test invalid _rowid value."""
        response = client.get(f'/datasets/{self.dataset_id}?_rowid=show')
        self.assertEqual(response.status_code, 400)

    def test_total_hide(self):
        """Test _total=hide."""
        response = client.get(f'/datasets/{self.dataset_id}?_total=hide')
        self.assertEqual(response.status_code, 200)
        result = response.get_json()
        self.assertNotIn('total', result)

    def test_invalid_total_value(self):
        """Test invalid _total value."""
        response = client.get(f'/datasets/{self.dataset_id}?_total=show')
        self.assertEqual(response.status_code, 400)

    def test_repeated_size_parameter(self):
        """Test repeated _size parameter."""
        response = client.get(f'/datasets/{self.dataset_id}?_size=5&_size=10')
        self.assertEqual(response.status_code, 400)
        result = response.get_json()
        self.assertIn('repeated', result.get('error', '').lower())

    def test_repeated_offset_parameter(self):
        """Test repeated _offset parameter."""
        response = client.get(f'/datasets/{self.dataset_id}?_offset=5&_offset=10')
        self.assertEqual(response.status_code, 400)

    def test_repeated_shape_parameter(self):
        """Test repeated _shape parameter."""
        response = client.get(f'/datasets/{self.dataset_id}?_shape=lists&_shape=objects')
        self.assertEqual(response.status_code, 400)

    def test_combined_sort_pagination(self):
        """Test sorting with pagination."""
        response = client.get(f'/datasets/{self.dataset_id}?_sort=name&_size=3&_offset=2')
        self.assertEqual(response.status_code, 200)
        result = response.get_json()
        self.assertEqual(len(result['rows']), 3)
        # After sorting by name, offset 2 should start with Charlie
        self.assertEqual(result['rows'][0][0], 'Charlie')

    def test_combined_all_parameters(self):
        """Test all parameters combined."""
        response = client.get(f'/datasets/{self.dataset_id}?_sort=name&_size=2&_offset=1&_shape=objects')
        self.assertEqual(response.status_code, 200)
        result = response.get_json()
        self.assertEqual(len(result['rows']), 2)
        # Object shape with sort+paginate
        self.assertIsInstance(result['rows'][0], dict)
        self.assertIn('name', result['rows'][0])

    def test_combined_total_hide(self):
        """Test combined with _total=hide."""
        response = client.get(f'/datasets/{self.dataset_id}?_sort=age&_size=5&_total=hide')
        self.assertEqual(response.status_code, 200)
        result = response.get_json()
        self.assertNotIn('total', result)

    def test_combined_rowid_hide(self):
        """Test combined with _rowid=hide (with objects)."""
        response = client.get(f'/datasets/{self.dataset_id}?_sort=name&_size=2&_shape=objects&_rowid=hide')
        self.assertEqual(response.status_code, 200)
        result = response.get_json()
        self.assertNotIn('rowid', result['rows'][0])


if __name__ == '__main__':
    unittest.main()
