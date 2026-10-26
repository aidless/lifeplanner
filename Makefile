.PHONY: help test check demo week report dashboard backup doctor site clean

PY ?= python3

help:
	@echo "make test      运行全部测试"
	@echo "make check     语法与死代码检查"
	@echo "make demo      在临时目录跑一遍完整流程"
	@echo "make week      每周复盘（需要交互）"
	@echo "make report    生成周报 examples/report.md"
	@echo "make dashboard 生成看板 examples/dashboard.html"
	@echo "make site      构建落地页 + 示例看板到 dist/"
	@echo "make backup    备份数据库"
	@echo "make doctor    系统体检"

test:
	$(PY) -m unittest discover -s tests

check:
	$(PY) -m compileall -q lifeplan
	@echo "未定义/未使用的公开函数："
	@$(PY) scripts/find_dead.py

demo:
	./scripts/demo.sh /tmp/lifeplan-demo

week:
	$(PY) -m lifeplan week

report:
	@mkdir -p examples
	$(PY) -m lifeplan report --out examples/report.md

dashboard:
	@mkdir -p examples
	$(PY) -m lifeplan dashboard --out examples/dashboard.html

site:
	$(PY) scripts/build_site.py

backup:
	$(PY) -m lifeplan backup

doctor:
	$(PY) -m lifeplan doctor

clean:
	find . -name "__pycache__" -type d -prune -exec rm -rf {} +
