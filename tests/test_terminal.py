"""真 PTY 验证中文历史分页和五秒刷新，测试退出恢复终端属性。"""

import fcntl
import importlib.util
import os
import pty
import select
import struct
import subprocess
import sys
import termios
import time
import unittest

from support import IsolatedPVE, MODULES, REPO
from test_history import stamp


class Terminal:
    def __init__(self, environment, script):
        self.master, self.slave = pty.openpty()
        fcntl.ioctl(self.slave, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 100, 0, 0))
        self.before = termios.tcgetattr(self.slave)
        environment = environment.copy()
        environment["TERM"] = "xterm-256color"
        def prepare():
            os.setsid()
            fcntl.ioctl(self.slave, termios.TIOCSCTTY, 0)
        preamble = 'set -uo pipefail; for module in ' + MODULES + '; do source "$APP_ROOT/lib/$module.sh" || exit; done; '
        self.process = subprocess.Popen(["bash", "-c", preamble + script], env=environment, stdin=self.slave, stdout=self.slave, stderr=self.slave, preexec_fn=prepare, close_fds=True)
        self.output = b""

    def read(self, duration=0.2):
        deadline = time.monotonic() + duration
        chunk = b""
        while time.monotonic() < deadline:
            ready, _, _ = select.select([self.master], [], [], max(0, deadline - time.monotonic()))
            if not ready:
                break
            try:
                part = os.read(self.master, 65536)
            except OSError:
                break
            self.output += part
            chunk += part
        return chunk.decode("utf-8", errors="replace")

    def until(self, text, timeout=3):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.read(0.05)
            if text in self.output.decode("utf-8", errors="replace"):
                return
        raise AssertionError(f"终端未显示 {text!r}：{self.output.decode('utf-8', errors='replace')}")

    def send(self, text):
        os.write(self.master, text.encode())

    def close(self):
        if self.process.poll() is None:
            self.process.kill()
            self.process.wait(timeout=2)
        os.close(self.master)
        os.close(self.slave)


class TerminalTests(unittest.TestCase):
    def setUp(self):
        self.pve = IsolatedPVE()
        self.addCleanup(self.pve.close)
        self.pve.managed()

    def prepare_history(self):
        for index in range(8):
            self.pve.write_log([(stamp(index * 10 + 1), "Power failure."), (stamp(index * 10 + 3), "Mains returned. No longer on UPS batteries.")], append=True)
        self.pve.cli("history")

    def test_real_terminal_history_next_previous_pages_and_exit(self):
        self.prepare_history()
        terminal = Terminal(self.pve.env, "history_menu")
        self.addCleanup(terminal.close)
        terminal.until("第 1 / 3 页")
        terminal.until("2. 下一页")
        terminal.send("2\n")
        terminal.until("第 2 / 3 页")
        terminal.send("2\n")
        terminal.until("第 3 / 3 页")
        terminal.send("3\n")
        terminal.read(0.3)
        self.assertIn("第 2 / 3 页", terminal.output.decode("utf-8", errors="replace"))
        terminal.send("0\n")
        self.assertEqual(terminal.process.wait(timeout=3), 0)
        self.assertEqual(termios.tcgetattr(terminal.slave), terminal.before)

    def test_five_second_unchanged_refresh_does_not_redraw_or_write(self):
        self.prepare_history()
        terminal = Terminal(self.pve.env, "history_menu")
        self.addCleanup(terminal.close)
        terminal.until("第 1 / 3 页")
        terminal.read(0.1)
        database = self.pve.state_dir / "history.db"
        before = (database.read_bytes(), database.stat().st_mtime_ns)
        output = terminal.read(5.3)
        self.assertNotIn("电池供电事件记录", output)
        self.assertEqual((database.read_bytes(), database.stat().st_mtime_ns), before)
        terminal.send("0\n")
        self.assertEqual(terminal.process.wait(timeout=3), 0)
        self.assertEqual(termios.tcgetattr(terminal.slave), terminal.before)

    def test_failed_clear_in_menu_never_claims_success(self):
        self.prepare_history()
        database = self.pve.state_dir / "history.db"
        before = database.read_bytes()
        self.pve.env["PVE_UPS_TEST_FAILPOINT"] = "disk-full"
        terminal = Terminal(self.pve.env, "history_menu")
        self.addCleanup(terminal.close)
        terminal.until("第 1 / 3 页")
        terminal.send("4\n")
        terminal.until("输入“清空”继续")
        terminal.send("清空\n")
        terminal.until("历史记录未能清空")
        self.assertNotIn("历史记录已清空。", terminal.output.decode("utf-8", errors="replace"))
        self.assertEqual(database.read_bytes(), before)
        terminal.send("\n")
        terminal.read(0.3)
        terminal.send("0\n")
        self.assertEqual(terminal.process.wait(timeout=3), 0)
        self.assertEqual(termios.tcgetattr(terminal.slave), terminal.before)


if __name__ == "__main__":
    unittest.main()
