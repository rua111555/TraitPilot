"""#9 todo 工具单测：解析/校验/渲染、默认不挂载、启用后经 ToolSet 执行。"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pathlib import Path  # noqa: E402

from skill_agent import todo as todo_mod  # noqa: E402
from skill_agent.skills import SkillRegistry  # noqa: E402
from skill_agent.tools import ToolSet  # noqa: E402


def test_parse_valid():
    items, err = todo_mod.parse_todos(json.dumps([
        {"content": "比对", "status": "in_progress"},
        {"content": "变异检测", "status": "pending"},
    ]))
    assert err is None and len(items) == 2
    assert items[0]["status"] == "in_progress"


def test_parse_default_status_pending():
    items, err = todo_mod.parse_todos([{"content": "x"}])
    assert err is None and items[0]["status"] == "pending"


def test_parse_rejects_bad_json_and_shape():
    assert todo_mod.parse_todos("not json")[1]
    assert todo_mod.parse_todos(json.dumps([{"content": ""}]))[1]     # 缺 content
    assert todo_mod.parse_todos(json.dumps([{"content": "x", "status": "weird"}]))[1]


def test_parse_rejects_multiple_in_progress():
    _, err = todo_mod.parse_todos([
        {"content": "a", "status": "in_progress"},
        {"content": "b", "status": "in_progress"},
    ])
    assert err and "in_progress" in err


def test_render_and_progress():
    items = [{"content": "a", "status": "completed"},
             {"content": "b", "status": "pending"}]
    text = todo_mod.render(items)
    assert "1/2" in text and "[x]" in text and "[ ]" in text
    p = todo_mod.progress(items)
    assert p == {"total": 2, "completed": 1, "in_progress": 0, "pending": 1}


# ---------------- 与 ToolSet 集成 ----------------

def test_todo_not_mounted_by_default():
    with tempfile.TemporaryDirectory() as d:
        ts = ToolSet(SkillRegistry(Path("/nonexistent")), Path(d))
        assert "TodoWrite" not in ts.registry_tools
        assert "TodoWrite" not in [s["name"] for s in ts.schemas_for(None)]


def test_todo_mounted_when_enabled_and_validates():
    with tempfile.TemporaryDirectory() as d:
        ts = ToolSet(SkillRegistry(Path("/nonexistent")), Path(d), enable_todo=True)
        assert "TodoWrite" in ts.registry_tools
        assert "TodoWrite" in [s["name"] for s in ts.schemas_for(None)]
        r = ts.execute("TodoWrite", {"todos": json.dumps(
            [{"content": "跑 QC", "status": "in_progress"}])})
        assert not r.is_error and "计划进度" in r.content
        assert r.meta["todo"]["in_progress"] == 1
        # 非法输入走 M6 一致的结构化报错
        bad = ts.execute("TodoWrite", {"todos": "["})
        assert bad.is_error and "输入校验失败" in bad.content


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
