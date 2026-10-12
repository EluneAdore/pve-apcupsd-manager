"""所有测试独立创建 /tmp 根目录，危险系统命令始终使用替身。"""

import json
import os
from pathlib import Path
import subprocess
import tempfile


REPO = Path(__file__).resolve().parents[1]
TESTS = Path(__file__).resolve().parent
MODULES = "common config lifecycle history menu update"


class IsolatedPVE:
    def __init__(self, installed=False):
        self.temporary = tempfile.TemporaryDirectory(prefix="pve-ups-regression-", dir="/tmp")
        self.root = Path(self.temporary.name)
        self.bin = self.root / "fake-bin"
        self.bin.mkdir()
        for name in ("apt-get", "apt-mark", "apt-cache", "dpkg-query", "systemctl", "apcaccess", "logger", "journalctl", "curl", "shutdown", "poweroff", "reboot", "halt", "killpower"):
            (self.bin / name).symlink_to(TESTS / "fake_system.py")
        self.env = os.environ.copy()
        for name in ("STATE_DIR", "CONFIG_FILE", "DEFAULTS_FILE", "INSTALL_DIR", "COMMAND_PATH", "HISTORY_FILE", "POLICY_FILE", "APT_LOG_DIR", "PYTHONPATH", "PVE_UPS_TEST_FAILPOINT", "PVE_UPS_TEST_ARCHIVE", "PVE_UPS_BOOTSTRAP_SOURCE_ONLY"):
            self.env.pop(name, None)
        self.env.update(PVE_UPS_TESTING="1", PVE_UPS_TEST_ROOT=str(self.root), APP_ROOT=str(REPO), PATH=str(self.bin) + ":" + os.environ["PATH"], LC_ALL="C.UTF-8", PYTHONDONTWRITEBYTECODE="1")
        self.state = self.root / "fake-system.json"
        self.state.write_text(json.dumps({"installed": installed, "active": "inactive", "enabled": "disabled", "automatic": [], "snapshot": {}}))
        self.config = self.root / "etc/apcupsd/apcupsd.conf"
        self.state_dir = self.root / "var/lib/pve-ups"
        self.log = self.root / "var/log/apcupsd.events"

    def close(self):
        self.temporary.cleanup()

    def shell(self, script, timeout=15, check=True):
        preamble = 'set -uo pipefail; for module in ' + MODULES + '; do source "$APP_ROOT/lib/$module.sh" || exit; done; '
        result = subprocess.run(["bash", "-c", preamble + script], env=self.env, text=True, capture_output=True, timeout=timeout)
        if check and result.returncode:
            raise AssertionError(f"命令退出 {result.returncode}：{script}\nstdout={result.stdout}\nstderr={result.stderr}")
        return result

    def cli(self, *args, timeout=15, check=True):
        result = subprocess.run(["bash", str(REPO / "bin/pve-ups"), *args], env=self.env, text=True, capture_output=True, timeout=timeout)
        if check and result.returncode:
            raise AssertionError(f"CLI 退出 {result.returncode}：{args}\nstdout={result.stdout}\nstderr={result.stderr}")
        return result

    def set_fake(self, **values):
        current = json.loads(self.state.read_text())
        current.update(values)
        self.state.write_text(json.dumps(current))

    def fake(self):
        return json.loads(self.state.read_text())

    def calls(self):
        log = self.root / "fake-calls.jsonl"
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    def managed(self):
        self.state_dir.mkdir(parents=True, exist_ok=True)
        (self.state_dir / ".pve-ups-owned").write_text("pve-ups-v1\n")
        manifest = {"app_id": "pve-ups-v1", "config_file": str(self.config), "defaults_file": str(self.root / "etc/default/apcupsd"), "install_directory": str(self.root / "opt/pve-ups"), "command_path": str(self.root / "usr/local/sbin/pve-ups")}
        (self.state_dir / "manifest.tsv").write_text("".join(f"{key}\t{value}\n" for key, value in manifest.items()))

    def write_config(self, values=None):
        self.config.parent.mkdir(parents=True, exist_ok=True)
        values = values or {"TIMEOUT": "900", "BATTERYLEVEL": "30", "MINUTES": "3"}
        self.config.write_text("".join(f"{key} {value}\n" for key, value in values.items()))
        control = self.config.parent / "apccontrol"
        control.write_text('#!/bin/sh\nSCRIPTDIR=/etc/apcupsd\n[ ! -f "$SCRIPTDIR/config" ] || . "$SCRIPTDIR/config"\n')
        control.chmod(0o755)

    def write_log(self, entries, append=False):
        self.log.parent.mkdir(parents=True, exist_ok=True)
        with self.log.open("a" if append else "w") as output:
            for timestamp, message in entries:
                output.write(f"{timestamp}  {message}\n")


def snapshot(state="ONLINE", start="", end="", reason="No transfers since turnon"):
    result = {"STATUS": state, "LASTXFER": reason, "XOFFBATT": end or "N/A"}
    if start:
        result["XONBATT"] = start
    return result
