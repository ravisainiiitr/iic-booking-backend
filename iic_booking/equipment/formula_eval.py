"""Restricted formula evaluator for equipment time/charge scripts.

Supports:
- Expression mode (backward compatible): a single expression returns the value.
- Statement mode: assign to time / charge (and temporary names),
  if / elif / else, bounded for / while loops,
  comparisons, arithmetic, and a small math whitelist.

No imports, attribute access, subscripts, function/class definitions, or
arbitrary calls.
"""

from __future__ import annotations

import ast
import math
import operator
from decimal import Decimal
from typing import Any, Mapping, MutableMapping


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

_AUG_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}

_ALLOWED_CALLS = {
    "min": min,
    "max": max,
    "abs": abs,
    "round": round,
    "ceil": math.ceil,
    "floor": math.floor,
    "int": int,
    "float": float,
    "range": range,
}

_MAX_LOOP_ITERS = 10_000
_MAX_TOTAL_ITERS = 50_000


def _to_number(value: Any) -> float | int | bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, Decimal):
        return float(value)
    if value is None:
        return 0
    if isinstance(value, range):
        return value  # type: ignore[return-value]
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


class _Break(Exception):
    pass


class _Continue(Exception):
    pass


class _SafeEval(ast.NodeVisitor):
    def __init__(self, env: MutableMapping[str, Any]):
        self.env = env
        self._iters = 0

    def _bump_iters(self, n: int = 1) -> None:
        self._iters += n
        if self._iters > _MAX_TOTAL_ITERS:
            raise FormulaError("Formula exceeded maximum loop iterations")

    def visit(self, node):  # type: ignore[override]
        method = getattr(self, f"visit_{type(node).__name__}", None)
        if method is None:
            raise FormulaError(f"Unsupported syntax: {type(node).__name__}")
        return method(node)

    def visit_Expression(self, node: ast.Expression):
        return self.visit(node.body)

    def visit_Module(self, node: ast.Module):
        for stmt in node.body:
            self.visit(stmt)
        return None

    def visit_Constant(self, node: ast.Constant):
        if isinstance(node.value, (int, float, bool)):
            return node.value
        if node.value is None:
            return 0
        raise FormulaError(f"Unsupported constant: {node.value!r}")

    def visit_Num(self, node):  # pragma: no cover
        return node.n

    def visit_NameConstant(self, node):  # pragma: no cover
        return node.value

    def visit_Name(self, node: ast.Name):
        if isinstance(node.ctx, ast.Store):
            return node.id
        if node.id == "True":
            return True
        if node.id == "False":
            return False
        if node.id == "None":
            return 0
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
            result: Any = True
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
        if node.func.id == "range":
            if not args or len(args) > 3:
                raise FormulaError("range() expects 1-3 arguments")
            nums = [int(_to_number(a)) for a in args]
            r = range(*nums)
            if len(r) > _MAX_LOOP_ITERS:
                raise FormulaError(f"range() size exceeds {_MAX_LOOP_ITERS}")
            return r
        return fn(*args)

    def visit_Expr(self, node: ast.Expr):
        self.visit(node.value)
        return None

    def visit_Pass(self, node: ast.Pass):
        return None

    def visit_Break(self, node: ast.Break):
        raise _Break()

    def visit_Continue(self, node: ast.Continue):
        raise _Continue()

    def _assign_name(self, name: str, value: Any) -> None:
        if name in {"pc", "sc", "SLOT_DURATION_MINUTES", "TIME"}:
            raise FormulaError(f"Cannot assign to read-only name: {name}")
        if len(name) == 1 and name.isupper() and name.isalpha():
            raise FormulaError(f"Cannot assign to read-only name: {name}")
        if not name.isidentifier() or name.startswith("_"):
            raise FormulaError(f"Invalid assignment target: {name}")
        if isinstance(value, range):
            self.env[name] = value
        else:
            self.env[name] = _to_number(value)

    def visit_Assign(self, node: ast.Assign):
        value = self.visit(node.value)
        for target in node.targets:
            if not isinstance(target, ast.Name):
                raise FormulaError("Only simple name assignments are allowed")
            self._assign_name(target.id, value)
        return None

    def visit_AnnAssign(self, node: ast.AnnAssign):
        if node.value is None:
            raise FormulaError("Annotated assignment without value is not allowed")
        if not isinstance(node.target, ast.Name):
            raise FormulaError("Only simple name assignments are allowed")
        self._assign_name(node.target.id, self.visit(node.value))
        return None

    def visit_AugAssign(self, node: ast.AugAssign):
        if not isinstance(node.target, ast.Name):
            raise FormulaError("Only simple name assignments are allowed")
        name = node.target.id
        if name not in self.env:
            raise FormulaError(f"Unknown variable: {name}")
        op = _AUG_OPS.get(type(node.op))
        if op is None:
            raise FormulaError("Unsupported augmented assignment")
        try:
            new_val = op(self.env[name], self.visit(node.value))
        except ZeroDivisionError as exc:
            raise FormulaError("Division by zero in formula") from exc
        self._assign_name(name, new_val)
        return None

    def visit_If(self, node: ast.If):
        if self.visit(node.test):
            for stmt in node.body:
                self.visit(stmt)
        else:
            for stmt in node.orelse:
                self.visit(stmt)
        return None

    def visit_While(self, node: ast.While):
        count = 0
        while self.visit(node.test):
            self._bump_iters()
            count += 1
            if count > _MAX_LOOP_ITERS:
                raise FormulaError(f"while loop exceeded {_MAX_LOOP_ITERS} iterations")
            try:
                for stmt in node.body:
                    self.visit(stmt)
            except _Continue:
                continue
            except _Break:
                break
        else:
            for stmt in node.orelse:
                self.visit(stmt)
        return None

    def visit_For(self, node: ast.For):
        if not isinstance(node.target, ast.Name):
            raise FormulaError("for-loop target must be a simple name")
        iterable = self.visit(node.iter)
        if not isinstance(iterable, range):
            raise FormulaError("for-loops only support range(...)")
        for item in iterable:
            self._bump_iters()
            self._assign_name(node.target.id, item)
            try:
                for stmt in node.body:
                    self.visit(stmt)
            except _Continue:
                continue
            except _Break:
                break
        else:
            for stmt in node.orelse:
                self.visit(stmt)
        return None


