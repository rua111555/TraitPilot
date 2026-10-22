"""一轮运行（Run）的状态容器：把「运行态 / 事件缓冲 / 一个 HTTP 响应」三件事拆开。

改造前的根因：三者同生命周期 —— SSE 响应一断就顺手 cancel + 复位 busy，而 agent 线程
还在跑；事件队列又是**每会话共享且破坏式 get()**，于是下一轮流先领到上一轮的残留事件。

这里把「一轮」升格为对象，由 agent 线程独占拥有：
- 只有 Run 的终结（线程 finally）与 /api/cancel 能改 state，SSE 断开只能退订
- 事件进**追加式日志**（带 seq），订阅者各持游标做非破坏式读取，可多订阅、可回放
- 进度快照让「切回来」的客户端能立刻看到这一轮在做什么
"""
from __future__ import annotations

import threading
import time
import uuid

# 状态机
RUNNING = "running"
CANCELLING = "cancelling"
DONE = "done"
ERROR = "error"
CANCELLED = "cancelled"
TERMINAL = (DONE, ERROR, CANCELLED)

LOG_MAX_EVENTS = 4000          # 事件日志条数上限
LOG_MAX_BYTES = 2 * 1024 * 1024    # 事件日志字节上限（按整事件淘汰）
SUB_QUEUE_MAX = 512            # 单个订阅者的待发队列上限


class Subscriber:
    """一个订阅者：自己持有游标，从 Run 的日志里非破坏式拉取。"""

    def __init__(self, run: "Run", after: int = 0):
        self.run = run
        self.cursor = after
        self.queue: list[dict] = []
        self.dropped = 0
        self.alive = True
        self._wake = threading.Event()
        self._lock = threading.Lock()

    def notify(self) -> None:
        self._wake.set()

    def close(self) -> None:
        self.alive = False
        self._wake.set()

    def next_event(self, timeout: float = 15.0):
        """取下一个该发的事件。返回 None 表示超时（调用方发心跳）。"""
        deadline = time.monotonic() + timeout
        while True:
            if not self.alive:
                return None
            ev = self.run.pull(self)
            if ev is not None:
                return ev
            if self.run.state in TERMINAL and self.cursor > self.run.last_seq:
                return None
            remain = deadline - time.monotonic()
            if remain <= 0:
                return None
            self._wake.wait(timeout=min(remain, 1.0))
            self._wake.clear()


