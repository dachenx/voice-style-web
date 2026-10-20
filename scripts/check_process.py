"""Bounded check processes with owned-tree tracking and cancellation cleanup."""

import os
import signal
import subprocess
import threading
import time


def process_table():
    rows = subprocess.check_output(["ps", "-eo", "pid=,ppid=,stat=,lstart="], text=True, timeout=5)
    return {int(parts[0]): (int(parts[1]), parts[3], parts[2])
            for line in rows.splitlines() if (parts := line.split(None, 3)) and len(parts) == 4}


def track(process, owned):
    table = process_table()
    # A start-time fingerprint avoids following a recycled PID.
    roots = {p for p, stamp in owned.items() if p in table and table[p][1] == stamp}
    if process.poll() is None:
        roots.add(process.pid)
    while True:
        children = {p for p, entry in table.items() if entry[0] in roots}
        if children <= roots:
            break
        roots |= children
    owned.update({p: table[p][1] for p in roots if p in table})
    return table


def stop(process, owned):
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        process.wait(timeout=10)
        return
    inspection_error = None
    # Always signal the process group, even if process inspection has failed.
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            table = track(process, owned)
        except (OSError, subprocess.SubprocessError) as exc:
            table = {}
            inspection_error = exc
        for pid, stamp in owned.items():
            if pid == process.pid or pid not in table or table[pid][1] != stamp:
                continue
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                pass
        # Do not signal a new process group that reused a reaped leader's PID.
        if (process.poll() is None or process.pid not in table
                or owned.get(process.pid) == table[process.pid][1]):
            try:
                os.killpg(process.pid, sig)
            except ProcessLookupError:
                pass
        if sig == signal.SIGTERM:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
    process.wait(timeout=10)
    if inspection_error:
        raise RuntimeError("process inspection failed; group stopped but detached cleanup unverified") from inspection_error
    deadline = time.monotonic() + 3
    while True:
        table = process_table()
        alive = [pid for pid, stamp in owned.items()
                 if pid in table and table[pid][1] == stamp and not table[pid][2].startswith("Z")]
        if not alive:
            break
        if time.monotonic() >= deadline:
            raise RuntimeError("owned check processes survived cleanup")
        time.sleep(0.05)


def run_check(argv, *, cwd, timeout, env=None, check=True):
    process = subprocess.Popen(argv, cwd=cwd, env=env, start_new_session=os.name != "nt")
    owned = {}
    previous = {}

    def interrupted(signum, _frame):
        raise InterruptedError(f"check interrupted by signal {signum}")

    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGTERM, signal.SIGINT):
            previous[sig] = signal.signal(sig, interrupted)
    deadline = time.monotonic() + timeout
    try:
        while True:
            if os.name != "nt":
                track(process, owned)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(argv, timeout)
            try:
                code = process.wait(timeout=min(0.2, remaining))
                break
            except subprocess.TimeoutExpired:
                continue
        if check and code:
            raise subprocess.CalledProcessError(code, argv)
        return subprocess.CompletedProcess(argv, code)
    finally:
        for sig in previous:
            signal.signal(sig, signal.SIG_IGN)
        try:
            stop(process, owned)
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)
