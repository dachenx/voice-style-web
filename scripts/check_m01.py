"""Run the locked M01 checks; use the project venv even from system Git hooks."""

from pathlib import Path
import os
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def main():
    if not PYTHON.exists() or not shutil.which("npm"):
        raise SystemExit("请先按 README 安装 .venv、Node.js 与前端依赖。")
    checks = [
        [str(PYTHON), "-m", "ruff", "check", "--config", "backend/pyproject.toml", "backend"],
        [str(PYTHON), "-m", "mypy", "--config-file", "backend/pyproject.toml", "backend/app"],
        [str(PYTHON), "-m", "pytest", "-q", "backend/tests"],
        ["npm", "--prefix", "frontend", "run", "lint"],
        ["npm", "--prefix", "frontend", "run", "typecheck"],
        ["npm", "--prefix", "frontend", "run", "build"],
        ["npm", "--prefix", "frontend", "run", "test:e2e"],
    ]
    for command in checks:
        print("RUN: " + " ".join(command[1:]), flush=True)
        subprocess.run(command, cwd=ROOT, check=True, timeout=180)


if __name__ == "__main__":
    try:
        main()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        sys.exit(str(exc))
