"""测试包初始化。

在这里设置测试专属的环境默认值，保证任何测试模块被加载前就生效：

- LIFEPLAN_JOURNAL_MODE=memory：测试不需要跨进程崩溃恢复，而沙箱/CI
  环境可能拒绝 SQLite 的 -journal 文件操作（实测会随机让整个测试基建瘫痪）。
- LIFEPLAN_HOME 由各测试类的 setUp 用 tempfile 指定，不在此处设置。
"""

import os

os.environ.setdefault("LIFEPLAN_JOURNAL_MODE", "memory")
