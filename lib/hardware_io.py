#!/usr/bin/env python3
"""按需读取网络、块设备与挂载元数据；不打开磁盘或探测休眠状态。"""

import math
import os
import re
import time
from pathlib import Path

PROC_NET_DEV = Path('/proc/net/dev')
PROC_DISKSTATS = Path('/proc/diskstats')
PROC_MOUNTS = Path('/proc/mounts')
SYS_NET = Path('/sys/class/net')
SYS_BLOCK = Path('/sys/block')
_MAX_ITEMS = 256
_MAX_TEXT = 1048576
_LOCAL_FS = {
    'ext2', 'ext3', 'ext4', 'xfs', 'btrfs', 'zfs', 'f2fs', 'vfat',
    'exfat', 'ntfs', 'ntfs3', 'fuseblk', 'bcachefs', 'jfs', 'reiserfs',
    'overlay',
}
_MEMORY_FS = {'tmpfs', 'ramfs', 'hugetlbfs'}


def _read_text(path):
    """限制单个元数据文件大小，缺失和权限错误单独降级。"""
    try:
        with Path(path).open('r', encoding='utf-8', errors='replace') as stream:
            return stream.read(_MAX_TEXT).strip()
    except (OSError, ValueError):
        return None


def _clean(value):
    return ''.join(char for char in str(value or '') if char.isprintable())


def _number(value):
    try:
        result = int(value)
        return result if 0 <= result <= 18446744073709551615 else None
    except (ValueError, TypeError):
        return None


def _path_identity(path):
    try:
        return str(path.resolve())
    except (OSError, RuntimeError):
        return str(path)


def _passthrough(path):
    """只检查驱动链接；不读取已经交给 vfio 的设备接口。"""
    try:
        resolved = path.resolve()
        for parent in (resolved, *resolved.parents):
            if parent == Path('/'):
                break
            driver = parent / 'driver'
            if driver.is_symlink() and driver.resolve().name.startswith('vfio'):
                return True
    except (OSError, RuntimeError):
        return True
    return False


def _entries(path):
    try:
        entries = sorted(path.iterdir(), key=lambda item: item.name)
        return entries[:_MAX_ITEMS], len(entries) > _MAX_ITEMS
    except OSError:
        return [], False


def _net_counters(text):
    counters = {}
    for line in (text or '').splitlines():
        if ':' not in line:
            continue
        name, values = line.rsplit(':', 1)
        fields = values.split()
        parsed = [_number(value) for value in fields[:16]]
        if len(parsed) != 16 or any(value is None for value in parsed):
            continue
        counters[name.strip()] = {
            'rx': parsed[0], 'rx_errors': parsed[2], 'rx_drops': parsed[3],
            'tx': parsed[8], 'tx_errors': parsed[10], 'tx_drops': parsed[11],
        }
    return counters


def _net_kind(path):
    if (path / 'device').exists():
        return '物理设备'
    if (path / 'bridge').is_dir():
        return '虚拟网桥'
    if (path / 'bonding').is_dir():
        return '聚合接口'
    if (path / 'tun_flags').exists():
        return 'TUN/TAP 虚拟接口'
    if path.name.startswith('veth'):
        return 'VETH 虚拟接口'
    if path.name == 'lo':
        return '回环接口'
    return '虚拟/其他接口'


def _network_snapshot(reader=None):
    read_text = reader or _read_text
    counters = _net_counters(read_text(PROC_NET_DEV))
    stamp = time.monotonic()
    entries, truncated = _entries(SYS_NET)
    result = {}
    for path in entries:
        device = path / 'device'
        if _passthrough(device if device.exists() else path):
            continue
        kind = _net_kind(path)
        result[path.name] = {
            'kind': kind, 'physical': kind == '物理设备',
            'identity': _path_identity(device if device.exists() else path),
            'ifindex': _number(read_text(path / 'ifindex')),
            'mac': _clean(read_text(path / 'address')),
            'state': read_text(path / 'operstate'),
            'carrier': _number(read_text(path / 'carrier')),
            'speed': _number(read_text(path / 'speed')),
            'counters': counters.get(path.name),
        }
    return result, stamp, truncated


def _disk_counters(text):
    counters = {}
    for line in (text or '').splitlines():
        fields = line.split()
        if len(fields) < 14:
            continue
        parsed = [_number(value) for value in fields[3:14]]
        if any(value is None for value in parsed):
            continue
        counters[fields[2]] = {
            'reads': parsed[0], 'read_bytes': parsed[2] * 512,
            'writes': parsed[4], 'write_bytes': parsed[6] * 512,
            'in_flight': parsed[8],
        }
    return counters


def _disk_kind(path):
    if (path / 'device').exists():
        return '物理块设备'
    if path.name.startswith('dm-'):
        return '设备映射层'
    if path.name.startswith('md'):
        return '软件 RAID 层'
    if path.name.startswith('zd'):
        return 'ZFS 虚拟块设备'
    if path.name.startswith('zram'):
        return '内存块设备'
    return '虚拟/其他块设备'


