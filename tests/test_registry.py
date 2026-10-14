"""M4 工具注册表单测：注册/查找/过滤、插件目录加载、动态工具与内置同权。"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pathlib import Path  # noqa: E402

from skill_agent.registry import ToolRegistry, load_plugin_dir  # noqa: E402
from skill_agent.skills import SkillRegistry  # noqa: E402
from skill_agent.tools import Tool, ToolResult, ToolSet  # noqa: E402


def _tool(name):
    return Tool(name=name, description="d", input_schema={"type": "object"},
                executor=lambda a: ToolResult("ok"))


def test_register_get_names():
    r = ToolRegistry()
    r.register(_tool("A"))
    r.register(_tool("B"))
    assert r.names() == ["A", "B"] and len(r) == 2
    assert r.get("A").name == "A" and "B" in r


def test_unregister_and_replace():
    r = ToolRegistry()
    r.register(_tool("A"), source="builtin")
    assert r.unregister("A") is True and r.get("A") is None
    r.register(_tool("A")); r.replace(_tool("A"), source="plugin:x")
    assert r.get("A").source == "plugin:x"


def test_schemas_whitelist_with_always():
    r = ToolRegistry()
    for n in ("Read", "Bash", "Skill", "TaskOutput"):
        r.register(_tool(n))
    names = sorted(s["name"] for s in r.schemas({"Read"}))
    assert names == ["Read", "Skill", "TaskOutput"]     # 保底 always


def test_by_source():
    r = ToolRegistry()
    r.register(_tool("A"), source="plugin:/x/a.json")
    r.register(_tool("B"))
    assert r.by_source("plugin:") == ["A"]


def test_toolset_backed_by_registry():
    ts = ToolSet(SkillRegistry(Path("/nonexistent")), Path("."))
    assert ts.registry_tools.names() == ["Bash", "Edit", "Glob", "Grep", "Read",
                                         "Skill", "TaskOutput", "Write"]
    assert all(ts.registry_tools.get(n).source == "builtin"
               for n in ts.registry_tools.names())


def test_dynamic_tool_executes_through_toolset():
    ts = ToolSet(SkillRegistry(Path("/nonexistent")), Path("."))
    from skill_agent.tools import Tool as T
    ts.register_tool(T(name="Echo", description="echo",
                       input_schema={"type": "object",
                                     "properties": {"s": {"type": "string"}},
                                     "required": ["s"]},
                       executor=lambda a: ToolResult("E:" + a.get("s", ""))),
                     source="plugin:test")
    r = ts.execute("Echo", {"s": "hi"})
    assert not r.is_error and r.content == "E:hi"
    # 动态工具同样受 M6 输入校验约束
    bad = ts.execute("Echo", {})
    assert bad.is_error and "输入校验失败" in bad.content


# ---------------- 插件目录加载 ----------------

def test_load_plugin_dir(tmp_path):
    mod = tmp_path / "mytool.py"
    mod.write_text(
        "from skill_agent.tools import ToolResult\n"
        "def make_executor():\n"
        "    return lambda args: ToolResult('plugin-ok')\n", encoding="utf-8")
    (tmp_path / "mytool.json").write_text(json.dumps({
        "name": "Plugged", "description": "x",
        "input_schema": {"type": "object"},
        "path": str(mod), "executor": "make_executor"}), encoding="utf-8")

    r = ToolRegistry()
    loaded = load_plugin_dir(r, tmp_path)
    assert loaded == ["Plugged"]
    assert r.get("Plugged").source.startswith("plugin:")
    assert r.get("Plugged").executor({}).content == "plugin-ok"


def test_load_plugin_dir_allowlist(tmp_path):
    (tmp_path / "a.json").write_text(json.dumps(
        {"name": "A", "path": "/nope", "executor": "make_executor"}), encoding="utf-8")
    (tmp_path / "b.json").write_text(json.dumps(
        {"name": "B", "path": "/nope", "executor": "make_executor"}), encoding="utf-8")
    r = ToolRegistry()
    loaded = load_plugin_dir(r, tmp_path, allow=["A"])
    # A 在 allow 但入口缺失 → 跳过；B 不在 allow → 跳过
    assert loaded == [] and len(r) == 0


def test_load_plugin_dir_missing_returns_empty():
    r = ToolRegistry()
    assert load_plugin_dir(r, Path("/no/such/dir")) == []


def test_bad_spec_does_not_crash(tmp_path):
    (tmp_path / "bad.json").write_text("{ not json", encoding="utf-8")
    (tmp_path / "ok.json").write_text(json.dumps(
        {"name": "NoMod"}), encoding="utf-8")           # 缺 module/path
    r = ToolRegistry()
    assert load_plugin_dir(r, tmp_path) == []          # 全部静默跳过


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
