"""Real process-tree cleanup, including cancellation and detached grandchildren."""

import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch, call

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from check_process import run_check
import check_process


@unittest.skipIf(os.name == "nt", "POSIX tree checks; Windows not claimed")
class ProcessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.addCleanup(self.close_unrelated)

    def close_unrelated(self):
        self.unrelated.terminate()
        self.unrelated.wait(timeout=5)

    def tree(self, parent_seconds):
        # Keep the parent present long enough to register the owned detached session.
        child = "import time; time.sleep(60)"
        return [
            sys.executable, "-c",
            "import subprocess,sys,time;from pathlib import Path;"
            f"p=subprocess.Popen([sys.executable,'-c',{child!r}],start_new_session=True);"
            "Path('child.pid').write_text(str(p.pid));"
            f"time.sleep({parent_seconds})",
        ]

    def assert_child_stopped(self):
        pid = int((self.root / "child.pid").read_text())
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            state = subprocess.run(["ps", "-p", str(pid), "-o", "stat="],
                                   capture_output=True, text=True).stdout.strip()
            if not state or state.startswith("Z"):
                break
            time.sleep(0.05)
        else:
            os.kill(pid, signal.SIGKILL)
            self.fail("owned descendant still running")
        self.assertIsNone(self.unrelated.poll())

    def test_timeout_reaps_detached_child_and_preserves_unrelated(self):
        with self.assertRaises(subprocess.TimeoutExpired):
            run_check(self.tree(60), cwd=self.root, timeout=1)
        self.assert_child_stopped()

    def test_normal_parent_exit_reaps_tracked_detached_child(self):
        run_check(self.tree(0.6), cwd=self.root, timeout=5)
        self.assert_child_stopped()

    def test_external_termination_reaps_tree(self):
        script = (
            f"import sys;sys.path.insert(0,{str(Path(check_process.__file__).parent)!r});"
            "from check_process import run_check;"
            f"run_check({self.tree(60)!r},cwd={str(self.root)!r},timeout=30)"
        )
        runner = subprocess.Popen([sys.executable, "-c", script],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + 5
            while not (self.root / "child.pid").exists():
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.05)
            time.sleep(0.3)
            runner.terminate()
            self.assertNotEqual(runner.wait(timeout=10), 0)
            self.assert_child_stopped()
        finally:
            if runner.poll() is None:
                runner.kill()
                runner.wait()

    def test_inspection_failure_still_stops_group_and_fails(self):
        captured = []
        original = subprocess.Popen

        def spawn(*args, **kwargs):
            process = original(*args, **kwargs)
            captured.append(process)
            return process

        with patch.object(check_process.subprocess, "Popen", side_effect=spawn), \
                patch.object(check_process, "process_table", side_effect=OSError("fixture ps failed")), \
                self.assertRaisesRegex(RuntimeError, "cleanup unverified"):
            run_check([sys.executable, "-c", "import time; time.sleep(60)"],
                      cwd=self.root, timeout=5)
        self.assertIsNotNone(captured[0].poll())
        self.assertIsNone(self.unrelated.poll())

    def test_recycled_parent_and_child_are_not_followed_or_signaled(self):
        process = Mock(pid=100)
        process.poll.return_value = 0
        owned = {100: "old-parent", 101: "old-child"}
        tables = [
            {100: (1, "new-parent"), 101: (1, "old-child"), 102: (100, "unrelated")},
            {100: (1, "new-parent"), 101: (1, "new-child"), 102: (100, "unrelated")},
            {100: (1, "new-parent"), 101: (1, "new-child"), 102: (100, "unrelated")},
        ]
        with patch.object(check_process, "process_table", side_effect=tables), \
                patch.object(check_process.os, "kill") as kill, \
                patch.object(check_process.os, "killpg") as group:
            check_process.stop(process, owned)
            self.assertEqual(kill.call_args_list, [call(101, signal.SIGTERM)])
            group.assert_not_called()
            self.assertEqual(owned[100], "old-parent")
            self.assertNotIn(102, owned)

    def test_surviving_owned_process_is_not_reported_as_success(self):
        process = Mock(pid=100)
        process.poll.return_value = 0
        with patch.object(check_process, "process_table",
                          return_value={101: (1, "owned", "S")}), \
                patch.object(check_process.os, "kill"), \
                patch.object(check_process.os, "killpg"), \
                patch.object(check_process.time, "monotonic", side_effect=[0, 4]), \
                self.assertRaisesRegex(RuntimeError, "survived cleanup"):
            check_process.stop(process, {101: "owned"})
