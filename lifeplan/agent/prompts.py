"""Agent 的提示词与输出契约。

输出契约是硬的：模型必须返回一个 JSON 对象，要么给工具调用，要么给最终答复。
这不是为了好看，而是为了让「模型说了什么」在进入系统的第一步就被校验——
自由文本无法审计，JSON 可以。
"""

from __future__ import annotations

import json
import re

from .adapters import Decision

SYSTEM_PROMPT = """你是 Jev，一台「人生规划系统」的专属 agent。
你的职责是回答关于主人计划状态的问题，并在他授权下记录数据、生成待确认的操作提案。

硬性规则（任何情况下不得违反）：
1. 只能依据工具返回的结果陈述事实。没有调用工具就不要给出数字。
2. 不得编造任何数值、日期、名次、指标。不确定就说不确定。
3. 涉及执行命令或启动应用时，只能调用 term_exec / app_launch 生成**待确认提案**，
   绝不能声称"已执行"。提案需要主人回填令牌才会真正发生。
4. 最终答复用中文，简洁，并在 evidence 字段列出你引用的工具结果序号。

输出必须是单个 JSON 对象，二选一：

调用工具：
{"thought": "为什么调这个", "tool": "工具名", "args": {...}}

给出最终答复：
{"thought": "为什么这样答", "final": "答复正文", "evidence": [1, 2]}

不要输出 JSON 以外的任何内容。"""

TOOL_CONTRACT = """可用工具（name｜tier｜说明｜参数）：
{tools}

tier 0 = 只读，可直接调用。
tier 1 = 写入本地数据库（可逆），需要会话开启 --write。
tier 2 = 有外部影响。你只能生成待确认提案，永远无法自行执行。"""


def build_user_prompt(question: str, observations: list[str], tool_lines: list[str] | None = None) -> str:
    parts = []
    if tool_lines:
        parts.append(TOOL_CONTRACT.format(tools="\n".join(tool_lines)))
    parts.append(f"主人问：{question}")
    if observations:
        parts.append("已获得的工具结果：\n" + "\n".join(observations))
        parts.append("基于以上结果作答，或继续调用工具。不要重复已调用过的工具。")
    else:
        parts.append("你还没有任何工具结果。请先选一个最相关的工具调用。")
    parts.append('只输出一个 JSON 对象。')
    return "\n\n".join(parts)


_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def _extract_json(raw: str) -> dict | None:
    """从模型输出里挖出第一个 JSON 对象。容忍代码块包裹与前后废话。"""
    if not raw:
        return None
    text = raw.strip()
    m = _FENCE.search(text)
    if m:
        text = m.group(1).strip()
    # 先整体试
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        pass
    # 再扫第一个平衡的 {...}
    start = text.find("{")
    while start != -1:
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(text[start : i + 1])
                        if isinstance(obj, dict):
                            return obj
                    except json.JSONDecodeError:
                        break
        start = text.find("{", start + 1)
    return None


def parse_decision(raw: str) -> Decision:
    """把模型输出解析成 Decision。解析失败不抛异常，而是标记 note 让上层降级。"""
    obj = _extract_json(raw)
    if obj is None:
        return Decision(note=f"模型输出不是合法 JSON，已忽略：{raw[:80]!r}")

    tool = obj.get("tool")
    if isinstance(tool, str) and tool.strip():
        args = obj.get("args") or {}
        if not isinstance(args, dict):
            args = {}
        return Decision(tool=tool.strip(), args=args, note=str(obj.get("thought", ""))[:200])

    final = obj.get("final")
    if isinstance(final, str) and final.strip():
        ev = obj.get("evidence") or []
        if not isinstance(ev, list):
            ev = []
        ids = [int(x) for x in ev if isinstance(x, (int, float, str)) and str(x).isdigit()]
        return Decision(final=final.strip(), evidence=ids,
                        note=str(obj.get("thought", ""))[:200])

    return Decision(note="JSON 里既没有 tool 也没有 final，已忽略")
