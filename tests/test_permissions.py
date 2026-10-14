"""M8a 结构化权限单测：默认全关=零行为变化；开启后规则/边界/写审批生效。"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pathlib import Path  # noqa: E402

from skill_agent.permissions import PermissionModel  # noqa: E402
from skill_agent.skills import SkillRegistry  # noqa: E402
from skill_agent.tools import ToolSet  # noqa: E402


def _ts(cwd, pm):
    return ToolSet(SkillRegistry(Path("/nonexistent-project")), Path(cwd),
                   permission_model=pm)


# ---------- 默认关闭：行为与改造前一致 ----------

def test_default_model_disabled():
    pm = PermissionModel()
    assert pm.enabled is False
    assert pm.check_path(Path("/etc/passwd")) is None      # 不拦
    assert pm.needs_confirm("Write", {"file_path": "a"}) == (False, "")


def test_default_toolset_allows_outside_path():
    with tempfile.TemporaryDirectory() as d:
        ts = _ts(d, PermissionModel())
        # 默认无边界，读工作区外文件应照旧成功
        r = ts.execute("Read", {"file_path": "/etc/hostname"})
        assert not r.is_error or "不存在" in r.content


def test_default_no_write_approval():
    with tempfile.TemporaryDirectory() as d:
        ts = _ts(d, PermissionModel())
        r = ts.execute("Write", {"file_path": "a.txt", "content": "hi"})
        assert not r.is_error                          # 无需确认直接写


# ---------- 路径边界 ----------

def test_restrict_paths_blocks_traversal():
    with tempfile.TemporaryDirectory() as d:
        pm = PermissionModel(restrict_paths=True, roots=[Path(d).resolve()])
        ts = _ts(d, pm)
        r = ts.execute("Read", {"file_path": "../../../etc/hostname"})
        assert r.is_error and "边界" in r.content


def test_restrict_paths_allows_inside():
    with tempfile.TemporaryDirectory() as d:
        Path(d, "in.txt").write_text("ok")
        pm = PermissionModel(restrict_paths=True, roots=[Path(d).resolve()])
        ts = _ts(d, pm)
        r = ts.execute("Read", {"file_path": "in.txt"})
        assert not r.is_error


def test_extra_roots_whitelists_system_path():
    with tempfile.TemporaryDirectory() as d:
        pm = PermissionModel(restrict_paths=True, roots=[Path(d).resolve()],
                             extra_roots=[str(Path.home() / ".claude")])
        ts = _ts(d, pm)
        r = ts.execute("Glob", {"pattern": "*", "path": str(Path.home() / ".claude")})
        assert not r.is_error


# ---------- 写操作审批 ----------

def test_write_approval_requires_confirm_no_tty_denied():
    with tempfile.TemporaryDirectory() as d:
        pm = PermissionModel(write_approval=True)
        ts = _ts(d, pm)                                # 无 confirm_handler、无 TTY
        assert pm.needs_confirm("Write", {"file_path": "a"})[0] is True
        r = ts.execute("Write", {"file_path": "a.txt", "content": "x"})
        assert r.is_error and "拒绝" in r.content


def test_write_approval_with_confirm_handler():
    with tempfile.TemporaryDirectory() as d:
        seen = {}

        def handler(preview, reason):
            seen["preview"] = preview
            seen["reason"] = reason
            return True

        pm = PermissionModel(write_approval=True)
        ts = ToolSet(SkillRegistry(Path("/x")), Path(d),
                     permission_model=pm, confirm_handler=handler)
        r = ts.execute("Write", {"file_path": "a.txt", "content": "x"})
        assert not r.is_error
        assert "a.txt" in seen["preview"]


# ---------- 规则表 ----------

def test_deny_rule_regex():
    pm = PermissionModel(rules=[{"tool": "Bash", "match": r"rm\s+-rf", "action": "deny"}])
    with tempfile.TemporaryDirectory() as d:
        ts = _ts(d, pm)
        r = ts.execute("Bash", {"command": "rm -rf /tmp/x"})
        assert r.is_error and "拒绝" in r.content
        r2 = ts.execute("Bash", {"command": "echo ok"})
        assert not r2.is_error


def test_deny_rule_glob():
    pm = PermissionModel(rules=[{"tool": "*", "match": "*.env", "action": "deny"}])
    with tempfile.TemporaryDirectory() as d:
        ts = _ts(d, pm)
        r = ts.execute("Read", {"file_path": "secret.env"})
        assert r.is_error and "拒绝" in r.content


def test_ask_rule_needs_confirm():
    pm = PermissionModel(rules=[{"tool": "Bash", "match": "^curl", "action": "ask"}])
    assert pm.needs_confirm("Bash", {"command": "curl http://x"})[0] is True
    assert pm.needs_confirm("Bash", {"command": "echo hi"})[0] is False


def test_first_matching_rule_wins():
    pm = PermissionModel(rules=[
        {"tool": "Bash", "match": "git", "action": "allow"},
        {"tool": "Bash", "match": ".*", "action": "ask"},
    ])
    assert pm.needs_confirm("Bash", {"command": "git status"}) == (False, "")
    assert pm.needs_confirm("Bash", {"command": "ls"})[0] is True


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
