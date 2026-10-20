#!/usr/bin/env python3
"""Codex event adapter. Writes only ignored local state; no trust bypass."""

import json
from pathlib import Path
import subprocess
import sys

from quality_gate import GateError, digest, files, git


def main():
    event = json.load(sys.stdin)
    root = Path(git(Path(event.get("cwd", ".")), "rev-parse", "--show-toplevel").decode().strip())
    if not (root / "quality/active-module.json").exists():
        print("{}")
        return
    kind = event.get("hook_event_name")
    state_dir = root / ".quality-local"
    state_dir.mkdir(exist_ok=True)
    state_file = state_dir / "last-quick-digest"
    fingerprint = digest(files(root))
    if kind == "PostToolUse":
        if state_file.exists() and state_file.read_text() == fingerprint:
            print("{}")
            return
        mode = "quick"
    elif kind == "Stop":
        if not git(root, "status", "--porcelain", "--untracked-files=all").strip():
            print("{}")
            return
        if state_file.exists() and state_file.read_text() == fingerprint:
            print("{}")
            return
        mode = "quick"
    else:
        print("{}")
        return
    try:
        result = subprocess.run([sys.executable, str(root / "scripts/quality_gate.py"), mode], cwd=root, capture_output=True, text=True, timeout=150)
        detail = (result.stdout + result.stderr)[-6000:]
        ok = result.returncode == 0
    except subprocess.TimeoutExpired:
        detail, ok = "检查超时，必须手动检查，不能视为通过。", False
    if mode == "quick" and ok:
        state_file.write_text(fingerprint)
    if not ok:
        reason = "基础检查未通过，请修复后重查；不要直接提交。\n" + detail
        if kind == "Stop" and event.get("stop_hook_active"):
            print(json.dumps({"systemMessage": reason + "\n已提醒一次，结束时须如实报告阻塞。"}, ensure_ascii=False))
        else:
            print(json.dumps({"decision": "block", "reason": reason}, ensure_ascii=False))
    elif kind == "PostToolUse":
        print(json.dumps({"hookSpecificOutput": {"hookEventName": kind, "additionalContext": "基础检查通过；按改动风险执行定向验证，必要时安排独立审查。"}}, ensure_ascii=False))
    else:
        print(json.dumps({"systemMessage": "基础检查通过；不重复功能全套，交付时如实报告验证和剩余问题。"}, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except (GateError, OSError, ValueError) as exc:
        print(json.dumps({"systemMessage": "Hook 自身失败，需手动检查：" + str(exc)}, ensure_ascii=False))
        sys.exit(1)
