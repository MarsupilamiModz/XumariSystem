"""Safe arithmetic expression evaluator for configurable formulas (e.g. the level formula).

Only numbers, the whitelisted variable names and ``+ - * / // % **`` plus a few math functions are
allowed. Anything else raises ``ValueError`` – ``eval`` is never used.
"""

from __future__ import annotations

import ast
import math
import operator
from collections.abc import Callable, Mapping
from typing import Any

_BIN_OPS: dict[type[ast.operator], Callable[[Any, Any], Any]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY_OPS: dict[type[ast.unaryop], Callable[[Any], Any]] = {ast.UAdd: operator.pos, ast.USub: operator.neg}
_FUNCS: dict[str, Callable[..., Any]] = {
    "min": min,
    "max": max,
    "round": round,
    "floor": math.floor,
    "ceil": math.ceil,
    "sqrt": math.sqrt,
    "log": math.log,
}


class SafeExpression:
    def __init__(self, expression: str, variables: tuple[str, ...]) -> None:
        self.expression = expression
        self.variables = variables
        self._tree = ast.parse(expression, mode="eval")
        self._validate(self._tree.body)

    def _validate(self, node: ast.AST) -> None:
        if isinstance(node, ast.Constant):
            if not isinstance(node.value, (int, float)):
                raise ValueError("only numeric constants are allowed")
        elif isinstance(node, ast.Name):
            if node.id not in self.variables:
                raise ValueError(f"unknown variable {node.id!r}")
        elif isinstance(node, ast.BinOp):
            if type(node.op) not in _BIN_OPS:
                raise ValueError("operator not allowed")
            self._validate(node.left)
            self._validate(node.right)
        elif isinstance(node, ast.UnaryOp):
            if type(node.op) not in _UNARY_OPS:
                raise ValueError("operator not allowed")
            self._validate(node.operand)
        elif isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in _FUNCS or node.keywords:
                raise ValueError("function not allowed")
            for arg in node.args:
                self._validate(arg)
        else:
            raise ValueError(f"expression element {type(node).__name__} not allowed")

    def _eval(self, node: ast.AST, env: Mapping[str, float]) -> Any:
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            return env[node.id]
        if isinstance(node, ast.BinOp):
            left = self._eval(node.left, env)
            right = self._eval(node.right, env)
            if isinstance(node.op, ast.Pow) and abs(right) > 100:
                raise ValueError("exponent too large")
            return _BIN_OPS[type(node.op)](left, right)
        if isinstance(node, ast.UnaryOp):
            return _UNARY_OPS[type(node.op)](self._eval(node.operand, env))
        if isinstance(node, ast.Call):
            assert isinstance(node.func, ast.Name)
            return _FUNCS[node.func.id](*(self._eval(a, env) for a in node.args))
        raise ValueError("invalid expression")

    def __call__(self, **env: float) -> float:
        return float(self._eval(self._tree.body, env))
