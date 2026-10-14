#!/usr/bin/env python3
"""按需读取宿主机硬件状态；不执行设备控制命令，不写入采集数据。"""

import math
import os
from pathlib import Path
import re
import signal
import time

from hardware_io import render_io, sample_io

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


def cpu_information(text):
    models = []
    processors = []
    frequencies = []
    for line in (text or "").splitlines():
        key, sep, value = line.partition(":")
        if not sep:
            continue
        key, value = key.strip(), value.strip()
        if key in ("model name", "Hardware") and value and value not in models:
            models.append(value)
        elif key == "processor" and value.isdigit():
            processors.append(value)
        elif key == "cpu MHz":
            try:
                frequency = float(value)
                if math.isfinite(frequency) and 0 < frequency <= 100000:
                    frequencies.append(frequency)
            except ValueError:
                pass
    return models, len(set(processors)), frequencies


def frequency_line(reader, cpuinfo):
    frequencies = []
    try:
        cpus = sorted((SYS / "devices/system/cpu").glob("cpu[0-9]*"))
    except OSError:
        cpus = []
    for cpu in cpus:
        if not re.fullmatch(r"cpu\d+", cpu.name):
            continue
        if reader.read(cpu / "online", 16) == "0":
            continue
        value = integer(reader.read(cpu / "cpufreq/scaling_cur_freq"))
        if value is not None and 0 < value <= 100000000:
            frequencies.append(value / 1000)
    source = "scaling_cur_freq"
    if not frequencies:
        frequencies = cpu_information(cpuinfo)[2]
        source = "/proc/cpuinfo cpu MHz"
    if not frequencies:
        return "当前频率：不可用"
    average = sum(frequencies) / len(frequencies)
    return (f"当前频率：{average:.0f} MHz（{len(frequencies)} 个可读逻辑 CPU 的平均值；"
            f"{source}）")


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
    while pending and len(visited) < 256:
        path, depth = pending.pop()
        try:
            identity = str(path.resolve())
            if identity in visited:
                continue
            visited.add(identity)
            if depth > 8 or not path.is_dir():
                continue
            name = reader.read(path / "name", 256)
            if name and (re.fullmatch(r"package(?:-\d+)?", name.lower())
                         or name.lower() in ("core", "cores")):
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


def rapl_lines(domains, first, second):
    lines = []
    kinds = set()
    for domain in domains:
        kind = "Package" if domain["name"].lower().startswith("package") else "Core"
        kinds.add(kind)
        identity = domain["identity"]
        label = safe_text(domain["topology"])
        lines.append(f"{kind} 功耗 [{label}]：{rapl_power(first.get(identity), second.get(identity))}")
        lines.append(f"  来源：{safe_text(identity, 600)}/energy_uj")
    for kind in ("Package", "Core"):
        if kind not in kinds:
            lines.append(f"{kind} 功耗：不可用（未发现对应 powercap 功耗域）")
    lines.append("Package / Core 为各自功耗域，不相加，也不代表宿主机整机功耗。")
    return lines


def memory_lines(text):
    values = {}
    for line in (text or "").splitlines():
        key, _, value = line.partition(":")
        fields = value.split()
        if fields and len(fields) >= 2 and fields[1] == "kB":
            values[key] = integer(fields[0])

    def capacity(value):
        return f"{value / 1024 / 1024:.2f} GiB" if value >= 1024 * 1024 else f"{value / 1024:.1f} MiB"

    total, available = values.get("MemTotal"), values.get("MemAvailable")
    lines = []
    if total is None or total <= 0:
        lines.append("内存：不可用")
    elif available is None or not 0 <= available <= total:
        lines.extend((f"内存总量：{capacity(total)}", "内存已使用 / 可用 / 使用率：不可用（MemAvailable 未提供有效值）"))
    else:
        used = total - available
        lines.extend((f"内存总量：{capacity(total)}", f"已使用：{capacity(used)}    可用：{capacity(available)}",
                      f"内存使用率：{used * 100 / total:.1f} %（按 MemAvailable 计算）"))
    swap, free = values.get("SwapTotal"), values.get("SwapFree")
    if swap is None or free is None or swap < 0 or not 0 <= free <= swap:
        lines.append("Swap：不可用")
    else:
        lines.append(f"Swap：总量 {capacity(swap)}    已使用 {capacity(swap - free)}")
    return lines


def temperature_value(text):
    raw = integer(text)
    if raw is None:
        return None
    value = raw / 1000
    if not -273.15 <= value <= 1000:
        return "异常原始值 " + safe_text(text)
    return f"{value:.1f} °C"


