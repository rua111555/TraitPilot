"""M1a 用量采集单测：不触网，stub client.create。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from skill_agent.agent import Agent  # noqa: E402


class _Cfg:
    model = "test-model"
    base_url = "http://x"
    api_key = "k"
    max_tokens = 16
    stream = False
    verbose = False
    cwd = None


class _FakeClient:
    def __init__(self):
        self.n = 0

    def create(self, *, system, tools, messages, stream, on_delta):
        self.n += 1
        # 第一次返回 tool_use，第二次返回纯文本收尾
        if self.n == 1:
            return ([{"type": "tool_use", "id": "t1", "name": "Noop", "input": {}}],
                    "tool_use", 11)
        return ([{"type": "text", "text": "done"}], "end_turn", 7)


class _FakeTools:
    def schemas_for(self, allowed):
        return []

    def execute(self, name, args):
        from skill_agent.tools import ToolResult
        return ToolResult("ok")

    def summarize_tasks(self):
        return ""


class _FakeRegistry:
    def listing(self):
        return "(none)"

    def get(self, name):
        return None


def test_usage_accumulates_and_emits():
    cfg = _Cfg()
    agent = Agent(cfg, _FakeRegistry(), _FakeTools())
    agent.client = _FakeClient()
    events = []
    agent.on_event = events.append

    out = agent.answer("hi")
    assert out == "done"
    u = agent.usage_bar
    assert u["steps"] == 2
    assert u["output_tokens"] == 18        # 11 + 7
    assert u["model"] == "test-model"
    usage_events = [e for e in events if e.get("type") == "usage"]
    assert len(usage_events) == 2
    assert usage_events[-1]["total_output_tokens"] == 18


def test_usage_resets_per_request():
    cfg = _Cfg()
    agent = Agent(cfg, _FakeRegistry(), _FakeTools())
    agent.client = _FakeClient()
    agent.answer("hi")
    assert agent.usage_bar["steps"] == 2
    agent.client = _FakeClient()           # 新一轮请求重新计数
    agent.answer("again")
    assert agent.usage_bar["steps"] == 2   # 不是 4


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
