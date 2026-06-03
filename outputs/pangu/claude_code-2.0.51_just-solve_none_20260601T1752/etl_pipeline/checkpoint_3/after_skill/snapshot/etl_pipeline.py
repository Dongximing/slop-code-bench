#!/usr/bin/env python3
"""ETL Pipeline Executor

Reads a JSON pipeline specification from STDIN, validates/normalizes,
and either returns normalized pipeline (--execute=false) or executes it.
"""

import argparse
import json
import operator
import re
import sys
from typing import Any, Union


VALID_OPS = {
    "select": {"required": {"columns"}, "optional": set()},
    "filter": {"required": {"where"}, "optional": set()},
    "map": {"required": {"as", "expr"}, "optional": set()},
    "rename": {"required": set(), "optional": set(), "exclusive_groups": [
        {"from", "to"},
        {"mapping"}
    ]},
    "limit": {"required": {"n"}, "optional": set()},
    "branch": {"required": {"branches", "merge"}, "optional": set()},
}


class ExprParser:
    """Simple recursive descent parser for ETL expressions."""

    def __init__(self, expr: str):
        self.expr = expr.replace(' ', '')
        self.pos = 0
        self.length = len(self.expr)

    def parse_atom(self) -> Any:
        if self.pos >= self.length:
            raise ValueError("unexpected end of expression")

        ch = self.expr[self.pos]

        if ch == '"':
            return self.parse_string()

        if self.expr.startswith('true', self.pos):
            self.pos += 4
            return True

        if self.expr.startswith('false', self.pos):
            self.pos += 5
            return False

        if self.expr.startswith('null', self.pos):
            self.pos += 4
            return None

        if ch.isdigit() or (ch == '-' and self.pos + 1 < self.length and self.expr[self.pos + 1].isdigit()):
            return self.parse_number()

        if ch.isalpha() or ch == '_':
            return self.parse_identifier()

        if ch == '(':
            self.pos += 1
            expr = self.parse_expr(0)
            if self.pos >= self.length or self.expr[self.pos] != ')':
                raise ValueError("missing closing parenthesis")
            self.pos += 1
            return expr

        raise ValueError(f"unexpected character: {ch}")

    def parse_string(self) -> str:
        if self.expr[self.pos] != '"':
            raise ValueError("expected string literal")
        self.pos += 1
        start = self.pos
        while self.pos < self.length and self.expr[self.pos] != '"':
            self.pos += 1
        if self.pos >= self.length:
            raise ValueError("unterminated string literal")
        result = self.expr[start:self.pos]
        self.pos += 1
        return result

    def parse_number(self) -> Union[int, float]:
        start = self.pos
        if self.expr[self.pos] == '-':
            self.pos += 1
        while self.pos < self.length and self.expr[self.pos].isdigit():
            self.pos += 1
        if self.pos < self.length and self.expr[self.pos] == '.':
            self.pos += 1
            while self.pos < self.length and self.expr[self.pos].isdigit():
                self.pos += 1
        num_str = self.expr[start:self.pos]
        if '.' in num_str:
            return float(num_str)
        return int(num_str)

    def parse_identifier(self) -> str:
        start = self.pos
        while self.pos < self.length and (self.expr[self.pos].isalnum() or self.expr[self.pos] == '_'):
            self.pos += 1
        return self.expr[start:self.pos]

    def parse_unary(self) -> Any:
        if self.pos < self.length and self.expr[self.pos] == '!':
            self.pos += 1
            operand = self.parse_unary()
            return ('!', operand)
        if self.pos < self.length and self.expr[self.pos] == '-':
            self.pos += 1
            operand = self.parse_unary()
            return ('neg', operand)
        return self.parse_atom()

    def parse_mul_div(self) -> Any:
        expr = self.parse_unary()
        while self.pos < self.length:
            if self.expr[self.pos] == '*':
                self.pos += 1
                right = self.parse_unary()
                expr = ('*', expr, right)
            elif self.expr[self.pos] == '/':
                self.pos += 1
                right = self.parse_unary()
                expr = ('/', expr, right)
            else:
                break
        return expr

    def parse_add_sub(self) -> Any:
        expr = self.parse_mul_div()
        while self.pos < self.length:
            if self.expr[self.pos] == '+':
                self.pos += 1
                right = self.parse_mul_div()
                expr = ('+', expr, right)
            elif self.expr[self.pos] == '-':
                self.pos += 1
                right = self.parse_mul_div()
                expr = ('-', expr, right)
            else:
                break
        return expr

    def parse_cmp(self) -> Any:
        expr = self.parse_add_sub()
        while self.pos < self.length:
            if self.expr[self.pos] == '<':
                if self.pos + 1 < self.length and self.expr[self.pos + 1] == '=':
                    self.pos += 2
                    right = self.parse_add_sub()
                    expr = ('<=', expr, right)
                else:
                    self.pos += 1
                    right = self.parse_add_sub()
                    expr = ('<', expr, right)
            elif self.expr[self.pos] == '>':
                if self.pos + 1 < self.length and self.expr[self.pos + 1] == '=':
                    self.pos += 2
                    right = self.parse_add_sub()
                    expr = ('>=', expr, right)
                else:
                    self.pos += 1
                    right = self.parse_add_sub()
                    expr = ('>', expr, right)
            elif self.expr.startswith('==', self.pos):
                self.pos += 2
                right = self.parse_add_sub()
                expr = ('==', expr, right)
            elif self.expr.startswith('!=', self.pos):
                self.pos += 2
                right = self.parse_add_sub()
                expr = ('!=', expr, right)
            else:
                break
        return expr

    def parse_and(self) -> Any:
        expr = self.parse_cmp()
        while self.pos < self.length:
            if self.expr.startswith('&&', self.pos):
                self.pos += 2
                right = self.parse_cmp()
                expr = ('&&', expr, right)
            elif self.expr.startswith('and', self.pos):
                self.pos += 3
                right = self.parse_cmp()
                expr = ('&&', expr, right)
            else:
                break
        return expr

    def parse_or(self) -> Any:
        expr = self.parse_and()
        while self.pos < self.length:
            if self.expr.startswith('||', self.pos):
                self.pos += 2
                right = self.parse_and()
                expr = ('||', expr, right)
            elif self.expr.startswith('or', self.pos):
                self.pos += 2
                right = self.parse_and()
                expr = ('||', expr, right)
            else:
                break
        return expr

    def parse_expr(self, min_precedence=0) -> Any:
        return self.parse_or()

    def parse(self) -> Any:
        try:
            result = self.parse_expr()
            if self.pos < self.length:
                raise ValueError(f"unexpected trailing characters: {self.expr[self.pos:]}")
            return result
        except ValueError as e:
            raise ValueError(f"expression parse error: {e}")


