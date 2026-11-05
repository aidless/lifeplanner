"""诚实性漂移守卫。

文档里的测试条数、CI 的 Python 版本、以及「分数 ≠ 生活结果」这几句
如果和代码分叉，这里失败。历史笔记里的旧数字不在扫描范围内。
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
README = (ROOT / "README.md").read_text(encoding="utf-8")
WORKFLOWS = ROOT / ".github" / "workflows"

# 拆开写，避免本文件自己的字面量被「绝对路径」扫描命中。
AUTHOR_PATH = re.compile("/" + "Users/" + "|" + "/" + "home/")
TEXT_SUFFIX = {
    ".md", ".py", ".html", ".yml", ".yaml", ".js", ".css", ".json",
    ".toml", ".sh", ".txt", ".svg",
}
SKIP_DIRS = {".git", "dist", "__pycache__", ".impeccable"}


def _suite_count() -> int:
    loader = unittest.TestLoader()
    suite = loader.discover(str(ROOT / "tests"), top_level_dir=str(ROOT))
    return suite.countTestCases()


class TestHonestyDrift(unittest.TestCase):
    def test_problem_statement_is_at_most_three_lines(self):
        match = re.search(
            r"<!-- problem:start -->\n(.*?)\n<!-- problem:end -->",
            README,
            re.S,
        )
        self.assertIsNotNone(match, "README 缺少 problem 标记")
        lines = [ln for ln in match.group(1).splitlines() if ln.strip()]
        self.assertLessEqual(len(lines), 3, lines)
        self.assertGreaterEqual(len(lines), 1)

    def test_readme_labels_synthetic_data_and_scores(self):
        self.assertIn("虚构", README)
        self.assertIn("不是生活结果", README)
        self.assertIn("landing/demo/dashboard.html", README)
        self.assertIn("aidless/life-planner", README)
        self.assertIn("不共享数据库", README)
        self.assertIn("FAILURE_MODES.md", README)

    def test_documented_test_count_matches_suite(self):
        match = re.search(r"honesty:test-count=(\d+)", README)
        self.assertIsNotNone(match, "README 缺少 honesty:test-count")
        self.assertEqual(int(match.group(1)), _suite_count())
        for path in (ROOT / "PRODUCT.md", ROOT / "charter" / "PROJECT_CHARTER.md"):
            text = path.read_text(encoding="utf-8")
            stray = re.findall(r"\d+\s*[个项]\s*测试", text)
            self.assertEqual(stray, [], f"{path.name} 仍手写测试条数：{stray}")

    def test_ci_python_matches_marker_and_is_single_matrix(self):
        match = re.search(r"honesty:python=([0-9.,]+)", README)
        self.assertIsNotNone(match, "README 缺少 honesty:python")
        expected = match.group(1).split(",")
        names = sorted(p.name for p in WORKFLOWS.glob("*.yml"))
        self.assertEqual(names, ["ci.yml", "pages.yml"])
        ci = (WORKFLOWS / "ci.yml").read_text(encoding="utf-8")
        block = re.search(r"python-version:\s*\[([^\]]+)\]", ci)
        self.assertIsNotNone(block)
        got = re.findall(r"\d+\.\d+", block.group(1))
        self.assertEqual(got, expected)
        self.assertIn("python -m unittest discover -s tests", ci)
        pages = (WORKFLOWS / "pages.yml").read_text(encoding="utf-8")
        page_py = re.search(r'python-version:\s*"(\d+\.\d+)"', pages)
        self.assertIsNotNone(page_py)
        self.assertIn(page_py.group(1), expected)
        self.assertIn("make test", pages)

    def test_no_absolute_author_paths(self):
        hits = []
        for path in ROOT.rglob("*"):
            if not path.is_file():
                continue
            if any(part in SKIP_DIRS for part in path.parts):
                continue
            if path.suffix.lower() not in TEXT_SUFFIX:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            if AUTHOR_PATH.search(text):
                hits.append(str(path.relative_to(ROOT)))
        self.assertEqual(hits, [])

    def test_no_fake_badges_or_adopters(self):
        blob = README + "\n" + (ROOT / "landing" / "index.html").read_text(encoding="utf-8")
        self.assertNotIn("shields.io", blob)
        self.assertNotIn("github/stars", blob)
        for phrase in ("位用户", "已被", "下载量", "推荐语", "testimonials"):
            self.assertNotIn(phrase, blob)

    def test_failure_modes_cover_miscalibration_and_false_greens(self):
        text = (ROOT / "FAILURE_MODES.md").read_text(encoding="utf-8")
        self.assertIn("误校准", text)
        self.assertIn("假绿", text)
        self.assertIn("样本不足", text)
        self.assertIn("workspace_jail", text)
        self.assertIn("fsmonitor", text)
        self.assertIn("不是生活结果", text)
        self.assertIn("https://", text)


if __name__ == "__main__":
    unittest.main()
