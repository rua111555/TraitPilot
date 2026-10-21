"""M1b 预算熔断单测：默认不限=旧行为；限轮次/时长/token 时在请求前熔断且不杀后台。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from skill_agent.agent import Agent  # noqa: E402


class _Cfg:
    model = "m"; base_url = "http://x"; api_key = "k"; max_tokens = 16
    stream = False; verbose = False; cwd = None
    max_turns = None; max_output_tokens = None; max_seconds = None; warn_ratio = 0.8


class _Tools:
    def schemas_for(self, allowed): return []
    def execute(self, n, a):
        from skill_agent.tools import ToolResult
        return ToolResult("ok")
    def summarize_tasks(self): return ""


class _Reg:
    def listing(self): return ""
    def get(self, n): return None


class _LoopClient:
    """永远返回一个 tool_use，制造无限循环（用于验证轮次上限）。"""
    def __init__(self):
        self.calls = 0

    def create(self, *, system, tools, messages, stream, on_delta, on_thinking=None):
        self.calls += 1
        return ([{"type": "tool_use", "id": f"t{self.calls}", "name": "Bash", "input": {}}],
                "tool_use", 10)


def test_max_turns_halts():
    cfg = _Cfg(); cfg.max_turns = 4
    ag = Agent(cfg, _Reg(), _Tools())
    ag.client = _LoopClient()
    events = []; ag.on_event = events.append
    out = ag.answer("go")
    assert "最大工具轮次" in out
    assert ag.client.calls == 4                     # 恰好 4 次，未多跑
    # 不应有 usage 之外的异常
    assert any(e.get("type") == "usage" for e in events)


def test_max_output_tokens_halts_before_call():
    cfg = _Cfg(); cfg.max_output_tokens = 25        # 每轮 10 → 第 3 轮前应超
    ag = Agent(cfg, _Reg(), _Tools())
    ag.client = _LoopClient()
    events = []; ag.on_event = events.append
    out = ag.answer("go")
    assert "预算熔断" in out and "上限" in out
    assert ag.client.calls == 3                     # 累计 30 ≥ 25 后下一轮前熔断
    assert any(e.get("type") == "notice" for e in events)


def test_max_seconds_halts(monkeypatch):
    cfg = _Cfg(); cfg.max_seconds = 1
    ag = Agent(cfg, _Reg(), _Tools())
    ag.client = _LoopClient()
    # 让第一次调用后时间"跳"过 1 秒
    import skill_agent.agent as A
    real = A.time.monotonic
    ticks = iter([0.0, 0.0, 2.0, 2.0, 2.0, 2.0])
    monkeypatch.setattr(A.time, "monotonic", lambda: next(ticks, 2.0))
    out = ag.answer("go")
    assert "预算熔断" in out and "时长" in out


def test_default_no_budget_behaves_like_before():
    cfg = _Cfg()                                    # 全 None
    ag = Agent(cfg, _Reg(), _Tools())

    class _One:
        def create(self, *, system, tools, messages, stream, on_delta, on_thinking=None):
            return ([{"type": "text", "text": "done"}], "end_turn", 1)

    ag.client = _One()
    assert ag.answer("go") == "done"


def test_warn_at_ratio_once():
    cfg = _Cfg(); cfg.max_output_tokens = 100; cfg.warn_ratio = 0.1
    ag = Agent(cfg, _Reg(), _Tools())
    ag.client = _LoopClient()
    p = _LoopClient(); ag.client = p
    events = []; ag.on_event = events.append
    ag.answer("go")
    notices = [e for e in events if e.get("type") == "notice" and "预算提醒" in e.get("message", "")]
    assert len(notices) == 1                        # 只提醒一次


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