def _disk_snapshot(reader=None):
    read_text = reader or _read_text
    counters = _disk_counters(read_text(PROC_DISKSTATS))
    stamp = time.monotonic()
    entries, truncated = _entries(SYS_BLOCK)
    result = {}
    for path in entries:
        # /sys/block 本身只列完整块设备；额外检查兼容非标准布局。
        if (path / 'partition').exists() or path.name.startswith(('loop', 'ram')):
            continue
        device = path / 'device'
        if _passthrough(device if device.exists() else path):
            continue
        kind = _disk_kind(path)
        model = read_text(device / 'model')
        if not model:
            model = read_text(path / 'dm' / 'name')
        sectors = _number(read_text(path / 'size'))
        result[path.name] = {
            'kind': kind, 'physical': kind == '物理块设备',
            'identity': _path_identity(path),
            'dev': read_text(path / 'dev'), 'model': _clean(model),
            # Linux sysfs size 和 diskstats 扇区始终以 512 B 为单位。
            'capacity': sectors * 512 if sectors is not None else None,
            'counters': counters.get(path.name),
        }
    return result, stamp, truncated


def _mount_decode(value):
    return re.sub(r'\\([0-7]{3})', lambda match: chr(int(match.group(1), 8)), value)


def _memory_space(mount):
    """只查询内存文件系统；对磁盘、远程挂载不调用 statvfs。"""
    try:
        info = os.statvfs(mount)
        block_size = info.f_frsize or info.f_bsize
        values = (block_size, info.f_blocks, info.f_bfree, info.f_bavail)
        if any(value < 0 for value in values) or block_size == 0:
            return None
        total = info.f_blocks * block_size
        used = max(0, info.f_blocks - info.f_bfree) * block_size
        available = info.f_bavail * block_size
        return total, used, available
    except (OSError, ValueError):
        return None


def _mount_snapshot(reader=None):
    read_text = reader or _read_text
    result = []
    seen = set()
    for line in (read_text(PROC_MOUNTS) or '').splitlines():
        fields = line.split()
        if len(fields) < 4:
            continue
        source, mount, fs_type = [_mount_decode(value) for value in fields[:3]]
        if fs_type not in _LOCAL_FS | _MEMORY_FS and mount != '/':
            continue
        # 不列 /dev/shm、/run 下大量子挂载；仍保留主要内存挂载。
        if fs_type in _MEMORY_FS and mount not in {'/run', '/tmp', '/dev/shm', '/dev', '/'}:
            continue
        identity = source, fs_type, mount
        if identity in seen:
            continue
        seen.add(identity)
        space = _memory_space(mount) if fs_type in _MEMORY_FS else None
        result.append({
            'source': _clean(source), 'mount': _clean(mount), 'type': _clean(fs_type),
            'memory': fs_type in _MEMORY_FS, 'space': space,
        })
        if len(result) >= _MAX_ITEMS:
            break
    return result


def sample_io(reader=None):
    """共享外部采样窗口；本函数不睡眠、不写入或调用外部程序。"""
    nets, net_at, net_truncated = _network_snapshot(reader)
    disks, disk_at, disk_truncated = _disk_snapshot(reader)
    return {
        'nets': nets, 'disks': disks, 'mounts': _mount_snapshot(reader),
        'net_at': net_at, 'disk_at': disk_at,
        'net_truncated': net_truncated, 'disk_truncated': disk_truncated,
    }


def _size(value):
    if value is None or value < 0:
        return '不可用'
    try:
        amount = float(value)
    except (ValueError, TypeError, OverflowError):
        return '不可用'
    if not math.isfinite(amount):
        return '不可用'
    for unit in ('B', 'KiB', 'MiB', 'GiB', 'TiB', 'PiB'):
        if amount < 1024 or unit == 'PiB':
            return f'{amount:.1f} {unit}'
        amount /= 1024
    return '不可用'


def _interval(first, second, key, fallback):
    try:
        interval = second[key] - first[key]
    except (KeyError, TypeError):
        interval = fallback
    return interval if isinstance(interval, (int, float)) and math.isfinite(interval) and interval > 0 else None


def _rate(before, after, field, elapsed):
    if not isinstance(elapsed, (int, float)) or not math.isfinite(elapsed) or elapsed <= 0 or not before or not after:
        return None
    initial, final = before.get(field), after.get(field)
    if initial is None or final is None or final < initial:
        return None
    return (final - initial) / elapsed


def _byte_rate(value):
    return f'{_size(value)}/s' if value is not None else '不可用'


def _link(info):
    state = info.get('state')
    carrier = info.get('carrier')
    if carrier == 0:
        return '无载波'
    if carrier == 1:
        return '已连接'
    states = {'up': '运行中（载波未提供）', 'down': '未连接', 'dormant': '等待激活',
              'lowerlayerdown': '下层未连接', 'testing': '测试中', 'notpresent': '设备不存在'}
    return states.get(state, '不可用' if not state else f'未知（{_clean(state)}）')


