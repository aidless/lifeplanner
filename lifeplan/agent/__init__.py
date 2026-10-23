"""Jev：人生规划系统的专属 agent。

用法：

    lifeplan agent ask "现在状态怎么样"
    lifeplan agent ask "我对自己判断准不准" --verbose
    lifeplan agent tools
    lifeplan agent doctor

设计要点见 loop.py 与 adapters.py 的模块注释。一句话概括：
**模型只负责决定"看什么、怎么问"，不负责"说了算"** ——
所有事实来自工具返回，所有数字要能追溯，所有副作用要人确认。
"""

from __future__ import annotations

from .adapters import (
    BaseAdapter,
    JevLocalAdapter,
    OpenAICompatAdapter,
    ScriptedAdapter,
    available_adapters,
    pick_adapter,
)
from .loop import Answer, Observation, answer
from .tools import TOOLS, Context, ToolResult, call, tool_catalog, tool_catalog_lines
from .verify import unsupported_numbers, verify

__all__ = [
    "answer",
    "Answer",
    "Observation",
    "BaseAdapter",
    "JevLocalAdapter",
    "OpenAICompatAdapter",
    "ScriptedAdapter",
    "available_adapters",
    "pick_adapter",
    "TOOLS",
    "Context",
    "ToolResult",
    "call",
    "tool_catalog",
    "tool_catalog_lines",
    "verify",
    "unsupported_numbers",
]