def read_temperature(reader):
    lines = []
    seen = set()
    try:
        sensors = sorted((SYS / "class/hwmon").glob("hwmon*"))
    except OSError:
        sensors = []
    for sensor in sensors[:128]:
        try:
            identity = str((sensor / "device").resolve()) if (sensor / "device").exists() else str(sensor.resolve())
            if device_assigned(Path(identity)):
                continue
            name = reader.read(sensor / "name", 256) or sensor.name
            inputs = sorted(sensor.glob("temp[0-9]*_input"))
        except (OSError, RuntimeError):
            continue
        for path in inputs[:64]:
            stem = path.name.removesuffix("_input")
            if not re.fullmatch(r"temp\d+", stem):
                continue
            try:
                sensor_identity = (str(path.resolve()), identity)
            except (OSError, RuntimeError):
                continue
            if sensor_identity in seen:
                continue
            seen.add(sensor_identity)
            label = reader.read(sensor / (stem + "_label"), 256) or stem
            value = temperature_value(reader.read(path)) or "不可用"
            extra = []
            for field, title in (("max", "max"), ("crit", "crit")):
                threshold = temperature_value(reader.read(sensor / (stem + "_" + field)))
                if threshold is not None:
                    extra.append(f"{title} {threshold}")
            alarm = reader.read(sensor / (stem + "_alarm"))
            if alarm is not None:
                number = integer(alarm)
                state = "已触发" if number == 1 else "未触发" if number == 0 else "异常原始值 " + safe_text(alarm)
                extra.append(f"设备告警 {state}")
            details = "    " + "；".join(extra) if extra else ""
            lines.append(f"{safe_text(name)} / {safe_text(label)}：{value}{details}")
            lines.append(f"  设备：{safe_text(identity, 600)}；通道 {stem}")
    if not lines:
        lines.append("硬件温度：不可用（未发现可读取的 hwmon 温度通道）")
    return lines


def pve_version(reader):
    # 从已安装包的本地元数据读取版本，不访问 PVE API 或执行包管理操作。
    status = reader.read(Path("/var/lib/dpkg/status"), 16 * 1024 * 1024)
    if status:
        for paragraph in status.split("\n\n"):
            fields = dict(line.split(": ", 1) for line in paragraph.splitlines() if ": " in line and not line.startswith(" "))
            if fields.get("Package") == "pve-manager" and fields.get("Status") == "install ok installed":
                return safe_text(fields.get("Version", "不可用"))
    return "不可用（本地未找到已安装的 pve-manager 元数据）"


def system_lines(reader):
    lines = ["PVE 版本：" + pve_version(reader),
             "Linux 内核：" + safe_text(reader.read(PROC / "sys/kernel/osrelease") or "不可用")]
    uptime = reader.read(PROC / "uptime")
    try:
        seconds = float(uptime.split()[0])
        if not math.isfinite(seconds) or seconds < 0:
            raise ValueError()
        total = int(seconds)
        days, rem = divmod(total, 86400)
        hours, rem = divmod(rem, 3600)
        minutes, seconds = divmod(rem, 60)
        lines.append(f"运行时间：{days} 天 {hours} 小时 {minutes} 分 {seconds} s")
    except (AttributeError, IndexError, ValueError):
        lines.append("运行时间：不可用")
    load = reader.read(PROC / "loadavg")
    try:
        values = [float(value) for value in load.split()[:3]]
        if len(values) != 3 or any(not math.isfinite(value) or value < 0 for value in values):
            raise ValueError()
        lines.append("平均负载（1 / 5 / 15 min）：" + " / ".join(f"{value:.2f}" for value in values))
    except (AttributeError, ValueError):
        lines.append("平均负载（1 / 5 / 15 min）：不可用")
    return lines


def main():
    reader = Reader(time.monotonic() + 4)
    cpuinfo = reader.read(PROC / "cpuinfo", 4 * 1024 * 1024)
    models, threads, _ = cpu_information(cpuinfo)
    domains = discover_rapl(reader)
    started = time.monotonic()
    first_cpu = parse_cpu_stat(reader.read(PROC / "stat"))
    first_power = sample_rapl(domains, reader)
    first_io = sample_io(reader.read)
    first_time = time.monotonic()
    time.sleep(max(0, 1 - (time.monotonic() - started)))
    second_cpu = parse_cpu_stat(reader.read(PROC / "stat"))
    second_power = sample_rapl(domains, reader)
    second_io = sample_io(reader.read)
    elapsed = time.monotonic() - first_time
    lines = ["──────── CPU ────────", "型号：" + (" / ".join(safe_text(model) for model in models) or "不可用"),
             "逻辑处理器：" + (str(threads) if threads else "不可用"),
             "CPU 总使用率：" + cpu_usage(first_cpu, second_cpu), frequency_line(reader, cpuinfo)]
    lines.extend(rapl_lines(domains, first_power, second_power))
    lines.extend(("", "──────── 内存 ────────"))
    lines.extend(memory_lines(reader.read(PROC / "meminfo")))
    lines.extend(("", "──────── 硬件温度 ────────"))
    lines.extend(read_temperature(reader))
    lines.extend(("", *render_io(first_io, second_io, elapsed)))
    lines.extend(("", "──────── 系统 ────────"))
    lines.extend(system_lines(reader))
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
