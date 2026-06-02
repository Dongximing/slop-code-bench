"""Minimal Rego-like policy evaluator."""

import json
import re
import math


class RegoParser:
    @staticmethod
    def parse(code):
        rules = []
        lines = code.split('\n')
        i = 0

        while i < len(lines):
            line = lines[i].strip()
            i += 1

            if not line or line.startswith('#'):
                continue

            if line.startswith('package '):
                continue
            if line.startswith('import '):
                continue

            # deny/warn contains {...} if { ... }
            m = re.match(r'^(deny|warn)\s+contains\s+(\{[^}]*\})\s+if\s*\{', line)
            if m:
                kind = m.group(1)
                try:
                    violation_obj = RegoParser._parse_rego_object(m.group(2))
                except Exception:
                    violation_obj = {"raw": m.group(2)}
                body = _collect_body(lines, i)
                i += len(body) + 1
                rules.append({"kind": kind, "var_name": "violation",
                               "violation_template": violation_obj, "body": body})
                continue

            # deny[violation] { ... } or warn[violation] { ... }
            m = re.match(r'^(deny|warn)\s*\[(\w+)\]\s*\{', line)
            if m:
                body = _collect_body(lines, i)
                i += len(body) + 1
                rules.append({"kind": m.group(1), "var_name": m.group(2), "body": body})
                continue

            # Complete rule with 'if': name if { body }
            m = re.match(r'^(\w+)\s+if\s*\{', line)
            if m:
                body = _collect_body(lines, i)
                i += len(body) + 1
                rules.append({"kind": "complete", "name": m.group(1),
                               "default_value": None, "body": body})
                continue

            # Complete rule: name = value { body } or name { body }
            m = re.match(r'^(\w+)\s*(?:=\s*([^{\n]+))?\s*\{', line)
            if m:
                default_val = m.group(2).strip() if m.group(2) else None
                body = _collect_body(lines, i)
                i += len(body) + 1
                rules.append({"kind": "complete", "name": m.group(1),
                               "default_value": default_val, "body": body})
                continue

            # Assignment: var := expr
            m = re.match(r'^(\w+)\s*:?=\s*(.+)$', line)
            if m:
                rules.append({"kind": "assignment", "name": m.group(1), "expr": m.group(2).strip()})

        return rules

    @staticmethod
    def _parse_rego_object(obj_str):
        result = {}
        obj_str = obj_str.strip()
        if obj_str.startswith('{') and obj_str.endswith('}'):
            obj_str = obj_str[1:-1]

        parts = []
        current = ""
        depth = 0
        for char in obj_str:
            if char in ('{', '['):
                depth += 1
            elif char in ('}', ']'):
                depth -= 1
            elif char == ',' and depth == 0:
                parts.append(current.strip())
                current = ""
                continue
            current += char
        if current.strip():
            parts.append(current.strip())

        for part in parts:
            if ':' in part:
                key, val = part.split(':', 1)
                key = key.strip().strip('"')
                val = val.strip()
                if val.startswith('"') and val.endswith('"'):
                    result[key] = val[1:-1]
                elif val.startswith("'") and val.endswith("'"):
                    result[key] = val[1:-1]
                elif val == 'true':
                    result[key] = True
                elif val == 'false':
                    result[key] = False
                elif val == 'null':
                    result[key] = None
                else:
                    try:
                        result[key] = int(val)
                    except ValueError:
                        try:
                            result[key] = float(val)
                        except ValueError:
                            result[key] = val
        return result


def _collect_body(lines, start_i):
    """Collect lines of a brace-delimited body starting at index start_i."""
    body = []
    depth = 1
    i = start_i
    while i < len(lines) and depth > 0:
        l = lines[i].strip()
        depth += l.count('{') - l.count('}')
        if depth > 0:
            body.append(l)
        else:
            remaining = l[:-1].strip() if l.endswith('}') else l
            if remaining:
                body.append(remaining)
        i += 1
    return body


