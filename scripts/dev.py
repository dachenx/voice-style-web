"""Start the local app and stop all child processes on Ctrl+C or child exit."""

import argparse
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]


def signal_group(group_id, sig):
    try:
        os.killpg(group_id, sig)
    except ProcessLookupError:
        pass
    except PermissionError as denied:
        # EPERM was observed during repeated cleanup; its cause is not assumed.
        # Only verified absence of live group members counts as completed cleanup.
        try:
            states = subprocess.run(["ps", "-eo", "pgid=,stat="],
                                    capture_output=True, text=True, check=True).stdout
            members = [(int(group), state) for group, state in
                       (line.split() for line in states.splitlines())]
        except (OSError, subprocess.SubprocessError, ValueError) as inspection_error:
            raise denied from inspection_error
        if any(group == group_id and not state.startswith("Z") for group, state in members):
            raise


def stop_processes(processes):
    """Clean owned groups even if a supervisor exited before its descendants."""
    for process in processes:
        if os.name == "nt":
            if process.poll() is None:
                process.terminate()
        else:
            signal_group(process.pid, signal.SIGTERM)
    for process in processes:
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            if os.name == "nt":
                process.kill()
    if os.name != "nt":
        # A leader can exit on SIGTERM while an uncooperative child remains.
        for process in processes:
            signal_group(process.pid, signal.SIGKILL)
    for process in processes:
        process.wait()


def main():
    parser = argparse.ArgumentParser(description="声作本地服务")
    parser.add_argument("--built", action="store_true", help="使用 frontend/dist，单服务运行")
    args = parser.parse_args()
    python = ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not python.exists():
        sys.exit("缺少 .venv，请先按 README 安装依赖。")
    if args.built and not (ROOT / "frontend/dist/index.html").is_file():
        sys.exit("请先运行 npm --prefix frontend run build。")
    if not args.built and not shutil.which("npm"):
        sys.exit("缺少 npm，请先安装 Node.js 22.12+。")
    commands = [[str(python), "-m", "uvicorn", "backend.app.main:app", "--host", "127.0.0.1", "--port", "8000"]]
    if not args.built:
        commands[0].extend(["--reload", "--reload-dir", "backend"])
        commands.append(["npm", "--prefix", "frontend", "run", "dev"])
    processes = []

    def stop(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    try:
        for command in commands:
            processes.append(subprocess.Popen(command, cwd=ROOT, start_new_session=os.name != "nt"))
        print("页面：http://127.0.0.1:" + ("8000" if args.built else "5173"), flush=True)
        print("按 Ctrl+C 停止服务。数据保存在 data/（或 VOICE_DATA_DIR）。", flush=True)
        while all(process.poll() is None for process in processes):
            time.sleep(0.3)
        failed = next(process for process in processes if process.poll() is not None)
        return failed.returncode or 1
    except KeyboardInterrupt:
        return 0
    finally:
        stop_processes(processes)


if __name__ == "__main__":
    sys.exit(main())
