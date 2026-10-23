"""证据分级与折减。

这是整个系统的价值锚点之一：

    万事万物皆可量化 —— 但不可核验的量化值，只值一半分。

证据等级（tier）：
    0 断言    只有一句"我做了"，无任何记录
    1 自述    有文字描述，但无法独立复核（默认档）
    2 有记录  留下可查的痕迹：截图、日志、文件、提交记录
    3 可核验  第三方可独立复核：公开链接、榜单、CI 结果、第三方背书

置信度权重不是线性折扣，而是"指数衰减 + 门槛差折减"：
没有证据支撑的高分会被显著惩罚，从而使系统不会奖励"喊口号"。
"""

from __future__ import annotations

# 每级证据的置信权重
TIER_WEIGHT: dict[int, float] = {
    0: 0.10,
    1: 0.55,
    2: 0.85,
    3: 1.00,
}

TIER_LABEL: dict[int, str] = {
    0: "断言",
    1: "自述",
    2: "有记录",
    3: "可核验",
}

# 证据缺口的折减系数：达不到指标要求的证据等级时，置信度按比例打折
EVIDENCE_GAP_PENALTY = 0.5


def tier_weight(tier: int) -> float:
    return TIER_WEIGHT.get(int(tier), TIER_WEIGHT[1])


def tier_label(tier: int) -> str:
    return TIER_LABEL.get(int(tier), f"未知({tier})")


def normalize_tier(tier: int | None) -> int:
    if tier is None:
        return 1
    t = int(tier)
    if t < 0:
        return 0
    if t > 3:
        return 3
    return t


def apply_evidence_gap(confidence: float, achieved_tier: int, required_tier: int) -> float:
    """证据门槛差折减。

    指标要求 tier 3（可核验），但手上只有 tier 1（自述）时，
    置信度不是简单按 tier 比值打折，而是再乘一层门槛惩罚，
    确保"用弱证据声称已完成硬指标"不会获得接近真实的分数。
    """
    required_w = tier_weight(required_tier)
    achieved_w = tier_weight(achieved_tier)
    if required_w <= 0:
        return confidence
    gap = max(0.0, required_w - achieved_w) / required_w
    return confidence * (1.0 - EVIDENCE_GAP_PENALTY * gap)


def infer_tier(evidence_ref: str, note: str = "") -> int:
    """根据观测记录自动推断证据等级（用户可覆盖）。"""
    ref = (evidence_ref or "").strip()
    txt = note.strip()
    if ref:
        if ref.startswith(("http://", "https://")):
            return 3
        if "/" in ref or "\\" in ref or len(ref) >= 8:
            return 2
        return 2
    if txt:
        return 1
    return 0