class RegoEvaluator:
    @staticmethod
    def eval_set_rule(rule, ctx):
        body = rule.get("body", [])
        var_name = rule.get("var_name", "violation")
        violation_template = rule.get("violation_template")

        local_vars = {}
        assignment_expr = None

        for line in body:
            line = line.strip()
            if not line:
                continue
            m = re.match(r'^(\w+)\s*:?=\s*(.+)$', line)
            if m:
                lname, lexpr = m.group(1), m.group(2).strip()
                if lname == var_name:
                    assignment_expr = lexpr
                else:
                    local_vars[lname] = RegoEvaluator._eval_expr(lexpr, ctx, local_vars)
                continue
            if not RegoEvaluator._eval_condition(line, ctx, local_vars):
                return []

        if violation_template:
            return [violation_template.copy()]
        if assignment_expr:
            val = RegoEvaluator._eval_expr(assignment_expr, ctx, local_vars)
            return val if isinstance(val, list) else [val]
        return [{}]

    @staticmethod
    def eval_complete_rule(rule, ctx):
        body = rule.get("body", [])
        if not body:
            default = rule.get("default_value")
            return RegoEvaluator._eval_expr(default, ctx, {}) if default is not None else True

        local_vars = {}
        for line in body:
            line = line.strip()
            if not line:
                continue
            m = re.match(r'^(\w+)\s*:?=\s*(.+)$', line)
            if m:
                local_vars[m.group(1)] = RegoEvaluator._eval_expr(m.group(2).strip(), ctx, local_vars)
                continue
            if not RegoEvaluator._eval_condition(line, ctx, local_vars):
                return None

        default = rule.get("default_value")
        return RegoEvaluator._eval_expr(default, ctx, local_vars) if default is not None else True

    @staticmethod
    def _eval_condition(expr, ctx, local_vars):
        expr = expr.strip()
        if expr.startswith('not '):
            return not RegoEvaluator._eval_truthy(expr[4:].strip(), ctx, local_vars)
        return RegoEvaluator._eval_truthy(expr, ctx, local_vars)

    @staticmethod
    def _eval_truthy(expr, ctx, local_vars):
        expr = expr.strip()
        for op in ['!=', '==', '>=', '<=', '>', '<']:
            idx = _find_operator(expr, op)
            if idx >= 0:
                left = RegoEvaluator._eval_expr(expr[:idx].strip(), ctx, local_vars)
                right = RegoEvaluator._eval_expr(expr[idx + len(op):].strip(), ctx, local_vars)
                return _compare(left, right, op)
        return bool(RegoEvaluator._eval_expr(expr, ctx, local_vars))

    @staticmethod
    def _eval_expr(expr, ctx, local_vars):
        expr = expr.strip()
        if not expr:
            return None

        # String literal
        if (expr.startswith('"') and expr.endswith('"')) or \
           (expr.startswith("'") and expr.endswith("'")):
            return expr[1:-1]

        # Boolean/Null
        if expr == 'true':
            return True
        if expr == 'false':
            return False
        if expr == 'null':
            return None

        # Number
        try:
            return float(expr) if '.' in expr else int(expr)
        except (ValueError, TypeError):
            pass

        # Object literal
        if expr.startswith('{') and expr.endswith('}'):
            inner = expr[1:-1].strip()
            return RegoEvaluator._parse_object_literal(inner, ctx, local_vars) if inner else {}

        # Array literal
        if expr.startswith('[') and expr.endswith(']'):
            inner = expr[1:-1].strip()
            if not inner:
                return []
            return [RegoEvaluator._eval_expr(i.strip(), ctx, local_vars)
                    for i in _split_by_comma(inner)]

        # Function call
        m = re.match(r'^(\w+)\s*\((.+)\)$', expr)
        if m:
            args = [RegoEvaluator._eval_expr(a.strip(), ctx, local_vars)
                    for a in _split_by_comma(m.group(2))]
            return _call_function(m.group(1), args)

        # Parenthesized
        if expr.startswith('(') and expr.endswith(')'):
            return RegoEvaluator._eval_expr(expr[1:-1], ctx, local_vars)

        # Reference
        return RegoEvaluator._resolve_ref(expr, ctx, local_vars)

    @staticmethod
    def _parse_object_literal(inner, ctx, local_vars):
        result = {}
        for part in _split_by_comma(inner):
            colon_idx = part.strip().find(':')
            if colon_idx > 0:
                key = RegoEvaluator._eval_expr(part[:colon_idx].strip(), ctx, local_vars)
                val = RegoEvaluator._eval_expr(part[colon_idx+1:].strip(), ctx, local_vars)
                if isinstance(key, str):
                    result[key] = val
        return result

    @staticmethod
    def _resolve_ref(ref, ctx, local_vars):
        parts = ref.split('.')

        # Check local vars
        if parts[0] in local_vars:
            return _traverse(local_vars[parts[0]], parts[1:])

        # Check context
        if parts[0] in ctx:
            return _traverse(ctx[parts[0]], parts[1:])

        return local_vars.get(parts[0]) if len(parts) == 1 else None


def _traverse(val, parts):
    for part in parts:
        if isinstance(val, dict):
            val = val.get(part)
        elif isinstance(val, list):
            try:
                val = val[int(part)]
            except (ValueError, IndexError):
                return None
        else:
            return None
    return val


