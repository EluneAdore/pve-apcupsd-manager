#!/usr/bin/env python3
"""主菜单的只读硬件概览；不探测磁盘、不更改系统配置、不保存历史。"""

import argparse
import math
import os
from pathlib import Path
import re
import signal
import sys
import time
import unicodedata

PROC = Path("/proc")
SYS = Path("/sys")

class ReadTimeout(Exception):
    """单个内核接口读取超时。"""

class Reader:
    """每次读取及整个采集均有界，慢传感器只使自身不可用。"""

    def __init__(self, deadline):
        self.deadline = deadline

    def read(self, path, limit=65536):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            return None
        fd = None
        previous = signal.getsignal(signal.SIGALRM)

        def expired(_signum, _frame):
            raise ReadTimeout()

        try:
            signal.signal(signal.SIGALRM, expired)
            signal.setitimer(signal.ITIMER_REAL, min(0.12, remaining))
            fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK)
            chunks = []
            size = 0
            while size <= limit:
                chunk = os.read(fd, min(65536, limit + 1 - size))
                if not chunk:
                    return b"".join(chunks).decode("utf-8", "replace").strip()
                chunks.append(chunk)
                size += len(chunk)
            return None
        except (OSError, ValueError, ReadTimeout):
            return None
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous)
            if fd is not None:
                os.close(fd)

def safe_text(value, limit=240):
    return "".join(c for c in str(value) if c.isprintable())[:limit]

def integer(value):
    if value is None or not re.fullmatch(r"-?\d+", value.strip()):
        return None
    try:
        parsed = int(value)
        return parsed if -(1 << 63) <= parsed < 1 << 64 else None
    except ValueError:
        return None

def parse_cpu_stat(text):
    if not text:
        return None
    for line in text.splitlines():
        fields = line.split()
        if fields and fields[0] == "cpu" and len(fields) >= 5:
            # guest/guest_nice 已包含在 user/nice 中，不能再次累加。
            values = [integer(v) for v in fields[1:9]]
            if any(v is None or v < 0 for v in values):
                return None
            return values + [0] * (8 - len(values))
    return None

def cpu_usage(first, second):
    if first is None or second is None or len(first) != 8 or len(second) != 8:
        return "不可用"
    delta = [b - a for a, b in zip(first, second)]
    total = sum(delta)
    if total <= 0 or any(v < 0 for v in delta):
        return "不可用（计数器重置或异常）"
    busy = total - delta[3] - delta[4]
    return f"{busy * 100 / total:.1f} %"

def device_assigned(path):
    """不读取已由 vfio 接管设备的传感器。"""
    try:
        resolved = path.resolve()
        for ancestor in (resolved, *resolved.parents):
            driver = ancestor / "driver"
            if driver.is_symlink() and driver.resolve().name.startswith("vfio"):
                return True
    except (OSError, RuntimeError):
        return True
    return False

def discover_rapl(reader):
    root = SYS / "class/powercap"
    pending = [(root, 0)]
    visited = set()
    domains = []
    # class 下可同时出现区域链接与嵌套区域；按真实路径去重。
    while pending and time.monotonic() < reader.deadline:
        path, depth = pending.pop()
        try:
            identity = str(path.resolve())
            if identity in visited:
                continue
            visited.add(identity)
            if depth > 8 or not path.is_dir():
                continue
            name = reader.read(path / "name", 256)
            if name and re.fullmatch(r"package(?:-\d+)?", name.lower()):
                domains.append({"path": path, "identity": identity, "name": name})
            for child in sorted(path.iterdir(), reverse=True):
                if child.is_dir():
                    pending.append((child, depth + 1))
        except (OSError, RuntimeError):
            continue
    domains.sort(key=lambda domain: domain["identity"])
    for domain in domains:
        ancestors = [parent for parent in domains
                     if domain["identity"].startswith(parent["identity"] + "/")
                     and parent["name"].lower().startswith("package")]
        parent = max(ancestors, key=lambda item: len(item["identity"]), default=None)
        domain["topology"] = (parent["name"] + " / " if parent else "") + domain["name"]
    return domains

def sample_rapl(domains, reader):
    result = {}
    for domain in domains:
        path = domain["path"]
        before = time.monotonic()
        energy = integer(reader.read(path / "energy_uj"))
        after = time.monotonic()
        maximum = integer(reader.read(path / "max_energy_range_uj"))
        result[domain["identity"]] = {
            "energy": energy, "range": maximum, "time": (before + after) / 2,
            "identity": domain["identity"],
        }
    return result

