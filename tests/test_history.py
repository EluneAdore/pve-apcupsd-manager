"""供电事件、迁移、清空和故障恢复场景；不使用实际 UPS 或系统服务。"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import importlib.util
import gzip
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

from support import IsolatedPVE, REPO, snapshot


T0 = datetime.strptime("2026-10-11 12:00:00 +0800", "%Y-%m-%d %H:%M:%S %z")


def stamp(seconds=0):
    return (T0 + timedelta(seconds=seconds)).strftime("%Y-%m-%d %H:%M:%S %z")


def nanos(seconds=0, extra=0):
    return int(T0.timestamp()) * 1_000_000_000 + int(seconds * 1_000_000_000) + extra


def raw_snapshot(status="ONLINE", start="", end="", reason="No transfers since turnon", counter=None, daemon_start=""):
    values = snapshot(status, start, end, reason)
    if counter is not None:
        values["NUMXFERS"] = str(counter)
    if daemon_start:
        values["STARTTIME"] = daemon_start
    return "".join(f"{key:<9}: {value}\n" for key, value in values.items())


class HistoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("pve_ups_history_store", REPO / "lib/history_store.py")
        cls.module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = cls.module
        spec.loader.exec_module(cls.module)

    def setUp(self):
        self.pve = IsolatedPVE()
        self.addCleanup(self.pve.close)
        self.pve.managed()
        self.engine = self.module.Engine(str(self.pve.state_dir), str(self.pve.log), str(self.pve.state_dir / "history.tsv"))
        self.addCleanup(self.engine.close)
        self.token = 0

    def query(self):
        return self.engine.query()

    def rows(self):
        return self.query()["rows"]

    def emit(self, event, seconds, message=None, details="", extra=0, session="daemon-A"):
        if message:
            self.pve.write_log([(stamp(seconds), message)], append=True)
            info = self.pve.log.stat()
            anchor = {"dev": info.st_dev, "ino": info.st_ino, "offset": info.st_size}
        else:
            anchor = None
        self.token += 1
        self.engine.capture(event, nanos(seconds, extra), f"fixture-event-{self.token}", session=session, log_anchor=anchor, snapshot_raw=details)

    def cycle(self, start=1, duration=5, reason="Low line voltage", counter=1, daemon_start=None):
        daemon_start = daemon_start or stamp(0)
        details = raw_snapshot("ONBATT", stamp(start), reason=reason, counter=counter, daemon_start=daemon_start)
        self.emit("powerout", start, "Power failure.", details)
        self.emit("onbattery", start, "Running on UPS batteries.", details, extra=100)
        restored = raw_snapshot("ONLINE", stamp(start), stamp(start + duration), reason, counter, daemon_start)
        self.emit("offbattery", start + duration, "Mains returned. No longer on UPS batteries.", restored)
        self.emit("mainsback", start + duration, "Power is back. UPS running on mains.", restored, extra=100)
        return restored

    def startup(self, seconds=0):
        self.pve.write_log([(stamp(seconds), "apcupsd 3.14.14 (31 May 2016) debian startup succeeded")], append=True)

    def test_ordinary_power_failure_and_restore(self):
        self.startup()
        self.cycle(reason="Input line failure")
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["duration"], 5)
        self.assertEqual(rows[0]["state"], "restored")
        self.assertEqual(rows[0]["reason_raw"], "Input line failure")

    def test_low_voltage_reason_does_not_claim_complete_outage(self):
        self.startup()
        self.cycle()
        row = self.rows()[0]
        self.assertEqual(row["reason_raw"], "Low line voltage")
        self.assertEqual(row["reason_cn"], "输入市电电压过低")
        self.assertNotIn("断电", row["reason_cn"])

    def test_short_transfers_one_to_five_seconds_capture_without_onbattery(self):
        self.startup()
        for index, duration in enumerate(range(1, 6), 1):
            start = index * 10
            details = raw_snapshot("ONBATT", stamp(start), reason="Low line voltage", counter=index, daemon_start=stamp(0))
            self.emit("powerout", start, "Power failure.", details)
            self.emit("mainsback", start + duration, "Power is back. UPS running on mains.", raw_snapshot("ONLINE", stamp(start), stamp(start + duration), "Low line voltage", index, stamp(0)))
        rows = self.rows()
        self.assertEqual(len(rows), 5)
        self.assertEqual(sorted(row["duration"] for row in rows), [1, 2, 3, 4, 5])

    def test_continuous_multiple_cycles_are_never_merged(self):
        self.startup()
        reasons = ["Low line voltage", "High line voltage", "Forced by software", "Input frequency out of range"]
        for index, reason in enumerate(reasons, 1):
            self.cycle(index * 3, 1, reason, index)
        rows = self.rows()
        self.assertEqual(len(rows), 4)
        self.assertEqual({r["reason_raw"] for r in rows}, set(reasons))
        self.assertEqual(sum(r["duration"] for r in rows), 4)

    def test_four_native_events_one_switch_and_repeated_sync(self):
        self.startup()
        details = self.cycle()
        for _ in range(4):
            self.engine.sync(details)
        self.assertEqual(len(self.rows()), 1)

    def test_same_second_daemon_startups_cannot_merge_snapshot_identity(self):
        # STARTTIME 只有秒精度，两次启动相同时间并不构成同一 daemon 身份。
        self.engine.sync(raw_snapshot("ONLINE", stamp(1), stamp(2), "Low line voltage", 1, stamp(0)))
        original = self.rows()[0]["id"]
        self.startup()
        self.startup()
        self.pve.write_log([(stamp(3), "Power failure."), (stamp(4), "Power is back. UPS running on mains.")], append=True)
        self.engine.sync(raw_snapshot("ONLINE", stamp(3), stamp(4), "High line voltage", 1, stamp(0)))
        result = self.query()
        self.assertEqual(result["summary"]["count"], 1)
        self.assertEqual(result["summary"]["unassociated"], 1)
        self.assertNotEqual(result["rows"][0]["id"], original)
        self.assertFalse(result["rows"][0]["reason_raw"])
        self.assertEqual(result["pending"][0]["id"], original)
        self.assertEqual(result["pending"][0]["reason_raw"], "Low line voltage")

    def test_log_snapshot_time_skew_uses_daemon_counter_identity(self):
        self.startup()
        for seconds, message in ((10, "Power failure."), (11, "Running on UPS batteries."), (16, "Mains returned. No longer on UPS batteries."), (16, "Power is back. UPS running on mains.")):
            self.pve.write_log([(stamp(seconds), message)], append=True)
        details = raw_snapshot("ONLINE", stamp(13), stamp(19), "Low line voltage", 1, stamp(0))
        self.engine.sync(details)
        self.assertEqual(len(self.rows()), 1)
        row = self.rows()[0]
        self.assertEqual(row["reason_raw"], "Low line voltage")
        self.assertEqual(row["duration"], 6)

    def test_later_native_identity_replaces_skewed_snapshot_times_and_keeps_raw_proof(self):
        original = raw_snapshot("ONLINE", stamp(13), stamp(19), "Low line voltage", 1, stamp(0))
        self.engine.sync(original)
        self.assertEqual(self.rows()[0]["origin"], "snapshot")
        self.pve.write_log([(stamp(0), "apcupsd 3.14.14 (31 May 2016) debian startup succeeded"), (stamp(10), "Power failure."), (stamp(16), "Power is back. UPS running on mains.")])
        self.engine.sync()
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["origin"], "log")
        self.assertEqual(row["start_ns"], nanos(10))
        self.assertEqual(row["end_ns"], nanos(16))
        self.assertEqual(row["duration"], 6)
        self.assertEqual(row["reason_raw"], "Low line voltage")
        saved = self.engine.db.execute("SELECT raw,cycle FROM snapshots").fetchall()
        self.assertEqual(len(saved), 1)
        self.assertEqual(json.loads(saved[0]["raw"]), self.module.snapshot_fields(original))
        self.assertEqual(saved[0]["cycle"], row["id"])

    def test_uncertain_log_prefix_invalidates_counter_reason_association(self):
        initial = [(stamp(1), "Power failure."), (stamp(2), "Power is back. UPS running on mains.")]
        self.startup()
        self.pve.write_log(initial, append=True)
        self.engine.sync(raw_snapshot("ONLINE", stamp(1), stamp(2), "Low line voltage", 1, stamp(0)))
        self.pve.write_log(initial)
        self.engine.sync()
        self.pve.log.rename(self.pve.log.with_name("apcupsd.events.1"))
        self.pve.write_log(initial)
        self.pve.write_log([(stamp(3), "Power failure."), (stamp(4), "Power is back. UPS running on mains.")], append=True)
        latest = raw_snapshot("ONLINE", stamp(3), stamp(4), "High line voltage", 2, stamp(0))
        self.engine.sync(latest)
        result = self.query()
        rows = sorted(result["rows"], key=lambda row: row["start_ns"])
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["reason_raw"], "Low line voltage")
        self.assertEqual(rows[1]["reason_cn"], "原因未记录")
        self.assertFalse(rows[1]["reason_raw"])
        self.assertIsNone(rows[1]["transfer"], "不确定前缀之后不能假定会话转移序号")
        self.assertEqual(result["summary"]["unassociated"], 1)
        self.assertFalse(any(row["reason_raw"] == "High line voltage" for row in rows + result["pending"]))
        self.assertFalse(self.engine._json("log_state")["known"])
        saved = self.engine.db.execute("SELECT raw,cycle FROM snapshots ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(json.loads(saved["raw"]), self.module.snapshot_fields(latest))
        self.assertIsNone(saved["cycle"], "最新设备原文须保存但不得硬关联到错误事件")

    def test_unmatched_strong_snapshot_counter_does_not_confirm_old_active_cycle(self):
        self.startup()
        self.pve.write_log([(stamp(1), "Power failure.")], append=True)
        self.engine.sync(raw_snapshot("ONBATT", stamp(1), reason="Low line voltage", counter=1, daemon_start=stamp(0)))
        self.assertEqual(self.query()["summary"]["current"], 1)
        latest = raw_snapshot("ONBATT", stamp(2), reason="High line voltage", counter=2, daemon_start=stamp(0))
        self.engine.sync(latest)
        result = self.query()
        self.assertEqual(result["summary"]["count"], 1)
        self.assertEqual(result["summary"]["current"], 0)
        self.assertEqual(result["summary"]["unknown"], 1)
        self.assertEqual(result["rows"][0]["state"], "unknown")
        self.assertEqual(result["rows"][0]["reason_raw"], "Low line voltage")
        saved = self.engine.db.execute("SELECT raw,cycle FROM snapshots ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(json.loads(saved["raw"]), self.module.snapshot_fields(latest))
        self.assertIsNone(saved["cycle"])

    def test_same_second_distinct_switches_preserved_with_event_identity(self):
        self.startup()
        for index, reason in enumerate(("Low line voltage", "High line voltage"), 1):
            self.emit("powerout", 10, "Power failure.", raw_snapshot("ONBATT", stamp(10), reason=reason, counter=index, daemon_start=stamp(0)), extra=index * 1000)
            self.emit("mainsback", 10, "Power is back. UPS running on mains.", raw_snapshot("ONLINE", stamp(10), stamp(10), reason, index, stamp(0)), extra=index * 1000 + 500)
        rows = self.rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual(len({row["id"] for row in rows}), 2)
        self.assertEqual({row["reason_raw"] for row in rows}, {"Low line voltage", "High line voltage"})

    def test_adjacent_second_distinct_log_only_events_not_time_matched(self):
        self.pve.write_log([(stamp(1), "Power failure."), (stamp(2), "Mains returned. No longer on UPS batteries."), (stamp(3), "Power failure."), (stamp(4), "Mains returned. No longer on UPS batteries.")])
        self.engine.sync()
        self.assertEqual(len(self.rows()), 2)
        for row in self.rows():
            self.assertEqual(row["reason_cn"], "原因未记录")
            self.assertFalse(row["reason_raw"])

    def test_hooks_capture_when_native_log_temporarily_unavailable(self):
        self.emit("powerout", 1)
        self.emit("mainsback", 3)
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["duration"], 2)
        self.assertEqual(rows[0]["reason_cn"], "原因未记录")

    def test_delayed_native_log_after_hook_capture_does_not_duplicate_cycle(self):
        self.emit("powerout", 1)
        self.emit("mainsback", 2)
        self.assertEqual(len(self.rows()), 1)
        self.pve.write_log([(stamp(0), "apcupsd 3.14.14 (31 May 2016) debian startup succeeded"), (stamp(1), "Power failure."), (stamp(2), "Power is back. UPS running on mains.")])
        self.engine.sync()
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["duration"], 1)
        # 无会话计数证明时，保留后台捕获的独立证据供核查，禁止猜测绑定。
        self.assertEqual(self.query()["summary"]["unassociated"], 1)
        self.assertTrue(self.query()["notice"])
        self.assertIn("fixture-event-1", self.engine.diagnostics())

    def test_delayed_snapshot_does_not_assign_latest_reason_to_old_cycles(self):
        self.startup()
        for seconds, message in ((1, "Power failure."), (2, "Mains returned. No longer on UPS batteries."), (3, "Power failure."), (4, "Mains returned. No longer on UPS batteries.")):
            self.pve.write_log([(stamp(seconds), message)], append=True)
        self.engine.sync(raw_snapshot("ONLINE", stamp(3), stamp(4), "High line voltage", 2, stamp(0)))
        rows = sorted(self.rows(), key=lambda row: row["start"])
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["reason_cn"], "原因未记录")
        self.assertEqual(rows[1]["reason_raw"], "High line voltage")

    def test_monitor_restart_during_outage_and_missing_end_completed(self):
        self.startup()
        self.emit("powerout", 1, "Power failure.", raw_snapshot("ONBATT", stamp(1), reason="Low line voltage", counter=1, daemon_start=stamp(0)))
        self.startup(2)
        self.engine.sync(raw_snapshot("ONBATT", reason="No transfers since turnon", counter=0, daemon_start=stamp(2)))
        self.emit("offbattery", 5, "Mains returned. No longer on UPS batteries.", raw_snapshot("ONLINE", end=stamp(5), counter=0, daemon_start=stamp(2)), session="daemon-B")
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.rows()[0]["duration"], 4)
        self.assertEqual(self.rows()[0]["reason_raw"], "Low line voltage")
        self.assertEqual(self.rows()[0]["state"], "restored")

    def test_communication_loss_then_restore_keeps_evidence(self):
        self.startup()
        self.emit("powerout", 1, "Power failure.")
        self.engine.sync(raw_snapshot("COMMLOST"))
        self.pve.write_log([(stamp(2), "Communications with UPS lost."), (stamp(3), "Communications with UPS restored."), (stamp(6), "Mains returned. No longer on UPS batteries.")], append=True)
        self.engine.sync(raw_snapshot("ONLINE", stamp(1), stamp(6), "Low line voltage", 1, stamp(0)))
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.rows()[0]["duration"], 5)

    def test_failed_snapshot_after_communication_loss_or_daemon_stop_is_unknown(self):
        self.startup()
        self.emit("powerout", 1, "Power failure.", raw_snapshot("ONBATT", stamp(1), reason="Low line voltage", counter=1, daemon_start=stamp(0)))
        self.assertEqual(self.query()["summary"]["current"], 1)
        self.pve.write_log([(stamp(2), "Communications with UPS lost.")], append=True)
        self.engine.sync("")
        self.assertEqual(self.query()["summary"]["current"], 0)
        self.assertEqual(self.query()["summary"]["unknown"], 1)
        self.engine.sync(raw_snapshot("ONBATT", stamp(1), reason="Low line voltage", counter=1, daemon_start=stamp(0)))
        self.assertEqual(self.query()["summary"]["current"], 1)
        self.pve.write_log([(stamp(3), "apcupsd shutdown succeeded")], append=True)
        self.engine.sync("")
        self.assertEqual(self.query()["summary"]["current"], 0)
        self.assertEqual(self.query()["summary"]["unknown"], 1)

    def test_old_unfinished_rows_are_unknown_unless_current_confirmed(self):
        legacy = self.pve.state_dir / "history.tsv"
        legacy.write_text(f"{stamp(1)}\t-\t-\t输入市电电压过低\tLow line voltage\n{stamp(3)}\t-\t-\t原因未记录\t\n")
        self.engine.sync(raw_snapshot("ONLINE"))
        result = self.query()
        self.assertEqual(result["summary"]["current"], 0)
        self.assertEqual(result["summary"]["unknown"], 2)
        self.assertTrue(all(row["state"] == "unknown" for row in result["rows"]))

    def test_legacy_valid_rows_survive_compatibility_migration(self):
        legacy = self.pve.state_dir / "history.tsv"
        original = f"{stamp(1)}\t{stamp(6)}\t5\t输入市电电压过低\tLow line voltage\n{stamp(8)}\t-\t-\t软件控制强制切换\tForced by software\n{stamp(8)}\t-\t-\t用户保存原因\tVendor custom transfer\n"
        legacy.write_text(original)
        self.engine.sync()
        self.engine.sync()
        rows = self.rows()
        self.assertEqual(len(rows), 3)
        self.assertEqual({row["reason_raw"] for row in rows}, {"Low line voltage", "Forced by software", "Vendor custom transfer"})
        self.assertEqual(legacy.read_text(), original)

    def test_legacy_identical_second_rows_are_all_preserved(self):
        legacy = self.pve.state_dir / "history.tsv"
        row = f"{stamp(1)}\t{stamp(1)}\t0\t输入市电电压过低\tLow line voltage\n"
        legacy.write_text(row * 2)
        self.engine.sync()
        self.assertEqual(len(self.rows()), 2)
        self.assertEqual(len({event["id"] for event in self.rows()}), 2)

    def test_legacy_completed_record_with_conflicting_new_evidence_is_not_overwritten(self):
        legacy = self.pve.state_dir / "history.tsv"
        original = f"{stamp(10)}\t{stamp(10)}\t0\t输入市电电压过低\tLow line voltage\n"
        legacy.write_text(original)
        self.startup()
        self.pve.write_log([(stamp(10), "Power failure."), (stamp(12), "Mains returned. No longer on UPS batteries.")], append=True)
        self.engine.sync(raw_snapshot("ONLINE", stamp(10), stamp(12), "High line voltage", 1, stamp(0)))
        rows = self.rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual({(row["duration"], row["reason_raw"]) for row in rows}, {(0, "Low line voltage"), (2, "High line voltage")})
        self.assertEqual(legacy.read_text(), original)

    def test_schema1_claimed_hooks_stay_cleared_after_database_upgrade(self):
        # 固定fixture由 git show 116ccdd:lib/history_store.py 的旧Engine实跑导出，
        # 正式测试不依赖当前HEAD、Git历史、网络或真实系统服务。
        self.engine.close()
        database = self.pve.state_dir / "history.db"
        database.unlink()
        fixture = REPO / "tests/fixtures/history-schema1-cleared.sql"
        with sqlite3.connect(database) as connection:
            connection.executescript(fixture.read_text())
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM claims").fetchone()[0], 2)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM hooks WHERE suppressed=0").fetchone()[0], 2)
        connection.close()
        self.engine = self.module.Engine(str(self.pve.state_dir), str(self.pve.log))
        self.addCleanup(self.engine.close)
        self.engine.sync()
        result = self.query()
        self.assertEqual(result["rows"], [])
        self.assertEqual(result["pending"], [])
        self.assertEqual(self.engine.db.execute("PRAGMA user_version").fetchone()[0], 2)
        self.assertEqual(self.engine.db.execute("SELECT COUNT(*) FROM evidence").fetchone()[0], 3)
        self.assertEqual(self.engine.db.execute("SELECT COUNT(*) FROM hooks WHERE suppressed=1").fetchone()[0], 2)
        self.assertIn("schema1-start", self.engine.diagnostics())
        self.assertIn("schema1-end", self.engine.diagnostics())
        self.emit("powerout", 20)
        self.emit("mainsback", 21)
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.rows()[0]["duration"], 1)

    def test_clear_prevents_old_log_and_delayed_old_hooks_reimport(self):
        self.startup()
        self.cycle()
        self.engine.clear(captured_ns=nanos(20))
        self.engine.sync()
        self.assertEqual(self.rows(), [])
        self.engine.capture("powerout", nanos(1), "delayed-old-hook", session="daemon-A")
        self.pve.write_log([(stamp(1), "Power failure."), (stamp(6), "Mains returned. No longer on UPS batteries.")], append=True)
        self.engine.sync()
        self.assertEqual(self.rows(), [])
        self.cycle(21, 2, counter=2)
        self.assertEqual(len(self.rows()), 1)

    def test_clear_same_second_new_event_is_not_filtered_by_epoch(self):
        self.emit("powerout", 10, extra=100)
        self.emit("mainsback", 10, extra=200)
        self.engine.clear(captured_ns=nanos(10, 300))
        self.emit("powerout", 10, extra=400)
        self.emit("mainsback", 10, extra=500)
        self.assertEqual(len(self.rows()), 1)

    def test_clear_same_kernel_tick_keeps_later_process_identities(self):
        with patch.object(self.module, "boot_clock", return_value=("fixture-boot", 100.0)):
            self.engine.clear(captured_ns=nanos(10), captured_uptime=100.0,
                              boot_id="fixture-boot", clear_pid=100, clear_birth=10000)
            # 三个事件的出生 tick / uptime 相同，只有独立进程身份能区分边界。
            self.engine.capture("powerout", nanos(10), "fixture-boot:99:10000:powerout", captured_uptime=100.0, boot_id="fixture-boot")
            self.engine.capture("powerout", nanos(10, 100), "fixture-boot:101:10000:powerout", captured_uptime=100.0, boot_id="fixture-boot")
            self.engine.capture("mainsback", nanos(10, 200), "fixture-boot:102:10000:mainsback", captured_uptime=100.0, boot_id="fixture-boot")
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.rows()[0]["state"], "restored")
        self.assertEqual(self.engine.db.execute("SELECT COUNT(*) FROM hooks").fetchone()[0], 3)

    def test_clear_and_new_capture_concurrently_preserve_post_clear_event(self):
        self.emit("powerout", 1)
        self.emit("mainsback", 2)
        def clear():
            engine = self.module.Engine(str(self.pve.state_dir), str(self.pve.log), str(self.pve.state_dir / "history.tsv"))
            try:
                engine.clear(captured_ns=nanos(10, 100))
            finally:
                engine.close()
        def new_event():
            engine = self.module.Engine(str(self.pve.state_dir), str(self.pve.log), str(self.pve.state_dir / "history.tsv"))
            try:
                engine.capture("powerout", nanos(10, 200), "concurrent-new-start", session="daemon-A")
                engine.capture("mainsback", nanos(10, 300), "concurrent-new-end", session="daemon-A")
            finally:
                engine.close()
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(clear), executor.submit(new_event)]
            for future in futures:
                future.result(timeout=10)
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["state"], "restored")

    def test_clear_commits_after_new_capture_but_respects_requested_clear_time(self):
        self.emit("powerout", 1)
        self.emit("mainsback", 2)
        self.emit("powerout", 10, extra=200)
        self.emit("mainsback", 10, extra=300)
        # 清空请求先发生，但等待写锁后才提交；之后发生的新事件必须保留。
        self.engine.clear(captured_ns=nanos(10, 100))
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.rows()[0]["state"], "restored")

    def test_out_of_order_workers_use_capture_time_not_arrival_order(self):
        self.engine.capture("mainsback", nanos(3), "late-order-end", session="daemon-A")
        self.engine.capture("powerout", nanos(1), "late-order-start", session="daemon-A")
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["state"], "restored")
        self.assertEqual(rows[0]["duration"], 2)

    def test_log_rotation_duplicate_copy_and_new_cycle_preserved(self):
        self.startup()
        self.cycle()
        previous = self.pve.log.read_text()
        self.pve.log.rename(self.pve.log.with_suffix(".events.1"))
        self.pve.log.write_text(previous)
        self.engine.sync()
        self.assertEqual(len(self.rows()), 1)
        self.cycle(10, 3, "High line voltage", 2)
        self.assertEqual(len(self.rows()), 2)

    def test_same_second_identical_active_log_inode_keeps_distinct_raw_evidence(self):
        self.pve.write_log([(stamp(10), "apcupsd 3.14.14 (31 May 2016) debian startup succeeded"), (stamp(10), "Power failure."), (stamp(10), "Power is back. UPS running on mains.")])
        self.engine.sync()
        original = self.pve.log.read_bytes()
        original_inode = self.pve.log.stat().st_ino
        self.pve.log.rename(self.pve.log.with_name("apcupsd.events.1"))
        self.pve.log.write_bytes(original)
        self.assertNotEqual(self.pve.log.stat().st_ino, original_inode)
        self.engine.sync()
        result = self.query()
        self.assertEqual(result["summary"]["count"], 1)
        self.assertEqual(result["summary"]["unassociated"], 1)
        self.assertEqual(len(result["pending"]), 1)
        self.assertEqual(result["pending"][0]["origin"], "log")
        self.assertFalse(result["pending"][0]["reason_raw"])
        self.assertEqual(self.engine.db.execute("SELECT COUNT(*) FROM evidence").fetchone()[0], 6)
        self.assertEqual(self.engine.db.execute("SELECT COUNT(DISTINCT source) FROM evidence").fetchone()[0], 2)
        self.assertIn(result["pending"][0]["id"], self.engine.diagnostics())

    def test_real_token_birth_ticks_take_priority_over_late_entry_uptime(self):
        ticks = os.sysconf("SC_CLK_TCK")
        tokens = {"powerout": f"fixture-boot:101:{ticks}:powerout", "onbattery": f"fixture-boot:102:{ticks + 1}:onbattery", "mainsback": f"fixture-boot:103:{ticks * 2}:mainsback"}
        with patch.object(self.module, "boot_clock", return_value=("fixture-boot", 50.0)), patch.object(self.module.time, "time_ns", return_value=nanos(50)):
            # 先执行出生稍晚的onbattery/end worker；powerout出生更早却晚获调度。
            for event, uptime in (("onbattery", 1 + 1 / ticks), ("mainsback", 2.0), ("powerout", 10.0)):
                self.engine.capture(event, token=tokens[event], session="fixture-boot:42:50", captured_uptime=uptime, boot_id="fixture-boot")
        result = self.query()
        self.assertEqual(result["summary"]["count"], 1)
        self.assertEqual(result["summary"]["unassociated"], 0)
        row = result["rows"][0]
        self.assertEqual(row["id"], "hook:" + tokens["powerout"])
        self.assertEqual(row["start_ns"], nanos(1))
        self.assertEqual(row["end_ns"], nanos(2))
        self.assertEqual(row["duration"], 1)
        self.assertEqual(self.engine.db.execute("SELECT COUNT(*) FROM hooks").fetchone()[0], 3)
        self.assertEqual(self.engine.db.execute("SELECT COUNT(*) FROM hook_members WHERE cycle=?", (row["id"],)).fetchone()[0], 3)
        for token in tokens.values():
            self.assertIn(token, self.engine.diagnostics())

    def test_clear_rotation_restart_timezone_old_rows_stay_cleared(self):
        self.startup()
        self.cycle()
        previous = self.pve.log.read_text()
        self.engine.clear(captured_ns=nanos(20))
        self.pve.log.rename(self.pve.log.with_suffix(".events.1"))
        self.pve.log.write_text(previous.replace("2026-10-11 12:", "2026-10-11 04:").replace("+0800", "+0000"))
        self.engine.sync()
        self.assertEqual(self.rows(), [])
        self.startup(25)
        self.cycle(26, 2, counter=1, daemon_start=stamp(25))
        self.assertEqual(len(self.rows()), 1)

    def test_numeric_rotation_order_ignores_unrelated_mtime(self):
        self.pve.write_log([(stamp(5), "Mains returned. No longer on UPS batteries.")])
        archive = self.pve.log.with_name("apcupsd.events.1")
        archive.write_text(f"{stamp(0)}  apcupsd 3.14.14 (31 May 2016) debian startup succeeded\n{stamp(1)}  Power failure.\n")
        # 复制/压缩常改变 mtime，不能据此把恢复行读到起始行之前。
        os.utime(self.pve.log, (100, 100))
        os.utime(archive, (200, 200))
        self.engine.sync()
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["duration"], 4)
        self.assertEqual(rows[0]["state"], "restored")

    def test_clear_same_second_timezone_normalized_rotation_copy_stays_cleared(self):
        self.pve.write_log([(stamp(10), "Power failure."), (stamp(10), "Mains returned. No longer on UPS batteries.")])
        self.engine.sync()
        previous = self.pve.log.read_text()
        self.engine.clear(captured_ns=nanos(10, 100))
        self.pve.log.rename(self.pve.log.with_name("apcupsd.events.1"))
        self.pve.log.write_text(previous.replace("2026-10-11 12:", "2026-10-11 04:").replace("+0800", "+0000"))
        self.engine.sync()
        self.assertEqual(self.rows(), [])

    def test_gzip_rotation_preserves_previously_captured_events(self):
        self.startup()
        self.cycle()
        previous = self.pve.log.read_bytes()
        self.pve.log.unlink()
        with gzip.open(self.pve.log.with_name("apcupsd.events.1.gz"), "wb") as output:
            output.write(previous)
        self.pve.write_log([(stamp(10), "Power failure."), (stamp(12), "Mains returned. No longer on UPS batteries.")])
        self.engine.sync()
        self.assertEqual(len(self.rows()), 2)
        self.assertEqual(sorted(row["duration"] for row in self.rows()), [2, 5])

    def test_clear_uses_same_boot_monotonic_event_watermark(self):
        with patch.object(self.module, "boot_clock", return_value=("fixture-boot", 100.0)):
            self.engine.capture("powerout", nanos(1), "old-boot-event", session="daemon-A", captured_uptime=99.0, boot_id="fixture-boot")
            self.engine.clear(captured_ns=nanos(10))
            # 墙钟倒退时，同一 boot 的调用时钟仍能正确区别清空前后。
            self.engine.capture("powerout", nanos(1), "new-clock-stepped-start", session="daemon-A", captured_uptime=100.1, boot_id="fixture-boot")
            self.engine.capture("mainsback", nanos(2), "new-clock-stepped-end", session="daemon-A", captured_uptime=100.2, boot_id="fixture-boot")
            self.engine.capture("powerout", nanos(12), "delayed-pre-clear-start", session="daemon-A", captured_uptime=99.9, boot_id="fixture-boot")
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["state"], "restored")

    def test_existing_log_but_stopped_logging_does_not_erase_callbacks(self):
        self.startup()
        self.engine.sync()
        self.emit("powerout", 1)
        self.emit("mainsback", 3)
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.rows()[0]["duration"], 2)

    def test_old_unclaimed_log_evidence_does_not_consume_new_cycle_callbacks(self):
        self.pve.write_log([(stamp(0), "apcupsd 3.14.14 (31 May 2016) debian startup succeeded"), (stamp(1), "Power failure."), (stamp(1), "Running on UPS batteries."), (stamp(2), "Mains returned. No longer on UPS batteries."), (stamp(2), "Power is back. UPS running on mains.")])
        self.engine.sync()
        self.emit("powerout", 3)
        self.emit("mainsback", 4)
        rows = self.rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual(sorted(row["duration"] for row in rows), [1, 1])

    def test_runtime_unclaimed_old_log_does_not_consume_later_callbacks(self):
        self.startup()
        self.engine.sync()
        self.pve.write_log([(stamp(1), "Power failure."), (stamp(2), "Power is back. UPS running on mains.")], append=True)
        self.engine.sync()
        self.emit("powerout", 20)
        self.emit("mainsback", 21)
        rows = self.rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual(sorted(row["duration"] for row in rows), [1, 1])
        self.assertEqual(self.query()["summary"]["unassociated"], 0)

    def test_log_unavailable_callback_and_snapshot_do_not_double_count(self):
        self.emit("powerout", 10)
        self.emit("mainsback", 15)
        details = raw_snapshot("ONLINE", stamp(13), stamp(18), "Low line voltage", 1, stamp(0))
        self.engine.sync(details)
        result = self.query()
        self.assertEqual(result["summary"]["count"], 1)
        self.assertEqual(result["summary"]["unassociated"], 1)
        self.assertEqual(result["rows"][0]["reason_raw"], "Low line voltage")
        # 无唯一身份时不硬把偏移时间当成同一事件，原始回调仍可完整核查。
        self.assertEqual(self.engine.db.execute("SELECT COUNT(*) FROM hooks").fetchone()[0], 2)
        self.assertIn("fixture-event-1", self.engine.diagnostics())

    def test_same_second_delayed_old_log_cannot_reappear_after_clear(self):
        self.emit("powerout", 10, extra=100)
        self.emit("mainsback", 10, extra=200)
        self.engine.clear(captured_ns=nanos(10, 300))
        # 秒精度日志晚到不能证明它发生在清空之后，不得复活旧记录。
        self.pve.write_log([(stamp(10), "Power failure."), (stamp(10), "Power is back. UPS running on mains.")])
        self.engine.sync()
        self.assertEqual(self.rows(), [])
        self.emit("powerout", 10, extra=400)
        self.emit("mainsback", 10, extra=500)
        self.assertEqual(len(self.rows()), 1, "有独立回调身份证据的同秒新事件必须保留")

    def test_late_offbattery_corrects_previous_later_mainsback_time(self):
        self.emit("powerout", 1)
        self.emit("mainsback", 4)
        self.emit("offbattery", 2)
        row = self.rows()[0]
        self.assertEqual(row["end_ns"], nanos(2))
        self.assertEqual(row["duration"], 1)
        self.assertEqual(row["state"], "restored")

    def test_unrelated_later_log_after_restart_does_not_hide_hook_history(self):
        self.startup()
        self.engine.sync()
        self.emit("powerout", 1)
        self.emit("mainsback", 2)
        self.assertEqual(len(self.rows()), 1)
        self.startup(9)
        self.pve.write_log([(stamp(10), "Power failure."), (stamp(12), "Power is back. UPS running on mains.")], append=True)
        self.engine.sync()
        rows = self.rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual(sorted(row["duration"] for row in rows), [1, 2])
        self.assertEqual(self.query()["summary"]["unassociated"], 0)

    def test_empty_snapshot_invalidates_previous_current_supply_confirmation(self):
        self.startup()
        self.emit("powerout", 1, "Power failure.", raw_snapshot("ONBATT", stamp(1), reason="Low line voltage", counter=1, daemon_start=stamp(0)))
        self.assertEqual(self.query()["summary"]["current"], 1)
        self.engine.sync("")
        result = self.query()
        self.assertEqual(result["summary"]["current"], 0)
        self.assertEqual(result["summary"]["unknown"], 1)

    def test_native_unknown_restore_cannot_prove_later_callback_is_independent(self):
        self.startup()
        self.pve.write_log([(stamp(1), "Power failure.")], append=True)
        self.engine.sync()
        self.emit("powerout", 2)
        self.emit("mainsback", 3)
        result = self.query()
        self.assertEqual(result["summary"]["count"], 1)
        self.assertEqual(result["summary"]["unknown"], 1)
        self.assertEqual(result["summary"]["total_seconds"], 0)
        self.assertEqual(result["summary"]["unassociated"], 1)
        self.assertEqual(result["rows"][0]["state"], "unknown")
        self.assertIsNone(result["rows"][0]["end_ns"])
        self.assertEqual(result["pending"][0]["origin"], "hook")
        self.assertEqual(result["pending"][0]["duration"], 1)
        self.assertFalse(result["pending"][0]["reason_raw"])
        self.assertEqual(self.engine.db.execute("SELECT COUNT(*) FROM hooks").fetchone()[0], 2)

    def test_unchanged_sync_does_not_write_history_database(self):
        self.startup()
        details = self.cycle()
        self.engine.sync(details)
        database = self.pve.state_dir / "history.db"
        before = (database.read_bytes(), database.stat().st_mtime_ns, database.stat().st_ino)
        for _ in range(5):
            self.engine.sync(details)
        self.assertEqual((database.read_bytes(), database.stat().st_mtime_ns, database.stat().st_ino), before)

    def test_repeated_capture_token_is_idempotent(self):
        self.engine.capture("powerout", nanos(1), "same-event-token", session="daemon-A")
        self.engine.capture("powerout", nanos(1), "same-event-token", session="daemon-A")
        self.engine.capture("mainsback", nanos(2), "restore-token", session="daemon-A")
        self.engine.capture("mainsback", nanos(2), "restore-token", session="daemon-A")
        self.assertEqual(len(self.rows()), 1)

    def test_disk_full_and_write_failures_preserve_rows_and_recover(self):
        self.startup()
        self.cycle()
        original = self.rows()
        for failpoint in ("disk-full", "write-error", "commit-before"):
            with self.subTest(failpoint=failpoint):
                with patch.dict(os.environ, {"PVE_UPS_TESTING": "1", "PVE_UPS_TEST_FAILPOINT": failpoint}):
                    with self.assertRaises(Exception):
                        self.engine.capture("powerout", nanos(20), "failure-" + failpoint, session="daemon-A")
                self.assertEqual(self.rows(), original)
                self.assertTrue(self.query()["notice"], "同步故障必须可查询")
        self.emit("powerout", 30)
        self.emit("mainsback", 31)
        self.assertEqual(len(self.rows()), 2)
        connection = sqlite3.connect(self.pve.state_dir / "history.db")
        self.addCleanup(connection.close)
        self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_real_sqlite_full_rolls_back_original_error_and_recovers(self):
        self.startup()
        self.cycle()
        original = self.rows()
        old_max = self.engine.db.execute("PRAGMA max_page_count").fetchone()[0]
        current_pages = self.engine.db.execute("PRAGMA page_count").fetchone()[0]
        self.engine.db.execute(f"PRAGMA max_page_count={current_pages}")
        with self.assertRaises(Exception) as raised:
            self.engine.capture("powerout", nanos(20), "full-page-token-" + "x" * 500_000, session="daemon-A")
        self.assertIn("full", str(raised.exception).lower())
        self.assertEqual(self.rows(), original)
        self.assertIn("full", self.query()["notice"].lower())
        self.engine.db.execute(f"PRAGMA max_page_count={old_max}")
        self.cycle(30, 2, counter=2)
        self.assertEqual(len(self.rows()), 2)

    def test_native_eventsfilemax_suffix_trim_preserves_identity(self):
        entries = [(stamp(0), "apcupsd 3.14.14 (31 May 2016) debian startup succeeded"), (stamp(1), "Power failure."), (stamp(2), "Mains returned. No longer on UPS batteries."), (stamp(3), "Power failure."), (stamp(4), "Mains returned. No longer on UPS batteries.")]
        self.pve.write_log(entries)
        self.engine.sync()
        original_inode = self.pve.log.stat().st_ino
        self.pve.write_log(entries[-2:])
        self.assertEqual(self.pve.log.stat().st_ino, original_inode)
        self.engine.sync()
        self.assertEqual(len(self.rows()), 2)
        self.pve.write_log([(stamp(5), "Power failure."), (stamp(6), "Mains returned. No longer on UPS batteries.")], append=True)
        self.engine.sync()
        self.assertEqual(len(self.rows()), 3)

    def test_large_native_eventsfilemax_trim_above_64kb_never_reimports(self):
        entries = [(stamp(0), "apcupsd 3.14.14 (31 May 2016) debian startup succeeded")]
        for index in range(1500):
            entries.extend([(stamp(index * 2 + 1), "Power failure."), (stamp(index * 2 + 2), "Mains returned. No longer on UPS batteries.")])
        self.pve.write_log(entries)
        self.engine.sync()
        self.assertEqual(len(self.rows()), 1500)
        original = self.pve.log.read_bytes()
        retained = original[int(len(original) * 0.2):].split(b"\n", 1)[1]
        self.assertGreater(len(retained), 65_536)
        self.pve.log.write_bytes(retained)
        self.engine.sync()
        self.assertEqual(len(self.rows()), 1500)
        self.pve.write_log([(stamp(4000), "Power failure."), (stamp(4001), "Mains returned. No longer on UPS batteries.")], append=True)
        self.engine.sync()
        self.assertEqual(len(self.rows()), 1501)

    def test_clear_failure_is_reported_and_previous_rows_survive(self):
        self.startup()
        self.cycle()
        original = self.rows()
        with patch.dict(os.environ, {"PVE_UPS_TESTING": "1", "PVE_UPS_TEST_FAILPOINT": "disk-full"}):
            with self.assertRaises(Exception):
                self.engine.clear(captured_ns=nanos(20))
        self.assertEqual(self.rows(), original)
        self.assertTrue(self.query()["notice"])

    def test_malformed_legacy_preserves_valid_rows_and_diagnostic(self):
        legacy = self.pve.state_dir / "history.tsv"
        original = f"{stamp(1)}\t{stamp(2)}\t1\t输入市电电压过低\tLow line voltage\nnot-an-event\n{stamp(3)}\t-\t-\t原因未记录\t\n"
        legacy.write_text(original)
        self.engine.sync()
        self.assertEqual(len(self.rows()), 2)
        self.assertTrue(self.query()["notice"])
        self.assertEqual(legacy.read_text(), original)

    def test_abnormal_database_is_not_overwritten_and_can_recover_after_repair(self):
        database = self.pve.state_dir / "history.db"
        database.write_bytes(b"this is not a sqlite database\n")
        original = database.read_bytes()
        with self.assertRaises(Exception):
            self.engine.sync()
        self.assertEqual(database.read_bytes(), original)
        diagnostic = self.engine.diagnostics()
        self.assertIn("not a database", diagnostic)
        self.engine.close()
        database.rename(database.with_suffix(".damaged"))
        self.engine = self.module.Engine(str(self.pve.state_dir), str(self.pve.log), str(self.pve.state_dir / "history.tsv"))
        self.addCleanup(self.engine.close)
        self.engine.sync()
        self.emit("powerout", 1)
        self.emit("mainsback", 2)
        self.assertEqual(len(self.rows()), 1)

    def test_statistics_distinguish_active_restored_and_unknown(self):
        self.pve.write_log([(stamp(1), "Power failure."), (stamp(4), "Mains returned. No longer on UPS batteries."), (stamp(5), "Power failure.")])
        self.engine.sync(raw_snapshot("ONBATT", stamp(5), reason="Low line voltage"))
        result = self.query()
        self.assertEqual(result["summary"]["count"], 2)
        self.assertEqual(result["summary"]["total_seconds"], 3)
        self.assertEqual(result["summary"]["current"], 1)
        self.assertEqual(result["summary"]["unknown"], 0)
        self.engine.sync(raw_snapshot("ONLINE"))
        self.assertEqual(self.query()["summary"]["current"], 0)
        self.assertEqual(self.query()["summary"]["unknown"], 1)

    def test_history_pagination_exposes_every_retained_event(self):
        for index in range(10):
            start = index * 10 + 1
            self.pve.write_log([(stamp(start), "Power failure."), (stamp(start + 2), "Mains returned. No longer on UPS batteries.")], append=True)
        self.engine.sync()
        pages = [self.engine.query(page=page, size=3) for page in range(1, 5)]
        self.assertEqual(pages[0]["summary"]["pages"], 4)
        all_ids = [row["id"] for page in pages for row in page["rows"]]
        self.assertEqual(len(all_ids), 10)
        self.assertEqual(len(set(all_ids)), 10)

    def test_incremental_sync_does_not_reparse_unchanged_log(self):
        for index in range(1000):
            self.pve.write_log([(stamp(index * 3 + 1), "Power failure."), (stamp(index * 3 + 2), "Mains returned. No longer on UPS batteries.")], append=True)
        self.engine.sync()
        read_before = self.engine.bytes_read
        parsed_before = self.engine.lines_parsed
        started = time.monotonic()
        for _ in range(3):
            self.engine.sync()
            self.engine.query(page=1, size=3)
        self.assertEqual(self.engine.bytes_read, read_before)
        self.assertEqual(self.engine.lines_parsed, parsed_before)
        self.assertLess(time.monotonic() - started, 2, "千条历史后的刷新必须及时响应")
        self.pve.write_log([(stamp(4000), "Power failure."), (stamp(4001), "Mains returned. No longer on UPS batteries.")], append=True)
        self.engine.sync()
        self.assertLessEqual(self.engine.lines_parsed - parsed_before, 2)
        self.assertEqual(len(self.rows()), 1001)

    def test_snapshot_change_with_three_thousand_native_and_hook_rows_stays_responsive(self):
        self.engine.sync(raw_snapshot("ONLINE"))
        records = []
        for index in range(3000):
            native_start = index * 4
            hook_start = native_start if index % 2 == 0 else native_start + 2
            for origin, start in (("log", native_start), ("hook", hook_start)):
                records.append((f"fixture-{origin}-{index}", stamp(start), nanos(start), stamp(start + 1), nanos(start + 1), 1, "", "原因未记录", "fixture-session", index + 1, "fixture-source", origin, 1))
        # 直接装载大量有效投影，避免把成千次逐事件写盘的准备时间算进刷新。
        with self.engine._transaction():
            self.engine.db.executemany("INSERT INTO cycles VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", records)
        started = time.monotonic()
        self.engine.sync(raw_snapshot("ONLINE", reason="UNKNOWN EVENT"))
        result = self.engine.query(page=1, size=3)
        elapsed = time.monotonic() - started
        self.assertEqual(result["summary"]["count"], 4500)
        self.assertEqual(result["summary"]["unassociated"], 1500)
        self.assertEqual(result["summary"]["total_seconds"], 4500)
        self.assertEqual(len(result["pending"]), 3)
        self.assertEqual(len(result["rows"]), 3)
        self.assertLess(elapsed, 2, "六千条投影且snapshot变化时，分类与分页应在2秒内完成")

    def test_write_interrupted_by_sigkill_rolls_back_and_can_continue(self):
        self.startup()
        self.cycle()
        original = self.rows()
        environment = self.pve.env.copy()
        environment["PVE_UPS_TEST_FAILPOINT"] = "pause-before-commit"
        arguments = [sys.executable, "-B", str(REPO / "lib/history_store.py"), "--state-dir", str(self.pve.state_dir), "--events", str(self.pve.log), "capture", "--event", "powerout", "--captured-ns", str(nanos(20)), "--token", "interrupted-transaction"]
        process = subprocess.Popen(arguments, env=environment, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(lambda: process.poll() is not None or process.kill())
        try:
            import select
            ready, _, _ = select.select([process.stderr], [], [], 3)
            self.assertTrue(ready, "未到达提交前中断点")
            self.assertIn("PVE_UPS_TEST_BEFORE_COMMIT", process.stderr.readline())
            process.kill()
            output, errors = process.communicate(timeout=3)
            self.assertEqual(process.returncode, -signal.SIGKILL, (output, errors))
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=3)
        self.assertEqual(self.rows(), original)
        self.cycle(30, 2, counter=2)
        self.assertEqual(len(self.rows()), 2)
        with sqlite3.connect(self.pve.state_dir / "history.db") as connection:
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        connection.close()

    def test_real_directory_permission_error_is_queryable_and_recoverable(self):
        self.startup()
        self.cycle()
        original = self.rows()
        self.pve.root.chmod(0o755)
        self.pve.state_dir.chmod(0o555)
        (self.pve.state_dir / "history.db").chmod(0o666)
        def demote():
            os.setgroups([])
            os.setgid(65534)
            os.setuid(65534)
        arguments = [sys.executable, "-B", str(REPO / "lib/history_store.py"), "--state-dir", str(self.pve.state_dir), "--events", str(self.pve.log), "capture", "--event", "powerout", "--captured-ns", str(nanos(20)), "--token", "permission-error-event"]
        result = subprocess.run(arguments, env=self.pve.env, input="", text=True, capture_output=True, preexec_fn=demote if os.geteuid() == 0 else None, timeout=3)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("历史", result.stderr)
        self.assertEqual(self.rows(), original)
        self.pve.state_dir.chmod(0o700)
        self.cycle(30, 2, counter=2)
        self.assertEqual(len(self.rows()), 2)


if __name__ == "__main__":
    unittest.main()
