#!/usr/bin/env bash
# 一键演示：从零建库到产出看板，全流程约 3 秒。
# 用法：./scripts/demo.sh [输出目录]
set -euo pipefail

OUT="${1:-/tmp/lifeplan-demo}"
PY="${PYTHON:-python3}"

cd "$(dirname "$0")/.."
export LIFEPLAN_HOME="$OUT"
rm -rf "$OUT"
mkdir -p "$OUT"

echo "▸ 初始化数据库并写入种子数据"
$PY -m lifeplan init --seed

echo
echo "▸ 当前状态"
$PY -m lifeplan score

echo "▸ 自我预测校准"
$PY -m lifeplan calibrate

echo "▸ 自我审计"
$PY -m lifeplan audit

echo "▸ 记录一条新观测（tier 3 可核验）"
$PY -m lifeplan observe --metric 1 --value 9 --evidence "https://github.com/aidless" --note "本周新增"

echo "▸ 登记一条预测"
$PY -m lifeplan predict --goal 1 --p 0.75 --horizon 2026-11-05 --statement "两周内拿到一次面试"

echo
echo "▸ 生成产出物"
$PY -m lifeplan report --out "$OUT/report.md"
$PY -m lifeplan dashboard --out "$OUT/dashboard.html"
$PY -m lifeplan export --out "$OUT/data.json"

echo
echo "✓ 完成。产出物："
ls -la "$OUT" | sed 's/^/  /'
echo
echo "  用浏览器打开：$OUT/dashboard.html"
