"""实际中文 CLI 与事件后台入口整合；避免只测试理想快照的内部接口。"""

import importlib.util
import json
import fcntl
import subprocess
import tarfile
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

    def test_real_cli_four_callbacks_no_snapshot_or_log_anchor(self):
        self.pve.set_fake(query_fail=True)
        for event, seconds in (("powerout", 1), ("onbattery", 1), ("offbattery", 2), ("mainsback", 2)):
            self.record(event, seconds, "cli-no-query-" + event)
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["duration"], 1)
        self.assertEqual(rows[0]["reason_cn"], "原因未记录")
        self.assertFalse(rows[0]["reason_raw"])
        self.assertEqual(rows[0]["state"], "restored")

    def test_first_cli_capture_does_not_consume_existing_log_history(self):
        self.pve.set_fake(query_fail=True)
        self.pve.write_log([(stamp(0), "apcupsd 3.14.14 (31 May 2016) debian startup succeeded"), (stamp(1), "Power failure."), (stamp(2), "Power is back. UPS running on mains.")])
        # 没有先运行 history/status：首个真实回调不得把旧事件当成自己的证据。
        self.record("powerout", 20, "new-after-existing-log-start")
        self.record("mainsback", 21, "new-after-existing-log-end")
        rows = self.rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual(sorted(row["duration"] for row in rows), [1, 1])

    def test_real_cli_two_distinct_same_second_cycles_no_snapshot(self):
        self.pve.set_fake(query_fail=True)
        for event, extra, token in (("powerout", 100, "first-start"), ("mainsback", 200, "first-end"), ("powerout", 300, "second-start"), ("mainsback", 400, "second-end")):
            self.pve.cli("history-record", event, "--captured-ns", str(nanos(1, extra)), "--session", "daemon-A", "--token", token)
        rows = self.rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual(len({row["id"] for row in rows}), 2)
        self.assertTrue(all(row["state"] == "restored" for row in rows))

    def test_real_cli_restoration_after_same_second_daemon_restart(self):
        self.pve.set_fake(query_fail=True)
        self.pve.cli("history-record", "powerout", "--captured-ns", str(nanos(1, 100)), "--session", "daemon-A", "--token", "old-session-start")
        self.pve.cli("history-record", "mainsback", "--captured-ns", str(nanos(1, 300)), "--session", "daemon-B", "--token", "restarted-session-end")
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["state"], "restored")
        self.assertEqual(rows[0]["duration"], 0)

    def test_real_cli_unchanged_status_does_not_rewrite_database(self):
        self.pve.write_log([(stamp(1), "Power failure."), (stamp(2), "Power is back. UPS running on mains.")])
        self.pve.cli("history")
        database = self.pve.state_dir / "history.db"
        before = (database.read_bytes(), database.stat().st_mtime_ns, database.stat().st_ino)
        for _ in range(3):
            self.pve.cli("status")
        self.assertEqual((database.read_bytes(), database.stat().st_mtime_ns, database.stat().st_ino), before)

    def test_real_cli_json_exposes_pending_identity_and_diagnostic_raw_tokens(self):
        self.pve.set_fake(query_fail=True)
        self.record("powerout", 1, "cli-pending-start")
        self.record("mainsback", 2, "cli-pending-end")
        self.pve.write_log([(stamp(0), "apcupsd 3.14.14 (31 May 2016) debian startup succeeded"), (stamp(1), "Power failure."), (stamp(2), "Power is back. UPS running on mains.")])
        current = snapshot("ONLINE", stamp(1), stamp(2), "Low line voltage")
        current.update(STARTTIME=stamp(0), NUMXFERS="1")
        self.pve.set_fake(query_fail=False, snapshot=current)
        result = json.loads(self.pve.cli("history", "--json").stdout)
        self.assertEqual(result["summary"]["count"], 1)
        self.assertEqual(result["summary"]["unassociated"], 1)
        self.assertEqual(len(result["rows"]), 1)
        self.assertEqual(len(result["pending"]), 1)
        pending = result["pending"][0]
        self.assertEqual(pending["id"], "hook:cli-pending-start")
        self.assertEqual(pending["origin"], "hook")
        self.assertEqual(pending["source"], "hook")
        self.assertFalse(pending["reason_raw"])
        self.assertEqual(pending["reason_cn"], "原因未记录")
        self.assertNotEqual(result["rows"][0]["id"], pending["id"])
        proof = result["proof"]
        self.assertEqual({hook["token"] for hook in proof["hooks"]}, {"cli-pending-start", "cli-pending-end"})
        self.assertEqual({hook["event"] for hook in proof["hooks"]}, {"powerout", "mainsback"})
        self.assertTrue(all(hook["captured_ns"] and hook["session"] == "daemon-A" for hook in proof["hooks"]))
        self.assertEqual(len(proof["evidence"]), 3)
        self.assertTrue(all(evidence["id"] and evidence["source"] and evidence["position"] is not None for evidence in proof["evidence"]))
        self.assertEqual({member["token"] for member in proof["hook_members"]}, {"cli-pending-start", "cli-pending-end"})
        self.assertTrue(all(member["cycle"] == pending["id"] for member in proof["hook_members"]))
        self.assertEqual(len(proof["snapshots"]), 1)
        saved = proof["snapshots"][0]
        self.assertEqual(saved["cycle"], result["rows"][0]["id"])
        self.assertEqual(json.loads(saved["raw"])["LASTXFER"], "Low line voltage")
        self.assertEqual(json.loads(saved["raw"])["XONBATT"], stamp(1))
        self.assertEqual(json.loads(saved["raw"])["XOFFBATT"], stamp(2))
        diagnosis = self.pve.cli("diagnose").stdout
        self.assertIn(pending["id"], diagnosis)
        self.assertIn("cli-pending-start", diagnosis)
        self.assertIn("cli-pending-end", diagnosis)

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

    def test_actual_shell_clear_keeps_new_capture_while_waiting_for_lock(self):
        self.pve.set_fake(query_fail=True)
        self.record("powerout", 1, "old-before-queued-clear-start")
        self.record("mainsback", 2, "old-before-queued-clear-end")
        lock = self.pve.state_dir / "history.tsv.lock"
        preamble = 'set -uo pipefail; for module in ' + MODULES + '; do source "$APP_ROOT/lib/$module.sh" || exit; done; '
        with lock.open("a") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            clearer = subprocess.Popen(["bash", "-c", preamble + "history_clear"], env=self.pve.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            self.addCleanup(lambda: clearer.poll() is not None or clearer.kill())
            # 观察实际 flock 子进程，保证清空请求已发出且在等待；不用睡眠猜测。
            deadline = time.monotonic() + 3
            blocked = False
            while time.monotonic() < deadline:
                pending = [clearer.pid]
                while pending:
                    pid = pending.pop()
                    try:
                        args = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
                        pending.extend(int(child) for child in Path(f"/proc/{pid}/task/{pid}/children").read_text().split())
                    except (FileNotFoundError, ProcessLookupError):
                        continue
                    if args and Path(args[0].decode(errors="replace")).name == "flock":
                        blocked = True
                        break
                if blocked:
                    break
                time.sleep(0.01)
            self.assertTrue(blocked, "正式清空函数未进入历史锁等待")
            # 模拟另一个先获写锁的事件写入者；事件发生在清空请求之后。
            engine = self.module.Engine(str(self.pve.state_dir), str(self.pve.log))
            try:
                after_request = time.time_ns()
                engine.capture("powerout", after_request, "new-after-clear-request-start", session="daemon-A")
                engine.capture("mainsback", after_request + 100, "new-after-clear-request-end", session="daemon-A")
            finally:
                engine.close()
            fcntl.flock(held, fcntl.LOCK_UN)
        output, errors = clearer.communicate(timeout=3)
        self.assertEqual(clearer.returncode, 0, (output, errors))
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["start_ns"], after_request)
        self.assertEqual(rows[0]["state"], "restored")

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

    def test_actual_tool_update_download_path_preserves_history_and_baseline(self):
        import shutil
        shutil.rmtree(self.pve.state_dir)
        self.pve.set_fake(installed=True)
        self.pve.write_config()
        self.pve.cli("install")
        self.pve.write_log([(stamp(1), "Power failure."), (stamp(3), "Power is back. UPS running on mains.")])
        self.pve.cli("history")
        rows = self.rows()
        baseline = (self.pve.state_dir / "baseline/files.tsv").read_bytes()
        archive = self.pve.root / "fixture-main.tar.gz"
        with tarfile.open(archive, "w:gz") as output:
            for name in ("VERSION", "README.md", "bootstrap.sh", "bin", "lib"):
                output.add(REPO / name, arcname="pve-apcupsd-manager-main/" + name)
        self.pve.env["PVE_UPS_TEST_ARCHIVE"] = str(archive)
        result = self.pve.cli("tool-update")
        self.assertIn("已下载最新管理工具", result.stdout)
        self.assertEqual(self.rows(), rows)
        self.assertEqual((self.pve.state_dir / "baseline/files.tsv").read_bytes(), baseline)
        settings = self.pve.shell('printf "%s/%s/%s\\n" "$(config_get TIMEOUT)" "$(config_get BATTERYLEVEL)" "$(config_get MINUTES)"').stdout.strip()
        self.assertEqual(settings, "900/30/3")
        self.assertEqual(len([call for call in self.pve.calls() if call[0] == "curl"]), 1)


if __name__ == "__main__":
    unittest.main()