class ExprEvaluator:
    """Evaluate an AST produced by ExprParser against a row (dict)."""

    def __init__(self, ast):
        self.ast = ast

    def evaluate(self, row: dict) -> Any:
        return self._eval(self.ast, row)

    def _eval(self, node, row):
        if isinstance(node, (int, float, str, bool, type(None))):
            return node

        if isinstance(node, tuple):
            op = node[0]

            if op == '!':
                val = self._eval(node[1], row)
                return not self._to_bool(val)

            if op == 'neg':
                val = self._eval(node[1], row)
                if val is None:
                    return None
                if not isinstance(val, (int, float)):
                    return None
                return -val

            if op in ('+', '-', '*', '/'):
                left = self._eval(node[1], row)
                right = self._eval(node[2], row)
                if left is None or right is None:
                    return None
                if not isinstance(left, (int, float)) or not isinstance(right, (int, float)):
                    return None
                if op == '+':
                    return left + right
                if op == '-':
                    return left - right
                if op == '*':
                    return left * right
                if op == '/':
                    if right == 0:
                        return None
                    return left / right

            if op in ('==', '!=', '<', '<=', '>', '>='):
                left = self._eval(node[1], row)
                right = self._eval(node[2], row)
                if left is None or right is None:
                    return op == '!='
                if type(left) != type(right):
                    return False
                if op == '==':
                    return left == right
                if op == '!=':
                    return left != right
                if op == '<':
                    return left < right
                if op == '<=':
                    return left <= right
                if op == '>':
                    return left > right
                if op == '>=':
                    return left >= right

            if op == '&&':
                left = self._eval(node[1], row)
                if not self._to_bool(left):
                    return False
                right = self._eval(node[2], row)
                return self._to_bool(right)

            if op == '||':
                left = self._eval(node[1], row)
                if self._to_bool(left):
                    return True
                right = self._eval(node[2], row)
                return self._to_bool(right)

            raise ValueError(f"unknown operator: {op}")

        # String identifier -> field lookup
        if isinstance(node, str):
            return row.get(node)

        raise ValueError(f"invalid AST node: {node}")

    @staticmethod
    def _to_bool(val) -> bool:
        return bool(val)


def is_valid_expression(expr: str, allow_unknown_ops=True) -> bool:
    if not expr or not isinstance(expr, str):
        return False

    expr = expr.strip()
    if not expr:
        return False

    try:
        parser = ExprParser(expr)
        parser.parse()
        return True
    except Exception:
        return False