class Run:
    """一轮运行。agent 线程是唯一写者；HTTP 侧只读。"""

    def __init__(self, session_id: str, prompt: str, turn_index: int = 0):
        self.run_id = uuid.uuid4().hex[:12]
        self.session_id = session_id
        self.prompt = prompt
        self.turn_index = turn_index
        self.state = RUNNING
        self.started_at = time.time()
        self.ended_at: float | None = None
        self.cancel_event = threading.Event()

        self._log: list[dict] = []       # 追加式事件日志（seq 从 1 起）
        self._log_bytes = 0
        self.truncated_before = 0        # 已被淘汰的最旧 seq 水位
        self.last_seq = 0
        self._lock = threading.Lock()    # 保护 _log / seq / state / progress
        self._subs: list[Subscriber] = []

        # 进度快照：回答「这一轮正在做什么」
        self.progress: dict = {"step": 0, "phase": "model", "tool": None,
                               "last_event_at": time.time(), "hint": ""}
        self.partial_text: list[str] = []    # 本轮已产出的正文（供切回来重建）
        self.partial_chars = 0

        self.pending_confirms: dict[str, dict] = {}
        self.final: str | None = None
        self.error: str | None = None
        self.result: dict = {}
        # prefix：本轮开始前该会话磁盘上已完成的条数（由 server 侧算好填）
        self.prefix = 0
        self.resume_discarded = 0

    # ---------- 事件日志 ----------
    def emit(self, ev: dict) -> int:
        """唯一写入口。任何 emit 都不因「无人订阅」而丢弃。"""
        with self._lock:
            self.last_seq += 1
            seq = self.last_seq
            rec = dict(ev)
            rec["seq"] = seq
            rec["run"] = self.run_id
            self._log.append(rec)
            self._log_bytes += len(str(rec))
            self._evict_locked()
            self._note_progress_locked(rec)
            for s in self._subs:
                s.notify()
            return seq

    def _evict_locked(self) -> None:
        """按条数/字节双上限淘汰最旧事件（整事件淘汰，抬高 truncated_before）。"""
        while self._log and (len(self._log) > LOG_MAX_EVENTS
                             or self._log_bytes > LOG_MAX_BYTES):
            old = self._log.pop(0)
            self._log_bytes -= len(str(old))
            self.truncated_before = old["seq"]

    def _note_progress_locked(self, rec: dict) -> None:
        """从流过的事件顺手更新进度快照 —— 「在跑什么」的唯一权威来源。"""
        p = self.progress
        p["last_event_at"] = time.time()
        t = rec.get("type")
        if t == "text":
            d = rec.get("delta") or ""
            self.partial_text.append(d)
            self.partial_chars += len(d)
            # 只留尾部，避免快照随轮次长度无限增长
            while self.partial_chars > 16000 and len(self.partial_text) > 1:
                self.partial_chars -= len(self.partial_text.pop(0))
        elif t == "tool_start":
            p["phase"] = "tool"
            p["tool"] = {"id": rec.get("id"), "name": rec.get("name"),
                         "started_at": time.time()}
            raw = rec.get("input") or ""
            try:
                import json as _j
                d = _j.loads(raw) if isinstance(raw, str) else raw
                p["hint"] = str(d.get("command") or d.get("file_path")
                                or d.get("pattern") or "")[:120]
            except Exception:
                p["hint"] = str(raw)[:120]
        elif t == "tool_end":
            p["phase"] = "model"
            p["tool"] = None
            p["hint"] = ""
        elif t == "usage":
            p["step"] = rec.get("step") or p["step"]
            p["phase"] = "model"

    def pull(self, sub: Subscriber) -> dict | None:
        """订阅者按游标取下一个事件；没有新的返回 None（非破坏式）。"""
        with self._lock:
            nxt = None
            for rec in self._log:
                if rec["seq"] > sub.cursor:
                    nxt = rec
                    break
            if nxt is None:
                return None
            sub.cursor = nxt["seq"]
            return nxt

    def subscribe(self, after: int = 0) -> Subscriber:
        with self._lock:
            s = Subscriber(self, after=max(0, after))
            self._subs.append(s)
            return s

    def unsubscribe(self, sub: Subscriber) -> None:
        with self._lock:
            if sub in self._subs:
                self._subs.remove(sub)
        sub.close()

    # ---------- 状态迁移（只有 agent 线程与 cancel 能调） ----------
    def finish(self, state: str, *, final: str | None = None,
               error: str | None = None, result: dict | None = None) -> None:
        with self._lock:
            if self.state in TERMINAL:
                return
            self.state = state
            self.ended_at = time.time()
            self.final = final
            self.error = error
            if result:
                self.result = result
            subs = list(self._subs)
        for s in subs:
            s.notify()

    def mark_cancelling(self) -> None:
        with self._lock:
            if self.state in TERMINAL:
                return
            self.state = CANCELLING
        self.cancel_event.set()

    @property
    def running(self) -> bool:
        return self.state not in TERMINAL

    # ---------- 快照 ----------
    def snapshot(self) -> dict:
        with self._lock:
            p = dict(self.progress)
            tail = "".join(self.partial_text)[-4000:]
            pend = None
            for uid, d in self.pending_confirms.items():
                pend = {"uid": uid, "command": d.get("command", ""),
                        "reason": d.get("reason", "")}
                break
            return {
                "run": self.run_id, "session": self.session_id,
                "state": self.state, "running": self.state not in TERMINAL,
                "prompt": self.prompt, "turn_index": self.turn_index,
                "started_at": self.started_at,
                "elapsed": round((self.ended_at or time.time()) - self.started_at, 1),
                "last_seq": self.last_seq, "truncated_before": self.truncated_before,
                "progress": p, "pending_confirm": pend,
                "partial_text": tail, "partial_chars": self.partial_chars,
                "prefix": self.prefix, "resume_discarded": self.resume_discarded,
                "final": self.final, "error": self.error,
                "subscribers": len(self._subs),
            }
