import re
import sys

sys.path.insert(0, '.')

def match_pattern(path: str, pattern: str) -> bool:
    path = path.replace('\\', '/')
    pattern = pattern.replace('\\', '/')
    if pattern == '**':
        return True
    return _match_glob_recursive(path, pattern, 0, 0)


def _match_glob_recursive(path: str, pattern: str, p_i: int, pt_i: int) -> bool:
    if pt_i >= len(pattern):
        return p_i >= len(path)

    if pt_i + 1 < len(pattern) and pattern[pt_i:pt_i+2] == '**':
        if _match_glob_recursive(path, pattern, p_i, pt_i + 2):
            return True
        if p_i < len(path):
            if _match_glob_recursive(path, pattern, p_i + 1, pt_i):
                return True
            if _match_glob_recursive(path, pattern, p_i + 1, pt_i + 2):
                return True
        return False

    if pt_i < len(pattern) and pattern[pt_i] == '[':
        end = pattern.find(']', pt_i)
        if end == -1:
            if p_i >= len(path) or path[p_i] != '[':
                return False
            return _match_glob_recursive(path, pattern, p_i + 1, pt_i + 1)

        if p_i >= len(path):
            return False

        if not _match_char_class(path[p_i], pattern[pt_i+1:end]):
            return False

        return _match_glob_recursive(path, pattern, p_i + 1, end + 1)

    if pt_i < len(pattern) and pattern[pt_i] == '?':
        if p_i >= len(path) or path[p_i] == '/':
            return False
        return _match_glob_recursive(path, pattern, p_i + 1, pt_i + 1)

    if pt_i < len(pattern) and pattern[pt_i] == '*':
        if _match_glob_recursive(path, pattern, p_i, pt_i + 1):
            return True
        if p_i < len(path) and path[p_i] != '/' and _match_glob_recursive(path, pattern, p_i + 1, pt_i):
            return True
        return False

    if p_i >= len(path):
        return False
    if path[p_i] != pattern[pt_i]:
        return False
    return _match_glob_recursive(path, pattern, p_i + 1, pt_i + 1)


def _match_char_class(ch: str, charset: str) -> bool:
    if not charset:
        return False
    negate = False
    if charset[0] == '!':
        negate = True
        charset = charset[1:]
    match = False
    i = 0
    while i < len(charset):
        if i + 2 < len(charset) and charset[i+1] == '-':
            start = charset[i]
            end = charset[i+2]
            if start <= ch <= end:
                match = True
                break
            i += 3
        else:
            if charset[i] == ch:
                match = True
                break
            i += 1
    if negate:
        match = not match
    return match

# Test
print("Testing A/* vs A/B.py")
print("Expected: False (should NOT match - nested)")
result = _match_glob_recursive('A/B.py', 'A/*', 0, 0)
print(f"Result: {result}")

print("\nTesting **/*.bin vs E.bin")
print("Expected: True")
result = _match_glob_recursive('E.bin', '**/*.bin', 0, 0)
print(f"Result: {result}")

print("\nTesting */*.bin vs E.bin")
print("Expected: False (no / in E.bin)")
result = _match_glob_recursive('E.bin', '*/*.bin', 0, 0)
print(f"Result: {result}")