def rapl_power(first, second):
    if not first or not second:
        return "不可用"
    if first.get("identity") != second.get("identity"):
        return "不可用（功耗域已变化）"
    old, new = first.get("energy"), second.get("energy")
    maximum = first.get("range")
    if (not isinstance(old, int) or not isinstance(new, int)
            or not isinstance(maximum, int) or maximum <= 0
            or maximum != second.get("range")
            or not 0 <= old <= maximum or not 0 <= new <= maximum):
        return "不可用（计数器或范围未提供有效值）"
    elapsed = second.get("time", 0) - first.get("time", 0)
    if not math.isfinite(elapsed) or elapsed <= 0 or elapsed > 5:
        return "不可用（采样间隔异常）"
    delta = new - old
    wrapped = delta < 0
    if wrapped:
        # 两个读数无法完全区分回绕和复位；只接受边界附近的单次回绕。
        if old < maximum * 0.9 or new > maximum * 0.1:
            return "不可用（计数器复位或无法确认回绕）"
        delta += maximum
    if delta > maximum / 2:
        return "不可用（能量增量异常或可能跨越多个周期）"
    watts = delta / elapsed / 1_000_000
    if not math.isfinite(watts) or watts < 0:
        return "不可用（功耗结果异常）"
    suffix = "；按单次回绕计算" if wrapped else ""
    return f"{watts:.2f} W（{elapsed:.2f} s 平均值{suffix}）"


def entries(path):
    try:
        return sorted(path.iterdir(), key=lambda item: item.name)
    except OSError:
        return None


def resolved(path):
    try:
        return path.resolve(strict=True)
    except (OSError, RuntimeError):
        return None


def virtual_device(path):
    physical = resolved(path)
    if physical is None or SYS / "devices/virtual" in physical.parents:
        return True
    for ancestor in (physical, *physical.parents):
        driver = resolved(ancestor / "driver")
        if driver and driver.name.startswith(("vfio", "virtio", "xen", "scsi_debug")):
            return True
    return False


def usb_bridge(path):
    for ancestor in (path, *path.parents):
        driver = resolved(ancestor / "driver")
        if driver and driver.name in ("usb-storage", "uas"):
            return True
    return False


def hwmon_devices(reader):
    devices = []
    for sensor in entries(SYS / "class/hwmon") or []:
        owner = resolved(sensor / "device")
        real = resolved(sensor)
        if owner is None and real:
            owner = real.parent.parent if real.parent.name == "hwmon" else real.parent
        if owner is None or device_assigned(owner):
            continue
        name = reader.read(sensor / "name", 256)
        if name:
            devices.append((sensor, owner, name.lower()))
    return devices


def temperature(reader, path):
    value = integer(reader.read(path))
    if value is None or not -273150 <= value <= 200000:
        return None
    return value / 1000


def cpu_temperature(reader, sensors):
    physical, control = [], []
    for sensor, _, driver in sensors:
        if driver not in ("k10temp", "coretemp", "zenpower"):
            continue
        labelled = []
        for path in sensor.glob("temp[0-9]*_input"):
            label = reader.read(path.with_name(path.name.replace("_input", "_label")), 256)
            if label:
                labelled.append((path, label))
        die = [path for path, label in labelled if label.lower() == "tdie"]
        package = [path for path, label in labelled if re.fullmatch(r"Package id \d+", label)]
        tctl = [path for path, label in labelled if label.lower() == "tctl"]
        # k10temp 的无标签 temp1_input 是驱动文档定义的 Tctl，明确标识控制温度。
        if not labelled and driver == "k10temp" and (sensor / "temp1_input").exists():
            tctl = [sensor / "temp1_input"]
        selected = die or package or tctl
        target = physical if die or package else control
        for path in selected:
            value = temperature(reader, path)
            if value is not None:
                target.append(value)
    values = physical or control
    if not values:
        return "不可用"
    suffix = "（Tctl）" if not physical else "（最高）" if len(values) > 1 else ""
    return f"{max(values):.1f} °C{suffix}"


