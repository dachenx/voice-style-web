"""Start the local app and stop all child processes on Ctrl+C or child exit."""

import argparse
import errno
import os
import json
from pathlib import Path
import socket
import signal
import subprocess
import sys
import time
import urllib.request
import webbrowser

if __package__:
    from .check_app import configured_environment, validate_node
else:
    from check_app import configured_environment, validate_node

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


def preflight(built):
    python = ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not python.is_file():
        raise ValueError("缺少 .venv，请先按《项目说明.md》安装依赖。")
    try:
        subprocess.run([str(python), "-c", "import uvicorn, fastapi"],
                       capture_output=True, check=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError("后端环境不可用，请按《项目说明.md》重新安装后端依赖。") from exc
    env = dict(os.environ) if built else configured_environment(ROOT)
    if built:
        if not (ROOT / "frontend/dist/index.html").is_file():
            raise ValueError("缺少页面构建，请先运行 npm --prefix frontend run build。")
    else:
        validate_node(env)
        if not (ROOT / "frontend/node_modules/vite/bin/vite.js").is_file():
            raise ValueError("缺少前端依赖，请先运行 npm --prefix frontend ci。")
    for port in ([8000] if built else [8000, 5173]):
        try:
            with socket.socket() as listener:
                if os.name != "nt":
                    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                listener.bind(("127.0.0.1", port))
        except OSError as exc:
            if exc.errno == errno.EADDRINUSE:
                raise ValueError(f"端口 {port} 已被占用。请先在原启动窗口按 Ctrl+C；"
                                 "不会自动停止其他进程。") from exc
            raise ValueError(f"无法监听本机端口 {port}，请检查终端或系统的网络权限。") from exc
    return python, env


def wait_ready(processes, page_url, timeout=30):
    # Local probes must not be routed through a system HTTP proxy.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if any(process.poll() is not None for process in processes):
            raise ValueError("服务在启动期间退出，请查看上方服务日志；检查依赖或数据目录是否已被使用。")
        try:
            with opener.open("http://127.0.0.1:8000/api/health", timeout=0.5) as response:
                healthy = json.loads(response.read(1024)) == {"status": "ok"}
            with opener.open(page_url, timeout=0.5) as response:
                page_ready = (response.status == 200
                              and "text/html" in response.headers.get("Content-Type", ""))
            with opener.open(page_url + "/api/health", timeout=0.5) as response:
                proxy_ready = json.loads(response.read(1024)) == {"status": "ok"}
            if healthy and page_ready and proxy_ready and all(p.poll() is None for p in processes):
                return
        except (OSError, ValueError):
            pass
        time.sleep(0.2)
    raise ValueError("启动等待超过30秒，尚未确认服务可用。请查看上方日志后重试；不要删除数据目录。")


def main(argv=None):
    parser = argparse.ArgumentParser(description="声作本地服务")
    parser.add_argument("--built", action="store_true", help="使用 frontend/dist，单服务运行")
    parser.add_argument("--open", action="store_true", help="服务就绪后打开默认浏览器")
    args = parser.parse_args(argv)
    processes = []
    previous_handler = signal.getsignal(signal.SIGTERM)

    def stop(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    try:
        python, env = preflight(args.built)
        commands = [[str(python), "-m", "uvicorn", "backend.app.main:app",
                     "--host", "127.0.0.1", "--port", "8000"]]
        if not args.built:
            commands[0].extend(["--reload", "--reload-dir", "backend"])
            commands.append(["npm", "--prefix", "frontend", "run", "dev"])
        print("正在启动，等待页面和数据服务就绪……", flush=True)
        for command in commands:
            processes.append(subprocess.Popen(command, cwd=ROOT, env=env,
                                              start_new_session=os.name != "nt"))
        page_url = "http://127.0.0.1:" + ("8000" if args.built else "5173")
        wait_ready(processes, page_url)
        print("服务已就绪。页面：" + page_url, flush=True)
        print("按 Ctrl+C 停止服务。数据保存在 data/（或 VOICE_DATA_DIR）。", flush=True)
        if args.open:
            try:
                if not webbrowser.open(page_url):
                    print("未能打开浏览器，请手动打开上方地址。", flush=True)
            except (webbrowser.Error, OSError):
                print("未能打开浏览器，请手动打开上方地址。", flush=True)
        while all(process.poll() is None for process in processes):
            time.sleep(0.3)
        print("服务意外退出，正在停止本次启动的其他服务。请查看上方日志后重试。", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("正在停止本次启动的服务……", flush=True)
        return 0
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print("启动失败：" + str(exc), file=sys.stderr)
        return 1
    finally:
        try:
            stop_processes(processes)
        finally:
            signal.signal(signal.SIGTERM, previous_handler)


if __name__ == "__main__":
    sys.exit(main())
