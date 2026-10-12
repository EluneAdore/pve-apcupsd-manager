#!/usr/bin/env python3
"""供电历史存储；不控制 UPS、服务、关机或原生事件脚本。

日志中每一行的文件身份和字节位置是证据身份。关联依据为原生事件
状态机、启动会话及 NUMXFERS；绝不按相近的墙钟时间合并切换。
"""

import argparse
from bisect import bisect_right
import contextlib
import datetime as dt
import errno
import gzip
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
import time


REASONS = {
    "No transfers since turnon": "开机以来无供电切换",
    "Low line voltage": "输入市电电压过低",
    "High line voltage": "输入市电电压过高",
    "Line voltage notch or spike": "市电电压出现陷波或尖峰",
    "Line voltage sag or blackout": "市电电压骤降或停电",
    "Line voltage sag": "市电电压骤降",
    "Line voltage spike": "市电电压尖峰脉冲",
    "Line voltage notch": "市电电压陷波凹陷",
    "Unacceptable line voltage changes": "输入市电电压波动超出允许范围",
    "Automatic or explicit self test": "自动或手动触发电池自检",
    "UNKNOWN EVENT": "未知切换事件",
    "Automatic self test": "电池自动自检",
    "Self test": "电池自检",
    "Forced by software": "软件控制强制切换",
    "Detection of line voltage fail": "检测到市电断电",
    "Unacceptable line freq": "输入市电频率超标",
    "Line frequency out of range": "输入市电频率超标",
    "Input frequency out of range": "输入市电频率超标",
    "Under frequency": "市电频率过低",
    "Over frequency": "市电频率过高",
    "Input line failure": "输入市电故障",
    "Blackout": "市电断电",
    "Power failure": "市电断电",
    "Sag": "市电电压骤降",
    "Spike": "市电电压尖峰",
    "Deep sag": "市电电压严重下陷",
    "Notch": "市电电压凹陷",
}
MESSAGES = {
    "Power failure.": "powerout",
    "Running on UPS batteries.": "onbattery",
    "Mains returned. No longer on UPS batteries.": "offbattery",
    "Power is back. UPS running on mains.": "mainsback",
    "Communications with UPS lost.": "commfailure",
    "Communications with UPS restored.": "commok",
    "UPS Self Test switch to battery.": "startselftest",
}
LINE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d [+-]\d{4})\s+(.*)$")
SCHEMA = """
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE sources(id TEXT PRIMARY KEY, dev INTEGER, ino INTEGER, generation INTEGER,
 path TEXT, physical_size INTEGER, mtime_ns INTEGER, offset INTEGER, tail TEXT,
 prefix TEXT, prefix_len INTEGER, compressed INTEGER DEFAULT 0, retained BLOB);
CREATE INDEX sources_inode ON sources(dev,ino,generation);
CREATE TABLE evidence(id TEXT PRIMARY KEY, source TEXT, position INTEGER,
 event TEXT, stamp TEXT, epoch_ns INTEGER, raw TEXT, cycle TEXT, suppressed INTEGER DEFAULT 0);
CREATE INDEX evidence_source ON evidence(source,position);
CREATE TABLE cycles(id TEXT PRIMARY KEY, start TEXT NOT NULL, start_ns INTEGER,
 end TEXT, end_ns INTEGER, duration INTEGER, reason_raw TEXT NOT NULL DEFAULT '',
 reason_cn TEXT NOT NULL DEFAULT '原因未记录', session TEXT, transfer INTEGER,
 source TEXT, origin TEXT NOT NULL, visible INTEGER NOT NULL DEFAULT 1);
CREATE INDEX cycles_session ON cycles(session,transfer);
CREATE INDEX cycles_display ON cycles(visible,start_ns DESC,id DESC);
CREATE TABLE hooks(token TEXT PRIMARY KEY, event TEXT NOT NULL, captured_ns INTEGER,
 boot TEXT, uptime REAL, session TEXT, anchor TEXT, snapshot TEXT, suppressed INTEGER);
CREATE TABLE claims(native_id TEXT PRIMARY KEY, hook_token TEXT UNIQUE NOT NULL);
CREATE TABLE chunks(source TEXT, start INTEGER, end INTEGER, sha TEXT,
 normalized_sha TEXT, body BLOB, PRIMARY KEY(source,start));
CREATE TABLE source_links(source TEXT, original_source TEXT, consumed INTEGER, mode TEXT);
CREATE TABLE hook_members(token TEXT PRIMARY KEY, cycle TEXT NOT NULL);
CREATE INDEX hook_members_cycle ON hook_members(cycle);
CREATE INDEX evidence_time ON evidence(epoch_ns,event);
CREATE INDEX hooks_time ON hooks(captured_ns);
CREATE TABLE snapshots(id INTEGER PRIMARY KEY, observed_ns INTEGER, fingerprint TEXT,
 raw TEXT NOT NULL, cycle TEXT);

"""


def parse_time(value):
    if not value or value in ("-", "N/A"):
        return None
    try:
        return int(dt.datetime.strptime(value.strip(), "%Y-%m-%d %H:%M:%S %z").timestamp()) * 1_000_000_000
    except (ValueError, OverflowError):
        return None


def stamp_from_ns(value):
    return dt.datetime.fromtimestamp(value / 1_000_000_000).astimezone().strftime("%Y-%m-%d %H:%M:%S %z")


def snapshot_fields(raw):
    if isinstance(raw, dict):
        return {str(k): str(v).strip() for k, v in raw.items()}
    result = {}
    for line in (raw or "").splitlines():
        key, separator, value = line.partition(":")
        if separator:
            result[key.strip()] = value.strip()
    return result


def digest(value):
    return hashlib.sha256(value).hexdigest()


def normalized_bytes(data):
    lines = []
    for binary in data.splitlines(keepends=True):
        text = binary.decode("utf-8", errors="replace")
        match = LINE.match(text.rstrip("\r\n"))
        if match and parse_time(match.group(1)) is not None:
            text = str(parse_time(match.group(1))) + " " + match.group(2) + "\n"
        lines.append(text)
    return "".join(lines).encode("utf-8")


def clean(value):
    return str("" if value is None else value).replace("\t", " ").replace("\n", " ").replace("\r", " ")


def boot_clock():
    try:
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        uptime = time.clock_gettime(time.CLOCK_BOOTTIME)
        return boot, uptime
    except (OSError, AttributeError):
        return "", time.monotonic()


