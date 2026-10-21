"""M3 主循环集成：halt 熔断、warn 注入到 tool_result、未熔断时正常收尾。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from skill_agent.agent import Agent  # noqa: E402
from skill_agent.loopguard import LoopGuard  # noqa: E402


class _Cfg:
    model = "m"; base_url = ""; api_key = "k"; max_tokens = 8
    stream = False; verbose = False; cwd = None
    max_turns = None; max_output_tokens = None; max_seconds = None; warn_ratio = 0.8


class _Tools:
    def schemas_for(self, allowed): return []
    def execute(self, n, a):
        from skill_agent.tools import ToolResult
        return ToolResult("same output")
    def summarize_tasks(self): return ""


class _Reg:
    def listing(self): return ""
    def get(self, n): return None


class _RepeatClient:
    """永远要求同一个工具调用 → 无进展循环。"""
    def __init__(self): self.calls = 0
    def create(self, *, system, tools, messages, stream, on_delta, on_thinking=None):
        self.calls += 1
        return ([{"type": "tool_use", "id": f"t{self.calls}", "name": "Bash",
                  "input": {"command": "true"}}], "tool_use", 5)


def test_loopguard_halts_and_returns():
    ag = Agent(_Cfg(), _Reg(), _Tools())
    ag.loopguard = LoopGuard(enabled=True, warn=2, halt=4)
    ag.client = _RepeatClient()
    events = []; ag.on_event = events.append
    out = ag.answer("go")
    assert "熔断" in out
    assert ag.client.calls <= 5                    # 未跑满默认 60 轮
    assert any(e.get("type") == "loop_guard" for e in events)


def test_warn_injected_into_tool_result():
    ag = Agent(_Cfg(), _Reg(), _Tools())
    ag.loopguard = LoopGuard(enabled=True, warn=2, halt=99)   # 只 warn 不 halt
    ag.client = _RepeatClient()
    ag.cfg.max_turns = 5
    ag.answer("go")
    # 某个 tool_result 里应含纠偏提示
    blob = ""
    for m in ag.history:
        c = m.get("content")
        if isinstance(c, list):
            for b in c:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    blob += str(b.get("content"))
    assert "疑似重复" in blob or "轮询无新进展" in blob


def test_no_loopguard_normal_flow():
    ag = Agent(_Cfg(), _Reg(), _Tools())

    class _One:
        def create(self, *, system, tools, messages, stream, on_delta, on_thinking=None):
            return ([{"type": "text", "text": "ok"}], "end_turn", 1)

    ag.client = _One()
    assert ag.answer("go") == "ok"


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
