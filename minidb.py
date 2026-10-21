#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MiniDB —— 带 TTL / 事务 / WAL 持久化的内存键值数据库(纯标准库实现)。"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple


# --------------------------------------------------------------------------- #
# 1. 异常体系
# --------------------------------------------------------------------------- #
class MiniDBError(Exception):
    """MiniDB 所有异常的基类。"""


class KeyNotFound(MiniDBError):
    """访问一个不存在的键。"""


class TypeMismatch(MiniDBError):
    """对非数值类型执行 INCR/DECR。"""


class TransactionError(MiniDBError):
    """事务嵌套非法或提交失败。"""


# --------------------------------------------------------------------------- #
# 2. 记录:值 + 可选过期时间戳
# --------------------------------------------------------------------------- #
@dataclass
class Record:
    value: Any
    expire_at: Optional[float] = None

    def expired(self, now: Optional[float] = None) -> bool:
        if self.expire_at is None:
            return False
        return (now if now is not None else time.time()) >= self.expire_at

    def to_json(self) -> Dict[str, Any]:
        return {"value": self.value, "expire_at": self.expire_at}

    @classmethod
    def from_json(cls, raw: Dict[str, Any]) -> "Record":
        return cls(value=raw.get("value"), expire_at=raw.get("expire_at"))


# --------------------------------------------------------------------------- #
# 3. 预写日志:先落盘再改内存,崩溃后可重放
# --------------------------------------------------------------------------- #
class WriteAheadLog:
    def __init__(self, path: Path, fsync: bool = False) -> None:
        self.path = Path(path)
        self.fsync = fsync
        self._fh = None

    def open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "a", encoding="utf-8")

    def append(self, op: str, key: str, record: Optional[Record]) -> None:
        if self._fh is None:
            return
        payload = {"op": op, "key": key,
                   "record": record.to_json() if record else None}
        self._fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self._fh.flush()
        if self.fsync:
            os.fsync(self._fh.fileno())

    def replay(self) -> Iterator[Tuple[str, str, Optional[Record]]]:
        if not self.path.exists():
            return
        with open(self.path, "r", encoding="utf-8") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    obj = json.loads(raw)
                except json.JSONDecodeError:
                    break
                rec = obj.get("record")
                yield obj["op"], obj["key"], (Record.from_json(rec) if rec else None)

    def truncate(self) -> None:
        self.close()
        if self.path.exists():
            self.path.unlink()

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None


# --------------------------------------------------------------------------- #
# 4. 核心:带锁的键空间 + 事务 + 淘汰
# --------------------------------------------------------------------------- #
class MiniDB:
    def __init__(self, data_dir: Optional[str] = None, fsync: bool = False) -> None:
        self._data: Dict[str, Record] = {}
        self._lock = threading.RLock()
        self._tx_stack: List[Dict[str, Optional[Record]]] = []
        self._watchers: Dict[str, List[Callable[[str, Any], None]]] = {}
        self.stats: Dict[str, int] = {"get": 0, "set": 0, "del": 0, "expired": 0}
        self.snapshot_path = Path(data_dir) / "snapshot.json" if data_dir else None
        self.wal = WriteAheadLog(Path(data_dir) / "wal.log", fsync) if data_dir else None
        if self.wal is not None:
            self.wal.open()
        self._recover()

    def _recover(self) -> None:
        if self.snapshot_path is not None and self.snapshot_path.exists():
            with open(self.snapshot_path, "r", encoding="utf-8") as fh:
                for key, raw in json.load(fh).items():
                    self._data[key] = Record.from_json(raw)
        if self.wal is not None:
            for op, key, rec in self.wal.replay():
                if op == "set":
                    self._data[key] = rec
                elif op == "del":
                    self._data.pop(key, None)

    def _purge_if_expired(self, key: str, now: float) -> bool:
        if self._tx_stack:
            return False
        rec = self._data.get(key)
        if rec is not None and rec.expired(now):
            self._data.pop(key, None)
            self.stats["expired"] += 1
            return True
        return False

    def _write(self, op: str, key: str, record: Optional[Record]) -> None:
        if self._tx_stack:
            self._tx_stack[-1][key] = record
            return
        if record is None:
            self._data.pop(key, None)
        else:
            self._data[key] = record
        if self.wal is not None:
            self.wal.append(op, key, record)
        self._notify(key, record.value if record else None)

    def _notify(self, key: str, value: Any) -> None:
        for cb in self._watchers.get(key, []):
            try:
                cb(key, value)
            except Exception:
                pass

    def watch(self, key: str, callback: Callable[[str, Any], None]) -> None:
        self._watchers.setdefault(key, []).append(callback)

    def get(self, key: str, default: Any = None) -> Any:
        now = time.time()
        with self._lock:
            self.stats["get"] += 1
            self._purge_if_expired(key, now)
            rec = self._data.get(key)
            return default if rec is None else rec.value

    def set(self, key: str, value: Any, ttl: Optional[float] = None) -> None:
        expire_at = None if ttl is None else time.time() + ttl
        with self._lock:
            self.stats["set"] += 1
            self._write("set", key, Record(value=value, expire_at=expire_at))

    def delete(self, key: str) -> bool:
        with self._lock:
            self.stats["del"] += 1
            existed = key in self._data
            if existed:
                self._write("del", key, None)
            return existed

    def incr(self, key: str, delta: int = 1) -> int:
        with self._lock:
            cur = self.get(key, 0)
            if not isinstance(cur, (int, float)) or isinstance(cur, bool):
                raise TypeMismatch(f"键 '{key}' 的值不是数值: {cur!r}")
            new = cur + delta
            old = self._data.get(key)
            ttl_left = None
            if old is not None and old.expire_at is not None:
                ttl_left = max(0.0, old.expire_at - time.time())
            self.set(key, new, ttl_left)
            return new

    def expire(self, key: str, ttl: float) -> bool:
        with self._lock:
            rec = self._data.get(key)
            if rec is None or rec.expired():
                return False
            self._write("set", key, Record(rec.value, time.time() + ttl))
            return True

    def ttl(self, key: str) -> Optional[float]:
        with self._lock:
            rec = self._data.get(key)
            if rec is None or rec.expired():
                return None
            if rec.expire_at is None:
                return -1.0
            return rec.expire_at - time.time()

    def keys(self, pattern: str = "*") -> List[str]:
        now = time.time()
        with self._lock:
            alive = [k for k in self._data if not self._purge_if_expired(k, now)]
            if pattern == "*":
                return sorted(alive)
            prefix = pattern.rstrip("*")
            return sorted(k for k in alive if k.startswith(prefix))

    def sweep(self) -> int:
        now = time.time()
        with self._lock:
            dead = [k for k, r in self._data.items() if r.expired(now)]
            for k in dead:
                self._data.pop(k, None)
                self.stats["expired"] += 1
            return len(dead)

    @contextmanager
    def transaction(self) -> Iterator["MiniDB"]:
        with self._lock:
            depth = len(self._tx_stack)
            if depth >= 8:
                raise TransactionError("事务嵌套过深(>8)")
            self._tx_stack.append({})
        try:
            yield self
        except Exception:
            with self._lock:
                self._tx_stack.pop()
            raise
        else:
            with self._lock:
                pending = self._tx_stack.pop()
                for key, record in pending.items():
                    if record is None:
                        self._data.pop(key, None)
                    else:
                        self._data[key] = record
                    if self.wal is not None:
                        self.wal.append("set" if record else "del", key, record)
                    self._notify(key, record.value if record else None)

    def snapshot(self) -> int:
        if self.snapshot_path is None:
            return 0
        with self._lock:
            self.sweep()
            payload = {k: r.to_json() for k, r in self._data.items()}
        tmp = self.snapshot_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.snapshot_path)
        if self.wal is not None:
            self.wal.truncate()
            self.wal.open()
        return len(payload)

    def flush(self) -> None:
        with self._lock:
            self._data.clear()
            if self.wal is not None:
                self.wal.truncate()
                self.wal.open()

    def close(self) -> None:
        self.snapshot()
        if self.wal is not None:
            self.wal.close()


