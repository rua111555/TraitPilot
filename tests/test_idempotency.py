"""M11 幂等去重单测：只作用有副作用工具、复用/拒绝两种模式、失败可重试、run 级重置。"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pathlib import Path  # noqa: E402

from skill_agent.idempotency import IdempotencyGuard  # noqa: E402
from skill_agent.skills import SkillRegistry  # noqa: E402
from skill_agent.tools import ToolSet  # noqa: E402


def _ts(cwd, guard):
    return ToolSet(SkillRegistry(Path("/nonexistent")), Path(cwd), idempotency=guard)


# ---------------- gate 默认 / 键 ----------------

def test_disabled_by_default():
    with tempfile.TemporaryDirectory() as d:
        ts = _ts(d, IdempotencyGuard(enabled=False))
        assert ts.idempotency.enabled is False
        ts.execute("Bash", {"command": "echo a >> f"})
        ts.execute("Bash", {"command": "echo a >> f"})
        assert Path(d, "f").read_text().count("a") == 2   # 未去重


def test_key_stable_and_arg_order_independent():
    k1 = IdempotencyGuard.key("Edit", {"file_path": "a", "old_string": "x", "new_string": "y"})
    k2 = IdempotencyGuard.key("Edit", {"new_string": "y", "old_string": "x", "file_path": "a"})
    assert k1 == k2
    assert k1 != IdempotencyGuard.key("Edit", {"file_path": "a", "old_string": "x", "new_string": "z"})


def test_only_side_effect_tools_eligible():
    assert IdempotencyGuard.eligible("Bash")
    assert IdempotencyGuard.eligible("Write") and IdempotencyGuard.eligible("Edit")
    assert not IdempotencyGuard.eligible("Read")
    assert not IdempotencyGuard.eligible("Grep")


# ---------------- reuse 模式 ----------------

def test_reuse_dedupes_side_effect():
    with tempfile.TemporaryDirectory() as d:
        ts = _ts(d, IdempotencyGuard(enabled=True, mode="reuse"))
        r1 = ts.execute("Bash", {"command": "echo a >> f"})
        r2 = ts.execute("Bash", {"command": "echo a >> f"})
        assert not r1.is_error and not r2.is_error
        assert Path(d, "f").read_text().count("a") == 1        # 第二次未真正执行
        assert r2.meta.get("idempotent_replay") is True
        assert "复用" in r2.content


def test_readonly_not_deduped():
    with tempfile.TemporaryDirectory() as d:
        Path(d, "f").write_text("hello")
        ts = _ts(d, IdempotencyGuard(enabled=True))
        a = ts.execute("Read", {"file_path": "f"})
        b = ts.execute("Read", {"file_path": "f"})
        assert not a.is_error and not b.is_error
        assert not b.meta.get("idempotent_replay")     # 只读不去重


# ---------------- deny 模式 ----------------

def test_deny_blocks_replay():
    with tempfile.TemporaryDirectory() as d:
        ts = _ts(d, IdempotencyGuard(enabled=True, mode="deny"))
        ts.execute("Bash", {"command": "echo a >> f"})
        r2 = ts.execute("Bash", {"command": "echo a >> f"})
        assert r2.is_error and "拒绝重放" in r2.content
        assert Path(d, "f").read_text().count("a") == 1


def test_different_args_not_deduped():
    with tempfile.TemporaryDirectory() as d:
        ts = _ts(d, IdempotencyGuard(enabled=True))
        ts.execute("Bash", {"command": "echo a >> f"})
        ts.execute("Bash", {"command": "echo b >> f"})
        assert Path(d, "f").read_text().count("\n") == 2


# ---------------- 失败可重试 / run 级重置 ----------------

def test_failed_calls_not_cached():
    with tempfile.TemporaryDirectory() as d:
        ts = _ts(d, IdempotencyGuard(enabled=True))
        r1 = ts.execute("Bash", {"command": "exit 3"})
        r2 = ts.execute("Bash", {"command": "exit 3"})
        assert r1.is_error and r2.is_error          # 失败不记 → 允许重试
        assert not r2.meta.get("idempotent_replay")


def test_guard_reset_clears():
    with tempfile.TemporaryDirectory() as d:
        g = IdempotencyGuard(enabled=True)
        ts = _ts(d, g)
        ts.execute("Bash", {"command": "echo a >> f"})
        assert g.size() == 1
        g.reset()
        assert g.size() == 0
        ts.execute("Bash", {"command": "echo a >> f"})
        assert Path(d, "f").read_text().count("a") == 2


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
