"""实际中文 CLI 与事件后台入口整合；避免只测试理想快照的内部接口。"""

import importlib.util
import json
import fcntl
import subprocess
import time
from pathlib import Path
import sys
import unittest

from support import IsolatedPVE, MODULES, REPO, snapshot
from test_history import nanos, stamp


class HistoryIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("pve_ups_integration_store", REPO / "lib/history_store.py")
        cls.module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = cls.module
        spec.loader.exec_module(cls.module)

    def setUp(self):
        self.pve = IsolatedPVE()
        self.addCleanup(self.pve.close)
        self.pve.managed()

    def rows(self):
        engine = self.module.Engine(str(self.pve.state_dir), str(self.pve.log))
        try:
            return engine.query()["rows"]
        finally:
            engine.close()

    def record(self, event, seconds, token):
        return self.pve.cli("history-record", event, "--captured-ns", str(nanos(seconds)), "--session", "daemon-A", "--token", token)

    def test_real_cli_no_query_short_event_capture(self):
        self.pve.set_fake(query_fail=True)
        self.record("powerout", 1, "cli-powerout")
        self.record("mainsback", 2, "cli-mainsback")
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["duration"], 1)
        self.assertEqual(rows[0]["reason_cn"], "原因未记录")

    def test_all_official_reasons_translated_and_unknown_value_preserved(self):
        official = {"No transfers since turnon": "开机以来无供电切换", "Automatic or explicit self test": "自动或手动触发电池自检", "Forced by software": "软件控制强制切换", "Low line voltage": "输入市电电压过低", "High line voltage": "输入市电电压过高", "Unacceptable line voltage changes": "输入市电电压波动超出允许范围", "Line voltage notch or spike": "市电电压出现陷波或尖峰", "Input frequency out of range": "输入市电频率超标", "UNKNOWN EVENT": "未知切换事件", "Vendor custom reason": "Vendor custom reason"}
        for raw, translated in official.items():
            self.assertEqual(self.module.REASONS.get(raw, raw), translated)
            self.pve.env["REASON_FOR_TEST"] = raw
            self.assertEqual(self.pve.shell('history_translate_reason "$REASON_FOR_TEST"').stdout.strip(), translated)
        self.pve.env.pop("REASON_FOR_TEST")

    def test_real_cli_delayed_callbacks_and_latest_snapshot_two_cycles(self):
        self.pve.write_log([(stamp(0), "apcupsd 3.14.14 (31 May 2016) debian startup succeeded"), (stamp(1), "Power failure."), (stamp(2), "Power is back. UPS running on mains."), (stamp(3), "Power failure."), (stamp(4), "Power is back. UPS running on mains.")])
        details = snapshot("ONLINE", stamp(3), stamp(4), "High line voltage")
        details.update(STARTTIME=stamp(0), NUMXFERS="2")
        self.pve.set_fake(snapshot=details)
        # 后台查询晚于两次切换，且执行顺序与原生发生顺序不同。
        for event, seconds, token in (("mainsback", 4, "last-end"), ("powerout", 1, "first-start"), ("mainsback", 2, "first-end"), ("powerout", 3, "last-start")):
            self.record(event, seconds, token)
        rows = sorted(self.rows(), key=lambda row: row["start_ns"])
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["reason_cn"], "原因未记录")
        self.assertEqual(rows[1]["reason_raw"], "High line voltage")
        self.assertTrue(all(row["state"] == "restored" for row in rows))

    def test_history_errors_visible_in_menu_and_diagnosis_callback_stays_success(self):
        self.pve.cli("history")
        self.pve.env["PVE_UPS_TEST_FAILPOINT"] = "disk-full"
        self.pve.set_fake(query_fail=True)
        result = self.record("powerout", 1, "failed-async-event")
        self.assertEqual(result.returncode, 0)
        self.pve.env.pop("PVE_UPS_TEST_FAILPOINT")
        self.assertIn("历史同步失败", self.pve.cli("history").stdout)
        self.assertIn("磁盘", self.pve.cli("diagnose").stdout)
        dangerous = {"shutdown", "poweroff", "reboot", "halt", "killpower"}
        self.assertFalse([call for call in self.pve.calls() if call[0] in dangerous])

    def test_lock_inode_survives_clear(self):
        self.record("powerout", 1, "before-clear-start")
        self.record("mainsback", 2, "before-clear-end")
        lock = self.pve.state_dir / "history.tsv.lock"
        old_inode = lock.stat().st_ino
        self.pve.shell("history_clear")
        self.assertEqual(lock.stat().st_ino, old_inode)
        self.pve.cli("history")
        self.assertEqual(lock.stat().st_ino, old_inode)

    def test_uninstall_and_waiting_history_writer_leave_no_orphan_state(self):
        self.record("powerout", 1, "before-removal")
        lock = self.pve.state_dir / "history.tsv.lock"
        preamble = 'set -uo pipefail; for module in ' + MODULES + '; do source "$APP_ROOT/lib/$module.sh" || exit; done; '
        with lock.open("a") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            remover = subprocess.Popen(["bash", "-c", preamble + "lifecycle_remove_owned_state"], env=self.pve.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            time.sleep(0.05)
            writer = subprocess.Popen(["bash", "-c", preamble + "history_sync || :"], env=self.pve.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            time.sleep(0.05)
            self.assertIsNone(remover.poll())
            self.assertIsNone(writer.poll())
            fcntl.flock(held, fcntl.LOCK_UN)
        remove_out, remove_err = remover.communicate(timeout=3)
        write_out, write_err = writer.communicate(timeout=3)
        self.assertEqual(remover.returncode, 0, (remove_out, remove_err))
        self.assertEqual(writer.returncode, 0, (write_out, write_err))
        self.assertFalse(self.pve.state_dir.exists())
        self.pve.cli("status")
        self.assertFalse(self.pve.state_dir.exists())

    def test_repair_update_uninstall_and_reinstall_data_consistency(self):
        # 本例需要完整首次安装基线，而非轻量的 managed fixture。
        import shutil
        shutil.rmtree(self.pve.state_dir)
        self.pve.cli("install")
        self.pve.write_log([(stamp(1), "Power failure."), (stamp(3), "Mains returned. No longer on UPS batteries.")])
        self.pve.cli("history")
        original = self.rows()
        baseline = (self.pve.state_dir / "baseline/files.tsv").read_bytes()
        for command in ("install", "tool-install", "upgrade"):
            self.pve.cli(command)
            self.assertEqual(self.rows(), original)
            self.assertEqual((self.pve.state_dir / "baseline/files.tsv").read_bytes(), baseline)
        self.pve.shell("history_clear")
        self.pve.cli("tool-install")
        self.pve.cli("history")
        self.assertEqual(self.rows(), [])
        self.pve.cli("uninstall", "--yes")
        self.assertFalse(self.pve.state_dir.exists())
        self.pve.cli("history")
        self.assertFalse(self.pve.state_dir.exists())
        self.pve.cli("install")
        self.assertTrue((self.pve.state_dir / "manifest.tsv").exists())


if __name__ == "__main__":
    unittest.main()
