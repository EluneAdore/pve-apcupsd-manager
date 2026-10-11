#!/usr/bin/env python3
"""隔离测试中的系统命令替身；不调用系统服务、软件包管理器或电源命令。"""

import json
import os
from pathlib import Path
import sys


def main():
    root = Path(os.environ["PVE_UPS_TEST_ROOT"])
    if os.environ.get("PVE_UPS_TESTING") != "1" or not str(root).startswith("/tmp/"):
        raise SystemExit("拒绝在隔离目录外运行系统替身")
    command = Path(sys.argv[0]).name
    args = sys.argv[1:]
    state_path = root / "fake-system.json"
    state = json.loads(state_path.read_text())
    with (root / "fake-calls.jsonl").open("a") as output:
        output.write(json.dumps([command, *args]) + "\n")
    if command in {"shutdown", "poweroff", "reboot", "halt", "killpower"}:
        raise SystemExit("测试禁止真实电源操作")
    if command == "systemctl":
        operation = args[0]
        if any(a not in {"--quiet", "--runtime", "apcupsd.service"} for a in args[1:]):
            raise SystemExit("只允许模拟 apcupsd.service")
        if operation == "is-active":
            if "--quiet" not in args:
                print(state["active"])
            return 0 if state["active"] == "active" else 3
        if operation == "is-enabled":
            print(state["enabled"])
            return 0 if state["enabled"] == "enabled" else 1
        if state.get("fail_systemctl") == operation:
            return 1
        if operation in {"start", "restart"}:
            state["active"] = "active"
        elif operation == "stop":
            state["active"] = "inactive"
        elif operation in {"enable", "disable", "mask"}:
            state["enabled"] = {"enable": "enabled", "disable": "disabled", "mask": "masked"}[operation]
        elif operation == "unmask" and state["enabled"] == "masked":
            state["enabled"] = "disabled"
        elif operation not in {"daemon-reload", "unmask"}:
            raise SystemExit("未实现的模拟 systemctl 操作：" + operation)
    elif command == "apcaccess":
        if state.get("query_fail"):
            return 1
        config = root / "etc/apcupsd/apcupsd.conf"
        values = {}
        if config.exists():
            for line in config.read_text().splitlines():
                if line.strip() and not line.lstrip().startswith("#"):
                    parts = line.split(maxsplit=1)
                    values[parts[0]] = parts[1] if len(parts) > 1 else ""
        defaults = {"MODEL": "Back-UPS BK650M2-CH", "STATUS": "ONLINE", "BCHARGE": "100 Percent", "TIMELEFT": "60.3 Minutes", "LOADPCT": "7 Percent", "TONBATT": "0 Seconds", "LASTXFER": "No transfers since turnon", "XOFFBATT": "N/A"}
        defaults.update(state.get("snapshot", {}))
        defaults.update({"MAXTIME": values.get("TIMEOUT", "1800") + " Seconds", "MBATTCHG": values.get("BATTERYLEVEL", "50") + " Percent", "MINTIMEL": values.get("MINUTES", "30") + " Minutes"})
        for key, value in defaults.items():
            print(f"{key:<9}: {value}")
        return 0
    elif command == "dpkg-query":
        installed = state["installed"]
        if "-L" in args:
            if not installed:
                return 1
            print("/etc/apcupsd/apcupsd.conf\n/etc/apcupsd/apccontrol\n/lib/systemd/system-shutdown/apcupsd_shutdown")
        elif "apcupsd" in args:
            if not installed:
                return 1
            if any("Status-Abbrev" in a for a in args):
                print("ii 3.14.14-5")
            else:
                print("apcupsd\tinstall ok installed\t3.14.14-5")
        elif installed:
            print("apcupsd\tinstall ok installed\t3.14.14-5")
        return 0
    elif command == "apt-mark":
        if args[0] == "showauto":
            print("\n".join(state.get("automatic", [])))
        elif args[0] in {"auto", "manual"}:
            automatic = set(state.get("automatic", []))
            for package in args[1:]:
                if args[0] == "auto":
                    automatic.add(package)
                else:
                    automatic.discard(package)
            state["automatic"] = sorted(automatic)
        else:
            return 1
    elif command == "apt-get":
        if state.get("fail_apt"):
            return 1
        if "-s" in args:
            if "purge" in args:
                print("Purg apcupsd [3.14.14-5]")
            return 0
        if "install" in args:
            state["installed"] = True
            directory = root / "etc/apcupsd"
            directory.mkdir(parents=True, exist_ok=True)
            config = directory / "apcupsd.conf"
            if not config.exists():
                config.write_text("UPSCABLE usb\nUPSTYPE usb\nDEVICE\nTIMEOUT 0\nBATTERYLEVEL 5\nMINUTES 3\n")
            control = directory / "apccontrol"
            if not control.exists():
                control.write_text('#!/bin/sh\nSCRIPTDIR=/etc/apcupsd\n[ ! -f "$SCRIPTDIR/config" ] || . "$SCRIPTDIR/config"\n')
                control.chmod(0o755)
        elif "purge" in args:
            state["installed"] = False
            directory = root / "etc/apcupsd"
            if directory.exists():
                import shutil
                shutil.rmtree(directory)
        elif "update" not in args:
            raise SystemExit("未实现的模拟 apt-get 操作")
    elif command == "logger":
        return 0
    elif command == "journalctl":
        print("隔离环境：无系统日志")
        return 0
    elif command == "apt-cache":
        print("apcupsd:\n  Installed: 3.14.14-5\n  Candidate: 3.14.14-5")
        return 0
    else:
        raise SystemExit("未实现的模拟命令：" + command)
    state_path.write_text(json.dumps(state))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
