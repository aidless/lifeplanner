"""Agent 主循环。

流程：问题 → 选适配器 → 逐步调用工具 → 汇总答复 → 核验数字 → 返回。

三条硬约束由代码保证，不依赖提示词自觉：

1. **tier2 工具不会执行**。`term_exec` / `app_launch` 只写一条 proposed 记录并返回令牌，
   真正执行必须由人在 CLI 里回填。agent 无法绕过这一步。
2. **答复里的每个数字都要有出处**。`verify.unsupported_numbers` 会逐一比对工具结果；
   找不到出处的数字会被列出来，answer.citations_ok 置 false。
3. **没有工具结果就不给数字**。空口作答会被直接标记为无依据。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from .adapters import BaseAdapter, pick_adapter
from .tools import TOOLS, Context, ToolResult, call
from .verify import verify


@dataclass
class Observation:
    idx: int
    tool: str
    args: dict
    ok: bool
    tier: int
    text: str
    data: dict

    def render(self) -> str:
        arg_txt = json.dumps(self.args, ensure_ascii=False) if self.args else ""
        return f"【{self.idx}】{self.tool}{arg_txt} → {self.text}"


@dataclass
class Answer:
    question: str
    text: str
    observations: list[Observation] = field(default_factory=list)
    evidence: list[int] = field(default_factory=list)   # 模型声明引用的观测序号
    adapter: str = ""
    skipped: list[str] = field(default_factory=list)
    citations_ok: bool = True
    issues: list[str] = field(default_factory=list)
    proposals: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def render(self, verbose: bool = False) -> str:
        out = [self.text]
        if self.observations:
            out.append("")
            out.append("依据（{} 次工具调用）：".format(len(self.observations)))
            for o in self.observations:
                head = o.text.splitlines()[0] if o.text else ""
                out.append(f"  【{o.idx}】{o.tool} → {head[:90]}")

        # 数据附录：模型负责解读，数据由系统保证在场。
        # 实测中 3B 模型常给出空泛的 final（"需要谨慎评估"）却不带数字；
        # 与其指望模型复述准确，不如把被引用的观测原文附在后面。
        cited = ([o for o in self.observations if o.idx in self.evidence]
                 if self.evidence else list(self.observations))
        if cited:
            out.append("")
            out.append("数据附录：")
            for o in cited:
                body = (o.text or "").strip()
                if len(body) > 700:
                    body = body[:700] + "…"
                out.append(f"【{o.idx}】{o.tool}")
                for line in body.splitlines():
                    out.append(f"    {line}")

        if self.proposals:
            out.append("")
            out.append("待确认提案（我没有执行，需要你回填令牌）：")
            for p in self.proposals:
                out.append(f"  · action#{p.get('action_id')} tier{p.get('tier')}　"
                           f"令牌 {p.get('token')}")
        if not self.citations_ok:
            out.append("")
            out.append("⚠ 核验未通过：")
            for i in self.issues:
                out.append(f"  · {i}")
        if verbose:
            out.append("")
            out.append(f"驱动：{self.adapter}")
            for s in self.skipped:
                out.append(f"  跳过 {s}")
        elif self.skipped:
            out.append("")
            out.append(f"（驱动：{self.adapter}；另有 {len(self.skipped)} 个适配器不可用，"
                       f"用 --verbose 查看）")
        return "\n".join(out)


def _summarize(obs: list[Observation]) -> str:
    """无模型时的确定性汇总：只做机械拼接，不加任何推论。"""
    if not obs:
        return ("我没有可依据的数据来回答这个问题。可以指定更具体的方面"
                "（状态 / 目标 / 校准 / 审计 / 宿主画像 / 待结算预测）。")
    lines = []
    for o in obs:
        lines.append(o.text.rstrip())
    return "\n\n".join(lines)


def answer(
    question: str,
    conn,
    allow_write: bool = False,
    as_of: str | None = None,
    max_steps: int = 4,
    preferred: str | None = None,
    cwd: str | None = None,
    adapter: BaseAdapter | None = None,
) -> Answer:
    chosen, skipped = (adapter, []) if adapter else pick_adapter(preferred)
    ctx = Context(conn=conn, as_of=as_of, allow_write=allow_write, cwd=cwd)

    observations: list[Observation] = []
    proposals: list[dict] = []
    notes: list[str] = []
    final: str | None = None
    evidence: list[int] = []

    for step in range(max_steps):
        rendered = [o.render() for o in observations]
        try:
            decision = chosen.decide(question, rendered, step)
        except Exception as exc:  # 适配器自身出错不应让整个 agent 崩掉
            notes.append(f"{chosen.name} 决策失败：{type(exc).__name__}: {exc}")
            break

        if decision.note:
            notes.append(decision.note)

        if decision.tool:
            if decision.tool not in TOOLS:
                notes.append(f"模型请求了不存在的工具「{decision.tool}」，已忽略")
                break
            result: ToolResult = call(decision.tool, ctx, decision.args)
            observations.append(
                Observation(
                    idx=len(observations) + 1,
                    tool=decision.tool,
                    args=decision.args,
                    ok=result.ok,
                    tier=result.tier,
                    text=result.text,
                    data=result.data,
                )
            )
            if result.has_proposal:
                proposals.append(result.data)
            continue

        if decision.final:
            final = decision.final
            evidence = decision.evidence
            break

        # 既没有工具也没有最终答复 → 交由汇总
        break

    if not final:
        final = _summarize(observations)
        # 汇总内容是机械拼接，把全部观测都算作依据
        evidence = [o.idx for o in observations]

    ok, issues = verify(final, evidence, [
        {"text": o.text, "data": o.data} for o in observations
    ])

    return Answer(
        question=question,
        text=final,
        observations=observations,
        evidence=evidence,
        adapter=chosen.name,
        skipped=skipped,
        citations_ok=ok,
        issues=issues,
        proposals=proposals,
        notes=notes,
    )
