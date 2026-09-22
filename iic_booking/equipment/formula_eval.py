"""Restricted expression evaluator for equipment time/charge formulas.

Supports numbers, arithmetic, comparisons, boolean ops, and if/else expressions
(IfExp). Names must come from an explicit environment dict. No attribute access,
imports, subscripts, or arbitrary calls (except a small math whitelist).
"""

from __future__ import annotations

import ast
import math
import operator
from decimal import Decimal
from typing import Any, Mapping


class FormulaError(ValueError):
    """Raised when a formula is invalid or fails evaluation."""


_BIN_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}

_UNARY_OPS = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
    ast.Not: operator.not_,
}

_CMP_OPS = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
}

_ALLOWED_CALLS = {
    "min": min,
    "max": max,
    "abs": abs,
    "round": round,
    "ceil": math.ceil,
    "floor": math.floor,
}


def _to_number(value: Any) -> float | int | bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, Decimal):
        return float(value)
    if value is None:
        return 0
    s = str(value).strip()
    if not s:
        return 0
    if s.lower() in {"true", "yes", "on"}:
        return True
    if s.lower() in {"false", "no", "off"}:
        return False
    try:
        if "." in s or "e" in s.lower():
            return float(s)
        return int(s)
    except (TypeError, ValueError) as exc:
        raise FormulaError(f"Non-numeric value in formula: {value!r}") from exc


class _SafeEval(ast.NodeVisitor):
    def __init__(self, env: Mapping[str, Any]):
        self.env = {str(k): _to_number(v) for k, v in env.items()}

    def visit(self, node):  # type: ignore[override]
        method = getattr(self, f"visit_{type(node).__name__}", None)
        if method is None:
            raise FormulaError(f"Unsupported expression: {type(node).__name__}")
        return method(node)

    def visit_Expression(self, node: ast.Expression):
        return self.visit(node.body)

    def visit_Constant(self, node: ast.Constant):
        if isinstance(node.value, (int, float, bool)):
            return node.value
        if node.value is None:
            return 0
        raise FormulaError(f"Unsupported constant: {node.value!r}")

    # py<3.8 compatibility name
    def visit_Num(self, node):  # pragma: no cover
        return node.n

    def visit_Name(self, node: ast.Name):
        if node.id not in self.env:
            raise FormulaError(f"Unknown variable: {node.id}")
        return self.env[node.id]

    def visit_UnaryOp(self, node: ast.UnaryOp):
        op = _UNARY_OPS.get(type(node.op))
        if op is None:
            raise FormulaError("Unsupported unary operator")
        return op(self.visit(node.operand))

    def visit_BinOp(self, node: ast.BinOp):
        op = _BIN_OPS.get(type(node.op))
        if op is None:
            raise FormulaError("Unsupported binary operator")
        left = self.visit(node.left)
        right = self.visit(node.right)
        try:
            return op(left, right)
        except ZeroDivisionError as exc:
            raise FormulaError("Division by zero in formula") from exc

    def visit_BoolOp(self, node: ast.BoolOp):
        if isinstance(node.op, ast.And):
            result = True
            for value in node.values:
                result = self.visit(value)
                if not result:
                    return result
            return result
        if isinstance(node.op, ast.Or):
            result = False
            for value in node.values:
                result = self.visit(value)
                if result:
                    return result
            return result
        raise FormulaError("Unsupported boolean operator")

    def visit_Compare(self, node: ast.Compare):
        left = self.visit(node.left)
        for op_node, comparator in zip(node.ops, node.comparators):
            op = _CMP_OPS.get(type(op_node))
            if op is None:
                raise FormulaError("Unsupported comparison")
            right = self.visit(comparator)
            if not op(left, right):
                return False
            left = right
        return True

    def visit_IfExp(self, node: ast.IfExp):
        return self.visit(node.body) if self.visit(node.test) else self.visit(node.orelse)

    def visit_Call(self, node: ast.Call):
        if not isinstance(node.func, ast.Name):
            raise FormulaError("Only simple function names are allowed")
        fn = _ALLOWED_CALLS.get(node.func.id)
        if fn is None:
            raise FormulaError(f"Function not allowed: {node.func.id}")
        if node.keywords:
            raise FormulaError("Keyword arguments are not allowed")
        args = [self.visit(a) for a in node.args]
        return fn(*args)


def evaluate_formula(expression: str, env: Mapping[str, Any]) -> float:
    """Evaluate a restricted formula expression and return a float result."""
    expr = (expression or "").strip()
    if not expr:
        raise FormulaError("Formula is empty")
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as exc:
        raise FormulaError(f"Invalid formula syntax: {exc.msg}") from exc
    result = _SafeEval(env).visit(tree)
    try:
        return float(result)
    except (TypeError, ValueError) as exc:
        raise FormulaError(f"Formula did not return a number: {result!r}") from exc