def _network_lines(first, second, elapsed):
    lines = ['──────── 网络 ────────']
    nets = second.get('nets', {})
    interval = _interval(first, second, 'net_at', elapsed)
    for name, info in sorted(nets.items(), key=lambda item: (not item[1]['physical'], item[0])):
        previous = first.get('nets', {}).get(name)
        if previous:
            if previous.get('ifindex') is None or info.get('ifindex') is None:
                previous = None
            elif (previous.get('identity'), previous.get('ifindex')) != (info.get('identity'), info.get('ifindex')):
                previous = None
        speed = info.get('speed')
        speed_text = f'{speed} Mbps' if speed and speed != 4294967295 and info.get('carrier') != 0 else '不可用'
        lines.append(f"{_clean(name)} [{info['kind']}]：{_link(info)}；协商速率：{speed_text}")
        if info.get('mac'):
            lines.append(f"  MAC：{info['mac']}；设备来源：{_clean(info['identity'])}")
        counts = info.get('counters')
        prior_counts = previous.get('counters') if previous else None
        if not counts:
            lines.append('  流量 / 丢包 / 错误：不可用')
            continue
        rx = _rate(prior_counts, counts, 'rx', interval)
        tx = _rate(prior_counts, counts, 'tx', interval)
        lines.append(f"  RX：{_size(counts['rx'])}；当前：{_byte_rate(rx)}    TX：{_size(counts['tx'])}；当前：{_byte_rate(tx)}")
        lines.append(f"  累计 RX 丢包 / 错误：{counts['rx_drops']} / {counts['rx_errors']}；TX 丢包 / 错误：{counts['tx_drops']} / {counts['tx_errors']}")
    if not nets:
        lines.append('网络接口：不可用')
    if second.get('net_truncated'):
        lines.append(f'接口数量较多，仅显示前 {_MAX_ITEMS} 个接口。')
    lines.append('各接口独立统计；桥接、虚拟接口可能包含重复流量，不合计。')
    return lines


def _disk_lines(first, second, elapsed):
    lines = ['──────── 磁盘 ────────']
    disks = second.get('disks', {})
    interval = _interval(first, second, 'disk_at', elapsed)
    for name, info in sorted(disks.items(), key=lambda item: (not item[1]['physical'], item[0])):
        previous = first.get('disks', {}).get(name)
        if previous:
            if previous.get('dev') is None or info.get('dev') is None:
                previous = None
            elif (previous.get('identity'), previous.get('dev')) != (info.get('identity'), info.get('dev')):
                previous = None
        lines.append(f"{_clean(name)} [{info['kind']}]：{info['model'] or '型号不可用'}；容量：{_size(info['capacity'])}")
        lines.append(f"  设备来源：{_clean(info['identity'])}")
        counts = info.get('counters')
        prior_counts = previous.get('counters') if previous else None
        if not counts:
            lines.append('  I/O 统计：不可用')
            continue
        read_rate = _rate(prior_counts, counts, 'read_bytes', interval)
        write_rate = _rate(prior_counts, counts, 'write_bytes', interval)
        read_ops = _rate(prior_counts, counts, 'reads', interval)
        write_ops = _rate(prior_counts, counts, 'writes', interval)
        ops = lambda value: f'{value:.1f} 次/s' if value is not None else '不可用'
        lines.append(f"  读取：{_byte_rate(read_rate)}（{ops(read_ops)}）；写入：{_byte_rate(write_rate)}（{ops(write_ops)}）；进行中 I/O：{counts['in_flight']}")
        lines.append(f"  累计读取 / 写入：{_size(counts['read_bytes'])} / {_size(counts['write_bytes'])}")
    if not disks:
        lines.append('宿主机可见完整块设备：不可用')
    if second.get('disk_truncated'):
        lines.append(f'块设备数量较多，仅显示前 {_MAX_ITEMS} 个设备。')
    lines.append('仅显示完整块设备，映射层独立列出、不合计；不探测休眠或访问直通设备。')
    return lines


def _mount_lines(second):
    lines = ['──────── 主要文件系统 ────────']
    mounts = second.get('mounts', [])
    for mount in mounts:
        lines.append(f"{mount['mount']} [{mount['type']}]：{mount['source']}")
        if mount['space'] is not None:
            total, used, available = mount['space']
            percent = f'{used / total * 100:.1f} %' if total else '不可用'
            lines.append(f'  容量：{_size(total)}；已用：{_size(used)}（{percent}）；可用：{_size(available)}')
        elif mount['memory']:
            lines.append('  容量 / 已用 / 可用：不可用')
        else:
            lines.append('  容量 / 已用 / 可用：不可用（未主动查询以保留休眠）')
    if not mounts:
        lines.append('主要本地挂载：不可用')
    lines.append('空间查询仅限内存文件系统；磁盘文件系统不执行 statvfs/df，避免唤醒 HDD。')
    return lines


def render_io(first, second, elapsed):
    """使用同一窗口两份快照，不复读计数器或聚合不同设备。"""
    return _disk_lines(first, second, elapsed) + [''] + _network_lines(first, second, elapsed) + [''] + _mount_lines(second)