def _find_operator(expr, op):
    in_string = False
    string_char = None
    paren_depth = 0
    i = 0
    while i < len(expr) - len(op) + 1:
        c = expr[i]
        if in_string:
            if c == string_char and (i == 0 or expr[i-1] != '\\'):
                in_string = False
            i += 1
            continue
        if c in ('"', "'"):
            in_string = True
            string_char = c
            i += 1
            continue
        if c == '(':
            paren_depth += 1
            i += 1
            continue
        if c == ')':
            paren_depth -= 1
            i += 1
            continue
        if paren_depth == 0 and expr[i:i+len(op)] == op:
            if op == '!=' and i + 2 < len(expr) and expr[i+2] == '=':
                i += 1
                continue
            if op == '==' and i > 0 and expr[i-1] == '!':
                i += 1
                continue
            return i
        i += 1
    return -1


def _compare(left, right, op):
    ops = {'==': lambda a, b: a == b, '!=': lambda a, b: a != b,
           '>': lambda a, b: a > b, '<': lambda a, b: a < b,
           '>=': lambda a, b: a >= b, '<=': lambda a, b: a <= b}
    return ops.get(op, lambda a, b: False)(left, right)


def _split_by_comma(s):
    parts = []
    depth = 0
    current = []
    in_string = False
    string_char = None
    for i, c in enumerate(s):
        if in_string:
            current.append(c)
            if c == string_char and (i == 0 or s[i-1] != '\\'):
                in_string = False
            continue
        if c in ('"', "'"):
            in_string = True
            string_char = c
            current.append(c)
            continue
        if c in ('(', '[', '{'):
            depth += 1
            current.append(c)
        elif c in (')', ']', '}'):
            depth -= 1
            current.append(c)
        elif c == ',' and depth == 0:
            parts.append(''.join(current))
            current = []
        else:
            current.append(c)
    if current:
        parts.append(''.join(current))
    return parts


_BUILTIN_FUNCTIONS = {
    'count': lambda args: len(args[0]) if isinstance(args[0], (list, dict, str)) else 0,
    'len': lambda args: len(args[0]) if isinstance(args[0], (list, dict, str)) else 0,
    'max': lambda args: max(args[0], args[1]),
    'min': lambda args: min(args[0], args[1]),
    'concat': lambda args: args[0].join(str(x) for x in (args[1] if isinstance(args[1], list) else [args[1]])),
    'contains': lambda args: args[1] in args[0] if isinstance(args[0], str) else False,
    'lower': lambda args: str(args[0]).lower(),
    'upper': lambda args: str(args[0]).upper(),
    'trim': lambda args: str(args[0]).strip(),
    'sprintf': lambda args: args[0] % tuple(args[1]) if isinstance(args[1], list) and len(args) > 1 else args[0],
    'json_marshal': lambda args: json.dumps(args[0]),
    'object': lambda args: args[0] if isinstance(args[0], dict) else {},
    'array_slice': lambda args: args[0][int(args[1]):int(args[2])] if isinstance(args[0], list) else [],
    'sort': lambda args: sorted(args[0]) if isinstance(args[0], list) else args[0],
    'unique': lambda args: sorted(set(str(x) for x in args[0])) if isinstance(args[0], list) else args[0],
    'abs': lambda args: abs(args[0]),
    'ceil': lambda args: math.ceil(args[0]),
    'floor': lambda args: math.floor(args[0]),
}


def _call_function(name, args):
    fn = _BUILTIN_FUNCTIONS.get(name)
    return fn(args) if fn else None


class RegoEngine:
    @staticmethod
    def evaluate(rego_modules, input_data, bundle_data=None, timeout_ms=500):
        result = {"deny": [], "warn": []}
        ctx = {"input": input_data, "data": bundle_data or {}}

        all_rules = []
        for module_name, module_code in rego_modules.items():
            try:
                all_rules.extend(RegoParser.parse(module_code))
            except Exception:
                continue

        for rule in all_rules:
            kind = rule.get("kind")
            if kind in ("deny", "warn"):
                violations = RegoEvaluator.eval_set_rule(rule, ctx)
                for v in violations:
                    if isinstance(v, dict):
                        result[kind].append(v)
            elif kind == "complete":
                name = rule.get("name", "")
                if name == "prod_target" or name.startswith("_"):
                    continue
                val = RegoEvaluator.eval_complete_rule(rule, ctx)
                parts = name.split('.')
                target = ctx
                for p in parts[:-1]:
                    target = target.setdefault(p, {})
                if parts:
                    target[parts[-1]] = val

        return result
