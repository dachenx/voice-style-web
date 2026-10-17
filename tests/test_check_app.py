"""Check local runtime discovery without weakening the application gate."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import check_app


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / check_app.RUNTIME_CONFIG
        self.config.parent.mkdir()

    def test_absent_config_preserves_environment_without_mutation(self):
        with patch.dict(os.environ, {"PATH": "inherited", "TEST_MARKER": "kept"}, clear=True):
            env = check_app.configured_environment(self.root)
            self.assertEqual(env, dict(os.environ))
            env["PATH"] = "changed"
            self.assertEqual(os.environ["PATH"], "inherited")

    def test_malformed_and_stale_config_blocks_instead_of_fallback(self):
        values = ["{", "[]", "null", '{"node_bin": 1}', '{"node_bin": "relative"}',
                  json.dumps({"node_bin": str(self.root / "missing")}),
                  json.dumps({"node_bin": str(self.root), "extra": True})]
        for value in values:
            with self.subTest(value=value):
                self.config.write_text(value)
                with self.assertRaises(ValueError):
                    check_app.configured_environment(self.root)

    def test_node_version_and_npm_failure_block(self):
        with patch.object(check_app.shutil, "which", return_value="fixture"):
            for version in ("v22.11.0", "v20.99.0", "unexpected"):
                with self.subTest(version=version), patch.object(
                        check_app.subprocess, "run",
                        return_value=subprocess.CompletedProcess([], 0, version)):
                    with self.assertRaises(ValueError):
                        check_app.validate_node({"PATH": "fixture"})
            with patch.object(check_app.subprocess, "run", side_effect=[
                    subprocess.CompletedProcess([], 0, "v22.12.0"),
                    subprocess.CalledProcessError(1, "npm")]):
                with self.assertRaises(subprocess.CalledProcessError):
                    check_app.validate_node({"PATH": "fixture"})

    def test_missing_node_blocks_configuration_without_overwriting(self):
        self.config.write_text("original")
        with patch.object(check_app.shutil, "which", return_value=None):
            with self.assertRaises(ValueError):
                check_app.configure_node(self.root)
        self.assertEqual(self.config.read_text(), "original")

    @unittest.skipIf(os.name == "nt", "POSIX executable fixture; Windows not claimed")
    def test_registered_directory_restores_child_path_and_npm_symlink(self):
        directory = self.root / "runtime with spaces" / "bin"
        directory.mkdir(parents=True)
        node = directory / "node"
        node.write_text('#!/bin/sh\nprintf "v24.0.0\\n"\n')
        node.chmod(0o755)
        npm_target = directory.parent / "npm-cli"
        npm_target.write_text('#!/bin/sh\nexec node --version\n')
        npm_target.chmod(0o755)
        (directory / "npm").symlink_to(npm_target)
        with patch.dict(os.environ, {"PATH": str(directory)}, clear=True):
            check_app.configure_node(self.root)
        value = json.loads(self.config.read_text())
        self.assertEqual(value["node_bin"], str(directory))
        with patch.dict(os.environ, {"PATH": "/usr/bin:/bin"}, clear=True):
            env = check_app.configured_environment(self.root)
            check_app.validate_node(env)
            result = subprocess.run(["npm", "--version"], env=env, check=True,
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.stdout.strip(), "v24.0.0")
            self.assertEqual(os.environ["PATH"], "/usr/bin:/bin")
        node.unlink()
        with self.assertRaises(ValueError):
            check_app.configured_environment(self.root)

    def test_every_check_receives_environment_and_failures_propagate(self):
        env = {"PATH": "registered"}
        with patch.object(check_app, "PYTHON", Path(sys.executable)), \
                patch.object(check_app, "configured_environment", return_value=env), \
                patch.object(check_app, "validate_node"), \
                patch.object(check_app.subprocess, "run") as run:
            check_app.main([])
            self.assertEqual(run.call_count, 7)
            self.assertEqual([call.kwargs["timeout"] for call in run.call_args_list],
                             [180, 180, 180, 180, 180, 180, 300])
            for call in run.call_args_list:
                self.assertEqual(call.kwargs["env"], env)
                self.assertTrue(call.kwargs["check"])
            for error in (subprocess.CalledProcessError(1, "check"),
                          subprocess.TimeoutExpired("check", 180)):
                run.reset_mock()
                run.side_effect = error
                with self.assertRaises(type(error)):
                    check_app.main([])
                self.assertEqual(run.call_count, 1)

    def test_final_e2e_timeout_and_failure_still_block(self):
        command = ["npm", "--prefix", "frontend", "run", "test:e2e"]
        for error in (subprocess.TimeoutExpired(command, 300),
                      subprocess.CalledProcessError(1, command)):
            with self.subTest(error=type(error).__name__), \
                    patch.object(check_app, "PYTHON", Path(sys.executable)), \
                    patch.object(check_app, "configured_environment", return_value={"PATH": "fixture"}), \
                    patch.object(check_app, "validate_node"), \
                    patch.object(check_app.subprocess, "run", side_effect=[
                        subprocess.CompletedProcess([], 0) for _ in range(6)
                    ] + [error]) as run:
                with self.assertRaises(type(error)):
                    check_app.main([])
                self.assertEqual(run.call_count, 7)
                self.assertEqual(run.call_args.args[0], command)
                self.assertEqual(run.call_args.kwargs["timeout"], 300)
                self.assertTrue(run.call_args.kwargs["check"])


if __name__ == "__main__":
    unittest.main()
