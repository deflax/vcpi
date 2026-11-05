"""Process totals and render visibility survive idle-thread display filtering."""

import unittest
from types import SimpleNamespace
from unittest.mock import patch
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from vcpi.host import VcpiCore

from vcpi.processes import _Snapshot, render_processes


class ProcessDisplayTests(unittest.TestCase):
    def render(self, show_all: bool = False) -> str:
        process = SimpleNamespace(pid=100, children=lambda recursive: [])
        host = SimpleNamespace(engine=SimpleNamespace(
            owner_pid=100, output_realtime={"thread_id": 104}, tracks=[]))
        first = _Snapshot(1, 10, 10485760, "sleeping", "python", 7,
                          {100: 1, 101: 1, 102: 1, 103: 1, 104: 1, 105: 1})
        last = _Snapshot(2, 11, 10485760, "sleeping", "python", 7,
                         {100: 1, 101: 1, 102: 1, 103: 2, 104: 1, 105: 1, 106: 1})
        with patch("vcpi.processes.psutil.Process", return_value=process), \
             patch("vcpi.processes._snapshot", side_effect=[first, last]), \
             patch("vcpi.processes.time.sleep"), \
             patch("vcpi.processes._thread_name", side_effect=lambda pid, tid, names:
                   "plugin-worker" if tid == 102 else "python"):
            return render_processes(cast("VcpiCore", host), show_all=show_all)

    def thread_ids(self, output: str) -> set[int]:
        return {int(parts[1]) for line in output.splitlines()
                if len(parts := line.split()) >= 2 and parts[0] == "100"
                and parts[1].isdecimal()}

    def test_default_hides_only_idle_generic_nonessential_rows(self):
        output = self.render()
        # Keep main, named, active, known render, and new/unmeasured threads.
        self.assertEqual(self.thread_ids(output), {100, 102, 103, 104, 106})
        self.assertIn("output render", output)
        self.assertIn("2 idle unnamed threads hidden", output)
        process_row = next(line.split() for line in output.splitlines()
                           if line.split()[:2] == ["100", "-"])
        self.assertEqual(process_row[-3:], ["100.0", "10.0", "7"])

    def test_all_includes_idle_threads_without_changing_totals(self):
        output = self.render(show_all=True)
        self.assertEqual(self.thread_ids(output), set(range(100, 107)))
        self.assertNotIn("threads hidden", output)
        process_row = next(line.split() for line in output.splitlines()
                           if line.split()[:2] == ["100", "-"])
        self.assertEqual(process_row[-3:], ["100.0", "10.0", "7"])

    def test_sorting_preserves_process_groups_and_orders_numeric_usage(self):
        root = SimpleNamespace(pid=100)
        child = SimpleNamespace(pid=200)
        root.children = lambda recursive: [child]
        host = SimpleNamespace(engine=SimpleNamespace(
            owner_pid=100, output_realtime={}, tracks=[]))
        first = {
            100: _Snapshot(1, 0, 0, "sleeping", "python", 3,
                           {100: 0, 101: 0, 102: 0}),
            200: _Snapshot(1, 0, 0, "sleeping", "worker", 1, {200: 0}),
        }
        last = {
            100: _Snapshot(2, 0.09, 100 * 1048576, "sleeping", "python", 3,
                           {100: 0, 101: 0.09, 102: 0.1}),
            200: _Snapshot(2, 0.8, 9 * 1048576, "sleeping", "worker", 1,
                           {200: 0.8}),
        }
        for sort_by, expected in (
                (None, [(100, "-"), (100, "100"), (100, "101"), (100, "102"),
                        (200, "-"), (200, "200")]),
                ("cpu", [(200, "-"), (200, "200"), (100, "-"), (100, "102"),
                         (100, "101"), (100, "100")]),
                ("mem", [(100, "-"), (100, "100"), (100, "101"), (100, "102"),
                         (200, "-"), (200, "200")])):
            with self.subTest(sort_by=sort_by), \
                 patch("vcpi.processes.psutil.Process", return_value=root), \
                 patch("vcpi.processes._snapshot",
                       side_effect=[first[100], first[200], last[100], last[200]]), \
                 patch("vcpi.processes.time.sleep"), \
                 patch("vcpi.processes._thread_name", return_value="python"):
                output = render_processes(cast("VcpiCore", host),
                                          show_all=True, sort_by=sort_by)
            rows = [(int(parts[0]), parts[1]) for line in output.splitlines()
                    if (parts := line.split()) and parts[0].isdecimal()]
            self.assertEqual(rows, expected)
