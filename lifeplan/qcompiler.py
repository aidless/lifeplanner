"""量化编译器 v2：用 LLM 把自然语言问题拆成测量计划。

与 v1（规则匹配）的分工：
    v1   关键词命中垂直域 → 固定计划。快、确定、但只会一种问法
    v2   LLM 在**信号注册表**内选择：它能说"这个问题的价值在哪个可测信号上"，
         但**不能 invent 一个测不了的问题**——注册表是硬边界

三条约束（防 LLM 越权）：
    1. 只能引用 `SIGNAL_REGISTRY` 里的信号（这些信号有 _evidence() 实现）
    2. 追加问题只能是 choice/score/noul（数值类 measure 必须走计算引擎）
    3. 输出必须是严格 JSON；解析失败 → 返回 None（调用方回退 v1）

LLM 不可用、超时、输出不合法时**静默回退**——编译器不能成为单点故障。
"""

from __future__ import annotations

import json
import os
import re

# ── 信号注册表：系统**实际能算**的东西 ──────────────────────
SIGNAL_REGISTRY = {
    "commit_30d": {
        "label": "投入强度", "unit": "commits_per_30d",
        "desc": "近 30 天 git 提交数（活动量代理，不等于成果）",
    },
    "verified_obs_30d": {
        "label": "证据密度", "unit": "verified_obs_per_30d",
        "desc": "近 30 天 tier≥2 的观测数（有据可查的进展）",
    },
    "calibration_accuracy": {
        "label": "自评准确度", "unit": "score_0_1",
        "desc": "1 − |过度自信度|，来自预测-结算校准台账",
    },
    "structural_completeness": {
        "label": "结构完整度", "unit": "ratio_0_1",
        "desc": "有指标的目标占比（没有指标的目标无法结算）",
    },
}

ALLOWED_EXTRA_TYPES = ("choice", "score", "noul")

PROMPT = """你在做一个「可审计的人生量化系统」的量化编译器。

你的任务：把用户的问题，映射到系统**实际能计算**的信号上，并补充必要的结构化问题。

可用的信号（只能从这里选，不能发明）：
{signals}

规则：
1. 只能从上面 4 个信号里选（英文 key）。如果这个问题跟它们都无关，supported 设为 false。
2. extra_questions 最多 3 个，type 只能是 choice / score / noul（不能是 measure——数值必须由计算引擎产生）。
3. choice 的 criteria 必须是 {{选项key: 一句话说明}}；score 的 criteria 必须是按顺序的等级数组。
4. 不要输出任何数字、不要计算、不要编造数据。
4b. unknowns 必须是你针对这个问题写的具体内容，禁止照抄示例里的占位文字。
5. 只输出 JSON，不要解释，不要 markdown 代码块。

输出格式：
{{"supported": true/false,
  "domain": "简短的领域名",
  "measures": ["信号key", ...],
  "extra_questions": [{{"qid": "risk", "type": "score", "criteria": ["低","中","高"], "instructions": "..."}}],
  "unknowns": ["<每条写一句这个计划测不到的具体东西，不要照抄本行>"]}}

用户问题：{state}
"""


def _extract_json(text: str) -> dict | None:
    """从模型输出里抠 JSON。容忍代码块与前后废话。"""
    if not text:
        return None
    # 去掉 ```json ... ``` 包裹
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1)
    # 找第一个平衡的 {...}
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    for i, ch in enumerate(text[start:], start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start : i + 1])
                except json.JSONDecodeError:
                    return None
    return None


def validate_plan(doc: dict) -> list[str]:
    """返回错误列表；空 = 合法。"""
    errors: list[str] = []
    if not isinstance(doc, dict):
        return ["输出不是对象"]
    if not isinstance(doc.get("supported"), bool):
        errors.append("supported 必须是布尔值")
    measures = doc.get("measures")
    if not isinstance(measures, list):
        errors.append("measures 必须是数组")
    else:
        for m in measures:
            if m not in SIGNAL_REGISTRY:
                errors.append(f"未知信号 {m!r}（只允许注册表内的 {sorted(SIGNAL_REGISTRY)}）")
    extras = doc.get("extra_questions") or []
    if not isinstance(extras, list):
        errors.append("extra_questions 必须是数组")
    elif len(extras) > 3:
        errors.append("extra_questions 最多 3 个")
    else:
        for i, q in enumerate(extras, 1):
            if not isinstance(q, dict):
                errors.append(f"extra[{i}] 不是对象")
                continue
            if q.get("type") not in ALLOWED_EXTRA_TYPES:
                errors.append(f"extra[{i}] type={q.get('type')!r} 不允许"
                              f"（只允许 {ALLOWED_EXTRA_TYPES}，数值必须走计算引擎）")
            if not q.get("qid"):
                errors.append(f"extra[{i}] 缺 qid")
            c = q.get("criteria")
            if q.get("type") == "choice" and not isinstance(c, dict):
                errors.append(f"extra[{i}] choice 的 criteria 必须是对象")
            if q.get("type") == "score" and not isinstance(c, list):
                errors.append(f"extra[{i}] score 的 criteria 必须是数组")
    if not isinstance(doc.get("unknowns", []), list):
        errors.append("unknowns 必须是数组")
    return errors


def compile_with_llm(state: str, call_llm, timeout_note: str = "") -> dict | None:
    """调用 LLM 编译。call_llm(prompt: str) -> str | None。

    返回合法 plan dict，或 None（调用方回退 v1 规则编译器）。
    """
    signals_desc = "\n".join(
        f"- {key}: {v['label']}（{v['unit']}）—— {v['desc']}"
        for key, v in SIGNAL_REGISTRY.items()
    )
    prompt = PROMPT.format(signals=signals_desc, state=state)
    try:
        raw = call_llm(prompt)
    except Exception:
        return None
    doc = _extract_json(raw or "")
    if doc is None:
        return None
    if validate_plan(doc):
        return None
    if not doc.get("supported"):
        return doc          # 合法但明确拒绝：由调用方决定如何呈现
    if not doc.get("measures"):
        return None         # 说支持却一个信号都没选 → 视为无效
    # 3B 模型常把示例占位照抄进 unknowns —— 过滤掉，别让占位符冒充未知项
    doc["unknowns"] = [
        u for u in (doc.get("unknowns") or [])
        if u and "不要照抄" not in u and "这个计划仍然测不到的东西" not in u
    ]
    doc["_compiled_by"] = "llm"
    return doc


def ollama_caller(model: str | None = None, temperature: float = 0.0):
    """返回一个 call_llm：走本机 Ollama 的 OpenAI 兼容端点。"""
    from .agent.adapters import OpenAICompatAdapter

    adapter = OpenAICompatAdapter()
    ok, _ = adapter.status()
    if not ok:
        return None

    def call(prompt: str) -> str | None:
        return adapter.complete(prompt, json_mode=True, max_tokens=700)

    return call


def enabled() -> bool:
    """LIFEPLAN_QCOMPILER=llm 开启；默认 auto（有 LLM 就用，失败静默回退）。"""
    return os.environ.get("LIFEPLAN_QCOMPILER", "auto").lower() not in ("rules", "off", "0")
