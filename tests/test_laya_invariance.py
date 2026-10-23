"""Laya 选项顺序不变性（阶段八的必要前置）。

为什么必须测：如果打乱选项顺序就换答案，那么「模型判断」掺了位置偏置，
所有 choice 题的概率都不能当证据用。蓝图 §13 把它列为决策任务的必测项。

设计：
    · 同一 state、同一组选项，只改顺序，跑 3 个排列
    · 每个选项的概率在排列间的最大差（max |Δp|）应小于 0.05
    · 结果写入 benchmarks/calibration/laya_invariance.json（benchmark 记录）

需要 laya 包 + 已缓存权重；任一缺失则 skip（不伪装成通过）。
"""

from __future__ import annotations

import itertools
import json
import os
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ARTIFACT = ROOT / "benchmarks" / "calibration" / "laya_invariance.json"

STATE = ("项目投入决策。state：这个开源项目还值得继续投入吗。"
         "价值总评 72/100，投入强度 41 次/30天，证据密度 12 条/30天，"
         "自评准确度 0.88，结构完整度 1.0")
CRITERIA = {
    "continue": "价值与证据都支持继续",
    "pivot": "方向需调整",
    "pause": "暂缓观察",
    "stop": "投入产出不成立",
    "collect_more_evidence": "关键证据缺失，无法判断",
}
EPS = 0.05


def _laya_available() -> tuple[bool, str]:
    try:
        import laya  # noqa: F401
    except ImportError:
        return False, "未安装 laya"
    try:
        from huggingface_hub import snapshot_download

        snapshot_download(os.environ.get("LIFEPLAN_S1_REPO",
                                        "convaiinnovations/laya-multilingual"),
                          local_files_only=True)
    except Exception:
        return False, "权重未缓存"
    return True, "ok"


OK, WHY = _laya_available()


@unittest.skipUnless(OK, f"Laya 不可用（{WHY}）")
class TestOptionOrderInvariance(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import resource

        from laya import Agent
        from huggingface_hub import snapshot_download

        path = snapshot_download(os.environ.get("LIFEPLAN_S1_REPO",
                                                "convaiinnovations/laya-multilingual"),
                                 local_files_only=True)
        try:
            cls.agent = Agent(path)
        except MemoryError as exc:
            # 沙箱 RLIMIT_AS 限制（本机实测 4GB）会让 safetensors mmap 失败。
            # 记录下来，并写一份 blocked 证据——不伪装成通过，也不报 Error。
            limit = resource.getrlimit(resource.RLIMIT_AS)[0]
            cls._write_blocked(limit, str(exc))
            raise unittest.SkipTest(
                f"内存不足无法加载 Laya（RLIMIT_AS={limit}）：{exc}。"
                f"需在可用内存 ≥8GB 的机器上运行本测试。"
            )

    @staticmethod
    def _write_blocked(limit: int | None, err: str) -> None:
        ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
        ARTIFACT.write_text(json.dumps({
            "test": "laya_option_order_invariance",
            "status": "blocked_memory",
            "rlimit_as_bytes": limit,
            "error": err,
            "note": "沙箱进程地址空间受限导致 safetensors mmap 失败；"
                    "测试未执行，不产生任何通过/失败结论。",
        }, ensure_ascii=False, indent=2), encoding="utf-8")

    def _probs(self, options: tuple[str, ...]) -> dict:
        questions = {
            "action": {
                "type": "choice",
                "instructions": "这个项目应该继续投入、调整方向、暂停、停止，还是先补充证据？",
                "criteria": {o: CRITERIA[o] for o in options},
            }
        }
        res = self.agent.predict(STATE, questions)
        return dict(res["answers"]["action"].get("probabilities") or {})

    def test_probabilities_stable_across_permutations(self):
        base = list(CRITERIA)
        orders = [
            tuple(base),
            tuple(reversed(base)),
            tuple(base[2:] + base[:2]),
        ]
        results = [self._probs(o) for o in orders]

        max_delta = 0.0
        per_option: dict[str, float] = {}
        for opt in base:
            vals = [r.get(opt, 0.0) for r in results]
            delta = max(vals) - min(vals)
            per_option[opt] = round(delta, 4)
            max_delta = max(max_delta, delta)

        # 记录到 benchmark 目录（无论通过与否都留证）
        ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
        ARTIFACT.write_text(json.dumps({
            "test": "laya_option_order_invariance",
            "state": STATE,
            "orders": [list(o) for o in orders],
            "probabilities": results,
            "max_delta_per_option": per_option,
            "max_delta": round(max_delta, 4),
            "epsilon": EPS,
            "passed": max_delta <= EPS,
        }, ensure_ascii=False, indent=2), encoding="utf-8")

        self.assertLessEqual(
            max_delta, EPS,
            f"选项顺序改变了概率（max |Δp|={max_delta:.3f}）——"
            f"逐项：{per_option}。详见 {ARTIFACT.relative_to(ROOT)}",
        )

    def test_sum_of_probabilities_is_one(self):
        probs = self._probs(tuple(CRITERIA))
        self.assertAlmostEqual(sum(probs.values()), 1.0, places=2)


if __name__ == "__main__":
    unittest.main()
