"""M3a 进展信号（ToolResult.meta）单测：只记不判，且不改变原行为。"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pathlib import Path  # noqa: E402

from skill_agent.skills import SkillRegistry  # noqa: E402
from skill_agent.tools import ToolResult, ToolSet  # noqa: E402


def _ts(cwd):
    return ToolSet(SkillRegistry(Path("/nonexistent-project")), Path(cwd))


def test_toolresult_meta_default_empty_and_positional_ok():
    r = ToolResult("hello")           # 位置参数仍按 (content, is_error)
    assert r.meta == {}
    r2 = ToolResult("bad", True)
    assert r2.is_error is True and r2.meta == {}


def test_bash_meta_exit_code():
    with tempfile.TemporaryDirectory() as d:
        ts = _ts(d)
        r = ts.execute("Bash", {"command": "true"})
        assert r.meta.get("exit_code") == 0
        assert "duration_ms" in r.meta
        r2 = ts.execute("Bash", {"command": "exit 7"})
        assert r2.meta.get("exit_code") == 7 and r2.is_error


def test_write_and_read_meta_path_size():
    with tempfile.TemporaryDirectory() as d:
        ts = _ts(d)
        w = ts.execute("Write", {"file_path": "a.txt", "content": "x\ny\n"})
        assert Path(w.meta["path"]).name == "a.txt"
        assert w.meta["size"] == 4
        r = ts.execute("Read", {"file_path": "a.txt"})
        assert r.meta["lines"] == 2
        assert r.meta["size"] == 4
        assert "mtime" in r.meta


def test_edit_meta_mtime_advances():
    with tempfile.TemporaryDirectory() as d:
        ts = _ts(d)
        ts.execute("Write", {"file_path": "a.txt", "content": "hello world"})
        time.sleep(0.01)
        e = ts.execute("Edit", {"file_path": "a.txt",
                                "old_string": "world", "new_string": "there"})
        assert not e.is_error
        assert "mtime" in e.meta


def test_background_task_meta():
    with tempfile.TemporaryDirectory() as d:
        ts = _ts(d)
        r = ts.execute("Bash", {"command": "sleep 0.1", "background": True})
        assert r.meta.get("task_id")
        assert Path(r.meta["log"]).name == "output.log"


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
