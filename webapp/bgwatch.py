"""后台任务看门狗：Bash background=true 的进程结束后，主动拉起一轮汇报。

用户诉求（2026-11-10）：「你没有阻塞等待，但是结束后似乎也没有返回……
应该是定时去检查 .done 是否完成，然后主动回复。」

机制：
- ``Bash background=true`` 启动后经 ``ToolSet.on_bg_start`` 把任务登记到 WebSession；
- 看门狗每 N 秒检查 ``pid`` 文件对应的进程是否还活着（``/proc`` 的 starttime 防 pid 复用）；
- 进程结束且会话空闲 → ``begin_run("[系统] ...")`` + ``run_turn``，主动汇报；
- 会话正在跑 → 等下一轮；已通知的任务落盘，服务重启不重复。
"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path

DEFAULT_INTERVAL = float(os.environ.get("SKILL_AGENT_BG_WATCH", "5") or 5)
MIN_AGE = 3.0          # 给 pid 文件一点写入时间
NO_PID_GRACE = 15.0    # 超过这么久还没有 pid 文件 → 视为启动失败


class BackgroundWatcher:
    def __init__(self, interval: float | None = None):
        self.interval = float(interval if interval is not None else DEFAULT_INTERVAL)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, name="bg-watch", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.scan_once()
            except Exception:
                pass

    def scan_once(self) -> None:
        from . import server
        with server.CFG_REF["lock"]:
            sessions = list(server.CFG_REF["sessions"].values())
        for s in sessions:
            try:
                self._scan_session(s)
            except Exception:
                continue

    def _scan_session(self, s) -> None:
        tasks = getattr(s, "bg_tasks", None)
        if not tasks:
            return
        finished = []
        for tid, t in list(tasks.items()):
            if t.get("notified"):
                continue
            status = self._status(t)
            if status is not None:
                finished.append((tid, t, status))
        if not finished:
            return
        run = getattr(s, "run", None)
        if run is not None and getattr(run, "running", False):
            return
        msg = self._message(finished)
        try:
            from . import server
            prefix = server._disk_prefix(s.id)
        except Exception:
            prefix = 0
        new_run = s.begin_run(msg, prefix=prefix)
        if new_run is None:
            return
        for tid, t, status in finished:
            t["notified"] = True
            t["result"] = status
        try:
            s.save_bg_tasks()
        except Exception:
            pass
        threading.Thread(target=s.run_turn, args=(new_run,), daemon=True).start()

    def _status(self, t: dict):
        started = float(t.get("started") or 0)
        if time.time() - started < MIN_AGE:
            return None
        pid_file = t.get("pid_file") or ""
        p = Path(pid_file) if pid_file else None
        if p is None or not p.is_file():
            if time.time() - started > NO_PID_GRACE:
                return "no_pid"
            return None
        try:
            pid = int(p.read_text(encoding="utf-8").strip())
        except Exception:
            return "bad_pid"
        if self._pid_alive(pid, t):
            return None
        return "exited"

    def _pid_alive(self, pid: int, t: dict) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
        # Linux: 用 /proc/<pid>/stat 的 starttime 防 pid 复用；状态 Z 视为已退出
        try:
            raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
            rest = raw.rsplit(")", 1)[1].split()
            if rest and rest[0] == "Z":
                return False
            start = rest[19] if len(rest) > 19 else ""
            old = t.get("pid_start")
            if old and start and old != start:
                return False
            if start:
                t["pid_start"] = start
        except Exception:
            pass
        return True

    @staticmethod
    def _message(finished) -> str:
        lines = ["[系统] 后台任务已结束（看门狗自动检查，不需要用户再发消息）："]
        for tid, t, status in finished:
            log = t.get("log") or "(无日志)"
            if status == "exited":
                lines.append(f"- {tid}：进程已退出。日志：{log}")
            else:
                lines.append(f"- {tid}：未找到有效 pid（{status}），可能启动失败。日志：{log}")
        lines.append("请用 TaskOutput/Read 查看日志末尾，并检查相关 .done/.verify 标记，"
                     "向用户汇报结果；不要重复执行该任务。")
        return "\n".join(lines)


def start_watcher() -> BackgroundWatcher:
    w = BackgroundWatcher()
    w.start()
    return w
