import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import ci_scope


class ScopeTests(unittest.TestCase):
    def test_only_documentation_skips_application_not_code_or_configuration(self):
        self.assertFalse(ci_scope.needs_application(["README.md", "docs/协作规则.md"]))
        for name in ["backend/app/main.py", "frontend/e2e/tasks.spec.ts", "quality/active-module.json",
                     ".github/workflows/quality.yml", ".githooks/pre-commit", "scripts/check_app.py"]:
            self.assertTrue(ci_scope.needs_application(["README.md", name]), name)

    def test_manual_missing_or_unknown_baseline_run_all(self):
        for env in [{"EVENT": "workflow_dispatch"}, {}, {"BEFORE": "0" * 40}]:
            with patch.dict(os.environ, env, clear=True):
                self.assertTrue(ci_scope.select())
        with patch.dict(os.environ, {"BEFORE": "missing"}, clear=True), \
                patch.object(ci_scope.subprocess, "run", return_value=subprocess.CompletedProcess([], 1)):
            self.assertTrue(ci_scope.select())

    def test_known_pr_base_classifies_whole_change(self):
        with patch.dict(os.environ, {"BASE": "base"}, clear=True), \
                patch.object(ci_scope.subprocess, "run",
                             return_value=subprocess.CompletedProcess([], 0, b"README.md\0")) as run:
            self.assertFalse(ci_scope.select())
            self.assertEqual(run.call_args.args[0], ["git", "diff", "--no-renames", "--name-only", "-z", "base", "HEAD"])

    def test_code_renamed_into_docs_remains_application_change(self):
        with patch.dict(os.environ, {"BEFORE": "base"}, clear=True), \
                patch.object(ci_scope.subprocess, "run",
                             return_value=subprocess.CompletedProcess(
                                 [], 0, b"backend/app/example.py\0docs/example.md\0")):
            self.assertTrue(ci_scope.select())
