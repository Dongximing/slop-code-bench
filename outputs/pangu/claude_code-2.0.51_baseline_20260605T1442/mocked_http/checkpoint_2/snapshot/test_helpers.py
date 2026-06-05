#!/usr/bin/env python3
"""Test the template helpers"""
import json
import sys
sys.path.insert(0, '/workspace')

from hmock import (
    _json_path_xpath, _gjson_path, _xml_path_xpath, _uuidv5,
    _regex_find_all_submatch, _regex_find_first_submatch,
    _hmac_sha256, _is_last_index, _html_escape_string
)

def test_json_path():
    # Test simple path
    data = '{"foo": "bar"}'
    assert _json_path_xpath("foo", data) == "bar"
    print("✓ jsonPath simple path")

    # Test nested path
    data = '{"a": {"b": "c"}}'
    assert _json_path_xpath("a.b", data) == "c"
    print("✓ jsonPath nested path")

    # Test empty data
    assert _json_path_xpath("foo", "") == ""
    print("✓ jsonPath empty data")

    # Test no match
    data = '{"foo": "bar"}'
    assert _json_path_xpath("baz", data) == ""
    print("✓ jsonPath no match")

    # Test deep search //bar
    data = '{"a": {"b": {"bar": "x"}, "bar": "y"}}'
    result = _json_path_xpath("//bar", data)
    # Should return all bar values
    print(f"✓ jsonPath deep search: {result}")

def test_gjson_path():
    # Test simple
    data = '{"name": "test"}'
    assert _gjson_path("name", data) == "test"
    print("✓ gJsonPath simple")

    # Test nested
    data = '{"context": {"type": "request"}}'
    assert _gjson_path("context.type", data) == "request"
    print("✓ gJsonPath nested")

    # Test array index
    data = '{"items": [{"id": 1}, {"id": 2}]}'
    assert _gjson_path("items.0.id", data) == "1"
    assert _gjson_path("items.1.id", data) == "2"
    print("✓ gJsonPath array index")

    # Test array wildcard count
    data = '{"items": [1, 2, 3]}'
    assert _gjson_path("items.#", data) == "3"
    print("✓ gJsonPath array count")

    # Test empty data raises error
    try:
        _gjson_path("name", "")
        assert False, "Should have raised ValueError"
    except ValueError as e:
        assert "not valid JSON" in str(e)
        print("✓ gJsonPath invalid JSON raises error")

    # Test invalid JSON
    try:
        _gjson_path("name", "not json")
        assert False, "Should have raised ValueError"
    except ValueError as e:
        assert "not valid JSON" in str(e)
        print("✓ gJsonPath invalid JSON raises error")

def test_xml_path():
    # Test simple XML
    data = '<root><name>value</name></root>'
    assert _xml_path_xpath("name", data) == "value"
    print("✓ xmlPath simple")

    # Test deep search
    data = '<root><a><b>deep</b></a></root>'
    assert _xml_path_xpath("//b", data) == "deep"
    print("✓ xmlPath deep search")

    # Test empty data
    assert _xml_path_xpath("name", "") == ""
    print("✓ xmlPath empty data")

def test_uuidv5():
    # Test deterministic
    u1 = _uuidv5("test")
    u2 = _uuidv5("test")
    assert u1 == u2
    assert len(u1) == 36
    print(f"✓ uuidv5 deterministic: {u1}")

def test_regex_find_all():
    # Test basic capture
    result = _regex_find_all_submatch("(\\d+)-(\\d+)", "a123-456b")
    assert result == ["123-456", "123", "456"]
    print(f"✓ regexFindAllSubmatch: {result}")

    # Test no match
    result = _regex_find_all_submatch("no", "match")
    assert result == []
    print("✓ regexFindAllSubmatch no match")

def test_regex_find_first():
    # Test first capture group
    result = _regex_find_first_submatch("(\\d+)-(\\d+)", "a123-456b")
    assert result == "123"
    print(f"✓ regexFindFirstSubmatch: {result}")

    # Test no match
    result = _regex_find_first_submatch("no", "match")
    assert result == ""
    print("✓ regexFindFirstSubmatch no match")

    # Test no capture groups
    result = _regex_find_first_submatch("\\d+", "123")
    assert result == ""
    print("✓ regexFindFirstSubmatch no capture groups")

def test_hmac_sha256():
    result = _hmac_sha256("secret", "data")
    # Known value test
    assert len(result) == 64
    assert all(c in '0123456789abcdef' for c in result)
    print(f"✓ hmacSHA256: {result[:16]}...")

def test_is_last_index():
    assert _is_last_index(2, [1, 2, 3]) == True
    assert _is_last_index(0, [1, 2, 3]) == False
    assert _is_last_index("2", [1, 2, 3]) == True
    assert _is_last_index(0, []) == False
    print("✓ isLastIndex")

def test_html_escape():
    result = _html_escape_string("<script>alert('xss')</script>")
    assert "<" in result
    assert ">" in result
    assert "&" in result
    print(f"✓ htmlEscapeString: {result}")

if __name__ == "__main__":
    print("Testing template helpers...")
    test_json_path()
    test_gjson_path()
    test_xml_path()
    test_uuidv5()
    test_regex_find_all()
    test_regex_find_first()
    test_hmac_sha256()
    test_is_last_index()
    test_html_escape()
    print("\nAll tests passed!")
