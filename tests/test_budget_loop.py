"""M1b 预算熔断单测：默认不限=旧行为；限轮次/时长/token 时在请求前熔断且不杀后台。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from skill_agent.agent import Agent  # noqa: E402


class _Cfg:
    model = "m"; base_url = "http://x"; api_key = "k"; max_tokens = 16
    stream = False; verbose = False; cwd = None
    max_turns = None; max_output_tokens = None; max_seconds = None; warn_ratio = 0.8
    max_turn_extensions = 0        # 单测默认关弹性延长 → 严格上限，行为可预测


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


def test_max_turns_extends_when_context_has_room():
    """弹性轮次：上下文仍有余量时，到点**延长**而不是硬停。

    与 test_max_turns_halts 是一对：那条证明「关掉延时就严格到点即停」，
    这条证明「开着延时会突破首段上限」—— 两条一起才有区分度
    （只有前者会把「永远不停」判为合格，只有后者会把「到点即停」判为不合格）。
    """
    cfg = _Cfg(); cfg.max_turns = 4; cfg.max_turn_extensions = 2
    ag = Agent(cfg, _Reg(), _Tools())
    ag.client = _LoopClient()            # 永远要求继续 → 必然用满全部延长
    events = []; ag.on_event = events.append
    out = ag.answer("go")
    cap = 4 * (1 + 2)                    # 累计上限 = max_turns × (1 + extensions)
    assert ag.client.calls == cap, f"期望恰好用满 {cap} 轮，实际 {ag.client.calls}"
    assert "最大工具轮次" in out
    assert "延长" in out                 # 如实说明延长过，不假装是首段上限
    # 延长必须有用户可见提示
    assert any("延长" in str(e.get("message", "")) for e in events
               if e.get("type") == "notice")


def test_extension_is_bounded():
    """延长**有界**：无限任务最终仍会停，不会永远占住这一轮。

    这是上一条的阴性对照 —— 若延长无界，`answer()` 将永不返回。
    """
    cfg = _Cfg(); cfg.max_turns = 3; cfg.max_turn_extensions = 1
    ag = Agent(cfg, _Reg(), _Tools())
    ag.client = _LoopClient()
    out = ag.answer("go")                # 若死循环，本测试会挂住（而非假通过）
    assert ag.client.calls == 3 * (1 + 1)
    assert "最大工具轮次" in out


def test_max_turn_extensions_zero_disables():
    """显式 0 必须真的关掉（反例：0 被当成「没设」而回落到默认 3）。"""
    cfg = _Cfg(); cfg.max_turns = 5; cfg.max_turn_extensions = 0
    ag = Agent(cfg, _Reg(), _Tools())
    ag.client = _LoopClient()
    out = ag.answer("go")
    assert ag.client.calls == 5, f"0 应严格关闭延长，实际跑了 {ag.client.calls} 轮"
    assert "延长" not in out


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
