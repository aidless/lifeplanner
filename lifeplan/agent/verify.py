"""答复核验：把「模型可能编数字」这件事变成可检测的问题。

两个检查：
    1. 引用完整性  最终答复声称引用了哪些工具结果，这些序号必须真实存在
    2. 数字可追溯  答复里出现的每个数字，都要在工具结果里找得到出处
                   （允许四舍五入与百分数换算，其余一律视为无依据）

第二条是这套 agent 最关键的一道闸。1B 级模型最容易犯的错不是逻辑错，
而是把数字写"顺眼"——0.438 写成 0.44、1824 名写成 1800 名。
在这种系统里，一个编造的分数会直接污染校准台账。
"""

from __future__ import annotations

import re

NUM = re.compile(r"-?\d+(?:\.\d+)?")


def _norm(num: float, decimals: int | None = None) -> float:
    if decimals is None:
        return round(num, 6)
    return round(num, decimals)


def extract_numbers(text: str) -> list[str]:
    return NUM.findall(text or "")


def _observed_numbers(observations: list[dict]) -> set[float]:
    out: set[float] = set()
    for o in observations:
        blob = f"{o.get('text', '')} {o.get('data', '')}"
        for raw in NUM.findall(blob):
            try:
                v = float(raw)
            except ValueError:
                continue
            out.add(round(v, 6))
            out.add(round(v * 100, 6))      # 百分数写法 0.292 → 29.2
            out.add(round(v / 100, 6))      # 反向
    return out


def unsupported_numbers(final: str, observations: list[dict]) -> list[str]:
    """返回在工具结果里找不到出处的数字。"""
    if not observations:
        # 没调用任何工具却给出数字 —— 全部视为无依据
        return extract_numbers(final)

    observed = _observed_numbers(observations)
    bad: list[str] = []
    for raw in extract_numbers(final):
        try:
            v = float(raw)
        except ValueError:
            continue
        # 允许四舍五入：0.292 可以写成 0.29 / 0.3
        candidates = {round(v, 6), round(v, 4), round(v, 3), round(v, 2), round(v, 1),
                      round(v, 0)}
        if candidates & observed:
            continue
        # 也允许它是某个观测值的舍入结果
        if any(abs(round(o, len(raw.split(".")[1]) if "." in raw else 0) - v) < 1e-9
               for o in observed):
            continue
        bad.append(raw)
    return bad


def check_evidence(evidence: list[int], n_obs: int) -> list[int]:
    """过滤掉越界的引用序号。"""
    return [i for i in evidence if 1 <= i <= n_obs] if evidence else []


def verify(final: str, evidence: list[int], observations: list[dict]) -> tuple[bool, list[str]]:
    issues: list[str] = []
    if not observations:
        # 没调任何工具就作答，无论有没有数字都不可信——
        # 这个分支曾被真模型踩中：它直接给了句空泛的总结，跳过所有工具。
        issues.append("未调用任何工具，此答复没有数据支撑")
        return False, issues

    bad = unsupported_numbers(final, observations)
    if bad:
        issues.append("以下数字在工具结果中找不到出处：" + "、".join(bad[:8]))

    if evidence:
        valid = check_evidence(evidence, len(observations))
        if len(valid) != len(evidence):
            issues.append(f"引用了不存在的工具结果序号：{sorted(set(evidence) - set(valid))}")
    else:
        issues.append("未声明 evidence，答复无法追溯到具体工具结果")

    return (not issues), issues
