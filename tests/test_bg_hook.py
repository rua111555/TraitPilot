# -*- coding: utf-8 -*-
"""Bash background=true 必须把任务登记给会话（看门狗据此主动汇报）。"""
import time
from pathlib import Path

from skill_agent import tools as tools_mod
from skill_agent.skills import SkillRegistry


def test_bash_background_reports_to_session(tmp_path, monkeypatch):
    monkeypatch.setattr(tools_mod, "BG_ROOT", tmp_path / "tasks")
    reg = SkillRegistry(Path("/nonexistent-project"))
    ts = tools_mod.ToolSet(reg, Path(tmp_path))
    seen = []
    ts.on_bg_start = seen.append
    r = ts.execute("Bash", {"command": "sleep 0.2; echo hi", "background": True})
    assert not r.is_error, r.content
    assert seen, "后台启动后没有回调 on_bg_start"
    meta = seen[0]
    assert meta["task_id"] == r.meta["task_id"]
    pidf = Path(meta["pid_file"])
    for _ in range(50):
        if pidf.exists():
            break
        time.sleep(0.05)
    assert pidf.exists(), "后台脚本没有写 pid 文件"
