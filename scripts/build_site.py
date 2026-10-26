#!/usr/bin/env python3
"""Build the static site: landing + a freshly generated demo dashboard."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = Path(os.environ.get("DIST", ROOT / "dist")).resolve()


def run(args: list[str], home: str) -> None:
    env = os.environ.copy()
    env["LIFEPLAN_HOME"] = home
    subprocess.run(
        [sys.executable, "-m", "lifeplan", *args],
        cwd=ROOT,
        env=env,
        check=True,
        stdout=subprocess.DEVNULL,
    )


def main() -> None:
    if DIST.exists():
        shutil.rmtree(DIST)
    shutil.copytree(ROOT / "landing", DIST)

    with tempfile.TemporaryDirectory(prefix="lifeplan-site-") as home:
        run(["init", "--seed"], home)
        demo = DIST / "demo"
        demo.mkdir(parents=True, exist_ok=True)
        run(["dashboard", "--out", str(demo / "dashboard.html")], home)
        run(["report", "--out", str(demo / "report.md")], home)
        run(["export", "--out", str(demo / "data.json")], home)

    print(f"site written to {DIST}")
    print(f"landing: {DIST / 'index.html'}")
    print(f"demo:    {DIST / 'demo' / 'dashboard.html'}")


if __name__ == "__main__":
    main()
