# -*- coding: utf-8 -*-
"""安全红线：任何删除都不执行，只把命令告诉用户（用户 2026-11-10 裁定）。"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pathlib import Path  # noqa: E402

from skill_agent.skills import SkillRegistry  # noqa: E402
from skill_agent.tools import ToolSet  # noqa: E402


def _ts(cwd):
    return ToolSet(SkillRegistry(Path("/nonexistent-project")), Path(cwd))


def test_deletion_commands_are_refused():
    with tempfile.TemporaryDirectory() as d:
        ts = _ts(d)
        cmds = [
            "rm -rf x", "rm -r x", "rmdir x", "unlink x",
            "find . -delete", "git clean -fdx",
            'bash -c "rm -f x"',
            "python -c \"import os; os.remove('x')\"",
        ]
        for c in cmds:
            r = ts.execute("Bash", {"command": c})
            assert r.is_error, c
            assert "拒绝" in r.content and "删除" in r.content, (c, r.content)
            assert c in r.content, c


def test_non_deletion_commands_still_run():
    with tempfile.TemporaryDirectory() as d:
        ts = _ts(d)
        r = ts.execute("Bash", {"command": 'echo "rm -rf is just text"'})
        assert not r.is_error, r.content
        r2 = ts.execute("Bash", {"command": "mkdir -p sub"})
        assert not r2.is_error, r2.content
        assert (Path(d) / "sub").is_dir()
