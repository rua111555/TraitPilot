# -*- coding: utf-8 -*-
"""后台任务启动后，本轮禁止 sleep 阻塞（否则整个会话被锁住，用户无法继续对话）。"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pathlib import Path  # noqa: E402

from skill_agent.skills import SkillRegistry  # noqa: E402
from skill_agent.tools import ToolSet  # noqa: E402


def _ts(cwd):
    return ToolSet(SkillRegistry(Path("/nonexistent-project")), Path(cwd))


def test_sleep_after_bg_start_is_refused():
    with tempfile.TemporaryDirectory() as d:
        ts = _ts(d)
        ts._bg_started_this_turn = True
        r = ts.execute("Bash", {"command": "sleep 20"})
        assert r.is_error
        assert "不要" in r.content and "后台任务" in r.content
        r2 = ts.execute("Bash", {"command": "echo hi"})
        assert not r2.is_error, r2.content


def test_begin_turn_resets_flag():
    with tempfile.TemporaryDirectory() as d:
        ts = _ts(d)
        ts._bg_started_this_turn = True
        ts.begin_turn()
        assert ts._bg_started_this_turn is False
        r = ts.execute("Bash", {"command": "echo ok"})
        assert not r.is_error
