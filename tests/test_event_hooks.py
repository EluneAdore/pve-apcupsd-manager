"""验证 Debian apccontrol 的 config→用户脚本→原生动作契约。

这里使用最小脚本契约，生产脚本及自定义脚本原件另外由生命周期测试检查。
参考 Debian apcupsd 的 platforms/apccontrol.in，不复制系统脚本，不执行电源动作。
"""

import json
import os
from pathlib import Path
import subprocess
import time
import unittest

from support import IsolatedPVE


class NativeEventHookTests(unittest.TestCase):
    def setUp(self):
        self.pve = IsolatedPVE(installed=True)
        self.addCleanup(self.pve.close)
        self.pve.write_config()
        self.pve.shell("lifecycle_configure_event_notifications")
        self.worker = self.pve.root / "history-worker"
        self.worker.write_text("""#!/usr/bin/env python3
import json, os, sys, time
from pathlib import Path
root = Path(os.environ['PVE_UPS_TEST_ROOT'])
with (root / 'hook-worker.jsonl').open('a') as output:
    output.write(json.dumps(sys.argv[1:]) + '\\n')
if os.environ.get('WORKER_BLOCK') == '1':
    time.sleep(2)
raise SystemExit(17)
""")
        self.worker.chmod(0o755)
        events = self.pve.config.parent / "config"
        events.write_text(events.read_text().replace("/usr/local/sbin/pve-ups", str(self.worker)))
        self.harness = self.pve.root / "native-event-contract"
        self.harness.write_text("""#!/bin/sh
SCRIPTDIR="$PVE_UPS_TEST_ROOT/etc/apcupsd"
WALL=/bin/true
. "$SCRIPTDIR/config"
if [ -x "$SCRIPTDIR/$1" ]; then
    "$SCRIPTDIR/$1" "$@"
    [ "$?" -ne 99 ] || exit 0
fi
printf '%s|%s|%s|%s\\n' "$1" "${2:-}" "${3:-}" "${4:-}" >> "$PVE_UPS_TEST_ROOT/native-actions"
# 不执行 shutdown 或 killpower；记录证明该原生分支仍被到达。
exit 0
""")
        self.harness.chmod(0o755)

    def run_native(self, event, blocked=False):
        environment = self.pve.env.copy()
        environment["WORKER_BLOCK"] = "1" if blocked else "0"
        started = time.monotonic()
        result = subprocess.run(["/bin/sh", str(self.harness), event, "pve", "0", "0"], env=environment, capture_output=True, text=True, timeout=2)
        self.assertEqual(result.returncode, 0, result.stderr)
        return time.monotonic() - started

    def workers(self, expected=1):
        path = self.pve.root / "hook-worker.jsonl"
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            if path.exists():
                rows = [json.loads(line) for line in path.read_text().splitlines()]
                if len(rows) >= expected:
                    return rows
            time.sleep(0.01)
        return []

    def test_four_power_events_capture_metadata_and_keep_original_arguments(self):
        events = ("powerout", "mainsback", "onbattery", "offbattery")
        for event in events:
            self.run_native(event)
        workers = self.workers(4)
        self.assertEqual(len(workers), 4)
        self.assertEqual({call[1] for call in workers}, set(events))
        tokens = []
        for call in workers:
            self.assertEqual(call[0], "history-record")
            for option in ("--captured-uptime", "--boot-id", "--session", "--token"):
                self.assertIn(option, call)
                self.assertTrue(call[call.index(option) + 1])
            tokens.append(call[call.index("--token") + 1])
        self.assertEqual(len(set(tokens)), 4)
        self.assertEqual(set((self.pve.root / "native-actions").read_text().splitlines()), {f"{event}|pve|0|0" for event in events})

    def test_blocked_history_never_delays_native_actions(self):
        elapsed = self.run_native("powerout", blocked=True)
        self.assertLess(elapsed, 0.5, "后台历史阻塞不应延迟原生分支")
        self.assertIn("powerout|pve|0|0", (self.pve.root / "native-actions").read_text())
        self.assertEqual(len(self.workers()), 1)

    def test_history_failure_preserves_custom_hook_exit_99(self):
        user = self.pve.config.parent / "powerout"
        user.write_text('#!/bin/sh\nprintf "%s\\n" "$1" > "$PVE_UPS_TEST_ROOT/custom-event"\nexit 99\n')
        user.chmod(0o755)
        self.run_native("powerout")
        self.assertEqual((self.pve.root / "custom-event").read_text(), "powerout\n")
        self.assertFalse((self.pve.root / "native-actions").exists())
        self.assertEqual(len(self.workers()), 1)

    def test_shutdown_and_protection_events_never_run_history(self):
        events = ("doshutdown", "failing", "timeout", "runlimit", "loadlimit", "killpower", "shutdown", "commfailure", "commok")
        for event in events:
            self.run_native(event)
        self.assertFalse((self.pve.root / "hook-worker.jsonl").exists())
        self.assertEqual(len((self.pve.root / "native-actions").read_text().splitlines()), len(events))
        self.assertFalse([call for call in self.pve.calls() if call[0] in {"shutdown", "poweroff", "reboot", "halt", "killpower"}])


if __name__ == "__main__":
    unittest.main()
