"""M2 主循环集成单测：REPAIR_TRUNCATION 合成配对 tool_result、pause_turn 不追加 user。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from skill_agent.agent import Agent  # noqa: E402


class _Cfg:
    model = "m"; base_url = "http://x"; api_key = "k"; max_tokens = 16
    stream = False; verbose = False; cwd = None


class _Tools:
    def schemas_for(self, allowed): return []
    def execute(self, n, a):
        from skill_agent.tools import ToolResult
        return ToolResult("ok")
    def summarize_tasks(self): return ""


class _Reg:
    def listing(self): return ""
    def get(self, n): return None


def _blocks(m):
    c = m.get("content")
    return c if isinstance(c, list) else []


def _agent_with(turns):
    ag = Agent(_Cfg(), _Reg(), _Tools())
    seq = list(turns)

    class _Client:
        def create(self, *, system, tools, messages, stream, on_delta, on_thinking=None):
            return seq.pop(0)

    ag.client = _Client()
    return ag


def test_repair_truncation_continues_with_paired_result():
    bad = {"type": "tool_use", "id": "t1", "name": "Bash", "input": {}, "_parse_error": True}
    ag = _agent_with([
        ([bad], "max_tokens", 5),                                  # 截断
        ([{"type": "text", "text": "重试完成"}], "end_turn", 5),    # 重试后收尾
    ])
    out = ag.answer("go")
    assert out == "重试完成"
    # 截断轮的 assistant 必须紧跟一个配对的 tool_result
    roles = [m["role"] for m in ag.history]
    assert roles[0] == "user"
    i = next(i for i, m in enumerate(ag.history)
             if any(b.get("type") == "tool_use" for b in _blocks(m)))
    nxt = _blocks(ag.history[i + 1])
    assert nxt and nxt[0]["type"] == "tool_result" and nxt[0]["tool_use_id"] == "t1"
    assert nxt[0]["is_error"] is True


def test_pause_turn_does_not_append_user():
    ag = _agent_with([
        ([{"type": "text", "text": "part1"}], "pause_turn", 5),
        ([{"type": "text", "text": "part2"}], "end_turn", 5),
    ])
    out = ag.answer("go")
    assert out == "part2"
    # pause_turn 不得产生连续两条 user
    for a, b in zip(ag.history, ag.history[1:]):
        assert a["role"] != b["role"]


def test_refusal_returns_message_no_tools():
    ag = _agent_with([([], "refusal", 1)])
    out = ag.answer("go")
    assert "拒绝" in out


def test_max_tokens_text_marks_truncation():
    ag = _agent_with([([{"type": "text", "text": "半截"}], "max_tokens", 1)])
    out = ag.answer("go")
    assert "半截" in out and "max_tokens" in out


def test_last_stop_exposed():
    ag = _agent_with([([{"type": "text", "text": "ok"}], "end_turn", 1)])
    ag.answer("go")
    assert ag._last_stop.action.value == "finalize"


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