def _make_path(prefix: str, field: str) -> str:
    return f"{prefix}.{field}" if prefix else f"pipeline.steps[][{field}]"


def validate_and_normalize_step(step: dict[str, Any], index: int, path_prefix: str = None) -> dict[str, Any]:
    if "op" not in step:
        raise ValueError(
            "ETL_ERROR: missing required field 'op'",
            "SCHEMA_VALIDATION_FAILED",
            _make_path(path_prefix or f"pipeline.steps[{index}]", "op")
        )

    op_raw = step["op"]

    if not isinstance(op_raw, str):
        raise ValueError(
            f"ETL_ERROR: op must be a string, got {type(op_raw).__name__}",
            "SCHEMA_VALIDATION_FAILED",
            _make_path(path_prefix or f"pipeline.steps[{index}]", "op")
        )

    op = op_raw.strip().lower()

    if not op:
        raise ValueError(
            "ETL_ERROR: op cannot be empty or whitespace-only",
            "SCHEMA_VALIDATION_FAILED",
            _make_path(path_prefix or f"pipeline.steps[{index}]", "op")
        )

    if op not in VALID_OPS:
        raise ValueError(
            f"ETL_ERROR: unsupported op '{op}'",
            "UNKNOWN_OP",
            _make_path(path_prefix or f"pipeline.steps[{index}]", "op")
        )

    op_config = VALID_OPS[op]
    step_data = {}

    for key, value in step.items():
        if key == "op":
            continue

        if key not in op_config["required"] and key not in op_config["optional"]:
            in_exclusive_group = False
            for group in op_config.get("exclusive_groups", []):
                if key in group:
                    in_exclusive_group = True
                    break
            if not in_exclusive_group:
                continue

        step_data[key] = value

    if op == "rename":
        has_from_to = "from" in step_data and "to" in step_data
        has_mapping = "mapping" in step_data

        if has_from_to and has_mapping:
            raise ValueError(
                "ETL_ERROR: rename cannot have both 'from'/'to' and 'mapping'",
                "SCHEMA_VALIDATION_FAILED",
                _make_path(path_prefix or f"pipeline.steps[{index}]", "mapping")
            )
        if not has_from_to and not has_mapping:
            raise ValueError(
                "ETL_ERROR: rename requires either 'from'/'to' or 'mapping'",
                "SCHEMA_VALIDATION_FAILED",
                _make_path(path_prefix or f"pipeline.steps[{index}]", "mapping")
            )

    for field in op_config["required"]:
        if op == "rename":
            if not ("from" in step_data and "to" in step_data) and "mapping" not in step_data:
                raise ValueError(
                    "ETL_ERROR: rename requires either 'from'/'to' or 'mapping'",
                    "SCHEMA_VALIDATION_FAILED",
                    _make_path(path_prefix or f"pipeline.steps[{index}]", "mapping")
                )
            continue

        if field not in step_data:
            raise ValueError(
                f"ETL_ERROR: missing required field '{field}'",
                "SCHEMA_VALIDATION_FAILED",
                _make_path(path_prefix or f"pipeline.steps[{index}]", field)
            )

    normalized = {"op": op}

    if op == "select":
        columns = step_data["columns"]
        if not isinstance(columns, list):
            raise ValueError(
                "ETL_ERROR: columns must be an array",
                "SCHEMA_VALIDATION_FAILED",
                _make_path(path_prefix or "pipeline", "columns")
            )
        normalized["columns"] = columns

    elif op == "filter":
        where = step_data["where"]
        if not isinstance(where, str):
            raise ValueError(
                "ETL_ERROR: where must be a string",
                "SCHEMA_VALIDATION_FAILED",
                _make_path(path_prefix or "pipeline", "where")
            )
        where = where.strip()
        if not where:
            raise ValueError(
                "ETL_ERROR: where cannot be empty",
                "SCHEMA_VALIDATION_FAILED",
                _make_path(path_prefix or "pipeline", "where")
            )
        if not is_valid_expression(where):
            raise ValueError(
                "ETL_ERROR: invalid expression syntax in where clause",
                "BAD_EXPR",
                _make_path(path_prefix or "pipeline", "where")
            )
        normalized["where"] = where

    elif op == "map":
        as_field = step_data["as"]
        if not isinstance(as_field, str):
            raise ValueError(
                "ETL_ERROR: as must be a string",
                "SCHEMA_VALIDATION_FAILED",
                _make_path(path_prefix or "pipeline", "as")
            )
        as_field = as_field.strip()
        if not as_field:
            raise ValueError(
                "ETL_ERROR: as cannot be empty",
                "SCHEMA_VALIDATION_FAILED",
                _make_path(path_prefix or "pipeline", "as")
            )
        normalized["as"] = as_field

        expr = step_data["expr"]
        if not isinstance(expr, str):
            raise ValueError(
                "ETL_ERROR: expr must be a string",
                "SCHEMA_VALIDATION_FAILED",
                _make_path(path_prefix or "pipeline", "expr")
            )
        expr = expr.strip()
        if not expr:
            raise ValueError(
                "ETL_ERROR: expr cannot be empty",
                "SCHEMA_VALIDATION_FAILED",
                _make_path(path_prefix or "pipeline", "expr")
            )
        if not is_valid_expression(expr):
            raise ValueError(
                "ETL_ERROR: invalid expression syntax in expr",
                "BAD_EXPR",
                _make_path(path_prefix or "pipeline", "expr")
            )
        normalized["expr"] = expr

    elif op == "rename":
        if "from" in step_data and "to" in step_data:
            from_val = step_data["from"]
            to_val = step_data["to"]
            if not isinstance(from_val, str):
                raise ValueError(
                    "ETL_ERROR: from must be a string",
                    "SCHEMA_VALIDATION_FAILED",
                    _make_path(path_prefix or "pipeline", "from")
                )
            if not isinstance(to_val, str):
                raise ValueError(
                    "ETL_ERROR: to must be a string",
                    "SCHEMA_VALIDATION_FAILED",
                    _make_path(path_prefix or "pipeline", "to")
                )
            from_val = from_val.strip()
            to_val = to_val.strip()
            if not from_val:
                raise ValueError(
                    "ETL_ERROR: from cannot be empty",
                    "SCHEMA_VALIDATION_FAILED",
                    _make_path(path_prefix or "pipeline", "from")
                )
            if not to_val:
                raise ValueError(
                    "ETL_ERROR: to cannot be empty",
                    "SCHEMA_VALIDATION_FAILED",
                    _make_path(path_prefix or "pipeline", "to")
                )
            normalized["mapping"] = {from_val: to_val}
        else:
            mapping = step_data["mapping"]
            if not isinstance(mapping, dict):
                raise ValueError(
                    "ETL_ERROR: mapping must be an object",
                    "SCHEMA_VALIDATION_FAILED",
                    _make_path(path_prefix or "pipeline", "mapping")
                )
            normalized["mapping"] = mapping

    elif op == "limit":
        n = step_data["n"]
        if not isinstance(n, int):
            raise ValueError(
                "ETL_ERROR: n must be an integer",
                "SCHEMA_VALIDATION_FAILED",
                _make_path(path_prefix or "pipeline", "n")
            )
        if n < 0:
            raise ValueError(
                "ETL_ERROR: n must be >= 0",
                "SCHEMA_VALIDATION_FAILED",
                _make_path(path_prefix or "pipeline", "n")
            )
        normalized["n"] = n

    elif op == "branch":
        branches = step_data["branches"]
        if not isinstance(branches, list):
            raise ValueError(
                "ETL_ERROR: branches must be an array",
                "SCHEMA_VALIDATION_FAILED",
                _make_path(path_prefix or "pipeline", "branches")
            )
        if len(branches) == 0:
            raise ValueError(
                "ETL_ERROR: branches must be non-empty",
                "SCHEMA_VALIDATION_FAILED",
                _make_path(path_prefix or "pipeline", "branches")
            )

        validated_branches = []
        has_otherwise = False

        for j, branch in enumerate(branches):
            if not isinstance(branch, dict):
                raise ValueError(
                    f"ETL_ERROR: branch {j} must be an object",
                    "SCHEMA_VALIDATION_FAILED",
                    _make_path(path_prefix or "pipeline", f"branches[{j}]")
                )

            if "when" not in branch:
                raise ValueError(
                    "ETL_ERROR: branch missing required field 'when'",
                    "SCHEMA_VALIDATION_FAILED",
                    _make_path(path_prefix or "pipeline", f"branches[{j}].when")
                )

            when_val = branch["when"]
            if not isinstance(when_val, str):
                raise ValueError(
                    "ETL_ERROR: when must be a string",
                    "SCHEMA_VALIDATION_FAILED",
                    _make_path(path_prefix or "pipeline", f"branches[{j}].when")
                )

            is_otherwise = when_val.strip().lower() == "otherwise"

            if is_otherwise:
                if has_otherwise:
                    raise ValueError(
                        "ETL_ERROR: multiple 'otherwise' branches not allowed",
                        "MALFORMED_STEP",
                        _make_path(path_prefix or "pipeline", "branches")
                    )
                if j != len(branches) - 1:
                    raise ValueError(
                        "ETL_ERROR: 'otherwise' branch must be last",
                        "MALFORMED_STEP",
                        _make_path(path_prefix or "pipeline", "branches")
                    )
                has_otherwise = True
            else:
                if not is_valid_expression(when_val.strip()):
                    raise ValueError(
                        "ETL_ERROR: invalid expression syntax in when clause",
                        "SCHEMA_VALIDATION_FAILED",
                        _make_path(path_prefix or "pipeline", f"branches[{j}].when")
                    )

            if "steps" not in branch:
                raise ValueError(
                    "ETL_ERROR: branch missing required field 'steps'",
                    "SCHEMA_VALIDATION_FAILED",
                    _make_path(path_prefix or "pipeline", f"branches[{j}].steps")
                )

            branch_steps = branch["steps"]
            if not isinstance(branch_steps, list):
                raise ValueError(
                    "ETL_ERROR: steps must be an array",
                    "SCHEMA_VALIDATION_FAILED",
                    _make_path(path_prefix or "pipeline", f"branches[{j}].steps")
                )

            branch_path_prefix = _make_path(path_prefix or "pipeline", f"branches[{j}]")

            normalized_branch_steps = []
            for k, step_in_branch in enumerate(branch_steps):
                if not isinstance(step_in_branch, dict):
                    raise ValueError(
                        f"ETL_ERROR: step {k} in branch must be an object",
                        "SCHEMA_VALIDATION_FAILED",
                        _make_path(branch_path_prefix, f"steps[{k}]")
                    )
                nested_prefix = f"{branch_path_prefix}.steps[{k}]"
                normalized_step = validate_and_normalize_step(step_in_branch, k, nested_prefix)
                normalized_branch_steps.append(normalized_step)

            validated_branch = {"steps": normalized_branch_steps, "when": when_val.strip()}
            if "id" in branch:
                id_val = branch["id"]
                if not isinstance(id_val, str):
                    raise ValueError(
                        "ETL_ERROR: id must be a string",
                        "SCHEMA_VALIDATION_FAILED",
                        _make_path(branch_path_prefix, "id")
                    )
                validated_branch["id"] = id_val
            validated_branches.append(validated_branch)

        normalized["branches"] = validated_branches

        merge = step_data["merge"]
        if not isinstance(merge, dict):
            raise ValueError(
                "ETL_ERROR: merge must be an object",
                "SCHEMA_VALIDATION_FAILED",
                _make_path(path_prefix or "pipeline", "merge")
            )

        strategy = merge.get("strategy", "concat")
        if not isinstance(strategy, str):
            raise ValueError(
                "ETL_ERROR: merge.strategy must be a string",
                "SCHEMA_VALIDATION_FAILED",
                _make_path(path_prefix or "pipeline", "merge.strategy")
            )

        if strategy not in ("concat",):
            raise ValueError(
                "ETL_ERROR: merge.strategy must be 'concat'",
                "SCHEMA_VALIDATION_FAILED",
                _make_path(path_prefix or "pipeline", "merge.strategy")
            )

        normalized["merge"] = {"strategy": strategy}

    # Sort keys: op first, then others alphabetically
    sorted_normalized = {"op": normalized["op"]}
    for key in sorted(normalized.keys()):
        if key != "op":
            sorted_normalized[key] = normalized[key]

    return sorted_normalized


def validate_and_normalize_pipeline(pipeline: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(pipeline, dict):
        raise ValueError(
            "ETL_ERROR: pipeline must be an object",
            "SCHEMA_VALIDATION_FAILED",
            "pipeline"
        )

    if "steps" not in pipeline:
        raise ValueError(
            "ETL_ERROR: pipeline must have a 'steps' field",
            "SCHEMA_VALIDATION_FAILED",
            "pipeline.steps"
        )

    steps = pipeline["steps"]
    if not isinstance(steps, list):
        raise ValueError(
            "ETL_ERROR: steps must be an array",
            "SCHEMA_VALIDATION_FAILED",
            "pipeline.steps"
        )

    normalized_steps = []
    for i, step in enumerate(steps):
        if not isinstance(step, dict):
            raise ValueError(
                f"ETL_ERROR: step {i} must be an object",
                "SCHEMA_VALIDATION_FAILED",
                f"pipeline.steps[{i}]"
            )
        normalized_step = validate_and_normalize_step(step, i)
        normalized_steps.append(normalized_step)

    return {"steps": normalized_steps}
