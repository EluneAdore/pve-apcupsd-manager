"""通过临时根目录和系统工具替身验证安装、维护、倒计时和恢复行为。"""

import json
import os
from pathlib import Path
import pty
import select
import signal
import stat
import subprocess
import tempfile
import tarfile
import time
import unittest


PROJECT = Path(__file__).resolve().parents[1]
FAKE_SYSTEM = PROJECT / "tests/fake_system.py"
FIXED_SHA = "1234567890abcdef1234567890abcdef12345678"
ONLINE = """APC      : 001,043,1051
UPSNAME  : 家用电源
MODEL    : Back-UPS 650
STATUS   : ONLINE
BCHARGE  : 100.0 Percent
TIMELEFT : 60.0 Minutes
LOADPCT  : 20.0 Percent
MAXTIME  : 1800 Seconds
TONBATT  : 0 Seconds
MBATTCHG : 5 Percent
MINTIMEL : 3 Minutes
"""
ONBATT = ONLINE.replace("STATUS   : ONLINE", "STATUS   : ONBATT").replace(
    "TONBATT  : 0 Seconds", "TONBATT  : 120 Seconds"
)


class IsolatedSystem:
    def __init__(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pve-ups-test-", dir="/tmp")
        self.root = Path(self.temp.name)
        self.bin = self.root / "fake-bin"
        self.bin.mkdir()
        for command in (
            "apt-get", "apt", "apt-cache", "apt-mark", "dpkg-query",
            "systemctl", "apcaccess", "id", "shutdown", "poweroff", "reboot", "pvesh",
            "curl", "wget",
            "pveversion",
        ):
            (self.bin / command).symlink_to(FAKE_SYSTEM)
        (self.root / "etc/pve").mkdir(parents=True)
        (self.root / "etc/default").mkdir(parents=True)
        (self.root / "usr/sbin").mkdir(parents=True)
        self.config = self.root / "etc/apcupsd/apcupsd.conf"
        self.defaults = self.root / "etc/default/apcupsd"
        self.db_file = self.root / "fake-system.json"
        self.set_db({
            "packages": {
                "bash": {"version": "5.2", "manual": True, "deps": []},
                "pve-manager": {"version": "9.0", "manual": True, "deps": []},
            },
            "service": {"active": False, "enabled": False, "masked": False},
            "ups": ONLINE,
        })
        self.env = os.environ.copy()
        self.env.update({
            "PATH": str(self.bin) + os.pathsep + os.environ["PATH"],
            "PVE_UPS_TESTING": "1",
            "PVE_UPS_TEST_ROOT": str(self.root),
            "TERM": "dumb",
            "LC_ALL": "C.UTF-8",
            "APP_ROOT": str(PROJECT),
            "STATE_DIR": str(self.root / "var/lib/pve-ups"),
            "CONFIG_FILE": str(self.config),
            "DEFAULTS_FILE": str(self.defaults),
            "APPCONFIG_DIR": str(self.config.parent),
            "INSTALL_DIR": str(self.root / "opt/pve-ups"),
            "COMMAND_PATH": str(self.root / "usr/local/sbin/pve-ups"),
            "POLICY_FILE": str(self.root / "usr/sbin/policy-rc.d"),
            "APT_LOG_DIR": str(self.root / "var/lib/pve-ups/logs"),
        })

    def close(self):
        self.temp.cleanup()

    def db(self):
        return json.loads(self.db_file.read_text())

    def set_db(self, data):
        self.db_file.write_text(json.dumps(data, indent=2))

    def update(self, **changes):
        db = self.db()
        db.update(changes)
        self.set_db(db)

    def calls(self):
        path = self.root / "commands.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def clear_calls(self):
        (self.root / "commands.jsonl").unlink(missing_ok=True)

    def shell(self, script):
        sources = """
set -Eeuo pipefail
source "$APP_ROOT/lib/common.sh"
source "$APP_ROOT/lib/config.sh"
for module in state lifecycle menu; do
    if [[ -f "$APP_ROOT/lib/$module.sh" ]]; then
        source "$APP_ROOT/lib/$module.sh"
    fi
done
"""
        return subprocess.run(
            ["bash", "-c", sources + "\n" + script], cwd=PROJECT,
            env=self.env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=30,
        )

    def command(self, *args):
        return self.command_from(PROJECT / "bin/pve-ups", *args)

    def installed_command(self, *args):
        return self.command_from(self.root / "usr/local/sbin/pve-ups", *args)

    def command_from(self, path, *args):
        return subprocess.run(
            ["bash", str(path), *args], cwd=self.root,
            env=self.env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=30,
        )

    def enable_bootstrap_fixture(self):
        archive = self.root / "fixture.tar.gz"
        prefix = "pve-apcupsd-manager-" + FIXED_SHA
        with tarfile.open(archive, "w:gz") as output:
            for item in ("VERSION", "bin", "lib", "bootstrap.sh", "README.md", "docs", "LICENSE"):
                path = PROJECT / item
                if path.exists():
                    output.add(path, arcname=prefix + "/" + item)
        self.update(
            bootstrap_archive=str(archive), bootstrap_script=str(PROJECT / "bootstrap.sh"),
            bootstrap_commit=FIXED_SHA,
        )

    def bootstrap(self, *args, pipe=False):
        if pipe:
            source = "https://raw.githubusercontent.com/EluneAdore/pve-apcupsd-manager/" + FIXED_SHA + "/bootstrap.sh"
            command = ["bash", "-c", 'set -o pipefail; curl -fsSL "$1" | bash -s -- "${@:2}"', "bootstrap-test", source, *args]
        else:
            command = ["bash", str(PROJECT / "bootstrap.sh"), *args]
        return subprocess.run(
            command, cwd=self.root, env=self.env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
        )

    def seed_existing(self, *, active=True, enabled=True, masked=False):
        self.config.parent.mkdir(parents=True, exist_ok=True)
        self.config.write_text(
            "# 用户原有配置\nUPSCABLE usb\nUPSTYPE usb\nDEVICE\n"
            "BATTERYLEVEL 9\nMINUTES 7\nTIMEOUT 720\nONBATTERYDELAY 10\n"
            "NETSERVER on\nNISIP 127.0.0.1\nNISPORT 3551\n"
            "LOCKFILE /var/lock\nPWRFAILDIR /etc/apcupsd\n"
            "NOLOGINDIR /etc\nKILLDELAY 0\n"
        )
        self.config.chmod(0o640)
        self.defaults.write_text('ISCONFIGURED="yes"\n# 原有设置\n')
        self.defaults.chmod(0o600)
        custom = self.config.parent / "custom-user-file"
        custom.write_text("原有自定义文件\n")
        custom.chmod(0o604)
        control = self.config.parent / "apccontrol"
        control.write_text("#!/bin/sh\n# 原有事件处理\nexit 0\n")
        control.chmod(0o755)
        db = self.db()
        db["packages"]["apcupsd"] = {
            "version": "3.14.14-3", "manual": False, "deps": ["libusb-0.1-4"],
        }
        db["packages"]["libusb-0.1-4"] = {"version": "1.0", "manual": False, "deps": []}
        db["service"] = {"active": active, "enabled": enabled, "masked": masked}
        self.set_db(db)


class MenuSession:
    """使用真正的伪终端检查输入、空闲刷新和自动保存的组合行为。"""

    def __init__(self, system):
        self.master, slave = pty.openpty()
        self.process = subprocess.Popen(
            ["bash", str(PROJECT / "bin/pve-ups")], cwd=PROJECT,
            env=system.env, stdin=slave, stdout=slave, stderr=slave,
            start_new_session=True,
        )
        os.close(slave)
        self.output = ""

    def close(self):
        if self.process.poll() is None:
            os.killpg(self.process.pid, signal.SIGTERM)
            self.process.wait(timeout=5)
        os.close(self.master)

    def write(self, text):
        os.write(self.master, text.encode())

    def collect(self, duration):
        end = time.monotonic() + duration
        chunk = b""
        while time.monotonic() < end:
            ready, _, _ = select.select([self.master], [], [], min(0.1, end - time.monotonic()))
            if ready:
                try:
                    data = os.read(self.master, 65536)
                except OSError:
                    break
                if not data:
                    break
                chunk += data
        decoded = chunk.decode(errors="replace")
        self.output += decoded
        return decoded

    def wait_for(self, text, duration=8):
        end = time.monotonic() + duration
        current = ""
        while time.monotonic() < end:
            current += self.collect(0.1)
            if text in current:
                return current
            if self.process.poll() is not None:
                break
        raise AssertionError(f"菜单未出现 {text!r}，已有输出：\n{self.output}")


class PveUpsTests(unittest.TestCase):
    def setUp(self):
        self.system = IsolatedSystem()
        self.addCleanup(self.system.close)

    def success(self, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def failed(self, result):
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)

    def install(self):
        self.success(self.system.shell("lifecycle_install"))

    def assert_no_maintenance(self):
        bad = [call for call in self.system.calls() if (
            call["command"] in {"apt", "apt-get"}
            or call["command"] == "systemctl" and any(
                action in call["args"] for action in {"start", "restart", "stop", "enable", "disable"}
            )
        )]
        self.assertEqual(bad, [], "被禁止的维护操作不应产生副作用")

    def test_countdown_uses_current_outage_not_total_history(self):
        self.system.update(ups=ONBATT + "CUMONBATT : 9600 Seconds\n")
        output = self.success(self.system.shell("read_ups_snapshot; countdown_seconds"))
        self.assertEqual(output.strip(), "1680")

    def test_return_of_mains_clears_previous_countdown_display(self):
        self.system.update(ups=ONBATT)
        output = self.success(self.system.shell("render_status"))
        self.assertIn("28", output)
        self.system.update(ups=ONLINE.replace("TONBATT  : 0 Seconds", "TONBATT  : 120 Seconds"))
        output = self.success(self.system.shell("render_status"))
        self.assertIn("未启动", output)
        self.assertNotIn("28 分", output)

    def test_communications_lost_never_reports_trusted_countdown(self):
        self.system.update(ups=ONBATT.replace("STATUS   : ONBATT", "STATUS   : COMMLOST"))
        output = self.success(self.system.shell("render_status"))
        self.assertIn("无法确认", output)

    def test_new_outage_starts_full_countdown_again(self):
        self.system.update(ups=ONBATT.replace("TONBATT  : 120 Seconds", "TONBATT  : 0 Seconds"))
        output = self.success(self.system.shell("read_ups_snapshot; countdown_seconds"))
        self.assertEqual(output.strip(), "1800")

    def test_timeout_zero_has_no_scheduled_shutdown(self):
        self.system.update(ups=ONBATT.replace("MAXTIME  : 1800 Seconds", "MAXTIME  : 0 Seconds"))
        output = self.success(self.system.shell("render_status"))
        self.assertIn("禁用", output)

    def test_cli_status_reads_live_ups(self):
        output = self.success(self.system.command("status"))
        self.assertIn("市电", output)
        self.assertIn("100", output)

    def test_install_sets_half_hour_and_native_usb_configuration(self):
        self.install()
        output = self.success(self.system.shell(
            'for key in TIMEOUT UPSCABLE UPSTYPE ONBATTERYDELAY NISIP; do config_get "$key"; done'
        ))
        self.assertEqual(output.splitlines(), ["1800", "usb", "usb", "0", "127.0.0.1"])
        self.assertTrue(self.system.db()["service"]["active"])
        self.assertTrue(self.system.db()["service"]["enabled"])

    def test_parameter_changes_are_immediately_saved_and_read_back(self):
        self.install()
        output = self.success(self.system.shell("config_change TIMEOUT 900; config_get TIMEOUT"))
        self.assertEqual(output.splitlines()[-1], "900")
        self.assertIn("TIMEOUT 900", self.system.config.read_text())
        self.assertTrue(any(call["command"] == "systemctl" and "restart" in call["args"] for call in self.system.calls()))

    def test_parameter_display_reads_running_value_then_saved_change(self):
        self.install()
        self.system.config.write_text(self.system.config.read_text().replace("TIMEOUT 1800", "TIMEOUT 600"))
        output = self.success(self.system.shell("read_ups_snapshot; current_setting TIMEOUT MAXTIME"))
        self.assertIn("30 分", output)
        self.assertIn("运行值", output)
        self.success(self.system.shell("config_change TIMEOUT 900"))
        output = self.success(self.system.shell("read_ups_snapshot; current_setting TIMEOUT MAXTIME"))
        self.assertIn("15 分", output)
        self.assertIn("运行值", output)

    def test_stopped_parameter_display_uses_configuration_value(self):
        self.install()
        db = self.system.db()
        db["service"]["active"] = False
        self.system.set_db(db)
        self.system.config.write_text(self.system.config.read_text().replace("TIMEOUT 1800", "TIMEOUT 600"))
        output = self.success(self.system.shell("read_ups_snapshot; current_setting TIMEOUT MAXTIME"))
        self.assertIn("10 分", output)
        self.assertIn("配置值", output)

    def test_invalid_parameter_never_writes_or_restarts(self):
        self.install()
        before = self.system.config.read_bytes()
        for value in ("-1", "hello", "1;touch /tmp/forbidden", "1.5"):
            with self.subTest(value=value):
                self.system.clear_calls()
                self.failed(self.system.shell("config_change TIMEOUT " + repr(value)))
                self.assertEqual(self.system.config.read_bytes(), before)
                self.assert_no_maintenance()

    def test_out_of_range_battery_percentage_is_rejected(self):
        self.install()
        before = self.system.config.read_bytes()
        self.failed(self.system.shell("config_change BATTERYLEVEL 101"))
        self.assertEqual(self.system.config.read_bytes(), before)

    def test_restart_failure_restores_previous_configuration_and_mode(self):
        self.install()
        self.system.config.chmod(0o640)
        before = self.system.config.read_bytes()
        self.system.update(restart_failures=1)
        self.failed(self.system.shell("config_change TIMEOUT 900"))
        self.assertEqual(self.system.config.read_bytes(), before)
        self.assertEqual(stat.S_IMODE(self.system.config.stat().st_mode), 0o640)
        self.assertTrue(self.system.db()["service"]["active"])

    def test_power_failure_blocks_config_upgrade_and_restart(self):
        self.install()
        before = self.system.config.read_bytes()
        for operation in ("config_change TIMEOUT 900", "lifecycle_upgrade", "service_action restart"):
            with self.subTest(operation=operation):
                self.system.update(ups=ONBATT)
                self.system.clear_calls()
                self.failed(self.system.shell(operation))
                self.assertEqual(self.system.config.read_bytes(), before)
                self.assert_no_maintenance()

    def test_active_communications_loss_blocks_maintenance(self):
        self.install()
        self.system.update(ups=ONLINE.replace("STATUS   : ONLINE", "STATUS   : COMMLOST"))
        self.system.clear_calls()
        self.failed(self.system.shell("config_change MINUTES 8"))
        self.assert_no_maintenance()

    def test_power_loss_between_edit_and_apply_prevents_restart(self):
        self.install()
        before = self.system.config.read_bytes()
        self.system.update(ups=ONBATT, ups_sequence=[ONLINE, ONLINE, ONBATT])
        self.system.clear_calls()
        self.failed(self.system.shell("config_change TIMEOUT 900"))
        self.assertEqual(self.system.config.read_bytes(), before)
        self.assert_no_maintenance()

    def test_active_failed_status_query_blocks_maintenance(self):
        self.install()
        self.system.update(ups_failed=True)
        self.system.clear_calls()
        self.failed(self.system.shell("lifecycle_upgrade"))
        self.assert_no_maintenance()

    def test_power_return_allows_configuration_without_previous_outage_time(self):
        self.install()
        self.system.update(ups=ONBATT)
        self.failed(self.system.shell("config_change TIMEOUT 900"))
        self.system.update(ups=ONLINE)
        self.success(self.system.shell("config_change TIMEOUT 900"))
        self.assertIn("TIMEOUT 900", self.system.config.read_text())

    def test_stopped_service_can_be_configured_without_live_ups(self):
        self.install()
        db = self.system.db()
        db["service"]["active"] = False
        db["ups_failed"] = True
        self.system.set_db(db)
        self.success(self.system.shell("config_change TIMEOUT 600"))
        self.assertIn("TIMEOUT 600", self.system.config.read_text())
        self.assertFalse(self.system.db()["service"]["active"])

    def test_repeated_install_keeps_user_parameters_and_first_baseline(self):
        self.system.seed_existing()
        original = self.system.config.read_bytes()
        self.install()
        self.success(self.system.shell("config_change TIMEOUT 900"))
        self.install()
        self.assertIn("TIMEOUT 900", self.system.config.read_text())
        self.assertEqual(sum(line.startswith("TIMEOUT ") for line in self.system.config.read_text().splitlines()), 1)
        self.success(self.system.shell("lifecycle_uninstall"))
        self.assertEqual(self.system.config.read_bytes(), original)

    def test_failed_install_rolls_back_added_packages_files_and_service(self):
        before_packages = self.system.db()["packages"]
        self.system.update(restart_failures=1)
        self.failed(self.system.shell("lifecycle_install"))
        self.assertEqual(self.system.db()["packages"], before_packages)
        self.assertFalse(self.system.config.exists())
        self.assertFalse(self.system.db()["service"]["active"])
        self.assertFalse(self.system.db()["service"]["enabled"])

    def test_failed_install_restores_existing_config_and_service(self):
        self.system.seed_existing(active=False, enabled=False)
        before = self.system.config.read_bytes()
        self.system.update(restart_failures=1)
        self.failed(self.system.shell("lifecycle_install"))
        self.assertEqual(self.system.config.read_bytes(), before)
        self.assertEqual(stat.S_IMODE(self.system.config.stat().st_mode), 0o640)
        self.assertFalse(self.system.db()["service"]["active"])
        self.assertFalse(self.system.db()["service"]["enabled"])

    def test_outage_during_install_keeps_existing_monitor_running(self):
        self.system.seed_existing(active=True, enabled=True)
        config = self.system.config.read_bytes()
        self.system.update(ups=ONBATT, ups_sequence=[ONLINE, ONBATT])
        self.failed(self.system.shell("lifecycle_install"))
        self.assertEqual(self.system.config.read_bytes(), config)
        self.assertTrue(self.system.db()["service"]["active"])
        self.assertTrue(any((self.system.root / "var/lib/pve-ups").glob(".transaction.*")))
        self.assertFalse((self.system.root / "usr/sbin/policy-rc.d").exists())
        interruptions = [call for call in self.system.calls() if call["command"] == "systemctl" and any(
            action in call["args"] for action in {"start", "restart", "stop"}
        )]
        self.assertEqual(interruptions, [])

    def test_initial_communication_failure_preserves_local_recovery_entry(self):
        self.system.update(ups_failed=True)
        self.failed(self.system.shell("lifecycle_install"))
        self.assertTrue(self.system.db()["service"]["active"])
        self.assertTrue((self.system.root / "usr/local/sbin/pve-ups").is_symlink())
        self.assertTrue(any((self.system.root / "var/lib/pve-ups").glob(".transaction.*")))
        self.assertFalse((self.system.root / "usr/sbin/policy-rc.d").exists())
        # 通信恢复后，本地入口可在断网状态完成撤销。
        self.system.update(ups_failed=False, network_failed=True)
        self.success(self.system.installed_command("uninstall", "--yes"))
        self.assertNotIn("apcupsd", self.system.db()["packages"])

    def test_uninstall_preserves_preexisting_software_and_restores_file_modes(self):
        self.system.seed_existing(active=False, enabled=False)
        config = self.system.config.read_bytes()
        defaults = self.system.defaults.read_bytes()
        self.install()
        self.success(self.system.shell("lifecycle_uninstall"))
        self.assertIn("apcupsd", self.system.db()["packages"])
        self.assertFalse(self.system.db()["packages"]["apcupsd"]["manual"])
        self.assertEqual(self.system.config.read_bytes(), config)
        self.assertEqual(self.system.defaults.read_bytes(), defaults)
        self.assertEqual(stat.S_IMODE(self.system.config.stat().st_mode), 0o640)
        self.assertEqual(stat.S_IMODE(self.system.defaults.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((self.system.config.parent / "custom-user-file").stat().st_mode), 0o604)
        self.assertFalse(self.system.db()["service"]["active"])
        self.assertFalse(self.system.db()["service"]["enabled"])

    def test_uninstall_restores_enabled_running_service(self):
        self.system.seed_existing(active=True, enabled=True)
        self.install()
        self.success(self.system.shell("lifecycle_uninstall"))
        self.assertTrue(self.system.db()["service"]["active"])
        self.assertTrue(self.system.db()["service"]["enabled"])

    def test_uninstall_restores_masked_inactive_service(self):
        self.system.seed_existing(active=False, enabled=False, masked=True)
        self.install()
        self.success(self.system.shell("lifecycle_uninstall"))
        self.assertTrue(self.system.db()["service"]["masked"])
        self.assertFalse(self.system.db()["service"]["active"])

    def test_uninstall_only_removes_owned_unneeded_dependencies(self):
        original = set(self.system.db()["packages"])
        self.install()
        db = self.system.db()
        db["packages"]["user-utility"] = {"version": "2.0", "manual": True, "deps": ["libusb-0.1-4"]}
        self.system.set_db(db)
        self.success(self.system.shell("lifecycle_uninstall"))
        remaining = set(self.system.db()["packages"])
        self.assertEqual(remaining, original | {"user-utility", "libusb-0.1-4"})
        self.assertFalse(any("autoremove" in call["args"] for call in self.system.calls()))

    def test_preexisting_dependency_is_never_removed(self):
        db = self.system.db()
        db["packages"]["libwrap0"] = {"version": "1.0", "manual": False, "deps": []}
        self.system.set_db(db)
        self.install()
        self.success(self.system.shell("lifecycle_uninstall"))
        self.assertIn("libwrap0", self.system.db()["packages"])

    def test_dependency_later_marked_manual_is_preserved(self):
        self.install()
        db = self.system.db()
        db["packages"]["libwrap0"]["manual"] = True
        self.system.set_db(db)
        self.success(self.system.shell("lifecycle_uninstall"))
        self.assertIn("libwrap0", self.system.db()["packages"])

    def test_apcupsd_later_required_by_user_software_is_not_blindly_purged(self):
        self.install()
        db = self.system.db()
        db["packages"]["ups-dashboard"] = {"version": "1.0", "manual": True, "deps": ["apcupsd"]}
        self.system.set_db(db)
        # 可以保留软件或拒绝卸载；不可通过清理本项目牵连用户后来安装的软件。
        self.system.shell("lifecycle_uninstall")
        self.assertIn("ups-dashboard", self.system.db()["packages"])
        self.assertIn("apcupsd", self.system.db()["packages"])

    def test_upgrade_preserves_parameters_and_disabled_stopped_service(self):
        self.install()
        self.success(self.system.shell("config_change TIMEOUT 960"))
        config = self.system.config.read_bytes()
        db = self.system.db()
        db["service"] = {"active": False, "enabled": False, "masked": False}
        self.system.set_db(db)
        self.success(self.system.shell("lifecycle_upgrade"))
        self.assertEqual(self.system.config.read_bytes(), config)
        self.assertEqual(self.system.db()["packages"]["apcupsd"]["version"], "3.14.14-4")
        self.assertFalse(self.system.db()["service"]["active"])
        self.assertFalse(self.system.db()["service"]["enabled"])

    def test_failed_upgrade_preserves_parameters_and_running_service(self):
        self.install()
        config = self.system.config.read_bytes()
        self.system.update(network_failed=True)
        self.failed(self.system.shell("lifecycle_upgrade"))
        self.assertEqual(self.system.config.read_bytes(), config)
        self.assertTrue(self.system.db()["service"]["active"])
        self.assertTrue(self.system.db()["service"]["enabled"])

    def test_outage_during_package_index_update_prevents_upgrade(self):
        self.install()
        self.system.update(ups_after_update=ONBATT)
        self.system.clear_calls()
        self.failed(self.system.shell("lifecycle_upgrade"))
        calls = self.system.calls()
        self.assertTrue(any(call["command"] == "apt-get" and "update" in call["args"] for call in calls))
        self.assertFalse(any(call["command"] == "apt-get" and "install" in call["args"] for call in calls))
        self.assertTrue(self.system.db()["service"]["active"])

    def test_unknown_install_directory_is_not_overwritten_or_removed(self):
        owned_dir = self.system.root / "opt/pve-ups"
        owned_dir.mkdir(parents=True)
        sentinel = owned_dir / "user-file"
        sentinel.write_text("不是本项目创建\n")
        self.failed(self.system.shell("lifecycle_install"))
        self.assertEqual(sentinel.read_text(), "不是本项目创建\n")
        self.failed(self.system.shell("lifecycle_uninstall"))
        self.assertTrue(sentinel.exists())

    def test_uninstall_works_without_network_or_github(self):
        self.install()
        self.system.update(network_failed=True)
        self.system.clear_calls()
        self.success(self.system.installed_command("uninstall", "--yes"))
        self.assertNotIn("apcupsd", self.system.db()["packages"])
        self.assertFalse(any(call["command"] in {"curl", "wget"} or "update" in call["args"] for call in self.system.calls()))

    def test_manifest_path_mismatch_prevents_deleting_packages(self):
        self.install()
        manifest = self.system.root / "var/lib/pve-ups/manifest.tsv"
        text = manifest.read_text()
        text = text.replace(str(self.system.config), str(self.system.root / "foreign.conf"))
        manifest.write_text(text)
        self.system.clear_calls()
        self.failed(self.system.shell("lifecycle_uninstall"))
        self.assertIn("apcupsd", self.system.db()["packages"])
        self.assert_no_maintenance()

    def test_modified_command_file_is_preserved_during_uninstall(self):
        self.install()
        command = self.system.root / "usr/local/sbin/pve-ups"
        command.unlink()
        command.write_text("用户后来创建的命令\n")
        self.success(self.system.shell("lifecycle_uninstall"))
        self.assertEqual(command.read_text(), "用户后来创建的命令\n")

    def test_directory_with_missing_ownership_marker_is_preserved(self):
        self.install()
        directory = self.system.root / "opt/pve-ups"
        (directory / ".pve-ups-owned").unlink()
        sentinel = directory / "later-user-file"
        sentinel.write_text("保留用户文件\n")
        self.success(self.system.shell("lifecycle_uninstall"))
        self.assertEqual(sentinel.read_text(), "保留用户文件\n")

    def test_uninstall_preserves_unrelated_later_configuration_file(self):
        self.install()
        sentinel = self.system.config.parent / "later-user-file"
        sentinel.write_text("用户后来添加的配置\n")
        self.success(self.system.shell("lifecycle_uninstall"))
        self.assertEqual(sentinel.read_text(), "用户后来添加的配置\n")

    def test_uninstall_preserves_later_empty_user_directory_and_mode(self):
        self.install()
        directory = self.system.config.parent / "later-user-directory"
        directory.mkdir()
        directory.chmod(0o751)
        self.success(self.system.shell("lifecycle_uninstall"))
        self.assertTrue(directory.is_dir())
        self.assertEqual(list(directory.iterdir()), [])
        self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o751)

    def test_existing_policy_script_is_restored_exactly(self):
        policy = self.system.root / "usr/sbin/policy-rc.d"
        content = "#!/bin/sh\n# 用户策略\nexit 101\n"
        policy.write_text(content)
        policy.chmod(0o750)
        self.install()
        self.assertEqual(policy.read_text(), content)
        self.assertEqual(stat.S_IMODE(policy.stat().st_mode), 0o750)
        self.success(self.system.shell("lifecycle_uninstall"))
        self.assertEqual(policy.read_text(), content)

    def test_policy_script_is_restored_after_install_failure(self):
        policy = self.system.root / "usr/sbin/policy-rc.d"
        content = "#!/bin/sh\n# 用户策略\nexit 101\n"
        policy.write_text(content)
        policy.chmod(0o750)
        self.system.update(install_failed=True)
        self.failed(self.system.shell("lifecycle_install"))
        self.assertEqual(policy.read_text(), content)
        self.assertEqual(stat.S_IMODE(policy.stat().st_mode), 0o750)

    def test_config_export_import_round_trip(self):
        self.install()
        target = self.system.root / "export.conf"
        self.success(self.system.shell(f'config_export "{target}"'))
        self.success(self.system.shell("config_change TIMEOUT 120"))
        self.success(self.system.shell(f'config_import "{target}"'))
        output = self.success(self.system.shell("config_get TIMEOUT"))
        self.assertEqual(output.strip(), "1800")

    def test_invalid_import_keeps_current_configuration(self):
        self.install()
        original = self.system.config.read_bytes()
        imported = self.system.root / "invalid.conf"
        imported.write_text(self.system.config.read_text().replace("TIMEOUT 1800", "TIMEOUT invalid"))
        self.system.clear_calls()
        self.failed(self.system.shell(f'config_import "{imported}"'))
        self.assertEqual(self.system.config.read_bytes(), original)
        self.assert_no_maintenance()

    def test_import_rejects_nonlocal_shutdown_directories_and_shared_modes(self):
        self.install()
        original = self.system.config.read_bytes()
        imported = self.system.root / "invalid-path.conf"
        for key, invalid_value in (
            ("SCRIPTDIR", "/tmp/foreign-events"),
            ("PWRFAILDIR", "/tmp/foreign-powerfail"),
            ("UPSCLASS", "sharemaster"),
            ("UPSMODE", "share"),
        ):
            with self.subTest(key=key):
                lines = self.system.config.read_text().splitlines()
                imported.write_text("\n".join(
                    key + " " + invalid_value if line.startswith(key + " ") else line
                    for line in lines
                ) + "\n")
                self.failed(self.system.shell(f'config_import "{imported}"'))
                self.assertEqual(self.system.config.read_bytes(), original)

    def test_terminal_menu_can_exit_without_modifying_system(self):
        session = MenuSession(self.system)
        self.addCleanup(session.close)
        session.wait_for("请选择")
        self.system.clear_calls()
        session.write("0\n")
        self.assertEqual(session.process.wait(timeout=5), 0, session.output)
        self.assert_no_maintenance()

    def test_terminal_end_of_input_exits_without_refresh_loop(self):
        session = MenuSession(self.system)
        self.addCleanup(session.close)
        session.wait_for("请选择")
        session.write("\x04")
        self.assertEqual(session.process.wait(timeout=5), 0, session.output)

    def test_terminal_parameter_edit_saves_without_separate_apply_button(self):
        self.install()
        session = MenuSession(self.system)
        self.addCleanup(session.close)
        session.wait_for("请选择")
        session.write("2\n")
        settings = session.wait_for("请选择")
        self.assertIn("关机设置", settings)
        session.write("1\n")
        session.wait_for("停电等待多少分钟")
        session.write("15\n")
        session.wait_for("按回车返回")
        self.assertIn("TIMEOUT 900", self.system.config.read_text())
        session.write("\n")
        settings = session.wait_for("请选择")
        self.assertIn("15 分", settings)
        self.assertNotIn("保存并应用", settings)
        session.write("0\n")
        session.wait_for("请选择")
        session.write("0\n")
        self.assertEqual(session.process.wait(timeout=5), 0, session.output)

    def test_terminal_refresh_pauses_after_first_typed_character(self):
        session = MenuSession(self.system)
        self.addCleanup(session.close)
        session.wait_for("请选择")
        session.write("2")
        output = session.collect(2.5)
        self.assertNotIn("PVE UPS 管理", output, "输入期间不应重绘并打断当前输入")
        session.write("\n")
        session.wait_for("请选择")
        session.write("0\n")
        session.wait_for("请选择")
        session.write("0\n")
        self.assertEqual(session.process.wait(timeout=5), 0, session.output)

    def test_idle_terminal_refresh_reads_changed_ups_status(self):
        session = MenuSession(self.system)
        self.addCleanup(session.close)
        session.wait_for("请选择")
        self.system.update(ups=ONLINE.replace("BCHARGE  : 100.0 Percent", "BCHARGE  : 55.0 Percent"))
        output = session.wait_for("55.0 %", duration=5)
        self.assertIn("电池电量", output)
        session.write("0\n")
        self.assertEqual(session.process.wait(timeout=5), 0, session.output)

    def test_bootstrap_pipeline_installs_pinned_commit_with_saved_configuration(self):
        self.system.enable_bootstrap_fixture()
        imported = self.system.root / "saved.conf"
        missing = self.system.root / "missing.conf"
        self.success(self.system.shell(f'prepare_config "{missing}" "{imported}"; config_set TIMEOUT 1260 "{imported}"'))
        output = self.success(self.system.bootstrap("--ref", FIXED_SHA, "--config", str(imported), pipe=True))
        self.assertIn(FIXED_SHA, output)
        self.assertIn("TIMEOUT 1260", self.system.config.read_text())
        self.assertTrue((self.system.root / "usr/local/sbin/pve-ups").is_symlink())
        self.assertTrue(self.system.db()["service"]["active"])
        downloads = [call for call in self.system.calls() if call["command"] == "curl"]
        self.assertTrue(any("codeload.github.com" in " ".join(call["args"]) for call in downloads))

    def test_bootstrap_tool_only_preserves_first_baseline_and_parameters(self):
        self.system.seed_existing()
        original = self.system.config.read_bytes()
        self.install()
        self.success(self.system.shell("config_change TIMEOUT 900"))
        baseline = (self.system.root / "var/lib/pve-ups/baseline/apcupsd-directory/apcupsd.conf").read_bytes()
        packages = self.system.db()["packages"]
        self.system.enable_bootstrap_fixture()
        self.system.clear_calls()
        self.success(self.system.bootstrap("--ref", "v0.1.0", "--tool-only", pipe=True))
        self.assertEqual(self.system.db()["packages"], packages)
        self.assertIn("TIMEOUT 900", self.system.config.read_text())
        self.assertEqual((self.system.root / "var/lib/pve-ups/baseline/apcupsd-directory/apcupsd.conf").read_bytes(), baseline)
        self.assert_no_maintenance()
        self.success(self.system.installed_command("uninstall", "--yes"))
        self.assertEqual(self.system.config.read_bytes(), original)

    def test_bootstrap_failed_pve_check_does_not_download_bundle(self):
        self.system.enable_bootstrap_fixture()
        self.system.update(pve_version="pve-manager/7.4/abcdef")
        self.failed(self.system.bootstrap("--ref", FIXED_SHA))
        self.assertFalse(any(call["command"] == "curl" for call in self.system.calls()))
        self.assertFalse(self.system.config.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