def physical_disks(reader):
    blocks = entries(SYS / "block")
    if blocks is None:
        return None
    controllers = [real for path in entries(SYS / "class/nvme") or []
                   if (real := resolved(path)) is not None and not virtual_device(real)]
    disks, owner_keys = {}, {}
    for block in blocks:
        if (block / "partition").exists() or virtual_device(block):
            continue
        owner = resolved(block / "device")
        real = resolved(block)
        if owner is None or real is None or virtual_device(owner):
            continue
        kind = reader.read(owner / "type", 16)
        if kind is not None and kind not in ("0", "14", "20"):
            continue
        nvme = next((controller for controller in controllers
                     if controller == owner or controller in owner.parents
                     or controller in real.parents), None)
        original_owner = owner
        owner = nvme or owner
        model = safe_text(reader.read(owner / "model", 4096) or "", 240).strip()
        rotational = integer(reader.read(block / "queue/rotational", 16))
        wwid = reader.read(block / "wwid", 512) or reader.read(owner / "wwid", 512)
        # 仅使用内核明确的全局标识合并多路径；型号与节点序号不能作为身份。
        reliable_id = wwid and re.fullmatch(r"(?:naa\.|eui\.|uuid\.|t10\.)[A-Za-z0-9._:-]+", wwid)
        if reliable_id and not re.search(r"[1-9a-fA-F]", wwid.split(".", 1)[1]):
            reliable_id = False
        suggested = ("nvme", str(owner)) if nvme else ("wwid", wwid, model) if reliable_id else ("device", str(owner))
        key = owner_keys.setdefault(owner, suggested)
        group = disks.setdefault(key, {"model": model, "nodes": [], "owners": set(),
                                       "rotational": [], "nvme": bool(nvme), "blocks": [],
                                       "usb_bridge": False})
        group["usb_bridge"] = group["usb_bridge"] or usb_bridge(owner)
        if not group["model"]:
            group["model"] = model
        try:
            inode = block.stat().st_ino
        except OSError:
            continue
        group["nodes"].append(block.name)
        group["owners"].add(owner)
        group["rotational"].append(rotational)
        group["blocks"].append((block, real, inode, original_owner))
    return sorted((disk for disk in disks.values() if disk["nodes"]),
                  key=lambda disk: (disk["model"].casefold(), sorted(disk["nodes"])))


def disk_alive(disk):
    live = []
    for block, real, inode, owner in disk["blocks"]:
        try:
            if (block.stat().st_ino == inode and resolved(block) == real
                    and resolved(block / "device") == owner and not virtual_device(block)):
                live.append(block.name)
        except OSError:
            continue
    return sorted(set(live))


def disk_temperature(reader, disk, sensors):
    # 旋转盘及类型未知的盘不读取温度：已有 drivetemp 也可能改变 HDD 休眠计时。
    if (disk["usb_bridge"] or not disk["rotational"]
            or any(value != 0 for value in disk["rotational"] )):
        return None
    if not disk_alive(disk) or any(device_assigned(owner) for owner in disk["owners"]):
        return None
    candidates = set()
    for sensor, owner, driver in sensors:
        if owner not in disk["owners"]:
            continue
        if driver == "nvme" and disk["nvme"]:
            for path in sensor.glob("temp[0-9]*_input"):
                label = reader.read(path.with_name(path.name.replace("_input", "_label")), 256)
                if label and label.casefold() == "composite":
                    real = resolved(path)
                    if real:
                        candidates.add(real)
        elif driver == "drivetemp":
            # drivetemp 文档定义的 temp1_input 属于该实体盘，不读取其他通道。
            real = resolved(sensor / "temp1_input")
            if real:
                candidates.add(real)
        else:
            # 其他存储 HWMON 必须精确归属该盘并明确标注整体磁盘温度。
            for path in sensor.glob("temp[0-9]*_input"):
                label = reader.read(path.with_name(path.name.replace("_input", "_label")), 256)
                if label and label.casefold() in ("drive temperature", "disk temperature"):
                    real = resolved(path)
                    if real:
                        candidates.add(real)
    # 多个独立主通道无法唯一选择时不猜测，重复的 sysfs 链接已由真实路径去重。
    if len(candidates) != 1:
        return None
    value = temperature(reader, next(iter(candidates)))
    return value if disk_alive(disk) else None


def text_width(text):
    return sum(0 if unicodedata.combining(c) else 2 if unicodedata.east_asian_width(c) in "WF" else 1
               for c in text)


def clipped(text, width):
    if text_width(text) <= width:
        return text
    result = ""
    for char in text:
        if text_width(result + char + "…") > width:
            break
        result += char
    return result + "…"


