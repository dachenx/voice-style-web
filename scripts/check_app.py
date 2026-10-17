"""Run the locked application checks; use the project venv even from system Git hooks."""

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
RUNTIME_CONFIG = Path(".quality-local/runtime.json")


def validate_node(env):
    node = shutil.which("node", path=env.get("PATH", ""))
    npm = shutil.which("npm", path=env.get("PATH", ""))
    if not node or not npm:
        raise ValueError("找不到 node/npm；请安装 Node.js 22.12+，或在可用终端运行 python3 scripts/check_app.py --configure-node。")
    version = subprocess.run([node, "--version"], env=env, capture_output=True,
                             text=True, check=True, timeout=10).stdout.strip()
    match = re.fullmatch(r"v(\d+)\.(\d+)\.(\d+)", version)
    if not match or tuple(map(int, match.groups())) < (22, 12, 0):
        raise ValueError("Node.js 版本不符合要求，需要 22.12.0 或更高版本。")
    subprocess.run([npm, "--version"], env=env, capture_output=True,
                   text=True, check=True, timeout=10)


def configured_environment(root):
    env = dict(os.environ)
    config = root / RUNTIME_CONFIG
    try:
        value = json.loads(config.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return env
    except (OSError, UnicodeError, ValueError) as exc:
        raise ValueError("本地运行时配置无法读取；请重新运行 --configure-node。") from exc
    if (not isinstance(value, dict) or set(value) != {"node_bin"}
            or not isinstance(value["node_bin"], str)):
        raise ValueError("本地运行时配置格式错误；请重新运行 --configure-node。")
    directory = Path(value["node_bin"])
    if (not directory.is_absolute() or not directory.is_dir()
            or not shutil.which("node", path=str(directory))
            or not shutil.which("npm", path=str(directory))):
        raise ValueError("本地运行时配置路径失效；请重新运行 --configure-node。")
    env["PATH"] = str(directory) + os.pathsep + env.get("PATH", "")
    return env


def configure_node(root):
    env = dict(os.environ)
    node = shutil.which("node", path=env.get("PATH", ""))
    if not node:
        raise ValueError("当前终端找不到 node，请先启用已安装的 Node.js。")
    # Keep the executable's bin directory, not the target of an npm symlink.
    directory = Path(node).absolute().parent
    if not shutil.which("npm", path=str(directory)):
        raise ValueError("所选 Node 目录没有 npm，请使用完整的 Node.js 安装。")
    env["PATH"] = str(directory) + os.pathsep + env.get("PATH", "")
    validate_node(env)
    config = root / RUNTIME_CONFIG
    config.parent.mkdir(parents=True, exist_ok=True)
    temporary = config.with_suffix(".tmp")
    temporary.write_text(json.dumps({"node_bin": str(directory)}) + "\n", encoding="utf-8")
    temporary.replace(config)
    print("已验证并记录本机 Node 路径至 .quality-local/runtime.json（不入库）。")


def main(argv=None):
    parser = argparse.ArgumentParser(description="执行项目应用验收")
    parser.add_argument("--configure-node", action="store_true", help="记录当前终端可用的 Node 路径，仅保存于本机")
    args = parser.parse_args(argv)
    if args.configure_node:
        configure_node(ROOT)
        return
    if not PYTHON.exists():
        raise ValueError("缺少项目 .venv，请按《项目说明.md》安装后端环境。")
    env = configured_environment(ROOT)
    validate_node(env)
    checks = [
        ([str(PYTHON), "-m", "ruff", "check", "--config", "backend/pyproject.toml", "backend"], 180),
        ([str(PYTHON), "-m", "mypy", "--config-file", "backend/pyproject.toml", "backend/app"], 180),
        ([str(PYTHON), "-m", "pytest", "-q", "backend/tests"], 180),
        (["npm", "--prefix", "frontend", "run", "lint"], 180),
        (["npm", "--prefix", "frontend", "run", "typecheck"], 180),
        (["npm", "--prefix", "frontend", "run", "build"], 180),
        # The full desktop/mobile suite has its own bounded budget; individual tests are unchanged.
        (["npm", "--prefix", "frontend", "run", "test:e2e"], 300),
    ]
    for command, timeout in checks:
        print("RUN: " + " ".join(command[1:]), flush=True)
        subprocess.run(command, cwd=ROOT, env=env, check=True, timeout=timeout)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        sys.exit(str(exc))
