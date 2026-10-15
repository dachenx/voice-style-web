"""Exercise gate failures and real Git hooks in disposable repositories."""

import contextlib
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import quality_gate as gate


PROJECT = Path(__file__).resolve().parents[1]


class QualityGateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.run_git("init", "-b", "main")
        self.run_git("config", "user.name", "Fixture")
        self.run_git("config", "user.email", "fixture@example.invalid")
        self.write(".gitignore", ".quality-local/\n__pycache__/\n")
        for name in ("AGENTS.md", "docs/DECISIONS.md", "docs/OPTIMIZATIONS.md"):
            self.write(name, "Fixture\n")
        self.write("scripts/feature.py", "answer = 42\n")
        self.config = {
            "id": "M00", "base_commit": None,
            "allowed_paths": [".gitignore", "AGENTS.md", "docs/", "scripts/", "quality/", ".githooks/"],
            "acceptance": ["fixture acceptance"],
            "checks": [{"name": "behavior", "argv": ["{python}", "-c", "assert 2 + 2 == 4"], "timeout": 10}],
            "review": "docs/reviews/M00.json"
        }
        self.save_config()

    def write(self, name, content):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    def save_config(self):
        self.write("quality/active-module.json", json.dumps(self.config))

    def run_git(self, *args, check=True):
        return subprocess.run(["git", "-C", str(self.root), *args], capture_output=True, text=True, check=check)

    def approve(self):
        self.write(self.config["review"], json.dumps({
            "module": self.config["id"], "reviewer": "test fixture only",
            "verdict": "PASS", "digest": gate.digest(gate.files(self.root)),
            "evidence": ["synthetic test record, not a real project approval"], "findings": []
        }))

    def test_valid_scope_and_review(self):
        self.approve()
        entries = gate.files(self.root)
        gate.quick(self.root, self.config, entries)
        gate.assert_review(self.root, self.config, entries)

    def test_scope_drift_and_deleted_required_doc_block(self):
        self.write("backend/app.py", "pass\n")
        with self.assertRaisesRegex(gate.GateError, "超出"):
            gate.quick(self.root, self.config, gate.files(self.root))
        (self.root / "backend/app.py").unlink()
        (self.root / "docs/DECISIONS.md").unlink()
        with self.assertRaisesRegex(gate.GateError, "缺少"):
            gate.quick(self.root, self.config, gate.files(self.root))

    def test_python_json_and_conflicts_block(self):
        for text in ("def invalid(\n", "<<<<<<< ours\n"):
            self.write("scripts/feature.py", text)
            with self.assertRaises(gate.GateError):
                gate.quick(self.root, self.config, gate.files(self.root))
        self.write("scripts/feature.py", "pass\n")
        self.write("quality/bad.json", "{bad")
        with self.assertRaisesRegex(gate.GateError, "语法"):
            gate.quick(self.root, self.config, gate.files(self.root))

    def test_private_material_and_secret_block_without_echo(self):
        self.write("docs/reference.wav", "private sample")
        with self.assertRaisesRegex(gate.GateError, "不允许"):
            gate.quick(self.root, self.config, gate.files(self.root))
        (self.root / "docs/reference.wav").unlink()
        secret = "ghp_" + "a" * 36
        self.write("docs/config.txt", secret)
        with self.assertRaises(gate.GateError) as caught:
            gate.quick(self.root, self.config, gate.files(self.root))
        self.assertNotIn(secret, str(caught.exception))

    def test_stale_review_and_unresolved_findings_block(self):
        self.approve()
        self.write("scripts/feature.py", "answer = 43\n")
        with self.assertRaisesRegex(gate.GateError, "过时"):
            gate.assert_review(self.root, self.config, gate.files(self.root))
        self.approve()
        path = self.root / self.config["review"]
        record = json.loads(path.read_text())
        record["findings"] = ["unresolved defect"]
        path.write_text(json.dumps(record))
        with self.assertRaisesRegex(gate.GateError, "未解决"):
            gate.assert_review(self.root, self.config, gate.files(self.root))

    def test_unstaged_change_and_untracked_file_block(self):
        self.run_git("add", ".")
        gate.assert_index(self.root, gate.files(self.root))
        self.write("scripts/feature.py", "answer = 99\n")
        with self.assertRaisesRegex(gate.GateError, "索引"):
            gate.assert_index(self.root, gate.files(self.root))
        self.run_git("add", ".")
        self.write("scripts/untracked.py", "pass\n")
        with self.assertRaisesRegex(gate.GateError, "索引"):
            gate.assert_index(self.root, gate.files(self.root))

    def test_acceptance_failure_missing_tool_and_timeout_block(self):
        for argv, timeout in ((["{python}", "-c", "raise SystemExit(1)"], 5),
                              (["does-not-exist-voice-test"], 5),
                              (["{python}", "-c", "import time; time.sleep(5)"], 1)):
            self.config["checks"] = [{"argv": argv, "timeout": timeout}]
            with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(gate.GateError):
                gate.run_checks(self.root, self.config)

    def test_new_module_requires_base_and_only_changed_scope(self):
        self.run_git("add", ".")
        self.run_git("commit", "-m", "fixture base")
        self.config["id"] = "M01"
        self.config["review"] = "docs/reviews/M01.json"
        self.save_config()
        with self.assertRaisesRegex(gate.GateError, "base_commit"):
            gate.quick(self.root, self.config, gate.files(self.root))
        self.config["base_commit"] = self.run_git("rev-parse", "HEAD").stdout.strip()
        self.config["allowed_paths"] = ["quality/"]
        self.save_config()
        gate.quick(self.root, self.config, gate.files(self.root))
        (self.root / "scripts/feature.py").unlink()
        with self.assertRaisesRegex(gate.GateError, "超出"):
            gate.quick(self.root, self.config, gate.files(self.root))

    def test_symlink_block(self):
        (self.root / "scripts/link.py").symlink_to("feature.py")
        with self.assertRaisesRegex(gate.GateError, "符号链接"):
            gate.files(self.root)

    def test_real_precommit_commitmsg_and_prepush_hooks(self):
        for name in ("quality_gate.py", "codex_hook.py"):
            shutil.copy(PROJECT / "scripts" / name, self.root / "scripts" / name)
        shutil.copytree(PROJECT / ".githooks", self.root / ".githooks")
        for path in (self.root / ".githooks").iterdir():
            path.chmod(0o755)
        self.run_git("config", "core.hooksPath", ".githooks")
        self.run_git("add", ".")
        missing = self.run_git("commit", "-m", "chore(M00): fixture", check=False)
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn("审查", missing.stdout + missing.stderr)
        self.approve()
        self.run_git("add", ".")
        bad_message = self.run_git("commit", "-m", "missing module", check=False)
        self.assertNotEqual(bad_message.returncode, 0)
        good = self.run_git("commit", "-m", "chore(M00): fixture", check=False)
        self.assertEqual(good.returncode, 0, good.stdout + good.stderr)
        head = self.run_git("rev-parse", "HEAD").stdout.strip()
        payload = "refs/heads/main %s refs/heads/main %s\n" % (head, "0" * 40)
        hook = str(self.root / ".githooks/pre-push")
        result = subprocess.run([hook, "origin", "https://github.com/dachenx/voice-style-web.git"], cwd=self.root, input=payload, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        wrong_remote = subprocess.run([hook, "origin", "https://github.com/example/other.git"], cwd=self.root, input=payload, capture_output=True, text=True)
        self.assertNotEqual(wrong_remote.returncode, 0)
        self.assertIn("远端", wrong_remote.stderr)
        with self.assertRaisesRegex(gate.GateError, "当前分支"):
            gate.push_refs(self.root, payload.replace("refs/heads/main", "refs/heads/other", 1))
        # Real git push triggers the unchanged hook. Only the SSH transport is
        # redirected to a disposable bare repository; no network is contacted.
        with tempfile.TemporaryDirectory() as transport_dir:
            remote = Path(transport_dir) / "remote.git"
            subprocess.run(["git", "init", "--bare", str(remote)], capture_output=True, check=True)
            ssh = Path(transport_dir) / "fixture-ssh"
            ssh.write_text("#!/bin/sh\nexec git-receive-pack " + shlex.quote(str(remote)) + "\n")
            ssh.chmod(0o755)
            env = dict(os.environ, GIT_SSH_COMMAND=shlex.quote(str(ssh)), GIT_SSH_VARIANT="ssh")
            def push(url):
                return subprocess.run(["git", "push", url, "main"], cwd=self.root, env=env, capture_output=True, text=True, timeout=15)
            wrong = push("git@github.com:example/other.git")
            self.assertNotEqual(wrong.returncode, 0)
            self.assertIn("远端", wrong.stderr)
            absent = subprocess.run(["git", "--git-dir", str(remote), "rev-parse", "--verify", "refs/heads/main"], capture_output=True)
            self.assertNotEqual(absent.returncode, 0)
            pushed = push("git@github.com:dachenx/voice-style-web.git")
            self.assertEqual(pushed.returncode, 0, pushed.stdout + pushed.stderr)
            remote_sha = subprocess.run(["git", "--git-dir", str(remote), "rev-parse", "refs/heads/main"], capture_output=True, text=True, check=True).stdout.strip()
            self.assertEqual(remote_sha, head)

    def test_codex_posttool_feedback_and_stop_no_infinite_loop(self):
        for name in ("quality_gate.py", "codex_hook.py"):
            shutil.copy(PROJECT / "scripts" / name, self.root / "scripts" / name)
        def invoke(event):
            event["cwd"] = str(self.root)
            result = subprocess.run([sys.executable, "scripts/codex_hook.py"], cwd=self.root, input=json.dumps(event), capture_output=True, text=True, check=True)
            return json.loads(result.stdout)
        first = invoke({"hook_event_name": "PostToolUse"})
        self.assertIn("hookSpecificOutput", first)
        self.assertEqual(invoke({"hook_event_name": "PostToolUse"}), {})
        self.write("scripts/feature.py", "def broken(\n")
        self.assertEqual(invoke({"hook_event_name": "PostToolUse"})["decision"], "block")
        self.assertEqual(invoke({"hook_event_name": "Stop"})["decision"], "block")
        repeated = invoke({"hook_event_name": "Stop", "stop_hook_active": True})
        self.assertNotIn("decision", repeated)
        self.assertIn("systemMessage", repeated)


if __name__ == "__main__":
    unittest.main()
