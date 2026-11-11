"""Client-side CPU/audio benchmark; the daemon only serves bounded snapshots.

CPU percentages use user+system process times (not child times or thread rows).
Timing percentiles are log2-histogram upper bounds, not exact quantiles. Polling
cannot observe processes born and exited entirely between samples; observed
churn and inaccessible processes make CPU measurements explicitly incomplete.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import signal
import socket
import sys
import time

import psutil

from vcpi.paths import DEFAULT_SOCK_PATH
from vcpi.presentation.status import format_realtime

OUTPUT_COUNTERS = ("callback_count", "underruns", "render_errors", "deadline_misses")
TRACK_COUNTERS = ("sequence", "submitted", "progress", "misses", "midi_sent", "midi_consumed")


@dataclass(frozen=True)
class ProcessSample:
    pid: int
    created: float
    cpu_seconds: float
    name: str

    @property
    def identity(self):
        return self.pid, self.created


@dataclass
class CpuSample:
    timestamp: float
    processes: tuple[ProcessSample, ...]
    errors: tuple[str, ...] = ()


def sample_cpu(root: psutil.Process) -> CpuSample:
    """Visit each daemon-tree process once, excluding this measurement client."""
    processes = []
    errors = []
    try:
        if not root.is_running():
            raise psutil.NoSuchProcess(root.pid)
        candidates = [root, *root.children(recursive=True)]
    except psutil.Error as exc:
        return CpuSample(time.monotonic(), (), (f"daemon tree unavailable: {exc}",))
    excluded = {os.getpid()}
    try:
        excluded.update(child.pid for child in psutil.Process().children(recursive=True))
    except psutil.Error as exc:
        errors.append(f"cannot identify benchmark children: {exc}")
    seen = set()
    for process in candidates:
        if process.pid in excluded or process.pid in seen:
            continue
        seen.add(process.pid)
        try:
            if not process.is_running():
                raise psutil.NoSuchProcess(process.pid)
            with process.oneshot():
                created = process.create_time()
                cpu = process.cpu_times()
                name = process.name()
            if not process.is_running():
                raise psutil.NoSuchProcess(process.pid)
            processes.append(ProcessSample(process.pid, created, cpu.user + cpu.system, name))
        except psutil.Error as exc:
            errors.append(f"process {process.pid} unavailable: {exc}")
    return CpuSample(time.monotonic(), tuple(processes), tuple(errors))


def cpu_delta(before: CpuSample, after: CpuSample) -> dict:
    """Only matched (PID, creation time) counters contribute to observed CPU."""
    elapsed = after.timestamp - before.timestamp
    first = {process.identity: process for process in before.processes}
    last = {process.identity: process for process in after.processes}
    issues = list(before.errors + after.errors)
    rows = []
    for identity in sorted(first.keys() | last.keys()):
        old, new = first.get(identity), last.get(identity)
        process = new or old
        row = {"pid": process.pid, "create_time": process.created, "name": process.name}
        if old is None:
            row.update(status="new", cpu_seconds=None, cpu_percent=None)
            issues.append(f"new process {process.pid} ({process.created}): no baseline CPU")
        elif new is None:
            row.update(status="exited_or_detached", cpu_seconds=None, cpu_percent=None)
            issues.append(f"exited/detached process {process.pid} ({process.created}): final CPU unavailable")
        else:
            seconds = new.cpu_seconds - old.cpu_seconds
            if seconds < 0:
                row.update(status="counter_regressed", cpu_seconds=None, cpu_percent=None)
                issues.append(f"CPU counter regressed for process {process.pid}")
            else:
                row.update(status="measured", cpu_seconds=seconds,
                           cpu_percent=100 * seconds / elapsed if elapsed > 0 else None)
        rows.append(row)
    if elapsed <= 0:
        issues.append("nonpositive CPU sampling interval")
    if not first or not last:
        issues.append("no daemon processes measured")
    observed = sum(row["cpu_seconds"] or 0 for row in rows)
    percent = 100 * observed / elapsed if elapsed > 0 else None
    return {"elapsed_seconds": elapsed, "complete": not issues,
            "cpu_seconds": observed if not issues else None,
            "cpu_percent": percent if not issues else None,
            "observed_cpu_seconds": observed, "observed_cpu_percent": percent,
            "issues": list(dict.fromkeys(issues)), "processes": rows}


def histogram_percentile(histogram: list[int], quantile: float) -> dict | None:
    """Inclusive integer-nanosecond upper bound; bucket 63 is open-ended."""
    count = sum(histogram)
    if not count:
        return None
    rank = math.ceil(count * quantile)
    cumulative = 0
    for index, samples in enumerate(histogram):
        cumulative += samples
        if cumulative >= rank:
            return {"lower_bound_ns": 0 if index == 0 else 1 << index,
                    "upper_bound_ns": None if index == 63 else (1 << (index + 1)) - 1}
    raise ValueError("invalid histogram")


def timing_delta(before: dict, after: dict) -> dict:
    if not before.get("coherent", True) or not after.get("coherent", True):
        raise ValueError("timing snapshot was not coherent")
    count = after["count"] - before["count"]
    if count < 0:
        raise ValueError("timing count regressed")
    clock_errors = after["cpu_clock_errors"] - before["cpu_clock_errors"]
    if clock_errors < 0:
        raise ValueError("renderer CPU clock error counter regressed")
    if clock_errors:
        raise ValueError(f"renderer thread CPU clock failed for {clock_errors} samples")
    result = {"count": count}
    for name in ("wakeup", "render", "render_cpu"):
        field = name + "_histogram"
        first, last = before[field], after[field]
        if len(first) != 64 or len(last) != 64:
            raise ValueError("timing histogram must have 64 bins")
        histogram = [end - start for start, end in zip(first, last)]
        total_ns = after[name + "_ns_sum"] - before[name + "_ns_sum"]
        if any(value < 0 for value in histogram) or total_ns < 0:
            raise ValueError(f"{name} timing counters regressed")
        if sum(histogram) != count:
            raise ValueError(f"{name} histogram/count mismatch")
        result[name] = {"count": count, "sum_ns": total_ns,
                        "mean_ns": total_ns / count if count else None,
                        "histogram": histogram,
                        **{f"p{int(q * 100)}": histogram_percentile(histogram, q)
                           for q in (0.5, 0.95, 0.99)}}
    result["render_wall_minus_cpu_mean_ns"] = (
        (result["render"]["sum_ns"] - result["render_cpu"]["sum_ns"]) / count
        if count else None)
    return result


def track_identity(track: dict) -> tuple:
    return (track["index"], track.get("generation"), track.get("transport_path"),
            track.get("renderer_pid"), track.get("worker_pid"))


def snapshot_issues(baseline: dict, snapshot: dict) -> list[str]:
    issues = []
    if snapshot.get("daemon_pid") != baseline.get("daemon_pid"):
        issues.append("daemon identity changed")
    if snapshot.get("config") != baseline.get("config"):
        issues.append("audio configuration/lifecycle changed")
    if not snapshot.get("output", {}).get("running"):
        issues.append("audio output is stopped")
    first = {track_identity(track): track for track in baseline["tracks"]}
    last = {track_identity(track): track for track in snapshot["tracks"]}
    if first.keys() != last.keys():
        issues.append("track set or renderer identity changed")
    if not last:
        issues.append("no tracks available for timing")
    for identity, track in last.items():
        label = f"track {track['index']}"
        if track.get("failure") or track.get("worker_failure"):
            issues.append(f"{label} worker failed: {track.get('failure') or track.get('worker_failure')}")
        if not track.get("timing_enabled"):
            issues.append(f"{label} timing is not enabled")
        if "timing" not in track:
            issues.append(f"{label} timing counters unavailable")
        elif not track["timing"].get("coherent", True):
            issues.append(f"{label} timing snapshot was not coherent")
        if "timing" in track and not all(
                field in track["timing"] for field in
                ("render_cpu_ns_sum", "render_cpu_histogram", "cpu_clock_errors")):
            issues.append(f"{label} CPU timing unavailable; rebuild and restart the daemon")
        if track.get("wakeup_mode") not in {"notify", "poll"}:
            issues.append(f"{label} wakeup mode unavailable; rebuild and restart the daemon")
        old = first.get(identity)
        if old is not None and any(old.get(key) != track.get(key)
                                   for key in ("name", "gain", "muted", "solo")):
            issues.append(f"{label} mixer configuration changed")
        if old is not None and (
                old.get("realtime", {}).get("requested_policy")
                != track.get("realtime", {}).get("requested_policy")):
            issues.append(f"{label} requested scheduling policy changed")
        if old is not None and old.get("wakeup_mode") != track.get("wakeup_mode"):
            issues.append(f"{label} wakeup mode changed")
    return issues


def counter_delta(before: dict, after: dict, fields: tuple[str, ...]) -> dict:
    result = {}
    for field in fields:
        difference = after[field] - before[field]
        if difference < 0:
            raise ValueError(f"{field} counter regressed")
        result[field] = difference
    return result


def audio_delta(before: dict, after: dict) -> dict:
    issues = snapshot_issues(before, after)
    output = None
    try:
        output = counter_delta(before["output"], after["output"], OUTPUT_COUNTERS)
        if output["callback_count"] == 0:
            issues.append("audio callbacks did not advance")
        if output["render_errors"]:
            issues.append("audio render errors occurred")
    except (KeyError, TypeError, ValueError) as exc:
        issues.append(f"output counters unavailable: {exc}")
    first = {track_identity(track): track for track in before["tracks"]}
    tracks = []
    for track in after["tracks"]:
        row = {"index": track["index"], "name": track.get("name"),
               "identity": list(track_identity(track)), "counters": None, "timing": None,
               "realtime": track.get("realtime", {}), "wakeup_mode": track.get("wakeup_mode")}
        old = first.get(track_identity(track))
        if old is not None:
            try:
                row["counters"] = counter_delta(old, track, TRACK_COUNTERS)
                row["timing"] = timing_delta(old["timing"], track["timing"])
            except (KeyError, TypeError, ValueError) as exc:
                issues.append(f"track {track['index']} counters unavailable: {exc}")
        tracks.append(row)
    return {"elapsed_seconds": after["sampled_at"] - before["sampled_at"],
            "output_counters": output, "tracks": tracks,
            "issues": list(dict.fromkeys(issues))}


class BenchmarkConnection:
    """One connection owns timing; closing it releases the daemon-side lease."""

    def __init__(self, path):
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.socket.settimeout(15)
        self.reader = self.writer = None
        self.started = False
        try:
            self.socket.connect(str(path))
            self.reader = self.socket.makefile("r", encoding="utf-8")
            self.writer = self.socket.makefile("w", encoding="utf-8")
            self._response()  # Existing CLI banner and sentinel.
        except BaseException:
            self.close()
            raise

    def _response(self):
        lines = []
        while True:
            line = self.reader.readline()
            if not line:
                raise ConnectionError("daemon disconnected before response sentinel")
            if line.rstrip("\n") == "\x00":
                return lines
            lines.append(line)

    def request(self, action):
        self.writer.write("\x03" + json.dumps({"action": action}) + "\n")
        self.writer.flush()
        lines = self._response()
        try:
            response = json.loads("".join(lines))
        except ValueError as exc:
            raise RuntimeError("daemon did not return a benchmark response (upgrade daemon)") from exc
        if not response.get("ok"):
            raise RuntimeError(response.get("error", "benchmark request failed"))
        if action == "start":
            self.started = True
        elif action == "stop":
            self.started = False
        return response.get("snapshot")

    def close(self):
        # shutdown first also handles an interrupted in-flight start whose reply
        # never reached us: the server sees EOF and releases that connection.
        try:
            self.socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        for stream in (self.reader, self.writer):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
        self.socket.close()


def aggregate_cpu(intervals: list[dict]) -> dict:
    elapsed = sum(item["cpu"]["elapsed_seconds"] for item in intervals)
    observed = sum(item["cpu"]["observed_cpu_seconds"] for item in intervals)
    complete = bool(intervals) and all(item["cpu"]["complete"] for item in intervals)
    percent = 100 * observed / elapsed if elapsed > 0 else None
    processes = {}
    for item in intervals:
        for process in item["cpu"]["processes"]:
            identity = process["pid"], process["create_time"]
            row = processes.setdefault(identity, {
                "pid": process["pid"], "create_time": process["create_time"],
                "name": process["name"], "complete": True,
                "observed_cpu_seconds": 0.0, "intervals_seen": 0})
            row["intervals_seen"] += 1
            row["complete"] &= process["status"] == "measured"
            row["observed_cpu_seconds"] += process["cpu_seconds"] or 0.0
    for row in processes.values():
        row["complete"] &= row.pop("intervals_seen") == len(intervals)
        seconds = row["observed_cpu_seconds"]
        contribution = 100 * seconds / elapsed if elapsed > 0 else None
        row.update(cpu_seconds=seconds if row["complete"] else None,
                   cpu_percent=contribution if row["complete"] else None,
                   observed_cpu_percent=contribution)
    return {"elapsed_seconds": elapsed, "complete": complete,
            "cpu_seconds": observed if complete else None,
            "cpu_percent": percent if complete else None,
            "observed_cpu_seconds": observed, "observed_cpu_percent": percent,
            "processes": [processes[identity] for identity in sorted(processes)]}


def run_benchmark(args) -> dict:
    report = {"schema_version": 3, "valid": False, "issues": [],
              "settings": {"duration_seconds": args.duration, "warmup_seconds": args.warmup,
                           "interval_seconds": args.interval, "socket": str(args.socket)},
              "method": {"cpu": "user+system process deltas; 100% = one core; client excluded",
                         "process_identity": "PID and OS creation time",
                         "cpu_limitations": "Processes born and exited between polls are unobservable; churn, unreadable processes and PID reuse invalidate completeness.",
                         "timing": "cumulative native log2-nanosecond histograms; inclusive percentile upper bounds; last bin open-ended",
                         "render_cpu": "CLOCK_THREAD_CPUTIME_ID around track DSP only; user+system CPU of the calling renderer thread",
                         "worker_wakeup": "per-track wakeup_mode: notify uses Mach semaphores on macOS or shared futexes on Linux; poll sleeps 100us when idle",
                         "instrumentation": "native timing is enabled during warmup and measurement"},
              "intervals": [], "baseline": None, "end": None, "aggregate": None}
    connection = None
    baseline = previous = None
    process_roles = {}
    try:
        connection = BenchmarkConnection(args.socket)
        initial = connection.request("start")
        issues = snapshot_issues(initial, initial)
        if issues:
            report["issues"].extend(issues)
            return report
        warmup_end = time.monotonic() + args.warmup
        previous = initial
        while time.monotonic() < warmup_end:
            time.sleep(min(args.interval, max(0, warmup_end - time.monotonic())))
            current = connection.request("sample")
            issues = snapshot_issues(initial, current)
            if issues:
                report["issues"].extend(issues)
                return report
            previous = current
        baseline = previous
        report["baseline"] = baseline
        root = psutil.Process(baseline["daemon_pid"])
        previous_cpu = sample_cpu(root)
        labels = {baseline["daemon_pid"]: "daemon",
                  baseline["config"]["owner_pid"]: "audio output"}
        for track in baseline["tracks"]:
            labels[track["worker_pid"]] = f"track {track['index']} ({track['name']})"
        process_roles = {process.identity: labels.get(process.pid, "descendant")
                         for process in previous_cpu.processes}
        report["daemon"] = {"pid": root.pid, "create_time": root.create_time()}
        started = time.monotonic()
        deadline = started + args.duration
        next_sample = min(started + args.interval, deadline)
        while True:
            time.sleep(max(0, next_sample - time.monotonic()))
            current = connection.request("sample")
            current_cpu = sample_cpu(root)
            cpu = cpu_delta(previous_cpu, current_cpu)
            audio = audio_delta(previous, current)
            issues = snapshot_issues(baseline, current) + audio["issues"] + cpu["issues"]
            report["intervals"].append({"cpu": cpu, "audio": audio})
            report["issues"].extend(issues)
            previous, previous_cpu = current, current_cpu
            report["end"] = current
            if issues or time.monotonic() >= deadline:
                break
            next_sample = min(next_sample + args.interval, deadline)
            if next_sample < time.monotonic():
                next_sample = min(time.monotonic() + args.interval, deadline)
    except KeyboardInterrupt:
        report["issues"].append("benchmark interrupted")
    except (OSError, RuntimeError, ValueError, KeyError, TypeError, psutil.Error) as exc:
        report["issues"].append(f"measurement failed: {exc}")
    finally:
        if connection is not None:
            try:
                if connection.started:
                    connection.request("stop")
            except (OSError, RuntimeError, ValueError, KeyboardInterrupt) as exc:
                report["issues"].append(f"timing stop not acknowledged; connection closed for cleanup: {exc}")
            finally:
                connection.close()
    if baseline is not None and report["end"] is not None:
        audio = audio_delta(baseline, report["end"])
        report["aggregate"] = {"cpu": aggregate_cpu(report["intervals"]), "audio": audio}
        for process in report["aggregate"]["cpu"]["processes"]:
            process["role"] = process_roles.get((process["pid"], process["create_time"]), "descendant")
        report["issues"].extend(audio["issues"])
        for track in audio["tracks"]:
            if track["timing"] is None or not track["timing"]["count"]:
                report["issues"].append(f"track {track['index']} has no timing measurements (renderer stalled or unavailable)")
    else:
        report["issues"].append("no measurement window completed")
    report["issues"] = list(dict.fromkeys(report["issues"]))
    report["valid"] = not report["issues"]
    return report


def print_summary(report):
    print("vcpi benchmark: " + ("VALID" if report["valid"] else "INVALID"))
    aggregate = report["aggregate"]
    if aggregate is not None:
        cpu, audio = aggregate["cpu"], aggregate["audio"]
        percent = cpu["cpu_percent"]
        print(f"Measured {cpu['elapsed_seconds']:.3f}s; CPU " +
              (f"{percent:.2f}% (100% = one core)" if percent is not None else
               f"incomplete; observed {cpu['observed_cpu_percent'] or 0:.2f}%"))
        for process in cpu["processes"]:
            percent = process["cpu_percent"]
            print(f"  process {process['pid']} {process['role']} [{process['name']}]: CPU " +
                  (f"{percent:.2f}%" if percent is not None else
                   f"incomplete; observed {process['observed_cpu_percent'] or 0:.2f}%"))
        for number, item in enumerate(report["intervals"], 1):
            interval = item["cpu"]
            measured = interval["cpu_percent"]
            counters = item["audio"]["output_counters"]
            print(f"  interval {number}: {interval['elapsed_seconds']:.3f}s CPU " +
                  (f"{measured:.2f}%" if measured is not None else "incomplete") +
                  f"; audio deltas {json.dumps(counters, sort_keys=True)}")
        print("Audio counter deltas: " + json.dumps(audio["output_counters"], sort_keys=True))
        for track in audio["tracks"]:
            print(f"Track {track['index']} {track['name']}: " +
                  json.dumps(track["counters"], sort_keys=True))
            print("  scheduling: " + format_realtime(track["realtime"]))
            print(f"  wakeup mode: {track['wakeup_mode']}")
            timing = track["timing"]
            for name, label in (("wakeup", "request-to-render"), ("render", "render elapsed"),
                                ("render_cpu", "render CPU")):
                values = timing[name] if timing else None
                if not values or not values["count"]:
                    print(f"  {label}: no measurements")
                    continue
                bounds = []
                for percentile in ("p50", "p95", "p99"):
                    value = values[percentile]
                    upper = value["upper_bound_ns"]
                    bounds.append(f"{percentile} <= {upper / 1000:.3f}us" if upper is not None
                                  else f"{percentile} >= {value['lower_bound_ns']}ns (open-ended)")
                print(f"  {label}: n={values['count']} mean={values['mean_ns'] / 1000:.3f}us; " + "; ".join(bounds))
            if timing and timing["render_wall_minus_cpu_mean_ns"] is not None:
                print(f"  elapsed-minus-CPU: mean={timing['render_wall_minus_cpu_mean_ns'] / 1000:.3f}us "
                      "(approximate non-running time, not pure scheduling delay)")
    for issue in report["issues"]:
        print("  INVALID: " + issue)
    print("CPU sampling cannot observe processes that start and exit between polls.")


def _positive(value):
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("must be a finite positive number")
    return number


def _nonnegative(value):
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise argparse.ArgumentTypeError("must be a finite nonnegative number")
    return number


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", type=Path, default=DEFAULT_SOCK_PATH)
    parser.add_argument("--duration", type=_positive, default=300.0, help="measurement seconds (default: 300)")
    parser.add_argument("--warmup", type=_nonnegative, default=30.0, help="warmup seconds (default: 30)")
    parser.add_argument("--interval", type=_positive, default=1.0, help="sampling seconds (default: 1)")
    parser.add_argument("--output", type=Path, help="write full JSON report")
    args = parser.parse_args(argv)

    def interrupted(signum, frame):
        raise KeyboardInterrupt

    previous_handler = signal.signal(signal.SIGTERM, interrupted)
    try:
        report = run_benchmark(args)
    finally:
        signal.signal(signal.SIGTERM, previous_handler)
    print_summary(report)
    if args.output is not None:
        try:
            args.output.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        except OSError as exc:
            print(f"Cannot write benchmark report: {exc}", file=sys.stderr)
            return 2
    return 0 if report["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