# --------------------------------------------------------------------------- #
# 5. 命令行 / REPL
# --------------------------------------------------------------------------- #
def _coerce(text: str) -> Any:
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        return text


def run_command(db: MiniDB, line: str) -> str:
    parts = shlex.split(line)
    if not parts:
        return ""
    cmd, args = parts[0].upper(), parts[1:]
    if cmd == "GET":
        val = db.get(args[0], default="<nil>")
        return f"{val!r}"
    if cmd == "SET":
        ttl = None
        if len(args) > 3 and args[2].upper() == "EX":
            ttl = float(args[3])
        db.set(args[0], _coerce(args[1]), ttl)
        return "OK"
    if cmd == "DEL":
        return "1" if db.delete(args[0]) else "0"
    if cmd == "INCR":
        return str(db.incr(args[0], int(args[1]) if len(args) > 1 else 1))
    if cmd == "EXPIRE":
        return "1" if db.expire(args[0], float(args[1])) else "0"
    if cmd == "TTL":
        v = db.ttl(args[0])
        return "<nil>" if v is None else ("-1" if v < 0 else f"{v:.1f}")
    if cmd == "KEYS":
        return " ".join(db.keys(args[0] if args else "*"))
    if cmd == "STATS":
        return json.dumps(db.stats, ensure_ascii=False)
    if cmd == "SAVE":
        return f"snapshot saved: {db.snapshot()} keys"
    if cmd == "FLUSH":
        db.flush()
        return "OK"
    return f"(error) 未知命令: {cmd}"


def repl(db: MiniDB) -> None:
    print("MiniDB REPL，输入 HELP 查看命令，QUIT 退出。")
    while True:
        try:
            line = input("minidb> ")
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if line.strip().upper() in ("QUIT", "EXIT"):
            break
        try:
            out = run_command(db, line)
        except (IndexError, ValueError) as exc:
            out = f"(error) 参数错误: {exc}"
        except MiniDBError as exc:
            out = f"(error) {exc}"
        if out:
            print(out)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="MiniDB: TTL/事务/WAL 内存键值库")
    ap.add_argument("--data-dir", default=None, help="持久化目录,缺省则纯内存")
    ap.add_argument("--fsync", action="store_true", help="每条写入强制刷盘")
    ap.add_argument("cmd", nargs="*", help="直接执行一条命令后退出")
    ns = ap.parse_args(argv)
    db = MiniDB(data_dir=ns.data_dir, fsync=ns.fsync)
    try:
        if ns.cmd:
            print(run_command(db, " ".join(ns.cmd)))
        else:
            repl(db)
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
