import sys
sys.path.insert(0, '.')

def match_pattern(path, pattern):
    path = path.replace('\\', '/')
    pattern = pattern.replace('\\', '/')
    return _match_glob(path, pattern, 0, 0)

def _match_glob(path, pattern, p, pt):
    if pt + 1 < len(pattern) and pattern[pt:pt+2] == '**':
        return _match_star_star(path, pattern, p, pt)
    if pt < len(pattern) and pattern[pt] == '*':
        return _match_single_star(path, pattern, p, pt)
    if pt < len(pattern) and pattern[pt] == '?':
        if p >= len(path) or path[p] == '/':
            return False
        return _match_glob(path, pattern, p + 1, pt + 1)
    if pt < len(pattern) and pattern[pt] == '[':
        return _match_char_class_bracket(path, pattern, p, pt)
    if p >= len(path):
        return pt >= len(pattern)
    if pattern[pt] != path[p]:
        return False
    return _match_glob(path, pattern, p + 1, pt + 1)

def _match_star_star(path, pattern, p, pt):
    # Try matching ** as empty (move past it)
    if _match_glob(path, pattern, p, pt + 2):
        return True
    # Try matching at least one character
    if p < len(path):
        # Stay in ** to match more
        if _match_star_star(path, pattern, p + 1, pt):
            return True
        # Exit ** after matching one
        if _match_glob(path, pattern, p + 1, pt + 2):
            return True
    return False

def _match_single_star(path, pattern, p, pt):
    # Try matching empty
    if _match_glob(path, pattern, p, pt + 1):
        return True
    # Try matching one non-/ char
    if p < len(path) and path[p] != '/' and _match_single_star(path, pattern, p + 1, pt):
        return True
    return False

def _match_char_class_bracket(path, pattern, p, pt):
    end = pattern.find(']', pt)
    if end == -1:
        if p >= len(path) or path[p] != '[':
            return False
        return _match_glob(path, pattern, p + 1, pt + 1)
    if p >= len(path):
        return False
    charset = pattern[pt+1:end]
    if not charset:
        return False
    if not _match_char_class(path[p], charset):
        return False
    return _match_glob(path, pattern, p + 1, end + 1)

def _match_char_class(ch, charset):
    if not charset:
        return False
    negate = charset[0] == '!'
    if negate:
        charset = charset[1:]
    match = False
    i = 0
    while i < len(charset):
        if i + 2 < len(charset) and charset[i+1] == '-':
            start, end = charset[i], charset[i+2]
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

# Test cases
tests = [
    ('A/I.py', 'A/*', True, 'Direct child'),
    ('A/B/C/D.py', 'A/*', False, 'Not direct child'),
    ('A/B.py', 'A/*', False, 'Not direct child (file directly under A/B)'),
    ('E.bin', '**/*.bin', True, 'Root-level .bin'),
    ('F/G.bin', '**/*.bin', True, 'Nested .bin'),
    ('A/B/E.bin', '**/*.bin', True, 'Deep path .bin'),
    ('A/I.py', '**/*.bin', False, 'Not .bin'),
    ('a/b/c', '**/c', True, 'Ends with c'),
    ('a/b/c', '**/b/**', True, 'Has b'),
    ('a/b', '**/**', True, 'Double star'),
    ('A1.py', 'A[0-9].py', True, 'Digit class'),
    ('Az.py', 'A[!z].py', False, 'Negated class'),
    ('Ax.py', 'A[!z].py', True, 'Negated class'),
]

for path, pattern, expected, desc in tests:
    result = match_pattern(path, pattern)
    status = 'PASS' if result == expected else 'FAIL'
    print(f'{status}: {path} vs {pattern} -> {result} (expected {expected}) - {desc}')
