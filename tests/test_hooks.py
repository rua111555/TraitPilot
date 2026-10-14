"""M10 钩子单测：Pre/Post 工具钩子、阻断、结果改写、异常隔离、默认空链。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pathlib import Path  # noqa: E402

from skill_agent.hooks import Hook, HookManager  # noqa: E402
from skill_agent.skills import SkillRegistry  # noqa: E402
from skill_agent.tools import ToolResult, ToolSet  # noqa: E402


def _ts(hooks=None):
    return ToolSet(SkillRegistry(Path("/nonexistent")), Path("/tmp"), hooks=hooks)


def test_empty_manager_is_noop():
    h = HookManager()
    assert h.empty and h.pre_tool_use("Bash", {}) is None


def test_no_hooks_toolset_unchanged():
    ts = _ts(None)
    r = ts.execute("Bash", {"command": "echo hi"})
    assert not r.is_error and "hi" in r.content


def test_pre_hook_blocks():
    h = HookManager()
    h.add("pre_tool_use", lambda name, args: "禁止执行",
          name="guard", blocking=True)
    ts = _ts(h)
    r = ts.execute("Bash", {"command": "echo hi"})
    assert r.is_error and "禁止执行" in r.content


def test_pre_hook_can_modify_args():
    h = HookManager()

    def inject(name, args):
        if name == "Bash":
            args["command"] = "echo injected"     # 原地改
        return None

    h.add("pre_tool_use", inject)
    ts = _ts(h)
    r = ts.execute("Bash", {"command": "echo orig"})
    assert "injected" in r.content and "orig" not in r.content


def test_pre_hook_returns_toolresult_directly():
    h = HookManager()
    h.add("pre_tool_use", lambda n, a: ToolResult("cached!", False))
    r = _ts(h).execute("Bash", {"command": "echo x"})
    assert not r.is_error and r.content == "cached!"


def test_post_hook_redacts():
    h = HookManager()

    def redact(name, args, result):
        result.content = result.content.replace("secret", "***")
        return result

    h.add("post_tool_use", redact)
    r = _ts(h).execute("Bash", {"command": "echo secret"})
    assert "secret" not in r.content and "***" in r.content


def test_hook_exception_is_swallowed():
    h = HookManager()

    def boom(name, args):
        raise RuntimeError("hook 崩了")

    h.add("pre_tool_use", boom)
    ts = _ts(h)
    r = ts.execute("Bash", {"command": "echo ok"})     # 不应被钩子拖垮
    assert not r.is_error and "ok" in r.content
    assert any("boom" in e or "崩了" in e for e in h.last_errors)


def test_nonblocking_pre_hook_does_not_stop():
    h = HookManager()
    h.add("pre_tool_use", lambda n, a: "只是观测", name="obs", blocking=False)
    r = _ts(h).execute("Bash", {"command": "echo ok"})
    assert not r.is_error


def test_session_events_emit():
    seen = []
    h = HookManager()
    h.add("session_start", lambda ctx: seen.append(("start", ctx)))
    h.add("session_end", lambda ctx: seen.append(("end", ctx)))
    h.emit("session_start", {"sid": "s1"})
    h.emit("session_end", {"sid": "s1"})
    assert seen == [("start", {"sid": "s1"}), ("end", {"sid": "s1"})]


def test_unknown_event_rejected():
    h = HookManager()
    try:
        h.register(Hook(event="nope", fn=lambda: None))
        assert False
    except ValueError:
        pass


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