def _as_float(result: Any, *, label: str) -> float:
    try:
        return float(result)
    except (TypeError, ValueError) as exc:
        raise FormulaError(f"{label} did not resolve to a number: {result!r}") from exc


def evaluate_formula(expression: str, env: Mapping[str, Any]) -> float:
    """Evaluate a restricted single expression (legacy API)."""
    expr = (expression or "").strip()
    if not expr:
        raise FormulaError("Formula is empty")
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as exc:
        raise FormulaError(f"Invalid formula syntax: {exc.msg}") from exc
    local_env: dict[str, Any] = {str(k): _to_number(v) for k, v in env.items()}
    result = _SafeEval(local_env).visit(tree)
    return _as_float(result, label="Formula")


def run_formula_script(
    code: str,
    env: Mapping[str, Any],
    *,
    result_var: str,
) -> float:
    """Run an expression or statement script and return result_var.

    - If code is a single expression, its value is returned.
    - If code contains statements, execute and require assignment to result_var
      (typically time or charge).
    """
    src = (code or "").strip()
    if not src:
        raise FormulaError("Formula is empty")

    local_env: dict[str, Any] = {str(k): _to_number(v) for k, v in env.items()}
    local_env.setdefault(result_var, 0)

    try:
        expr_tree = ast.parse(src, mode="eval")
    except SyntaxError:
        expr_tree = None

    if expr_tree is not None:
        result = _SafeEval(local_env).visit(expr_tree)
        value = _as_float(result, label=result_var)
        local_env[result_var] = value
        return value

    try:
        mod_tree = ast.parse(src, mode="exec")
    except SyntaxError as exc:
        raise FormulaError(f"Invalid formula syntax: {exc.msg}") from exc

    _SafeEval(local_env).visit(mod_tree)

    if result_var not in local_env:
        raise FormulaError(
            f"Formula must assign `{result_var}` "
            f"(e.g. `{result_var} = ...`) or be a single expression"
        )
    return _as_float(local_env[result_var], label=result_var)