def disk_lines(disks, sensors, reader, width):
    if disks is None:
        return ["磁盘温度：不可用"]
    values = []
    for disk in disks:
        value = disk_temperature(reader, disk, sensors)
        nodes = disk_alive(disk)
        if not nodes:
            continue
        suffix = "/".join(nodes)
        reading = f"{value:.1f} °C" if value is not None else "不可用"
        if disk["model"]:
            limit = max(4, min(32, width - text_width(suffix + reading) - 6))
            label = f"{clipped(disk['model'], limit)} ({suffix})"
        else:
            label = suffix
        values.append(f"{label}：{reading}")
    if not values:
        return ["磁盘温度：无可见实体磁盘"]
    lines = []
    row = "磁盘温度："
    for value in values:
        proposed = row + ("" if row == "磁盘温度：" else "    ") + value
        if row and text_width(proposed) > width:
            lines.append(row)
            row = value
        else:
            row = proposed
    lines.append(row)
    return lines


def memory_line(text):
    values = {}
    for line in (text or "").splitlines():
        key, _, value = line.partition(":")
        fields = value.split()
        if len(fields) == 2 and fields[1] == "kB":
            values[key] = integer(fields[0])
    total, available = values.get("MemTotal"), values.get("MemAvailable")
    if total is None or available is None or total <= 0 or not 0 <= available <= total:
        return "内存：不可用"
    used = total - available
    unit, divisor = ("GiB", 1048576) if total >= 1048576 else ("MiB", 1024)
    return f"内存：{used / divisor:.2f} / {total / divisor:.2f} {unit}（{used / total * 100:.1f} %）"


def package_line(domains, first, second):
    if not domains or len({d["name"] for d in domains}) != len(domains):
        return "CPU Package 功耗：不可用"
    readings = []
    for domain in domains:
        value = rapl_power(first.get(domain["identity"]), second.get(domain["identity"]))
        match = re.match(r"([0-9]+(?:\.[0-9]+)?) W", value)
        power = f"{float(match.group(1)):.1f} W" if match else "不可用"
        readings.append(f"{domain['name']} {power}" if len(domains) > 1 else power)
    return "CPU Package 功耗：" + "；".join(readings)


def overview(width=80, sample=True):
    reader = Reader(time.monotonic() + (1.8 if sample else 0.3))
    discovery = Reader(min(reader.deadline, time.monotonic() + 0.15))
    domains = discover_rapl(discovery) if sample else []
    if time.monotonic() >= discovery.deadline:
        domains = []
    started = time.monotonic()
    first_cpu = parse_cpu_stat(reader.read(PROC / "stat")) if sample else None
    first_power = sample_rapl(domains, reader) if sample else {}
    if sample:
        time.sleep(max(0, min(1 - (time.monotonic() - started), reader.deadline - time.monotonic())))
    second_cpu = parse_cpu_stat(reader.read(PROC / "stat")) if sample else None
    second_power = sample_rapl(domains, reader) if sample else {}
    usage = cpu_usage(first_cpu, second_cpu)
    if usage.startswith("不可用"):
        usage = "不可用"
    sensors = hwmon_devices(reader)
    disks = physical_disks(reader)
    cpu = f"CPU 使用率：{usage}    温度：{cpu_temperature(reader, sensors)}"
    power = package_line(domains, first_power, second_power)
    # 宽度允许时共享一行；多个 Package 或窄终端保留完整内容并分行。
    cpu_lines = [cpu + "    " + power] if text_width(cpu + "    " + power) <= width else [cpu, power]
    lines = ["──────── PVE 硬件状态 ────────", *cpu_lines,
             memory_line(reader.read(PROC / "meminfo"))]
    return lines + disk_lines(disks, sensors, reader, width)


def main(argv=None):
    parser = argparse.ArgumentParser(description="只读精简硬件概览")
    parser.add_argument("--width", type=int, default=80)
    parser.add_argument("--no-sample", action="store_true")
    parser.add_argument("--frame-rows", type=int, help="计算菜单文本的终端占用行数")
    args = parser.parse_args(argv)
    if args.frame_rows is not None:
        width = max(1, args.frame_rows)
        text = sys.stdin.read()
        print(sum(max(1, (text_width(line) + width - 1) // width) for line in text.splitlines()))
        return 0
    print("\n".join(overview(max(12, args.width), not args.no_sample)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
