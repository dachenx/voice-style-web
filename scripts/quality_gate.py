#!/usr/bin/env python3
"""Small, dependency-free project gate. Never calls an AI or pushes commits."""

import argparse
import ast
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys


class GateError(Exception):
    pass


def git(root, *args):
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True)
    if result.returncode:
        raise GateError(result.stderr.decode(errors="replace").strip())
    return result.stdout


def root_dir():
    return Path(git(Path.cwd(), "rev-parse", "--show-toplevel").decode().strip())


def files(root):
    names = git(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
    result = {}
    for name in sorted(set(names.decode().split("\0")) - {""}):
        path = root / name
        if path.is_symlink():
            raise GateError("不允许入库符号链接：" + name)
        if path.is_file():
            result[name] = ("100755" if path.stat().st_mode & 0o111 else "100644", path.read_bytes())
    return result


def digest(entries):
    h = hashlib.sha256()
    for name, (mode, content) in sorted(entries.items()):
        if name.startswith("docs/reviews/"):
            continue
        h.update(name.encode() + b"\0" + mode.encode() + b"\0")
        h.update(hashlib.sha256(content).digest())
    return h.hexdigest()


def module(root):
    try:
        result = json.loads((root / "quality/active-module.json").read_text())
    except (OSError, ValueError) as exc:
        raise GateError("模块配置无法读取：" + str(exc)) from exc
    if not re.fullmatch(r"M\d{2,}", result.get("id", "")):
        raise GateError("模块 ID 格式错误")
    if not result.get("allowed_paths") or not result.get("acceptance") or not result.get("checks"):
        raise GateError("必须声明模块范围、验收标准和功能检查命令")
    if result.get("review") != "docs/reviews/" + result["id"] + ".json":
        raise GateError("审查路径必须与当前模块对应")
    return result


def base_files(root, config):
    base = config.get("base_commit")
    if base is None:
        if config["id"] != "M00":
            raise GateError("M00 以后必须固定 base_commit")
        return {}
    if not isinstance(base, str) or not re.fullmatch(r"[0-9a-f]{40}", base):
        raise GateError("base_commit 必须是完整提交 SHA")
    git(root, "merge-base", "--is-ancestor", base, "HEAD")
    entries = {}
    for record in git(root, "ls-tree", "-r", "-z", base).split(b"\0"):
        if not record:
            continue
        meta, name = record.split(b"\t", 1)
        mode, kind, oid = meta.decode().split()
        if kind != "blob":
            raise GateError("不支持子模块")
        entries[name.decode()] = (mode, git(root, "cat-file", "blob", oid))
    return entries


def quick(root, config, entries):
    errors = []
    for required in ("AGENTS.md", "docs/DECISIONS.md", "docs/OPTIMIZATIONS.md"):
        if required not in entries:
            errors.append("缺少记录文件：" + required)
    baseline = base_files(root, config)
    changed = [p for p in set(entries) | set(baseline) if entries.get(p) != baseline.get(p)]
    for name in changed:
        if not any(name.startswith(p) if p.endswith("/") else name == p for p in config["allowed_paths"]):
            errors.append("超出当前模块范围：" + name)
    forbidden_extensions = {".wav", ".mp3", ".m4a", ".mp4", ".flac", ".ogg", ".pt", ".pth", ".ckpt", ".safetensors", ".onnx", ".db", ".sqlite", ".sqlite3", ".pem", ".key"}
    secret_patterns = [r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----", r"\bgh[pousr]_[A-Za-z0-9]{30,}\b", r"\bgithub_pat_[A-Za-z0-9_]{30,}\b", r"\bsk-[A-Za-z0-9_-]{30,}\b"]
    for name, (_, content) in entries.items():
        path = Path(name)
        if (path.suffix.lower() in forbidden_extensions or
                (path.name.startswith(".env") and path.name != ".env.example") or
                path.parts[0] in {"data", "uploads", "outputs", "models", "checkpoints"}):
            errors.append("不允许公开提交素材、模型、数据库或凭据：" + name)
        if len(content) > 2 * 1024 * 1024:
            errors.append("文件超过 2 MiB，须另行确认存储方式：" + name)
            continue
        try:
            source = content.decode("utf-8")
        except UnicodeDecodeError:
            errors.append("不允许未声明的二进制文件：" + name)
            continue
        if any(re.search(pattern, source) for pattern in secret_patterns):
            errors.append("发现疑似密钥（内容不显示）：" + name)
        for i, line in enumerate(source.splitlines(), 1):
            if re.match(r"^(<{7}|={7}|>{7})( |$)", line):
                errors.append("合并冲突标记：%s:%d" % (name, i))
            if line.rstrip() != line:
                errors.append("末尾空白：%s:%d" % (name, i))
        try:
            if path.suffix == ".py":
                ast.parse(source, filename=name)
            elif path.suffix == ".json":
                json.loads(source)
        except (SyntaxError, ValueError) as exc:
            errors.append("语法错误 %s: %s" % (name, exc))
    if errors:
        raise GateError("\n".join(errors))


def assert_index(root, entries):
    index = {}
    for record in git(root, "ls-files", "--stage", "-z").split(b"\0"):
        if not record:
            continue
        meta, name = record.split(b"\t", 1)
        mode, oid, stage = meta.decode().split()
        if stage != "0":
            raise GateError("索引存在未解决冲突")
        index[name.decode()] = (mode, git(root, "cat-file", "blob", oid))
    if index != entries:
        raise GateError("索引与工作区不一致；请明确暂存全部本模块修改后重新检查")


def assert_review(root, config, entries):
    try:
        review = json.loads((root / config["review"]).read_text())
    except (OSError, ValueError) as exc:
        raise GateError("缺少有效的独立监督审查记录") from exc
    if review.get("module") != config["id"] or review.get("verdict") != "PASS":
        raise GateError("监督员尚未通过当前模块")
    if not review.get("reviewer") or not review.get("evidence"):
        raise GateError("审查必须记录真实角色与验收证据")
    if review.get("findings"):
        raise GateError("审查仍有未解决问题")
    if review.get("digest") != digest(entries):
        raise GateError("审查已过时；当前内容改变，需要监督员重新审查")


def run_checks(root, config):
    for check in config["checks"]:
        argv = check.get("argv")
        if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
            raise GateError("验收命令必须为非空字符串数组")
        argv = [sys.executable if a == "{python}" else a for a in argv]
        timeout = check.get("timeout", 120)
        if not isinstance(timeout, int) or not 1 <= timeout <= 600:
            raise GateError("验收 timeout 必须为 1–600 秒")
        print("CHECK: " + check.get("name", argv[0]), flush=True)
        try:
            result = subprocess.run(argv, cwd=root, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise GateError("功能检查无法完成：" + str(exc)) from exc
        if result.returncode:
            raise GateError("功能检查失败：" + check.get("name", argv[0]))


def commit_message(root, file):
    config = module(root)
    lines = Path(file).read_text().splitlines()
    first = lines[0] if lines else ""
    if not re.match(r"^(feat|fix|docs|test|refactor|chore)\(" + config["id"] + r"\): .+", first):
        raise GateError("提交说明需使用 type(" + config["id"] + "): 内容")


def push_refs(root, stream):
    head = git(root, "rev-parse", "HEAD").decode().strip()
    branch = git(root, "symbolic-ref", "HEAD").decode().strip()
    for line in stream.splitlines():
        local_ref, local_sha, remote_ref, remote_sha = line.split()
        if local_ref != branch or remote_ref != branch or local_sha != head:
            raise GateError("仅允许推送当前分支 HEAD 到同名分支；不自动删除分支或推送其他版本")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["quick", "full", "digest", "commit-msg", "pre-push"])
    parser.add_argument("file", nargs="?")
    parser.add_argument("--no-review", action="store_true")
    parser.add_argument("--index", action="store_true")
    args = parser.parse_args()
    try:
        root = root_dir()
        if args.mode == "commit-msg":
            commit_message(root, args.file)
            return 0
        entries = files(root)
        if args.mode == "digest":
            print(digest(entries))
            return 0
        config = module(root)
        quick(root, config, entries)
        if args.mode == "pre-push":
            if args.file not in {"https://github.com/dachenx/voice-style-web.git", "git@github.com:dachenx/voice-style-web.git"}:
                raise GateError("远端不是已授权的 dachenx/voice-style-web 仓库")
            push_refs(root, sys.stdin.read())
            if git(root, "status", "--porcelain", "--untracked-files=all").strip():
                raise GateError("推送前工作区必须干净，确保测试的就是待推送版本")
        if args.mode in {"full", "pre-push"}:
            run_checks(root, config)
            if files(root) != entries:
                raise GateError("检查命令改动了入库文件；请重审并重新检查")
            if not args.no_review:
                assert_review(root, config, entries)
        if args.index:
            assert_index(root, entries)
        print("PASS: " + config["id"] + " / " + args.mode)
        return 0
    except (GateError, OSError, ValueError) as exc:
        print("BLOCK: " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
