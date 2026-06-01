def match_pattern(path, pattern):
    print(f"\nMatch: '{path}' vs '{pattern}'")
    return _match_glob(path, pattern, 0, 0, "")

def _match_glob(path, pattern, p, pt, indent):
    indent2 = indent + "  "
    
    # Check for ** pattern
    if pt + 1 < len(pattern) and pattern[pt:pt+2] == '**':
        print(f"{indent}** at path[{p}]='{path[p] if p < len(path) else 'EOF'}, pt={pt}")
        result = _match_star_star(path, pattern, p, pt, indent2)
        print(f"{indent}** result: {result}")
        return result

    # Check for * pattern
    if pt < len(pattern) and pattern[pt] == '*':
        result = _match_single_star(path, pattern, p, pt, indent2)
        print(f"{indent}* at path[{p}], pt={pt} => {result}")
        return result

    # Regular character match
    if p >= len(path):
        return pt >= len(pattern)
    if pattern[pt] != path[p]:
        return False
    return _match_glob(path, pattern, p + 1, pt + 1, indent2)

def _match_star_star(path, pattern, p, pt, indent):
    print(f"{indent}** star_star: path[{p}]='{path[p] if p < len(path) else 'EOF'}, pt={pt} pat[pt+2]='{pattern[pt+2] if pt+2 < len(pattern) else 'EOF'}'")

    print(f"{indent}  Try: past **, now pt={pt+2}")
    result = _match_glob(path, pattern, p, pt + 2, indent + "    ")
    if result:
        print(f"{indent}  -> True (empty match)")
        return True

    if p < len(path):
        print(f"{indent}  Try: stay in **")
        result = _match_star_star(path, pattern, p + 1, pt, indent + "    ")
        if result:
            return True

        print(f"{indent}  Try: exit ** after match")
        result = _match_glob(path, pattern, p + 1, pt + 2, indent + "    ")
        if result:
            return True

    print(f"{indent}  -> False")
    return False

def _match_single_star(path, pattern, p, pt, indent):
    result = _match_glob(path, pattern, p, pt + 1, indent)
    if result:
        return True

    if p < len(path) and path[p] != '/' and pt < len(pattern):
        result = _match_single_star(path, pattern, p + 1, pt, indent)
        if result:
            return True

    return False

# Test
match_pattern('E.bin', '**/*.bin')
print("\n" + "=" * 50)
match_pattern('A/I.py', 'A/*')
