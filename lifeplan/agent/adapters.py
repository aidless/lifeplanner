"""模型适配层：把「谁来决定下一步」抽象出来。

三种适配器，按优先级尝试：

    JevLocalAdapter    本地 Jev 检查点。**必须先通过健康检查**——
                       未训练或已退化的检查点会被拒绝，而不是拿随机权重胡说。
    OpenAICompatAdapter 任意 OpenAI 兼容端点（本地 Ollama / vLLM / 远程 API）。
    ScriptedAdapter    确定性规则路由器。永远可用，作为兜底。

为什么要兜底：一个 agent 如果只能靠模型驱动，模型不可用时它就完全不可用；
而一个只能靠规则驱动的 agent 又永远长不出推理能力。所以做成可替换的适配器，
并把「当前用的是什么、为什么」暴露给用户（`lifeplan agent doctor`）。
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Decision:
    """一步决策：要么给工具调用，要么给最终答复。"""

    tool: str | None = None
    args: dict = field(default_factory=dict)
    final: str | None = None
    note: str = ""
    evidence: list[int] = field(default_factory=list)   # 最终答复引用的观测序号


class BaseAdapter:
    name = "base"
    kind = "base"

    def status(self) -> tuple[bool, str]:
        """(是否可用, 原因)。"""
        raise NotImplementedError

    def decide(self, question: str, observations: list[dict], step: int) -> Decision:
        raise NotImplementedError

    def describe(self) -> str:
        ok, why = self.status()
        mark = "✓" if ok else "✗"
        return f"{mark} {self.name}（{self.kind}）：{why}"


# ── 本地 Jev 检查点 ─────────────────────────────────────────
class JevLocalAdapter(BaseAdapter):
    name = "jev-local"
    kind = "model"

    #: 探针损失高于此值即判定"没训练好"
    MAX_ACCEPTABLE_LOSS = 3.5

    def __init__(self, ckpt: str | None = None) -> None:
        self.ckpt = Path(
            ckpt
            or os.environ.get("LIFEPLAN_JEV_CKPT", "")
            or Path.home() / ".lifeplan" / "models" / "jev-1b.pt"
        )
        self._cache: tuple[bool, str] | None = None
        self._model = None

    # -- 依赖定位 --
    @staticmethod
    def _add_jev_path() -> bool:
        try:
            import jev  # noqa: F401

            return True
        except ImportError:
            pass
        # 同工作区的 jev1b 项目
        sibling = Path(__file__).resolve().parents[3] / "jev1b"
        if sibling.is_dir():
            sys.path.insert(0, str(sibling))
            try:
                import jev  # noqa: F401

                return True
            except ImportError:
                return False
        return False

    def status(self) -> tuple[bool, str]:
        if self._cache is not None:
            return self._cache
        self._cache = self._probe()
        return self._cache

    def _probe(self) -> tuple[bool, str]:
        if not self.ckpt.exists():
            return False, (
                f"未找到检查点 {self.ckpt}。Jev-1B 目前只有架构与参数量核算，"
                f"尚未预训练；训练后把 ckpt 放到该路径即可自动启用"
            )
        try:
            import torch  # noqa: F401
        except ImportError:
            return False, "未安装 torch，无法加载检查点"
        if not self._add_jev_path():
            return False, "无法导入 jev 包"

        try:
            import torch
            from jev.config import ModelConfig, get_config
            from jev.data import ByteTokenizer, generate_pairs, encode_batch
            from jev.losses import lm_loss
            from jev.model import build_model

            blob = torch.load(self.ckpt, map_location="cpu", weights_only=False)
            cfg = ModelConfig.from_dict(blob["config"]) if "config" in blob else get_config("jev-tiny")
            model = build_model(cfg)
            model.load_state_dict(blob["model"])
            model.eval()

            # 探针：在配对数据上量一次语言建模损失，判断权重是否真的学到东西
            tok = ByteTokenizer()
            pairs = generate_pairs(8, seed=7)
            ids, mask = encode_batch([p.view_a for p in pairs], tok, cfg.max_seq_len)
            with torch.no_grad():
                out = model(torch.from_numpy(ids))
                loss = float(lm_loss(out.logits, torch.from_numpy(ids), torch.from_numpy(mask)))

            if loss > self.MAX_ACCEPTABLE_LOSS:
                return False, (
                    f"检查点探针损失 {loss:.2f} > 阈值 {self.MAX_ACCEPTABLE_LOSS}，"
                    f"判定为未训练或已退化，拒绝用于决策"
                )
            self._model = (model, cfg, tok)
            return True, f"已加载 {self.ckpt.name}（探针损失 {loss:.2f}）"
        except Exception as exc:  # 加载失败的原因要如实呈现
            return False, f"加载失败：{type(exc).__name__}: {exc}"

    def decide(self, question: str, observations: list[dict], step: int) -> Decision:
        ok, why = self.status()
        if not ok:
            return Decision(note=why)

        from .prompts import SYSTEM_PROMPT, build_user_prompt
        from .tools import tool_catalog_lines

        try:
            import torch

            model, cfg, tok = self._model
            prompt = build_user_prompt(question, observations, tool_catalog_lines())
            text = SYSTEM_PROMPT + "\n\n" + prompt
            ids = tok.encode(text)[: cfg.max_seq_len]
            with torch.no_grad():
                out = model(torch.tensor([ids]))
            # 只做最小可用解码：取 argmax 续 96 个 token
            generated = list(ids)
            for _ in range(96):
                with torch.no_grad():
                    logits = model(torch.tensor([generated[-cfg.max_seq_len:]]))
                nxt = int(logits.logits[0, -1].argmax())
                if nxt == 2:   # EOS
                    break
                generated.append(nxt)
            raw = tok.decode(generated[len(ids):])
        except Exception as exc:
            return Decision(note=f"推理失败：{type(exc).__name__}: {exc}")

        from .prompts import parse_decision

        return parse_decision(raw)


# ── System 1 决策模型（Laya） ───────────────────────────────
class SystemOneAdapter(BaseAdapter):
    """用 Laya（开源 System 1 决策模型）做工具路由。

    与生成式 LLM 的分工：
        System 1  「下一步看哪个工具 / 信息够不够」——单次前向、~2s（本机 CPU）、
                   带校准概率、选项集按请求定义无需重训
        LLM       需要填参数、多步推理、生成最终措辞的场景

    硬边界（诚实声明）：
        · 只能路由**零参数工具**（7 个只读工具）。tier1/2 工具需要参数，
          System 1 给不了 → 升级给 escalate_to
        · 置信低于阈值 → 升级（act-or-escalate，Laya 自己的输出里就有 act_probability）
        · 出厂置信未完全校准（加载时官方会告警部分温标非法），
          所以这里的 confidence 只当门槛用，不当数值引用
    """

    name = "systemone"
    kind = "model"

    #: 置信低于此值升级给 LLM。Laya 官方自己说 base checkpoint 过自信，
    #: 这个门槛设得保守——宁可多升级，不硬路由。
    MIN_CONFIDENCE = 0.60

    def __init__(self, escalate_to: BaseAdapter | None = None) -> None:
        self.escalate_to = escalate_to
        self._agent = None
        self._note = ""

    # -- 可用性：只查「包在 + 权重已缓存」，不做重加载（Agent 初始化 ~20s）--
    def status(self) -> tuple[bool, str]:
        try:
            import laya  # noqa: F401
        except ImportError:
            return False, "未安装 laya 包（pip install laya）"
        try:
            from huggingface_hub import snapshot_download

            snapshot_download(
                os.environ.get(
                    "LIFEPLAN_S1_REPO", "convaiinnovations/laya-multilingual"
                ),
                local_files_only=True,
            )
        except Exception:
            return False, (
                "laya 已安装但权重未缓存。下载（约 650MB，国内走镜像）：\n"
                "  HF_ENDPOINT=https://hf-mirror.com HF_HUB_DISABLE_XET=1 \\\n"
                "  python -c \"from huggingface_hub import snapshot_download; "
                "snapshot_download('convaiinnovations/laya-multilingual')\""
            )
        note = "权重已缓存，首次调用加载约 20s（CPU）"
        if self._note:
            note += f"｜{self._note}"
        return True, note

    def _get_agent(self):
        """惰性加载，整个进程只加载一次。测试可 patch 此方法。"""
        if self._agent is None:
            from huggingface_hub import snapshot_download
            from laya import Agent

            path = snapshot_download(
                os.environ.get(
                    "LIFEPLAN_S1_REPO", "convaiinnovations/laya-multilingual"
                )
            )
            self._agent = Agent(path)
        return self._agent

    def _predict(self, state: str, questions: dict) -> dict:
        return self._get_agent().predict(state, questions)

    @staticmethod
    def _build_state(question: str, observations: list[dict], limit: int = 2400) -> str:
        """把对话状态压进 state 预算（multilingual 1024ctx，state 约 320 token）。"""
        parts = [f"主人的问题：{question}"]
        for i, o in enumerate(observations, 1):
            head = (o.get("text") or "").strip()[:300]
            parts.append(f"【{i}】{o.get('tool', '')} → {head}")
        text = "\n".join(parts)
        return text[:limit]

    @staticmethod
    def _zero_arg_tools() -> dict[str, str]:
        from .tools import TOOLS

        return {name: t.summary for name, t in TOOLS.items() if not t.params}

    def decide(self, question: str, observations: list[dict], step: int) -> Decision:
        tools = self._zero_arg_tools()
        criteria = {"none": "信息已足够，不需要更多工具，直接汇总作答"}
        criteria.update(tools)
        state = self._build_state(question, observations)
        questions = {
            "next_tool": {
                "type": "choice",
                "instructions": "下一步应该调用哪个工具来回答主人的问题？",
                "criteria": criteria,
            },
        }
        # 只有已经拿到过数据，才允许判断「够了」；否则必须先调工具
        if observations:
            questions["enough"] = {
                "type": "noul",
                "instructions": "仅凭已有信息是否足以回答主人的问题（无需更多工具）？",
            }

        try:
            res = self._predict(state, questions)
        except Exception as exc:
            return self._escalate(
                question, observations, step,
                f"System 1 推理失败：{type(exc).__name__}: {exc}",
            )

        answers = res.get("answers", {})

        # 1) enough 门控：已经拿够数据就停，让 loop 汇总
        gate = answers.get("enough")
        if gate and gate.get("noul", 0) >= 0.5 and observations:
            return Decision(
                final=None,
                note=f"System 1 判断信息已足够（p={gate.get('noul'):.2f}），停止收集",
            )

        # 2) 工具选择
        pick = answers.get("next_tool", {})
        tool = pick.get("choice")
        conf = pick.get("confidence", 0.0) or 0.0

        if tool in (None, "none"):
            if observations:
                return Decision(final=None, note="System 1 选择不再调用工具，直接汇总")
            return self._escalate(question, observations, step,
                                  "System 1 未选出工具且还没有任何数据")

        if tool not in tools:
            # 选中了需要参数的工具（tier1/2）——System 1 给不了参数
            return self._escalate(
                question, observations, step,
                f"System 1 选中 {tool}，但该工具需要参数，升级给 LLM",
            )
        if conf < self.MIN_CONFIDENCE:
            return self._escalate(
                question, observations, step,
                f"System 1 选了 {tool} 但置信 {conf:.2f} < {self.MIN_CONFIDENCE}，升级给 LLM",
            )
        return Decision(tool=tool, args={},
                        note=f"System 1 路由（置信 {conf:.2f}）")

    def _escalate(self, question, observations, step, reason: str) -> Decision:
        if self.escalate_to is not None:
            d = self.escalate_to.decide(question, observations, step)
            d.note = (f"[升级：{reason}] " + d.note).strip()
            return d
        return Decision(note=reason)


# ── OpenAI 兼容端点 ─────────────────────────────────────────
class OpenAICompatAdapter(BaseAdapter):
    """任意 OpenAI 兼容端点。

    未配置环境变量时会自动探测本机 Ollama（127.0.0.1:11434）：
    列出可用模型，按 PREFERRED 顺序挑一个，跳过嵌入类模型。
    选择过程记录在 chosen_note 里，doctor 会如实展示「为什么是它」。

    实测教训（本机，无可用 GPU，CPU 推理）：
        llama3.2:3b   首次 8.8s（含 7s 加载），之后 ~1s —— 可用
        qwen3:8b      180s 超时 —— CPU 上不现实
    所以默认偏好 3B。有 GPU 的机器用 LIFEPLAN_LLM_MODEL 覆盖即可。
    """

    name = "openai-compat"
    kind = "model"

    DEFAULT_BASE = "http://127.0.0.1:11434/v1"
    PREFERRED = ["llama3.2:3b", "qwen3:8b", "llama3.1:8b", "qwen2.5:7b", "gemma3:4b"]
    EMBED_HINTS = ("embed", "bge", "minilm", "e5-", "nomic", "rerank")

    def __init__(self) -> None:
        self.base = os.environ.get("LIFEPLAN_LLM_BASE_URL", "").rstrip("/") or None
        self.model = os.environ.get("LIFEPLAN_LLM_MODEL", "") or None
        self.key = os.environ.get("LIFEPLAN_LLM_API_KEY", "")
        self.timeout = int(os.environ.get("LIFEPLAN_LLM_TIMEOUT", "120"))
        self.chosen_note = ""
        self._json_mode_ok: bool | None = None   # None = 未探测

    # -- 端点与模型选择 --
    def _list_models(self, base: str) -> list[str] | None:
        try:
            with urllib.request.urlopen(f"{base}/models", timeout=5) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            return [m["id"] for m in body.get("data", []) if m.get("id")]
        except (urllib.error.URLError, json.JSONDecodeError, TimeoutError, KeyError, OSError):
            return None

    def _auto_discover(self) -> bool:
        models = self._list_models(self.DEFAULT_BASE)
        if not models:
            self.chosen_note = "未配置端点，且本机 11434 端口没有可用的 OpenAI 兼容服务"
            return False
        self.base = self.DEFAULT_BASE
        for want in self.PREFERRED:
            if want in models:
                self.model = want
                self.chosen_note = f"自动发现本机 Ollama，按偏好顺序选中 {want}（共 {len(models)} 个模型）"
                return True
        chat_like = [m for m in models if not any(h in m.lower() for h in self.EMBED_HINTS)]
        if chat_like:
            self.model = chat_like[0]
            self.chosen_note = f"自动发现本机 Ollama，未命中偏好列表，取第一个非嵌入模型 {self.model}"
            return True
        self.chosen_note = "端点可达，但列出的全是嵌入类模型，没有可用的对话模型"
        return False

    def status(self) -> tuple[bool, str]:
        if self.base is None:
            if not self._auto_discover():
                return False, self.chosen_note
        else:
            if not self.model:
                models = self._list_models(self.base)
                if models:
                    self.model = models[0]
                    self.chosen_note = f"端点已配置但未指定模型，取第一个 {self.model}"
                else:
                    return False, f"端点 {self.base} 无法访问（curl 一下确认服务在跑）"
        # 真探针：模型列表里要有这个模型
        models = self._list_models(self.base)
        if models is None:
            return False, f"端点 {self.base} 无法访问"
        if self.model not in models:
            return False, (f"模型 {self.model} 不在端点列表里。可用：{models[:6]}…"
                           f"（用 LIFEPLAN_LLM_MODEL 指定）")
        note = f"{self.model} @ {self.base}"
        if self.chosen_note:
            note += f"｜{self.chosen_note}"
        return True, note

    def decide(self, question: str, observations: list[dict], step: int) -> Decision:
        ok, why = self.status()
        if not ok:
            return Decision(note=why)

        from .prompts import SYSTEM_PROMPT, build_user_prompt, parse_decision
        from .tools import tool_catalog_lines

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": build_user_prompt(
                    question, observations, tool_catalog_lines())},
            ],
            "temperature": 0.1,
            "max_tokens": 400,
        }
        raw = self._chat(payload)
        if raw is None:
            return Decision(note="调用失败（详见上一条 note）")
        return parse_decision(raw)

    def _chat(self, payload: dict) -> str | None:
        """发一次请求，带 JSON 模式降级与错误透传。"""
        headers = {"Content-Type": "application/json",
                   **({"Authorization": f"Bearer {self.key}"} if self.key else {})}
        body = dict(payload)
        if self._json_mode_ok is not False:
            body["response_format"] = {"type": "json_object"}
        raw, err = self._post(body, headers)
        if raw is not None:
            self._json_mode_ok = True
            return raw
        # JSON 模式不被支持时降级重试一次
        if self._json_mode_ok is None and "response_format" in body:
            body.pop("response_format")
            raw, err2 = self._post(body, headers)
            if raw is not None:
                self._json_mode_ok = False
                return raw
            err = err2
        self._json_mode_ok = False
        self.chosen_note = f"调用失败：{err}"
        return None

    def _post(self, body: dict, headers: dict) -> tuple[str | None, str]:
        try:
            req = urllib.request.Request(
                f"{self.base}/chat/completions",
                data=json.dumps(body).encode("utf-8"),
                headers=headers,
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            content = data["choices"][0]["message"]["content"]
            return (content or ""), ""
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8")[:200]
            except Exception:
                pass
            return None, f"HTTP {exc.code}: {detail}"
        except (urllib.error.URLError, json.JSONDecodeError, TimeoutError, KeyError, OSError) as exc:
            return None, f"{type(exc).__name__}: {exc}"


# ── 确定性规则路由 ──────────────────────────────────────────
class ScriptedAdapter(BaseAdapter):
    """关键词路由。不产生任何模型输出，只决定调哪些工具。

    最终答复由 loop 根据工具结果拼装，因此每条陈述都能追到一次工具调用。
    """

    name = "scripted"
    kind = "scripted"

    RULES: list[tuple[str, list[str]]] = [
        (r"校准|准不准|靠谱|准确|命中率|过度自信|高估|低估|brier|ece|calibrat",
         ["plan_calibration"]),
        (r"自欺|审计|基线追认|代理反噬|作弊|通胀|audit", ["plan_audit"]),
        (r"宿主|这台机器|机器.*(干|用途)|职业|我是做什么|host|profile", ["host_profile"]),
        (r"到期|结算|due|该结算", ["predictions_due"]),
        (r"提议|待办|提案|该做什么|下一步|proposal", ["proposals_list"]),
        (r"目标|指标|goals?", ["plan_goals"]),
        (r"状态|进展|分数|怎么样|如何|总体|score|status", ["plan_score"]),
    ]


    #: 自然语言里要"执行命令"的请求，规则路由不猜参数，直接引导到 CLI
    EXEC_HINT = re.compile(r"执行|运行|跑一下|帮我跑|启动|打开.*应用|run |exec", re.I)

    def status(self) -> tuple[bool, str]:
        return True, "确定性规则路由（无需模型）"

    def _plan(self, question: str) -> list[str]:
        picked: list[str] = []
        for pattern, tools in self.RULES:
            if re.search(pattern, question, re.I):
                for t in tools:
                    if t not in picked:
                        picked.append(t)
        if not picked:
            picked = ["plan_score", "proposals_list"]
        return picked[:3]

    def decide(self, question: str, observations: list[dict], step: int) -> Decision:
        # 涉及执行类请求：不猜参数，交给明确指令
        if self.EXEC_HINT.search(question) and not observations:
            return Decision(
                final=(
                    "涉及执行或启动应用时，我不会从自然语言里猜命令参数——"
                    "猜错的代价可能不可逆。请用明确形式发起，系统会生成待确认提案：\n"
                    "  lifeplan term run -- <命令与参数>\n"
                    "  lifeplan apps launch <应用名>"
                )
            )

        plan = self._plan(question)
        if step < len(plan):
            return Decision(tool=plan[step], args={})
        return Decision(final=None)   # 交给 loop 汇总


def available_adapters(preferred: str | None = None) -> list[BaseAdapter]:
    """按优先级返回适配器实例（不做可用性过滤，便于 doctor 全量展示）。"""
    order: list[BaseAdapter] = [
        JevLocalAdapter(),
        SystemOneAdapter(),
        OpenAICompatAdapter(),
        ScriptedAdapter(),
    ]
    if preferred:
        order.sort(key=lambda a: 0 if a.name == preferred else 1)
    return order


def pick_adapter(preferred: str | None = None) -> tuple[BaseAdapter, list[str]]:
    """选主适配器；System 1 可用时构建「快路径 + 升级链」。

    链条语义：SystemOne 负责零参数工具的快速路由与"够了没有"的门控；
    置信不足 / 需要参数 / 推理失败时，升级给 LLM；LLM 不可用再落到 scripted。
    """
    skipped: list[str] = []
    ads = available_adapters(preferred)
    systemone = next((a for a in ads if isinstance(a, SystemOneAdapter)), None)
    if systemone and preferred in (None, "systemone"):
        ok, why = systemone.status()
        if ok:
            # 升级目标：从其余适配器里选第一个可用的
            for a in ads:
                if a is systemone:
                    continue
                a_ok, _ = a.status()
                if a_ok:
                    systemone.escalate_to = a
                    return systemone, skipped
            return systemone, skipped
        skipped.append(f"systemone: {why}")
        ads = [a for a in ads if a is not systemone]

    for ad in ads:
        ok, why = ad.status()
        if ok:
            return ad, skipped
        skipped.append(f"{ad.name}: {why}")
    return ScriptedAdapter(), skipped
