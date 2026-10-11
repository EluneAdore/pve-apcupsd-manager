"""生命周期与安全边界回归；所有系统操作均在 fake PVE 中执行。"""

from pathlib import Path
import unittest

from support import IsolatedPVE, snapshot


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.pve = IsolatedPVE()
        self.addCleanup(self.pve.close)

    def config_values(self):
        return self.pve.shell('printf "%s/%s/%s\\n" "$(config_get TIMEOUT)" "$(config_get BATTERYLEVEL)" "$(config_get MINUTES)"').stdout.strip()

    def assert_safe_calls(self):
        dangerous = {"shutdown", "poweroff", "reboot", "halt", "killpower"}
        self.assertFalse([call for call in self.pve.calls() if call[0] in dangerous])

    def test_fresh_install_defaults_and_repeat_install(self):
        self.pve.cli("install")
        self.assertEqual(self.config_values(), "1800/50/30")
        baseline = (self.pve.state_dir / "baseline/files.tsv").read_bytes()
        self.pve.cli("install")
        self.assertEqual(self.config_values(), "1800/50/30")
        self.assertEqual((self.pve.state_dir / "baseline/files.tsv").read_bytes(), baseline)
        self.assert_safe_calls()

    def test_existing_settings_preserved_repair_upgrade_tool_update(self):
        self.pve.set_fake(installed=True)
        self.pve.write_config()
        self.pve.cli("install")
        baseline = self.pve.state_dir / "baseline/apcupsd-directory/apcupsd.conf"
        original = baseline.read_bytes()
        for args in (("install",), ("upgrade",), ("tool-install",)):
            self.pve.cli(*args)
            self.assertEqual(self.config_values(), "900/30/3")
            self.assertEqual(baseline.read_bytes(), original)
        self.assert_safe_calls()

    def test_uninstall_restores_native_config_user_scripts_and_service(self):
        self.pve.set_fake(installed=True, active="active", enabled="enabled")
        self.pve.write_config()
        config_dir = self.pve.config.parent
        original = self.pve.config.read_bytes()
        original_control = (config_dir / "apccontrol").read_bytes()
        user_hook = config_dir / "onbattery"
        user_hook.write_text("#!/bin/sh\n# 用户自定义脚本\nexit 0\n")
        user_config = config_dir / "config"
        user_config.write_text("# 用户自定义事件设置\nCUSTOM_FLAG=yes\n")
        original_events_config = user_config.read_bytes()
        self.pve.cli("install")
        self.assertEqual((config_dir / "apccontrol").read_bytes(), original_control)
        self.assertIn("用户自定义脚本", user_hook.read_text())
        self.pve.cli("uninstall", "--yes")
        self.assertEqual(self.pve.config.read_bytes(), original)
        self.assertEqual(user_config.read_bytes(), original_events_config)
        self.assertTrue(user_hook.exists())
        self.assertFalse(self.pve.state_dir.exists())
        self.assertEqual(self.pve.fake()["active"], "active")
        self.assertEqual(self.pve.fake()["enabled"], "enabled")
        self.assert_safe_calls()

    def test_unmanaged_views_do_not_prevent_first_install(self):
        for args in (("status",), ("history",), ("history-record", "powerout")):
            self.pve.cli(*args, check=False)
            self.assertFalse(self.pve.state_dir.exists(), args)
        self.pve.cli("install")
        self.assertTrue((self.pve.state_dir / "manifest.tsv").is_file())

    def test_uninstall_then_old_view_does_not_recreate_state(self):
        self.pve.cli("install")
        self.pve.cli("uninstall", "--yes")
        for args in (("status",), ("history",), ("history-record", "offbattery")):
            self.pve.cli(*args, check=False)
        self.assertFalse(self.pve.state_dir.exists())
        self.pve.cli("install")
        self.assertEqual(self.config_values(), "1800/50/30")

    def test_maintenance_refused_during_onbattery_or_communication_failure(self):
        self.pve.cli("install")
        original = self.pve.config.read_bytes()
        for status in ("ONBATT", "COMMLOST", "ONLINE COMMLOST"):
            self.pve.set_fake(snapshot=snapshot(status))
            for args in (("set", "TIMEOUT", "600"), ("install",), ("upgrade",), ("service", "restart"), ("uninstall", "--yes")):
                self.assertNotEqual(self.pve.cli(*args, check=False).returncode, 0, (status, args))
                self.assertEqual(self.pve.config.read_bytes(), original)
        self.assert_safe_calls()

    def test_outage_countdown_and_mains_recovery_semantics_unchanged(self):
        self.pve.set_fake(snapshot={"STATUS": "ONBATT", "TONBATT": "10 Seconds"})
        output = self.pve.cli("status").stdout
        self.assertIn("29 min 50 s", output)
        self.pve.set_fake(snapshot=snapshot("ONLINE"))
        self.assertIn("关机倒计时：未启动", self.pve.cli("status").stdout)
        self.pve.set_fake(snapshot=snapshot("ONBATT SHUTTING DOWN"))
        self.assertIn("已开始关机（来电后继续完成）", self.pve.cli("status").stdout)
        self.assert_safe_calls()

    def test_failed_tool_deploy_rolls_back_preserving_config_and_baseline(self):
        self.pve.cli("install")
        original = self.pve.config.read_bytes()
        baseline = (self.pve.state_dir / "baseline/files.tsv").read_bytes()
        result = self.pve.shell('lifecycle_deploy_bundle() { return 73; }; tool_install_bundle', check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.pve.config.read_bytes(), original)
        self.assertEqual((self.pve.state_dir / "baseline/files.tsv").read_bytes(), baseline)
        self.assertEqual(self.pve.fake()["active"], "active")


if __name__ == "__main__":
    unittest.main()
