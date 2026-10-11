#!/usr/bin/env python3
"""隔离测试使用的系统工具替身，所有读写都限制在临时测试根目录。"""

import json
import os
from pathlib import Path
import re
import shutil
import sys


ROOT = Path(os.environ["PVE_UPS_TEST_ROOT"]).resolve()
DB_FILE = ROOT / "fake-system.json"
DB = json.loads(DB_FILE.read_text())
COMMAND = Path(sys.argv[0]).name
ARGS = sys.argv[1:]
with (ROOT / "commands.jsonl").open("a") as stream:
    stream.write(json.dumps({"command": COMMAND, "args": ARGS}) + "\n")


def save():
    DB_FILE.write_text(json.dumps(DB, indent=2))


def package(name):
    return DB["packages"].get(name)


def installed_names():
    return sorted(DB["packages"])


def install_apcupsd():
    for name, deps in {
        "apcupsd": ["libusb-0.1-4", "libwrap0"],
        "libusb-0.1-4": [],
        "libwrap0": [],
    }.items():
        if name not in DB["packages"]:
            DB["packages"][name] = {
                "version": "3.14.14-3" if name == "apcupsd" else "1.0",
                "manual": name == "apcupsd",
                "deps": deps,
            }
    config = ROOT / "etc/apcupsd/apcupsd.conf"
    config.parent.mkdir(parents=True, exist_ok=True)
    if not config.exists():
        config.write_text(
            "UPSCABLE usb\nUPSTYPE usb\nDEVICE\nBATTERYLEVEL 5\n"
            "MINUTES 3\nTIMEOUT 0\nONBATTERYDELAY 6\n"
            "NETSERVER on\nNISIP 127.0.0.1\nNISPORT 3551\n"
            "LOCKFILE /var/lock\nPWRFAILDIR /etc/apcupsd\n"
            "NOLOGINDIR /etc\nKILLDELAY 0\n"
        )
    defaults = ROOT / "etc/default/apcupsd"
    defaults.parent.mkdir(parents=True, exist_ok=True)
    if not defaults.exists():
        defaults.write_text('ISCONFIGURED="no"\n')
    control = config.parent / "apccontrol"
    if not control.exists():
        control.write_text("#!/bin/sh\n# 软件包原生事件处理脚本\nexit 0\n")
        control.chmod(0o755)
    save()


def remove_closure(names):
    result = set(names)
    changed = True
    while changed:
        changed = False
        for name, entry in DB["packages"].items():
            if name not in result and result.intersection(entry.get("deps", [])):
                result.add(name)
                changed = True
    return sorted(result.intersection(DB["packages"]))


def load_runtime_parameters():
    config = ROOT / "etc/apcupsd/apcupsd.conf"
    if not config.exists():
        return
    values = {}
    for line in config.read_text().splitlines():
        words = line.split()
        if len(words) >= 2:
            values[words[0]] = words[1]
    for config_key, status_key in {
        "TIMEOUT": "MAXTIME", "BATTERYLEVEL": "MBATTCHG", "MINUTES": "MINTIMEL",
    }.items():
        if config_key in values:
            DB["ups"] = re.sub(
                rf"(?m)^({status_key}\s*:\s*)\d+(\.\d+)?",
                lambda match: match[1] + values[config_key], DB["ups"],
            )


if COMMAND in {"shutdown", "poweroff", "reboot", "pvesh"}:
    sys.exit("测试禁止执行真实电源操作")

if COMMAND in {"curl", "wget"}:
    if COMMAND != "curl" or not DB.get("bootstrap_archive"):
        sys.exit("测试禁止访问真实网络")
    address = next((arg for arg in ARGS if arg.startswith("https://")), "")
    if address.startswith("https://raw.githubusercontent.com/") and address.endswith("/bootstrap.sh"):
        print(Path(DB["bootstrap_script"]).read_text())
    elif address.startswith("https://api.github.com/repos/") and "/commits/" in address:
        print(DB["bootstrap_commit"])
    elif address.startswith("https://codeload.github.com/") and address.endswith("/" + DB["bootstrap_commit"]):
        if "-o" not in ARGS:
            sys.exit("隔离下载必须指定临时文件")
        destination = Path(ARGS[ARGS.index("-o") + 1]).resolve()
        if not (destination.is_relative_to(ROOT) or str(destination).startswith("/tmp/pve-ups-download.")):
            sys.exit("隔离下载目标不在临时目录中")
        shutil.copyfile(DB["bootstrap_archive"], destination)
    else:
        sys.exit("隔离下载地址不在预期范围内")
    sys.exit(0)

if COMMAND == "pveversion":
    if DB.get("pve_failed"):
        sys.exit(1)
    print(DB.get("pve_version", "pve-manager/9.0.0/abcdef (running kernel: 6.14.0-1-pve)"))
    sys.exit(0)

if COMMAND == "id":
    print("0")
    sys.exit(0)

if COMMAND == "apcaccess":
    if DB.get("ups_failed"):
        sys.exit(1)
    snapshots = DB.get("ups_sequence", [])
    if snapshots:
        current = snapshots.pop(0)
        save()
    else:
        current = DB["ups"]
    print(current)
    sys.exit(0)

