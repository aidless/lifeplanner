"""计算引擎：所有数值在这里产生，**模型永不碰算术**。

设计原则（来自 Q-One 架构约定）：
    · 表达式求值走 AST 白名单——没有 eval、没有属性访问、没有函数调用
    · 统计量用确定性实现，区间用自助法（bootstrap，固定种子可复算）
    · 任何除零/空输入返回 None 而不是抛异常——上层据此降级为 unknown
"""

from __future__ import annotations

import ast
import math
import random
import statistics
from typing import Callable, Iterable

_ALLOWED_BINOPS = (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow)
_ALLOWED_UNARY = (ast.UAdd, ast.USub)


class CalcError(ValueError):
    pass


def evaluate(expr: str, variables: dict | None = None) -> float | None:
    """安全求值算术表达式。只允许数字、变量名（来自 variables）与四则运算。"""
    variables = variables or {}
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as exc:
        raise CalcError(f"表达式语法错误：{exc}") from exc

    def _ev(node) -> float:
        if isinstance(node, ast.Expression):
            return _ev(node.body)
        if isinstance(node, ast.Constant):
            if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
                raise CalcError("只允许数字常量")
            return float(node.value)
        if isinstance(node, ast.Name):
            if node.id not in variables:
                raise CalcError(f"未知变量 {node.id}")
            v = variables[node.id]
            if not isinstance(v, (int, float)) or isinstance(v, bool):
                raise CalcError(f"变量 {node.id} 不是数字")
            return float(v)
        if isinstance(node, ast.BinOp) and isinstance(node.op, _ALLOWED_BINOPS):
            l, r = _ev(node.left), _ev(node.right)
            try:
                if isinstance(node.op, ast.Add):
                    return l + r
                if isinstance(node.op, ast.Sub):
                    return l - r
                if isinstance(node.op, ast.Mult):
                    return l * r
                if isinstance(node.op, ast.Div):
                    return l / r
                if isinstance(node.op, ast.FloorDiv):
                    return l // r
                if isinstance(node.op, ast.Mod):
                    return l % r
                if isinstance(node.op, ast.Pow):
                    return l ** r
            except ZeroDivisionError as exc:
                raise CalcError("除零") from exc
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, _ALLOWED_UNARY):
            v = _ev(node.operand)
            return v if isinstance(node.op, ast.UAdd) else -v
        raise CalcError(f"不允许的语法：{type(node).__name__}")

    return _ev(tree)


# ── 统计量 ──────────────────────────────────────────────────
def mean(xs: Iterable[float]) -> float | None:
    xs = list(xs)
    return sum(xs) / len(xs) if xs else None


def ratio(hit: float, total: float) -> float | None:
    if total <= 0:
        return None
    return hit / total


def bootstrap_ci(
    xs: Iterable[float],
    stat: Callable[[list[float]], float] | None = None,
    n_resamples: int = 2000,
    alpha: float = 0.05,
    seed: int = 0,
) -> tuple[float, float] | None:
    """自助法置信区间。固定 seed → 同样输入永远同样区间（可复算）。"""
    xs = list(xs)
    stat = stat or statistics.mean
    if len(xs) < 2:
        return None
    rng = random.Random(seed)
    n = len(xs)
    stats = []
    for _ in range(n_resamples):
        sample = [xs[rng.randrange(n)] for _ in range(n)]
        try:
            v = stat(sample)
        except Exception:
            continue
        if isinstance(v, (int, float)) and math.isfinite(v):
            stats.append(v)
    if len(stats) < 10:
        return None
    stats.sort()
    lo = stats[max(0, int((alpha / 2) * len(stats)))]
    hi = stats[min(len(stats) - 1, int((1 - alpha / 2) * len(stats)))]
    return (lo, hi)


def clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))
