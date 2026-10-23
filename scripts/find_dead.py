#!/usr/bin/env python3
"""找出「定义了但没人调用」的公开函数。

只做粗筛：统计每个公开函数名在所有源码（含测试）中出现的次数，
等于 1 次（只有定义处）就列为疑似死代码。

局限要说清楚：
    · 通过 getattr / 字符串反射调用的函数会被误报
    · 与标准库或第三方同名的方法会被淹没（比如 __init__ 之外的 common 名字）
    · 因此它只是**候选列表**，删之前必须人工确认

用法：
    python3 scripts/find_dead.py [--all]
"""

from __future__ import annotations

import argparse
import ast
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
PKG = ROOT / "lifeplan"
TESTS = ROOT / "tests"


def collect_definitions() -> dict[str, str]:
    defs: dict[str, str] = {}
    for path in sorted(PKG.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError as exc:  # 语法错误单独提示，不静默跳过
            print(f"⚠ 无法解析 {path}: {exc}", file=sys.stderr)
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name.startswith("_"):
                    continue
                rel = path.relative_to(ROOT)
                defs[f"{rel}::{node.name}"] = node.name
    return defs


def collect_sources() -> list[str]:
    files = list(PKG.rglob("*.py")) + list(TESTS.rglob("*.py")) + list((ROOT / "scripts").rglob("*.py"))
    out = []
    for p in files:
        try:
            out.append(p.read_text(encoding="utf-8"))
        except OSError:
            continue
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="列出全部函数及其出现次数")
    args = ap.parse_args()

    defs = collect_definitions()
    sources = collect_sources()

    rows: list[tuple[int, str]] = []
    for key, name in defs.items():
        hits = sum(len(re.findall(rf"\b{re.escape(name)}\b", s)) for s in sources)
        rows.append((hits, key))

    rows.sort()
    if args.all:
        for hits, key in rows:
            print(f"  {hits:>4}  {key}")
        return

    dead = [(h, k) for h, k in rows if h <= 1]
    print(f"公开函数 {len(defs)} 个，疑似未使用 {len(dead)} 个")
    for hits, key in dead:
        print(f"  · {key}")
    if dead:
        print("\n注意：这只是候选，字符串反射调用的函数会被误报，删前请确认。")


if __name__ == "__main__":
    main()