if COMMAND == "systemctl":
    action = next((arg for arg in ARGS if not arg.startswith("-")), "")
    if action in {"poweroff", "halt", "reboot", "kexec"}:
        sys.exit("测试禁止执行真实电源操作")
    service = DB["service"]
    if action in {"is-active", "is-enabled", "is-failed", "is-masked"}:
        if action == "is-active":
            value = "active" if service["active"] else "inactive"
            good = service["active"]
        elif action == "is-enabled":
            value = "masked" if service.get("masked") else "enabled" if service["enabled"] else "disabled"
            good = service["enabled"] and not service.get("masked")
        elif action == "is-masked":
            value = "masked" if service.get("masked") else "unmasked"
            good = bool(service.get("masked"))
        else:
            value, good = "inactive", False
        if "--quiet" not in ARGS:
            print(value)
        sys.exit(0 if good else 1)
    if action in {"start", "restart", "try-restart"}:
        if DB.get("restart_failures", 0):
            DB["restart_failures"] -= 1
            save()
            sys.exit(1)
        service["active"] = True
        load_runtime_parameters()
    elif action == "stop":
        service["active"] = False
    elif action == "enable":
        service["enabled"] = True
        if "--now" in ARGS:
            service["active"] = True
    elif action == "disable":
        service["enabled"] = False
        if "--now" in ARGS:
            service["active"] = False
    elif action == "mask":
        service["masked"] = True
    elif action == "unmask":
        service["masked"] = False
    elif action == "show":
        print("enabled" if service["enabled"] else "disabled")
    elif action == "status":
        print("apcupsd.service active" if service["active"] else "apcupsd.service inactive")
    save()
    sys.exit(0)

if COMMAND == "dpkg-query":
    if "-L" in ARGS:
        if not package("apcupsd"):
            sys.exit(1)
        print("/etc/apcupsd/apcupsd.conf\n/etc/apcupsd/apccontrol\n/lib/systemd/system-shutdown/apcupsd_shutdown")
        sys.exit(0)
    formatting = next((arg[3:] for arg in ARGS if arg.startswith("-f=")), "")
    if not formatting:
        formatting = next((arg[2:] for arg in ARGS if arg.startswith("-f") and len(arg) > 2), "")
    names = [arg for arg in ARGS if not arg.startswith("-")]
    if names:
        targets = [name for name in names if package(name)]
        if len(targets) != len(names):
            sys.exit(1)
    else:
        targets = installed_names()
    for name in targets:
        entry = package(name)
        fields = {
            "binary:Package": name,
            "Package": name,
            "Status": "install ok installed",
            "db:Status-Abbrev": "ii ",
            "Version": entry["version"],
        }
        value = formatting or "${binary:Package}\t${Version}\n"
        for key, replacement in fields.items():
            value = value.replace("${" + key + "}", replacement)
        value = value.replace("\\t", "\t").replace("\\n", "\n")
        sys.stdout.write(value)
    sys.exit(0)

if COMMAND == "apt-mark":
    action = ARGS[0]
    if action in {"showauto", "showmanual"}:
        for name in installed_names():
            if package(name)["manual"] == (action == "showmanual"):
                print(name)
    elif action in {"manual", "auto"}:
        for name in ARGS[1:]:
            if package(name):
                package(name)["manual"] = action == "manual"
        save()
    else:
        sys.exit(2)
    sys.exit(0)

if COMMAND in {"apt-get", "apt"}:
    action = next((arg for arg in ARGS if arg in {"update", "install", "purge", "remove", "autoremove"}), "")
    if action == "update":
        if DB.get("ups_after_update"):
            DB["ups"] = DB["ups_after_update"]
            save()
        sys.exit(1 if DB.get("network_failed") else 0)
    if action == "autoremove":
        sys.exit("项目禁止全局自动清理")
    if action == "install":
        if DB.get("install_failed") or DB.get("network_failed"):
            sys.exit(1)
        install_apcupsd()
        if "--only-upgrade" in ARGS:
            # Debian 的升级前脚本直接停止旧服务，不受 policy-rc.d 的拦截控制。
            DB["service"]["active"] = False
            package("apcupsd")["version"] = "3.14.14-4"
            save()
        sys.exit(0)
    if action in {"purge", "remove"}:
        targets = [arg for arg in ARGS[ARGS.index(action) + 1 :] if not arg.startswith("-")]
        removals = remove_closure(targets)
        if "-s" in ARGS or "--simulate" in ARGS:
            for name in removals:
                print(f"Purg {name} [{package(name)['version']}]")
            sys.exit(0)
        for name in removals:
            DB["packages"].pop(name, None)
        if "apcupsd" in removals:
            DB["service"]["active"] = False
            # Debian 的 postrm purge 会清除整个目录，包括后来添加的用户文件。
            shutil.rmtree(ROOT / "etc/apcupsd", ignore_errors=True)
            (ROOT / "etc/default/apcupsd").unlink(missing_ok=True)
        save()
        sys.exit(0)
    sys.exit(2)

if COMMAND == "apt-cache":
    if ARGS[0] == "policy":
        print("apcupsd:\n  Installed: " + (package("apcupsd") or {}).get("version", "(none)") + "\n  Candidate: 3.14.14-4")
    elif ARGS[0] in {"rdepends", "depends"}:
        print(ARGS[-1])
        if ARGS[0] == "rdepends":
            print("Reverse Depends:")
            for name, entry in DB["packages"].items():
                if ARGS[-1] in entry.get("deps", []):
                    print("  " + name)
        else:
            for name in (package(ARGS[-1]) or {}).get("deps", []):
                print("  Depends: " + name)
    sys.exit(0)

sys.exit(f"测试替身尚未支持：{COMMAND} {ARGS}")
