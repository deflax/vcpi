"""On-demand OS resource reporting; never queries audio/plugin owners."""

from __future__ import annotations

import os
from pathlib import Path
import threading
import time
from typing import NamedTuple, TYPE_CHECKING

import psutil

if TYPE_CHECKING:
    from core.host import VcpiCore


class _Snapshot(NamedTuple):
    timestamp: float
    cpu: float
    rss: int
    status: str
    name: str
    count: int
    threads: dict[int, float] | None


def _snapshot(process: psutil.Process) -> _Snapshot:
    # is_running checks creation time too, protecting against PID reuse.
    if not process.is_running():
        raise psutil.NoSuchProcess(process.pid)
    with process.oneshot():
        cpu = process.cpu_times()
        memory = process.memory_info().rss
        status = process.status()
        name = process.name()
        count = process.num_threads()
    try:
        threads = {item.id: item.user_time + item.system_time
                   for item in process.threads()}
    except (psutil.AccessDenied, NotImplementedError):
        threads = None
    return _Snapshot(time.monotonic(), cpu.user + cpu.system, memory, status, name, count, threads)


def _clean(text: str) -> str:
    return "".join(char if char.isprintable() else "?" for char in text)


def _thread_name(pid: int, tid: int, local_names: dict[int | None, str]) -> str:
    if pid == os.getpid() and tid in local_names:
        return _clean(local_names[tid])
    try:
        return _clean(Path(f"/proc/{pid}/task/{tid}/comm").read_text().strip())
    except OSError:
        return "thread"


def render_processes(host: VcpiCore) -> str:
    """Sample the daemon tree over 200 ms, with process totals and thread CPU."""
    root = psutil.Process(os.getpid())
    processes = [root, *root.children(recursive=True)]
    roles = {root.pid: "daemon", host.engine.owner_pid: "audio"}
    for index, slot in enumerate(host.engine.slots, 1):
        if slot is not None and slot.worker_pid:
            roles[slot.worker_pid] = f"slot {index}"
    processes.sort(key=lambda process: (process.pid != root.pid, process.pid))
    before = {}
    notes = []
    for process in processes:
        try:
            before[process.pid] = _snapshot(process)
        except psutil.NoSuchProcess:
            continue
        except psutil.AccessDenied:
            notes.append(f"PID {process.pid}: access denied")
    time.sleep(0.2)
    local_names = {thread.native_id: thread.name for thread in threading.enumerate()}
    rows = [("PID", "TID", "ROLE / NAME", "STATE", "CPU%", "RSS MiB", "THR")]
    for process in processes:
        first = before.get(process.pid)
        if first is None:
            continue
        try:
            last = _snapshot(process)
        except psutil.NoSuchProcess:
            continue
        except psutil.AccessDenied:
            notes.append(f"PID {process.pid}: access denied")
            continue
        elapsed = last.timestamp - first.timestamp
        cpu = max(0.0, last.cpu - first.cpu) * 100 / elapsed
        role = roles.get(process.pid, "child")
        rows.append((str(process.pid), "-", f"{role}: {_clean(last.name)}",
                     last.status, f"{cpu:.1f}", f"{last.rss / 1048576:.1f}", str(last.count)))
        if last.threads is None:
            notes.append(f"PID {process.pid}: thread details unavailable")
            continue
        for tid, total in sorted(last.threads.items()):
            previous = first.threads.get(tid) if first.threads is not None else None
            usage = (f"{max(0.0, total - previous) * 100 / elapsed:.1f}"
                     if previous is not None else "-")
            rows.append((str(process.pid), str(tid),
                         "  " + _thread_name(process.pid, tid, local_names),
                         "-", usage, "-", "-"))
    widths = [max(len(row[column]) for row in rows) for column in range(7)]
    lines = ["vcpi processes and threads (CPU sampled over ~200 ms)"]
    lines.extend("  ".join(value.ljust(widths[index]) if index < 4
                           else value.rjust(widths[index])
                           for index, value in enumerate(row)).rstrip()
                 for row in rows)
    lines.append("CPU: 100% = one core; process totals include threads. RSS is shared by threads.")
    lines.append("'-' = not applicable or no CPU baseline; exited tasks are omitted.")
    lines.extend(notes)
    return "\n".join(lines)