class Engine:
    """Public isolated API. Caller owns the lifecycle flock; SQLite serializes writers."""

    def __init__(self, state_dir, events_path, legacy_path=None):
        self.state_dir = Path(state_dir)
        self.events_path = Path(events_path)
        self.legacy_path = Path(legacy_path) if legacy_path else self.state_dir / "history.tsv"
        self.database_path = self.state_dir / "history.db"
        self.error_path = self.state_dir / "history-error.json"
        self.db = None
        self.bytes_read = 0
        self.lines_parsed = 0
        try:
            if not self.state_dir.is_dir() or self.state_dir.is_symlink():
                raise OSError("安装状态目录不存在或不安全")
            if self.database_path.is_symlink() or (self.database_path.exists() and not self.database_path.is_file()):
                raise OSError("历史数据库路径不是普通文件")
            fresh = not self.database_path.exists()
            self.db = sqlite3.connect(self.database_path, timeout=2.0, isolation_level=None)
            self.db.row_factory = sqlite3.Row
            self.db.execute("PRAGMA foreign_keys=ON")
            # Existing databases do not execute a write PRAGMA on every menu refresh.
            if fresh:
                self.db.execute("PRAGMA journal_mode=DELETE")
                self.db.executescript("BEGIN IMMEDIATE;" + SCHEMA + "PRAGMA user_version=2;COMMIT;")
                os.chmod(self.database_path, 0o600)
            else:
                version = self.db.execute("PRAGMA user_version").fetchone()[0]
                if version == 1:
                    # 保留上一实现的全部原始证据，撤销未经身份验证的类型 FIFO 关联。
                    self.db.executescript("""BEGIN IMMEDIATE;
                        CREATE TABLE hook_members(token TEXT PRIMARY KEY, cycle TEXT NOT NULL);
                        CREATE INDEX hook_members_cycle ON hook_members(cycle);
                        CREATE INDEX evidence_time ON evidence(epoch_ns,event);
                        CREATE INDEX hooks_time ON hooks(captured_ns);
                        CREATE TABLE snapshots(id INTEGER PRIMARY KEY, observed_ns INTEGER, fingerprint TEXT,
                            raw TEXT NOT NULL, cycle TEXT);
                        DELETE FROM claims WHERE hook_token NOT LIKE 'baseline:%';
                        INSERT OR REPLACE INTO meta VALUES ('projection_upgrade','1');
                        PRAGMA user_version=2; COMMIT;""")
                elif version != 2:
                    raise ValueError("历史数据库版本异常；已保留原文件，未覆盖")
                elif not self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='snapshots'").fetchone():
                    self.db.execute("CREATE TABLE snapshots(id INTEGER PRIMARY KEY, observed_ns INTEGER, fingerprint TEXT, raw TEXT NOT NULL, cycle TEXT)")
            self.db.execute("SELECT key,value FROM meta LIMIT 1").fetchone()
            self.db_identity = (self.database_path.stat().st_dev, self.database_path.stat().st_ino)
        except Exception as exc:
            self.report_error(exc)
            if self.db:
                self.db.close()
            raise

    def close(self):
        if self.db:
            self.db.close()
            self.db = None

    def __del__(self):
        with contextlib.suppress(Exception):
            self.close()

    def _get(self, key, default=""):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def _put(self, key, value):
        value = str(value)
        if self._get(key, None) != value:
            self.db.execute("INSERT INTO meta VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    def _json(self, key, default=None):
        text = self._get(key)
        return json.loads(text) if text else default

    def _put_json(self, key, value):
        self._put(key, json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")))

    def _failpoint(self):
        if os.environ.get("PVE_UPS_TESTING") != "1":
            return
        point = os.environ.get("PVE_UPS_TEST_FAILPOINT", "")
        if point == "disk-full":
            raise OSError(errno.ENOSPC, "模拟磁盘空间已满")
        if point == "write-error":
            raise PermissionError(errno.EACCES, "模拟历史写入权限错误")
        if point == "commit-before":
            raise RuntimeError("模拟提交前写入中断")
        if point == "pause-before-commit":
            print("PVE_UPS_TEST_BEFORE_COMMIT", file=sys.stderr, flush=True)
            time.sleep(30)

    @contextlib.contextmanager
    def _transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self._failpoint()
            self.db.execute("COMMIT")
        except BaseException:
            if self.db.in_transaction:
                self.db.execute("ROLLBACK")
            raise

    def report_error(self, error):
        text = clean(error)
        report = {"time": stamp_from_ns(time.time_ns()), "error": text}
        # A full/readonly disk may also reject this report; stderr is a separate fallback.
        print("历史同步失败：" + text, file=sys.stderr)
        if not self.state_dir.is_dir() or self.state_dir.is_symlink() or self.error_path.is_symlink():
            return
        temporary = None
        try:
            fd, temporary = tempfile.mkstemp(prefix=".history-error.", dir=self.state_dir)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(report, handle, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.error_path)
        except OSError:
            pass
        finally:
            if temporary:
                with contextlib.suppress(OSError):
                    os.unlink(temporary)

    def _success(self):
        if self.error_path.exists() and not self.error_path.is_symlink():
            # Keep the last failure inspectable after recovery, without repeated writes.
            try:
                report = json.loads(self.error_path.read_text(encoding="utf-8"))
                if not report.get("recovered"):
                    report["recovered"] = stamp_from_ns(time.time_ns())
                    fd, temporary = tempfile.mkstemp(prefix=".history-error.", dir=self.state_dir)
                    try:
                        with os.fdopen(fd, "w", encoding="utf-8") as handle:
                            json.dump(report, handle, ensure_ascii=False)
                            handle.flush()
                            os.fsync(handle.fileno())
                        os.replace(temporary, self.error_path)
                    finally:
                        with contextlib.suppress(OSError):
                            os.unlink(temporary)
            except (OSError, ValueError):
                pass

    def _call(self, action):
        try:
            try:
                status = self.database_path.stat()
                identity = (status.st_dev, status.st_ino)
            except FileNotFoundError:
                identity = None
            if identity != self.db_identity:
                # An administrator may move a corrupt database aside to repair it.
                # Reopen the current path; never overwrite the saved damaged file.
                self.close()
                replacement = Engine(self.state_dir, self.events_path, self.legacy_path)
                self.db = replacement.db
                self.db_identity = replacement.db_identity
                replacement.db = None
            result = action()
            self._success()
            return result
        except Exception as exc:
            self.report_error(exc)
            raise

    def _legacy_needed(self):
        return self._get("legacy_imported") != "1"

    def _migrate(self):
        if not self._legacy_needed():
            return
        warnings = []
        if self.legacy_path.exists():
            if self.legacy_path.is_symlink() or not self.legacy_path.is_file():
                raise OSError("旧版 history.tsv 路径异常；未修改原文件")
            for number, line in enumerate(self.legacy_path.read_text(encoding="utf-8").splitlines(), 1):
                if not line.strip():
                    continue
                fields = line.split("\t")
                if len(fields) != 5 or parse_time(fields[0]) is None:
                    warnings.append(str(number))
                    continue
                start, end, duration, translated, raw = fields
                start_ns = parse_time(start)
                end_ns = parse_time(end)
                if end not in ("", "-") and (end_ns is None or end_ns < start_ns):
                    warnings.append(str(number))
                    end_ns = None
                    end = ""
                duration_value = int(duration) if duration.isdigit() else None
                if end_ns is not None:
                    duration_value = (end_ns - start_ns) // 1_000_000_000
                elif duration_value is not None:
                    # Preserve valid duration reported by old versions even if end was lost.
                    duration_value = int(duration_value)
                identity = "legacy:" + digest((str(number) + "\0" + line).encode())
                if raw in ("Power failure", "No transfers since turnon"):
                    translated = "原因未记录（旧版标记）"
                self.db.execute("INSERT OR IGNORE INTO cycles VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1)",
                                (identity, start, start_ns, end if end_ns is not None else None, end_ns,
                                 duration_value, raw, translated or REASONS.get(raw, raw) or "原因未记录",
                                 None, None, str(self.legacy_path), "legacy"))
        old_boundary = Path(str(self.legacy_path) + ".cleared")
        if old_boundary.exists():
            if old_boundary.is_symlink() or not old_boundary.is_file():
                raise OSError("旧版清空水位路径异常")
            value = old_boundary.read_text(encoding="utf-8").split("\t", 1)[0].strip()
            if value.isdigit():
                self._put("legacy_clear_ns", int(value) * 1_000_000_000)
            else:
                warnings.append("旧版清空水位异常")
        if warnings:
            self._put("migration_notice", "旧历史异常行已原样保留，未导入的行/项目：" + ",".join(warnings))
        self._put("legacy_imported", "1")

    def _paths(self):
        candidates = [self.events_path]
        # Read rotated files only on identity/size changes, including compressed rotations.
        with contextlib.suppress(OSError):
            candidates += list(self.events_path.parent.glob(self.events_path.name + ".*"))
        unique = {}
        for path in candidates:
            try:
                if path.is_symlink() or not path.is_file():
                    continue
                stat = path.stat()
                unique[(stat.st_dev, stat.st_ino)] = (path, stat)
            except FileNotFoundError:
                continue
        def rotation_order(item):
            path = item[0]
            if path == self.events_path:
                return (1, 0, str(path))
            suffix = str(path)[len(str(self.events_path))+1:]
            number = re.match(r"^(\d+)(?:\.gz)?$", suffix)
            if number:
                return (0, -int(number.group(1)), str(path))
            return (0, 0, str(path))
        return sorted(unique.values(), key=rotation_order)

    def _changed(self, paths):
        for path, stat in paths:
            row = self.db.execute("SELECT physical_size,mtime_ns FROM sources WHERE dev=? AND ino=? ORDER BY generation DESC LIMIT 1", (stat.st_dev, stat.st_ino)).fetchone()
            if row is None or row["physical_size"] != stat.st_size or row["mtime_ns"] != stat.st_mtime_ns:
                return True
        return False

    def _read(self, path, start=0):
        opener = gzip.open if path.suffix == ".gz" else open
        with opener(path, "rb") as handle:
            handle.seek(start)
            data = handle.read()
            self.bytes_read += len(data)
        return data

    def _suppressed(self, epoch_ns, source_new=False):
        old = int(self._get("legacy_clear_ns", "0"))
        if old and epoch_ns is not None and epoch_ns <= old:
            return 1
        clear = self._json("clear", {})
        if clear and epoch_ns is not None:
            boundary = int(clear["wall_ns"]) // 1_000_000_000 * 1_000_000_000
            if epoch_ns < boundary:
                return 1
            if epoch_ns == boundary:
                # 秒精度日志不能证明位于清空请求的哪一侧。保存待核查证据，
                # 不计入清空后的次数；精确 BOOTTIME 的新回调仍独立保留。
                return 2
        return 0

    def _ingest_logs(self, paths):
        for path, stat in paths:
            row = self.db.execute("SELECT * FROM sources WHERE dev=? AND ino=? ORDER BY generation DESC LIMIT 1", (stat.st_dev, stat.st_ino)).fetchone()
            if row is not None and row["physical_size"] == stat.st_size and row["mtime_ns"] == stat.st_mtime_ns:
                continue
            data = None
            reset = row is None
            if row is not None and not row["compressed"]:
                with open(path, "rb") as handle:
                    prefix_sample = handle.read(row["prefix_len"])
                    handle.seek(max(0, row["offset"] - 128))
                    sample = handle.read(min(128, row["offset"]))
                    self.bytes_read += len(prefix_sample) + len(sample)
                reset = stat.st_size < row["offset"] or digest(sample) != row["tail"] or digest(prefix_sample) != row["prefix"]
            elif row is not None and row["compressed"]:
                # A gzipped rotation is immutable. A changed one gets its own generation.
                reset = True
            generation = (row["generation"] + 1) if reset and row is not None else (row["generation"] if row else 0)
            source = f"{stat.st_dev}:{stat.st_ino}:{generation}"
            offset = 0 if reset else row["offset"]
            ambiguous_prefix = 0
            if reset:
                data = self._read(path)
                # 完整分段哈希证明复制；两端小样本仅用于检测原地改写，不能去重。
                linked = None
                aliases = self.db.execute("SELECT * FROM sources WHERE offset>0 ORDER BY offset DESC").fetchall()
                for alias in aliases:
                    length = alias["offset"]
                    archive = path != self.events_path
                    same_inode = row is not None and alias["id"] == row["id"]
                    if length <= len(data) and self._chunk_match(alias["id"], data, length):
                        if archive or same_inode:
                            offset = length
                            linked = (alias["id"], "完整日志前缀复制")
                        else:
                            # 新活动 inode 中相同字节也可能是同秒重启的新事件。
                            # 不能声称复制身份已证明；保存整段原始证据供核查。
                            ambiguous_prefix = length
                            linked = (alias["id"], "新活动日志相同前缀，来源待核查")
                        break
                if not offset and row is not None:
                    # apcupsd EVENTSFILEMAX 会在同一 inode 内截短并保留尾部完整行。
                    # 仅完整原始字节的尾→头重合可跳过，绝不按时间/类型集合去重。
                    retained = bytes(row["retained"] or b"")
                    candidates = [0] + [index+1 for index, value in enumerate(retained) if value == 10]
                    for start in candidates:
                        suffix = retained[start:]
                        if suffix and data.startswith(suffix):
                            offset = len(suffix)
                            linked = (row["id"], "同 inode 原生尾部截短")
                            break
                    if not offset and row["offset"] > len(retained):
                        # 大型 EVENTSFILEMAX 的保留尾段可能超过缓存。原始字节已按
                        # 增量分段保存在数据库，仅截短时读取，正常刷新不读取旧日志。
                        original = b"".join(bytes(chunk[0]) for chunk in self.db.execute("SELECT body FROM chunks WHERE source=? ORDER BY start", (row["id"],)))
                        candidates = [0] + [index+1 for index, value in enumerate(original) if value == 10]
                        for start in candidates:
                            suffix = original[start:]
                            if suffix and data.startswith(suffix):
                                offset = len(suffix)
                                linked = (row["id"], "同 inode 原生完整尾段截短")
                                break
                prefix_data = data[:offset]
                if prefix_data:
                    self._store_chunk(source, 0, offset, prefix_data)
                if linked:
                    self.db.execute("INSERT INTO source_links VALUES (?,?,?,?)", (source, linked[0], offset or ambiguous_prefix, linked[1]))
            if data is None:
                data = self._read(path, offset)
                absolute_start = offset
            else:
                data = data[offset:]
                absolute_start = offset
            complete = data.rfind(b"\n") + 1
            final = offset + complete
            if complete:
                self._store_chunk(source, offset, final, data[:complete])
            position = absolute_start
            for binary in data[:complete].splitlines(keepends=True):
                text = binary.decode("utf-8", errors="replace").rstrip("\r\n")
                identity = f"log:{source}:{position}"
                self.lines_parsed += 1
                self._log_event(identity, source, position, text, source_new=reset, source_uncertain=position < ambiguous_prefix)
                position += len(binary)
            # Fingerprints cover consumed bytes, so appending does not reread old lines.
            opener = gzip.open if path.suffix == ".gz" else open
            with opener(path, "rb") as handle:
                prefix_len = min(final, 256)
                prefix = handle.read(prefix_len)
                handle.seek(max(0, final-128))
                tail = handle.read(min(final, 128))
                self.bytes_read += len(prefix) + len(tail)
            retained = ((bytes(row["retained"] or b"") if row is not None and not reset else prefix_data) + data[:complete])[-65536:]
            # 保存末尾完整行，供原生 EVENTSFILEMAX 截短后核对。
            if final > len(retained) and b"\n" in retained:
                retained = retained.split(b"\n", 1)[1]
            self.db.execute("INSERT INTO sources VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET path=excluded.path,physical_size=excluded.physical_size,mtime_ns=excluded.mtime_ns,offset=excluded.offset,tail=excluded.tail,prefix=excluded.prefix,prefix_len=excluded.prefix_len,retained=excluded.retained",
                            (source, stat.st_dev, stat.st_ino, generation, str(path), stat.st_size, stat.st_mtime_ns, final, digest(tail), digest(prefix), prefix_len, int(path.suffix == ".gz"), retained))

    def _store_chunk(self, source, start, end, data):
        self.db.execute("INSERT OR IGNORE INTO chunks VALUES (?,?,?,?,?,?)", (source, start, end, digest(data), digest(normalized_bytes(data)), data))

    def _chunk_match(self, source, data, length, normalized=True):
        chunks = self.db.execute("SELECT * FROM chunks WHERE source=? ORDER BY start", (source,)).fetchall()
        position = 0
        for chunk in chunks:
            if chunk["start"] != position or chunk["end"] > length:
                return False
            body = data[chunk["start"]:chunk["end"]]
            if digest(body) != chunk["sha"] and (not normalized or digest(normalized_bytes(body)) != chunk["normalized_sha"]):
                return False
            position = chunk["end"]
        return position == length

    def _new_cycle(self, identity, stamp, epoch_ns, source, state, origin="log", reason=""):
        transfer = state.get("transfer", 0) + 1
        state["transfer"] = transfer
        session = state.get("session", "")
        # A snapshot cycle with the exact session/counter identity is the same transfer,
        # even if the device and the log timestamps differ by several seconds.
        unique_session = session and state.get("known") and self.db.execute("SELECT COUNT(*) FROM evidence WHERE event='startup' AND epoch_ns=?", (parse_time(session),)).fetchone()[0] <= 1
        matches = self.db.execute("SELECT id FROM cycles WHERE session=? AND transfer=? AND origin='snapshot' AND visible=1", (session, transfer)).fetchall() if unique_session else []
        if len(matches) == 1:
            cycle = matches[0]["id"]
            # 原有端点来自快照。稍后同身份的原生日志恢复事件可以纠正它，
            # 原快照另存为不可变证据，不丢弃设备上报的旧时刻。
            self._put("end_source:" + cycle, "snapshot")
            self.db.execute("UPDATE cycles SET start=?,start_ns=?,source=?,origin='log',duration=CASE WHEN end_ns>=? THEN (end_ns-?)/1000000000 ELSE NULL END WHERE id=?", (stamp, epoch_ns, source, epoch_ns, epoch_ns, cycle))
        else:
            # 迁移行先原样保留；仅开始时间相同不能证明它与新事件是同一次。
            cycle = identity
            self.db.execute("INSERT OR IGNORE INTO cycles VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1)",
                            (cycle, stamp, epoch_ns, None, None, None, reason,
                             REASONS.get(reason, reason) or "原因未记录", session,
                             transfer if state.get("known") else None, source, origin))
        state["active"] = cycle
        state["restoring"] = ""
        state["mode"] = "battery"
        return cycle

    def _finish(self, cycle, stamp, epoch_ns, origin="log"):
        if not cycle:
            return
        row = self.db.execute("SELECT start_ns,end_ns FROM cycles WHERE id=?", (cycle,)).fetchone()
        correct_snapshot = origin == "log" and self._get("end_source:" + cycle) == "snapshot"
        if row and (row["end_ns"] is None or correct_snapshot) and epoch_ns >= row["start_ns"]:
            self.db.execute("UPDATE cycles SET end=?,end_ns=?,duration=? WHERE id=?", (stamp, epoch_ns, (epoch_ns-row["start_ns"])//1_000_000_000, cycle))
            if origin in ("log", "snapshot"):
                self._put("end_source:" + cycle, origin)

    def _log_event(self, identity, source, position, text, source_new=False, source_uncertain=False):
        if self.db.execute("SELECT 1 FROM evidence WHERE id=?", (identity,)).fetchone():
            return
        match = LINE.match(text)
        if not match:
            if text.strip():
                self._put("input_notice", "原生日志含不符合格式的行；原文件已保留，请查看诊断。")
            return
        stamp, message = match.groups()
        epoch_ns = parse_time(stamp)
        if epoch_ns is None:
            self._put("input_notice", "原生日志时间格式异常；未猜测发生时间。")
            return
        event = MESSAGES.get(message)
        if "startup succeeded" in message and "apcupsd" in message:
            event = "startup"
        elif "shutdown succeeded" in message and "apcupsd" in message:
            event = "daemonstop"
        elif message.startswith("UPS Self Test completed"):
            event = "endselftest"
        if not event:
            return
        suppressed = self._suppressed(epoch_ns, source_new)
        if source_uncertain and suppressed != 1:
            suppressed = 2
            self._put("input_notice", "新活动日志与已读内容相同，无法证明复制或新事件，原始证据已保留待核查。")
        state = self._json("log_state", {"session": "", "transfer": 0, "known": False, "active": "", "restoring": "", "mode": "unknown"})
        if source_uncertain:
            # 来源可能是复制，也可能包含真实新切换；不能将其计数并入
            # 已确认会话，否则随后 LASTXFER 会被错配到待核查的旧记录。
            state["known"] = False
        cycle = None
        if event == "startup":
            # An in-flight transfer survives a daemon restart, but the counter starts at
            # zero and no stale session is used to infer its reason/restore timestamp.
            state.update(session=stamp, transfer=0, known=not source_uncertain, mode="unknown", restoring="")
            self._put("active_session", stamp)
            self._put("current_cycle", "")
        elif event == "daemonstop":
            state["mode"] = "unknown"
            self._put("current_cycle", "")
        elif event in ("powerout", "startselftest"):
            if suppressed != 1:
                cycle = self._new_cycle(identity, stamp, epoch_ns, source, state, reason="Automatic or explicit self test" if event == "startselftest" else "")
                if suppressed == 2:
                    self.db.execute("UPDATE cycles SET visible=2 WHERE id=? AND visible<>0", (cycle,))
                    self._put("boundary_notice", "清空边界秒内的日志缺少精确先后证据，已保留待核查；不自动恢复旧记录。")
            else:
                state["transfer"] = state.get("transfer", 0) + 1
                state.update(active="", restoring="", mode="unknown")
        elif event == "onbattery":
            cycle = state.get("active")
            if not cycle and suppressed != 1:
                # Missing powerout: retain evidence of battery supply, with onset from
                # this event and a notice rather than inventing an earlier start.
                state["known"] = False
                cycle = self._new_cycle(identity, stamp, epoch_ns, source, state)
                if suppressed == 2:
                    self.db.execute("UPDATE cycles SET visible=2 WHERE id=? AND visible<>0", (cycle,))
                self._put("input_notice", "部分事件缺少 powerout；起点仅为首次已记录的电池供电事件。")
        elif event in ("offbattery", "mainsback", "endselftest"):
            cycle = state.get("active") or state.get("restoring")
            if suppressed != 1:
                self._finish(cycle, stamp, epoch_ns)
            if event == "offbattery":
                state.update(active="", restoring=cycle or "", mode="mains")
            else:
                state.update(active="", restoring="", mode="mains")
        elif event == "commfailure":
            state["mode"] = "unknown"
            self._put("current_cycle", "")
        elif event == "commok":
            state["mode"] = "unknown"
        self.db.execute("INSERT INTO evidence VALUES (?,?,?,?,?,?,?,?,?)", (identity, source, position, event, stamp, epoch_ns, message, cycle, int(suppressed)))
        self._put_json("log_state", state)

    def _snapshot_identity(self, values):
        status = values.get("STATUS", "")
        session = values.get("STARTTIME", "")
        transfers = values.get("NUMXFERS", "").split(" ")[0]
        if "COMMLOST" in status or not parse_time(session) or not transfers.isdigit():
            return None
        transfer = int(transfers)
        rows = self.db.execute("SELECT * FROM cycles WHERE session=? AND transfer=?", (session, transfer)).fetchall()
        # 两次 daemon 启动的秒级 STARTTIME 相同也不能确认来自哪一会话。
        starts = self.db.execute("SELECT COUNT(*) FROM evidence WHERE event='startup' AND epoch_ns=?", (parse_time(session),)).fetchone()[0]
        state = self._json("log_state", {})
        if len(rows) == 1 and rows[0]["visible"] == 1 and starts <= 1 and (rows[0]["origin"] == "snapshot" or
                (state.get("known") and state.get("session") == session)):
            return rows[0]
        # 相同 XON 秒数不能证明身份：同秒多次切换、重启或旧 TSV 都可能重合。
        return None

    def _ingest_snapshot(self, raw):
        values = snapshot_fields(raw)
        if not values:
            self._put("current_cycle", "")
            self._put("snapshot", "")
            return
        relevant = {key: values.get(key, "") for key in ("STARTTIME", "NUMXFERS", "XONBATT", "XOFFBATT", "LASTXFER", "STATUS")}
        fingerprint = json.dumps(relevant, sort_keys=True, separators=(",", ":"))
        row = self._snapshot_identity(values)
        state = self._json("log_state", {})
        status = values.get("STATUS", "")
        session = values.get("STARTTIME", "")
        transfers = values.get("NUMXFERS", "").split(" ")[0]
        xon = parse_time(values.get("XONBATT", ""))
        if row is None and not self._paths() and parse_time(session) is not None and transfers.isdigit() and xon is not None and "COMMLOST" not in status:
            clear = self._json("clear", {})
            suppressed = self._suppressed(xon, True) or (clear and xon <= clear.get("last_xon_ns", 0) and session == clear.get("snapshot_session"))
            if not suppressed:
                identity = "snapshot:" + session + ":" + transfers
                self.db.execute("INSERT OR IGNORE INTO cycles VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1)", (identity, values["XONBATT"], xon, None, None, None, "", "原因未记录", session, int(transfers), "apcaccess", "snapshot"))
                row = self.db.execute("SELECT * FROM cycles WHERE id=?", (identity,)).fetchone()
        active = ""
        if row is not None:
            raw_reason = values.get("LASTXFER", "")
            if raw_reason not in ("", "N/A", "No transfers since turnon") and row["reason_raw"] != raw_reason:
                self.db.execute("UPDATE cycles SET reason_raw=?,reason_cn=? WHERE id=?", (raw_reason, REASONS.get(raw_reason, raw_reason), row["id"]))
            xoff = parse_time(values.get("XOFFBATT", ""))
            if "ONBATT" not in status and "ONLINE" in status and xoff is not None:
                self._finish(row["id"], values["XOFFBATT"], xoff, origin="snapshot")
            if "ONBATT" in status and "COMMLOST" not in status and row["end_ns"] is None:
                active = row["id"]
        elif "ONBATT" in status and "COMMLOST" not in status:
            # State alone can confirm ongoing battery supply, but cannot assign a stale
            # reason. Use a complete latest stream only when no restart/loss intervened.
            if state.get("mode") == "battery" and (not parse_time(session) or not transfers.isdigit()):
                active = state.get("active", "")
        if self._get("snapshot") != fingerprint:
            self.db.execute("INSERT INTO snapshots(observed_ns,fingerprint,raw,cycle) VALUES (?,?,?,?)",
                            (time.time_ns(), fingerprint, json.dumps(values, ensure_ascii=False, sort_keys=True), row["id"] if row is not None else None))
        self._put("current_cycle", active)
        self._put("snapshot", fingerprint)

    def _snapshot_changed(self, raw):
        values = snapshot_fields(raw)
        if not values:
            return bool(self._get("current_cycle") or self._get("snapshot"))
        relevant = {key: values.get(key, "") for key in ("STARTTIME", "NUMXFERS", "XONBATT", "XOFFBATT", "LASTXFER", "STATUS")}
        if self._get("snapshot") != json.dumps(relevant, sort_keys=True, separators=(",", ":")):
            return True
        if "ONBATT" in values.get("STATUS", "") and "COMMLOST" not in values.get("STATUS", "") and not self._get("current_cycle"):
            count = values.get("NUMXFERS", "").split(" ")[0]
            if count.isdigit():
                rows = self.db.execute("SELECT id FROM cycles WHERE session=? AND transfer=? AND end_ns IS NULL AND visible=1", (values.get("STARTTIME", ""), int(count))).fetchall()
                return len(rows) == 1
        return False

    def _sync_locked(self, raw):
        self._migrate()
        self._ingest_logs(self._paths())
        self._ingest_snapshot(raw)
        for row in self.db.execute("SELECT id,session FROM cycles WHERE origin='snapshot' AND visible=1").fetchall():
            if self.db.execute("SELECT COUNT(*) FROM evidence WHERE event='startup' AND epoch_ns=?", (parse_time(row["session"]),)).fetchone()[0] > 1:
                self.db.execute("UPDATE cycles SET visible=2 WHERE id=?", (row["id"],))
        if self._get("projection_upgrade") == "1":
            self._rebuild_hook_cycles()
            self._put("projection_upgrade", "0")
        self._classify_hook_cycles()

    def sync(self, snapshot_raw=""):
        def action():
            paths = self._paths()
            need_baseline = self._get("capture_initialized") != "1"
            if need_baseline or self._legacy_needed() or self._get("projection_upgrade") == "1" or self._changed(paths) or self._snapshot_changed(snapshot_raw):
                with self._transaction():
                    self._sync_locked(snapshot_raw)
                    if need_baseline:
                        # 安装/升级先同步现有日志再启用新回调。这些旧证据不是
                        # 新回调的来源，不能吞掉日志停写后发生的真实新事件。
                        self.db.execute("INSERT OR IGNORE INTO claims SELECT id,'baseline:'||id FROM evidence WHERE event IN ('powerout','onbattery','offbattery','mainsback')")
                        self._put("capture_initialized", "1")
            return None
        return self._call(action)

    def _rebuild_hook_cycles(self):
        # 原始回调永不删除。投影允许重算，迟到的 offbattery 可纠正先收到的
        # mainsback；同一 boot 的出生 ticks/BOOTTIME 决定顺序，工作者到达无关。
        fence = self._json("clear", {})
        if fence:
            # schema 1 的清空只隐藏投影，未更新回调墓碑；升级重建时补齐，
            # 避免原先被错误 claim 的旧回调在新版成为新事件。
            for record in self.db.execute("SELECT * FROM hooks WHERE suppressed=0").fetchall():
                if self._hook_before_fence(record, fence):
                    self.db.execute("UPDATE hooks SET suppressed=1 WHERE token=?", (record["token"],))
        records = self.db.execute("SELECT * FROM hooks WHERE suppressed=0").fetchall()
        boot_order = {}
        for record in records:
            boot_order[record["boot"]] = min(boot_order.get(record["boot"], record["captured_ns"]), record["captured_ns"])
        def order(record):
            pieces = record["token"].split(":")
            uptime = record["uptime"]
            if len(pieces) >= 4 and pieces[-2].isdigit() and pieces[-3].isdigit() and int(pieces[-2]) > 0:
                position = int(pieces[-2]) / os.sysconf("SC_CLK_TCK")
                tie = int(pieces[-3])
            elif uptime is not None:
                position, tie = uptime, 0
            else:
                position, tie = record["captured_ns"] / 1_000_000_000, 0
            # 同精度内根据 native 事件语义排序；不丢弃同秒不同调用。
            rank = {"powerout": 0, "onbattery": 1, "offbattery": 2, "mainsback": 3}[record["event"]]
            return (boot_order[record["boot"]], position, tie, record["captured_ns"], rank, record["token"])
        records.sort(key=order)
        self.db.execute("DELETE FROM hook_members")
        self.db.execute("UPDATE cycles SET end=NULL,end_ns=NULL,duration=NULL WHERE origin='hook'")
        active, restoring, current_boot = "", "", None
        projected = set()
        for record in records:
            if current_boot != record["boot"]:
                active, restoring, current_boot = "", "", record["boot"]
            event, token, epoch_ns = record["event"], record["token"], record["captured_ns"]
            stamp = stamp_from_ns(epoch_ns)
            if event in ("powerout", "onbattery"):
                if event == "onbattery" and active:
                    cycle = active
                else:
                    cycle = "hook:" + token
                    self.db.execute("INSERT OR IGNORE INTO cycles VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1)",
                        (cycle, stamp, epoch_ns, None, None, None, "", "原因未记录", record["session"], None, "hook", "hook"))
                    active, restoring = cycle, ""
                projected.add(cycle)
            else:
                cycle = active or restoring
                self._finish(cycle, stamp, epoch_ns, origin="hook")
                active, restoring = "", cycle if event == "offbattery" else ""
            if cycle:
                self.db.execute("INSERT INTO hook_members VALUES (?,?)", (token, cycle))
        # 迟到 powerout 使先前单独 onbattery 成为确认消息，保留证据而移除旧投影。
        for row in self.db.execute("SELECT id FROM cycles WHERE origin='hook'").fetchall():
            if row["id"] not in projected:
                self.db.execute("UPDATE cycles SET visible=3 WHERE id=? AND visible<>0", (row["id"],))
        self._classify_hook_cycles()

    def _classify_hook_cycles(self):
        # 无跨源唯一身份时不认领日志、更不分配原因。相交的记录属于待核查
        # 证据，绝不声称已合并；明确互不相交的完整回调周期继续独立计数。
        # 恢复时间未知不是瞬间结束：不能据此证明后来的证据是独立事件。
        intervals = sorted((row["start_ns"] // 1_000_000_000,
                            row["end_ns"] // 1_000_000_000 if row["end_ns"] is not None else math.inf)
                           for row in self.db.execute("SELECT start_ns,end_ns FROM cycles WHERE origin IN ('log','snapshot') AND visible=1"))
        starts, furthest = [], []
        for start, end in intervals:
            starts.append(start)
            furthest.append(max(end, furthest[-1] if furthest else end))
        for hook in self.db.execute("SELECT * FROM cycles WHERE origin='hook' AND visible IN (1,2)").fetchall():
            start = hook["start_ns"] // 1_000_000_000
            end = hook["end_ns"] // 1_000_000_000 if hook["end_ns"] is not None else math.inf
            index = bisect_right(starts, end) - 1
            overlaps = index >= 0 and furthest[index] >= start
            visible = 2 if overlaps else 1
            if hook["visible"] != visible:
                self.db.execute("UPDATE cycles SET visible=? WHERE id=?", (visible, hook["id"]))
        # 当前状态只由最新成功状态查询确认，不由迟到的回调假定。

    def capture(self, event, captured_ns=None, token=None, session="", log_anchor=None,
                snapshot_raw="", captured_uptime=None, boot_id=""):
        if event not in ("powerout", "onbattery", "offbattery", "mainsback"):
            raise ValueError("不支持的历史事件类型")
        now_boot, now_uptime = boot_clock()
        derived_time = captured_ns is None and captured_uptime is not None
        pieces = str(token or "").split(":")
        if captured_ns is None and captured_uptime is not None and len(pieces) >= 4 and pieces[-2].isdigit() and pieces[-3].isdigit() and int(pieces[-2]) > 0:
            # apcupsd 在 fork 前写原生事件，子进程可能晚获调度。出生 ticks
            # 比脚本首次运行的 /proc/uptime 更接近触发时刻，也可靠保留因果顺序。
            captured_uptime = min(float(captured_uptime), int(pieces[-2]) / os.sysconf("SC_CLK_TCK"))
        if captured_uptime is not None and captured_ns is None:
            captured_ns = time.time_ns() - int((now_uptime-float(captured_uptime))*1_000_000_000)
        captured_ns = int(captured_ns if captured_ns is not None else time.time_ns())
        boot_id = boot_id or now_boot
        token = str(token or f"manual:{event}:{captured_ns}")
        def action():
            if self.db.execute("SELECT 1 FROM hooks WHERE token=?", (token,)).fetchone():
                return None
            with self._transaction():
                self._sync_locked(snapshot_raw)
                event_ns = captured_ns
                if derived_time:
                    # 同一 boot 的 epoch 换算基线只写一次，消除每个工作者
                    # 读取两个时钟造成的微小偏差，保留同 tick 原生因果顺序。
                    key = "boot_epoch:" + boot_id
                    epoch = self._get(key)
                    if not epoch:
                        epoch = str(time.time_ns() - int(boot_clock()[1] * 1_000_000_000))
                        self._put(key, epoch)
                    event_ns = int(epoch) + round(float(captured_uptime) * 1_000_000_000)
                clear = self._json("clear", {})
                old = False
                if clear:
                    old = self._hook_before_fence({"token": token, "uptime": captured_uptime, "boot": boot_id, "captured_ns": event_ns}, clear)
                self.db.execute("INSERT INTO hooks VALUES (?,?,?,?,?,?,?,?,?)", (token, event, event_ns, boot_id, captured_uptime, session, json.dumps(log_anchor or {}, sort_keys=True), json.dumps(snapshot_fields(snapshot_raw), sort_keys=True), int(old)))
                self._put("capture_initialized", "1")
                if not old:
                    self._rebuild_hook_cycles()
            return None
        return self._call(action)

    @staticmethod
    def _hook_before_fence(hook, fence):
        if hook["uptime"] is not None and hook["boot"] == fence.get("boot"):
            token = hook["token"].split(":")
            if fence.get("birth_ticks") and len(token) >= 4 and token[-2].isdigit() and token[-3].isdigit() and int(token[-2]) > 0:
                birth = int(token[-2])
                if birth != fence["birth_ticks"]:
                    return birth < fence["birth_ticks"]
                # 同 tick 的 PID 不同仍是独立调用，不以相等的低精度 uptime 删除。
                return int(token[-3]) <= fence["pid"]
            return float(hook["uptime"]) <= fence["uptime"]
        if hook["boot"] in fence.get("prior_boots", []):
            return True
        return hook["captured_ns"] <= fence["wall_ns"]

    def clear(self, snapshot_raw="", captured_ns=None, captured_uptime=None, boot_id="", clear_pid=0, clear_birth=0):
        # 请求发生时采集边界，而不是事务取得写锁以后。
        boot, uptime = boot_clock()
        if captured_ns is None:
            captured_ns = time.time_ns()
            if captured_uptime is not None and (not boot_id or boot_id == boot):
                captured_ns -= int((uptime-float(captured_uptime))*1_000_000_000)
        request_boot = boot_id or boot
        request_uptime = float(captured_uptime) if captured_uptime is not None else uptime
        def action():
            with self._transaction():
                # Consume existing input and advance each cursor in the same transaction
                # as the clear. Raw byte identities remain tombstones for replay/rotation.
                self._sync_locked(snapshot_raw)
                values = snapshot_fields(snapshot_raw)
                fence = {"wall_ns": int(captured_ns), "boot": request_boot, "uptime": request_uptime,
                         "pid": clear_pid, "birth_ticks": clear_birth,
                         "last_xon_ns": parse_time(values.get("XONBATT", "")) or 0, "snapshot_session": values.get("STARTTIME", ""),
                         "prior_boots": [row[0] for row in self.db.execute("SELECT DISTINCT boot FROM hooks WHERE boot<>?", (request_boot,))]}
                self._put_json("clear", fence)
                self.db.execute("UPDATE cycles SET visible=0 WHERE visible IN (1,2) AND origin<>'hook' AND start_ns<=?", (fence["wall_ns"],))
                for hook in self.db.execute("SELECT c.id,h.* FROM cycles c JOIN hooks h ON c.id='hook:'||h.token WHERE c.visible IN (1,2)").fetchall():
                    before = self._hook_before_fence(hook, fence)
                    if before:
                        self.db.execute("UPDATE cycles SET visible=0 WHERE id=?", (hook["id"],))
                for hook in self.db.execute("SELECT * FROM hooks WHERE suppressed=0").fetchall():
                    if self._hook_before_fence(hook, fence):
                        self.db.execute("UPDATE hooks SET suppressed=1 WHERE token=?", (hook["token"],))
                self._classify_hook_cycles()
                current = self._get("current_cycle")
                if current and self.db.execute("SELECT visible FROM cycles WHERE id=?", (current,)).fetchone()[0] == 0:
                    self._put("current_cycle", "")
                self._put("migration_notice", "")
            return None
        return self._call(action)

    def _notice(self):
        try:
            notices = [self._get("migration_notice"), self._get("input_notice"), self._get("boundary_notice")]
        except sqlite3.DatabaseError:
            notices = ["历史数据库异常；原文件已保留，未覆盖。"]
        try:
            report = json.loads(self.error_path.read_text(encoding="utf-8"))
            if report.get("recovered"):
                notices.append("上次历史同步失败（已恢复）：" + clean(report.get("error")) + "；详见诊断。")
            else:
                notices.append("历史同步失败：" + clean(report.get("error")) + "；现有记录已保留，请查看诊断。")
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            notices.append("历史错误记录无法读取，请查看目录权限及诊断。")
        try:
            pending = self.db.execute("SELECT COUNT(*) FROM cycles WHERE visible=2").fetchone()[0]
            if pending:
                notices.append(f"有 {pending} 份证据待核查：跨来源身份或清空边界无法确认，不计入已确认切换次数；详情及原始调用见诊断和 history --json。")
        except sqlite3.DatabaseError:
            pass
        return " ".join(item for item in notices if item)

    def query(self, page=1, size=0):
        page, size = max(1, int(page)), max(0, int(size))
        current = self._get("current_cycle")
        summary = self.db.execute("SELECT COUNT(*) AS count,COALESCE(SUM(duration),0) AS total_seconds,COALESCE(SUM(CASE WHEN end_ns IS NULL AND id=? THEN 1 ELSE 0 END),0) AS current,COALESCE(SUM(CASE WHEN end_ns IS NULL AND id<>? THEN 1 ELSE 0 END),0) AS unknown FROM cycles WHERE visible=1", (current, current)).fetchone()
        summary = dict(summary)
        summary["unassociated"] = self.db.execute("SELECT COUNT(*) FROM cycles WHERE visible=2").fetchone()[0]
        summary["pages"] = max(1, math.ceil(summary["count"] / size)) if size else 1
        page = min(page, summary["pages"])
        sql = "SELECT * FROM cycles WHERE visible=1 ORDER BY start_ns DESC,id DESC"
        parameters = ()
        if size:
            sql += " LIMIT ? OFFSET ?"
            parameters = (size, (page-1)*size)
        rows = []
        for record in self.db.execute(sql, parameters):
            row = dict(record)
            row["state"] = "restored" if row["end_ns"] is not None else ("active" if row["id"] == current else "unknown")
            row["evidence"] = clean(row["source"]) + " / " + row["origin"]
            rows.append(row)
        pending = [dict(row) for row in self.db.execute("SELECT * FROM cycles WHERE visible=2 ORDER BY start_ns DESC,id DESC" + (" LIMIT ? OFFSET ?" if size else ""), (size, (page-1)*size) if size else ())]
        return {"summary": summary, "rows": rows, "pending": pending, "notice": self._notice(), "page": page}

    def pages(self, size):
        size = max(1, int(size))
        count = self.db.execute("SELECT COUNT(*) FROM cycles WHERE visible=1").fetchone()[0]
        return max(1, math.ceil(count/size))

    def diagnostics(self):
        notice = self._notice()
        info = [f"历史数据库：{self.database_path}", f"原生事件日志：{self.events_path}", f"兼容历史文件：{self.legacy_path}（保留原文件）"]
        if self.error_path.exists():
            try:
                report = json.loads(self.error_path.read_text(encoding="utf-8"))
                info += ["上次失败时间："+clean(report.get("time")), "上次失败原因："+clean(report.get("error"))]
                if report.get("recovered"):
                    info.append("恢复时间："+clean(report["recovered"]))
            except (OSError, ValueError):
                info.append("错误记录格式异常，请检查 history-error.json。")
        info.append(notice or "历史模块：未记录同步错误。")
        try:
            unresolved = self.db.execute("SELECT COUNT(*) FROM hooks WHERE suppressed=0").fetchone()[0]
            info.append(f"持久保存的原生回调证据：{unresolved} 条（回调调度顺序不代替原生日志顺序）")
            for row in self.db.execute("SELECT * FROM cycles WHERE visible=2 ORDER BY start_ns"):
                info.append(f"待核查证据：{row['start']} → {row['end'] or '恢复时间未知'}；{row['reason_cn']}；证据 {row['id']}")
            for row in self.db.execute("SELECT token,event,session,captured_ns,suppressed FROM hooks ORDER BY captured_ns"):
                info.append(f"原始回调：{row['token']}；{row['event']}；{stamp_from_ns(row['captured_ns'])}；会话 {row['session']}；清空前证据 {row['suppressed']}")
        except sqlite3.DatabaseError:
            info.append("历史数据库暂时无法读取；请先保留损坏文件，再进行修复。")
        return "\n".join(info)


def output_tsv(data):
    summary = data["summary"]
    print("\t".join(map(str, ("summary", summary["count"], summary["total_seconds"], summary["current"], summary["unknown"], summary["pages"], summary["unassociated"]))))
    for row in data["rows"]:
        values = ("row", row["id"], row["start"], row["end"] or "-", row["duration"] if row["duration"] is not None else "-", row["reason_cn"], row["reason_raw"] or "-", row["state"], row["evidence"])
        print("\t".join(clean(value) for value in values))
    if data["notice"]:
        print("notice\t"+clean(data["notice"]))


def main():
    if sys.argv[1:] == ["clock"]:
        # 清空专用的因果边界。无状态目录操作、无 UPS 查询或服务操作。
        boot, _ = boot_clock()
        birth = int(Path("/proc/self/stat").read_text().rpartition(") ")[2].split()[19])
        print(f"{boot}\t{birth / os.sysconf('SC_CLK_TCK')}\t{os.getpid()}\t{birth}")
        return 0
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--events", required=True)
    parser.add_argument("--legacy")
    parser.add_argument("command", choices=("sync", "capture", "clear", "query", "pages", "diagnostics"))
    parser.add_argument("--event")
    parser.add_argument("--captured-ns", type=int)
    parser.add_argument("--captured-uptime", type=float)
    parser.add_argument("--boot-id", default="")
    parser.add_argument("--token")
    parser.add_argument("--session", default="")
    parser.add_argument("--log-dev", type=int)
    parser.add_argument("--log-ino", type=int)
    parser.add_argument("--log-offset", type=int)
    parser.add_argument("--clear-pid", type=int, default=0)
    parser.add_argument("--clear-birth", type=int, default=0)
    parser.add_argument("--page", type=int, default=1)
    parser.add_argument("--size", type=int, default=0)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    engine = None
    try:
        engine = Engine(args.state_dir, args.events, args.legacy)
        raw = sys.stdin.read() if args.command in ("sync", "capture", "clear") else ""
        if args.command == "sync":
            engine.sync(raw)
        elif args.command == "capture":
            anchor = {"dev": args.log_dev, "ino": args.log_ino, "offset": args.log_offset} if args.log_ino is not None else None
            engine.capture(args.event, args.captured_ns, args.token, args.session, anchor, raw, args.captured_uptime, args.boot_id)
        elif args.command == "clear":
            engine.clear(raw, args.captured_ns, args.captured_uptime, args.boot_id, args.clear_pid, args.clear_birth)
        elif args.command == "pages":
            print(engine.pages(args.size))
        elif args.command == "diagnostics":
            print(engine.diagnostics())
        else:
            data = engine.query(args.page, args.size)
            if args.json:
                data["proof"] = {table: [dict(row) for row in engine.db.execute("SELECT * FROM " + table)] for table in ("evidence", "hooks", "snapshots", "claims", "source_links", "hook_members")}
                print(json.dumps(data, ensure_ascii=False))
            else:
                output_tsv(data)
        return 0
    except Exception:
        return 1
    finally:
        if engine:
            engine.close()


if __name__ == "__main__":
    sys.exit(main())
