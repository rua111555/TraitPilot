"""M3 LoopGuard 单测：默认关=不干预；progress-aware、轮询白名单、halt 行为。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from skill_agent.loopguard import (GuardLevel, LoopGuard, action_fingerprint,  # noqa: E402
                                   is_polling, normalize_observation)


def test_default_disabled_no_intervention():
    g = LoopGuard()                                  # enabled=False
    for _ in range(20):
        v = g.record("Bash", {"command": "ls"}, "same", {})
    assert v.level == GuardLevel.OK


def test_disabled_agent_has_no_loopguard():
    from skill_agent.agent import Agent

    class C: model="m";base_url="";api_key="k";max_tokens=8;stream=False;verbose=False;cwd=None
    class T:
        def schemas_for(s,a): return []
        def execute(s,n,a):
            from skill_agent.tools import ToolResult; return ToolResult("x")
        def summarize_tasks(s): return ""
    class R:
        def listing(s): return ""
        def get(s,n): return None
    ag = Agent(C(), R(), T())
    assert ag.loopguard is None


def test_repeated_same_action_halts():
    g = LoopGuard(enabled=True, warn=3, halt=5)
    lvls = [g.record("Bash", {"command": "cat x"}, "identical", {}).level for _ in range(6)]
    assert GuardLevel.WARN in lvls
    assert lvls[-1] == GuardLevel.HALT


def test_progress_resets_counter():
    g = LoopGuard(enabled=True, warn=3, halt=5)
    g.record("Bash", {"command": "c"}, "a", {"exit_code": 1})
    g.record("Bash", {"command": "c"}, "a", {"exit_code": 1})
    v = g.record("Bash", {"command": "c"}, "a", {"exit_code": 0})   # 进展：失败→成功
    assert v.level == GuardLevel.OK and g._repeat == 0


def test_different_actions_do_not_accumulate():
    g = LoopGuard(enabled=True, warn=2, halt=3)
    for i in range(10):
        v = g.record("Bash", {"command": f"c{i}"}, f"out{i}", {})
    assert v.level == GuardLevel.OK


def test_polling_never_hard_halts():
    g = LoopGuard(enabled=True, warn=2, halt=3)
    last = GuardLevel.OK
    for _ in range(10):
        last = g.record("TaskOutput", {"log_path": "/tmp/r.log"}, "no new lines", {}).level
    assert last == GuardLevel.WARN          # 轮询只降级到 WARN，绝不 HALT


def test_is_polling_classification():
    assert is_polling("TaskOutput", {"log_path": "x"})
    assert is_polling("Read", {"file_path": "/p/run.log"})
    assert is_polling("Bash", {"command": "cat /p/x.done"})
    assert not is_polling("Bash", {"command": "ls -la"})


def test_normalize_strips_noise():
    a = normalize_observation("  12\tfile has 345 bytes at 2026-10-14 10:00:00 deadbeefcafe")
    b = normalize_observation("  99\tfile has 999 bytes at 2026-10-15 11:22:33 0123456789ab")
    assert a == b


def test_action_fingerprint_stable_across_key_order():
    assert action_fingerprint("Bash", {"a": 1, "b": 2}) == \
        action_fingerprint("Bash", {"b": 2, "a": 1})


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
