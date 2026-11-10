# -*- coding: utf-8 -*-
"""后台任务看门狗：进程结束后必须主动起一轮汇报（用户 2026-11-10 的诉求）。"""
import time
from pathlib import Path
from types import SimpleNamespace

from webapp.bgwatch import BackgroundWatcher


class FakeSession:
    def __init__(self, sid="s1", run=None):
        self.id = sid
        self.bg_tasks = {}
        self.run = run
        self.msgs = []
        self.saved = 0

    def begin_run(self, msg, prefix=0):
        self.msgs.append((msg, prefix))
        return object()

    def run_turn(self, run):
        pass

    def save_bg_tasks(self):
        self.saved += 1


def _task(tmp_path, pid=None, started=None):
    d = tmp_path / "t1"
    d.mkdir(exist_ok=True)
    log = d / "output.log"
    log.write_text("hi", encoding="utf-8")
    pidf = d / "pid"
    if pid is not None:
        pidf.write_text(str(pid), encoding="utf-8")
    return {"task_id": "t1", "log": str(log), "pid_file": str(pidf),
            "cmd": "sleep 1", "started": time.time() - 10 if started is None else started,
            "notified": False}


def test_finished_task_triggers_proactive_turn(tmp_path, monkeypatch):
    s = FakeSession()
    s.bg_tasks["t1"] = _task(tmp_path, pid=123456)
    w = BackgroundWatcher(interval=3600)
    monkeypatch.setattr(w, "_pid_alive", lambda pid, t: False)
    w._scan_session(s)
    assert s.msgs, "进程结束却没有起主动汇报"
    msg = s.msgs[0][0]
    assert "[系统]" in msg and "t1" in msg and "output.log" in msg
    assert s.bg_tasks["t1"]["notified"] is True
    assert s.saved >= 1
    w._scan_session(s)                      # 不重复通知
    assert len(s.msgs) == 1


def test_alive_pid_waits(tmp_path, monkeypatch):
    s = FakeSession()
    s.bg_tasks["t1"] = _task(tmp_path, pid=123456)
    w = BackgroundWatcher(interval=3600)
    monkeypatch.setattr(w, "_pid_alive", lambda pid, t: True)
    w._scan_session(s)
    assert not s.msgs and s.bg_tasks["t1"]["notified"] is False


def test_busy_session_defers(tmp_path, monkeypatch):
    s = FakeSession(run=SimpleNamespace(running=True))
    s.bg_tasks["t1"] = _task(tmp_path, pid=123456)
    w = BackgroundWatcher(interval=3600)
    monkeypatch.setattr(w, "_pid_alive", lambda pid, t: False)
    w._scan_session(s)
    assert not s.msgs and s.bg_tasks["t1"]["notified"] is False


def test_missing_pid_after_grace(tmp_path):
    s = FakeSession()
    s.bg_tasks["t1"] = _task(tmp_path, pid=None, started=time.time() - 20)
    w = BackgroundWatcher(interval=3600)
    w._scan_session(s)
    assert s.msgs and "未找到有效 pid" in s.msgs[0][0]


def test_young_task_waits(tmp_path):
    s = FakeSession()
    s.bg_tasks["t1"] = _task(tmp_path, pid=None, started=time.time())
    w = BackgroundWatcher(interval=3600)
    w._scan_session(s)
    assert not s.msgs


def test_bg_tasks_persist(tmp_path, monkeypatch):
    from webapp.session import WebSession

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    cfg = SimpleNamespace(model="m", permission="auto")
    s = WebSession("s1", cfg, None, None, None)
    s.register_bg_task({"task_id": "t1", "log": "/tmp/l", "pid_file": "/tmp/p", "cmd": "x"})
    s2 = WebSession("s1", cfg, None, None, None)
    s2.load_bg_tasks()
    assert "t1" in s2.bg_tasks
    assert s2.bg_tasks["t1"]["notified"] is False
