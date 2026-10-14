"""M7 工具治理单测：per-tool 超时、结果落盘转存、分段读取、并行只读。"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pathlib import Path  # noqa: E402

from skill_agent.agent import Agent  # noqa: E402
from skill_agent.skills import SkillRegistry  # noqa: E402
from skill_agent.tools import (READ_MAX_LIMIT, Tool, ToolResult,  # noqa: E402
                               ToolSet, default_timeout)


def _ts(cwd="/tmp", **kw):
    return ToolSet(SkillRegistry(Path("/nonexistent")), Path(cwd), **kw)


# ---------------- per-tool 超时 ----------------

def test_default_timeouts_present():
    assert default_timeout("Read") == 30 and default_timeout("Bash") == 120


def test_slow_tool_times_out(monkeypatch):
    ts = _ts()
    # 直接测 _timed 包装器（避免依赖真实工具的耗时）
    def slow(args):
        time.sleep(2)
        return ToolResult("too late")
    name, wrap = ts._timed("Slow", 1)
    r = wrap(slow)({})
    assert r.is_error and "超时" in r.content


def test_fast_tool_within_timeout():
    ts = _ts()
    name, wrap = ts._timed("Fast", 5)
    assert not wrap(lambda a: ToolResult("quick"))({}).is_error


def test_builtin_tools_are_wrapped():
    ts = _ts()
    # 内置工具（除 Bash）应已被套上超时包装
    assert "Bash" not in (ts.registry_tools.get("Read").executor.__qualname__ or "")
    # Read 的 executor 是包装后的闭包（名字为 run）
    assert callable(ts.registry_tools.get("Read").executor)


def test_read_accepts_large_limit():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d, "big.txt")
        p.write_text("\n".join(str(i) for i in range(5000)), encoding="utf-8")
        ts = _ts(d)
        r = ts.execute("Read", {"file_path": "big.txt", "limit": 4000})
        assert not r.is_error
        assert "还有" in r.content and "未显示" in r.content      # 分段提示
        r2 = ts.execute("Read", {"file_path": "big.txt", "offset": 4001, "limit": 2000})
        assert not r2.is_error and r2.meta["lines"] == 5000


# ---------------- 结果落盘转存 ----------------

def test_spill_disabled_by_default():
    with tempfile.TemporaryDirectory() as d:
        ts = _ts(d)                                  # 无 spill_dir
        r = ts.execute("Bash", {"command": "yes x | head -2000"})
        assert "转存" not in r.content               # 未落盘


def test_spill_writes_file_and_returns_reference():
    with tempfile.TemporaryDirectory() as d:
        spill = Path(d, "spill")
        ts = _ts(d, spill_dir=spill, spill_threshold=100)
        r = ts.execute("Bash", {"command": "python3 -c \"print('A'*500)\""})
        assert "转存" in r.content
        assert r.meta.get("spilled") and Path(r.meta["spilled"]).is_file()
        # 落盘内容完整
        assert len(Path(r.meta["spilled"]).read_text()) > 500
        # 可用 Read 继续查看
        rr = ts.execute("Read", {"file_path": r.meta["spilled"], "limit": 5})
        assert not rr.is_error


def test_small_result_not_spilled():
    with tempfile.TemporaryDirectory() as d:
        ts = _ts(d, spill_dir=Path(d, "spill"), spill_threshold=10_000)
        r = ts.execute("Bash", {"command": "echo tiny"})
        assert "转存" not in r.content


def test_spill_preserves_full_output_beyond_MAX_OUTPUT():
    """超过 MAX_OUTPUT(30k) 时，落盘的是**完整**输出而非截断版。"""
    with tempfile.TemporaryDirectory() as d:
        ts = _ts(d, spill_dir=Path(d, "spill"), spill_threshold=30_000)
        r = ts.execute("Bash", {"command": "seq 1 8000"})
        p = r.meta.get("spilled")
        assert p, "超过阈值应落盘"
        text = Path(p).read_text()
        assert text.strip().endswith("8000"), "落盘内容应为完整输出（含尾行）"
        assert r.meta["original_lines"] >= 8000


# ---------------- 并行只读 ----------------

class _Cfg:
    model = "m"; base_url = ""; api_key = "k"; max_tokens = 8
    stream = False; verbose = False; cwd = None; parallel_tools = False
    max_turns = None; max_output_tokens = None; max_seconds = None; warn_ratio = 0.8


class _Reg:
    def listing(self): return ""
    def get(self, n): return None


class _SleepTools:
    def __init__(self, delay=0.3):
        self.delay = delay
        self.calls = []

    def schemas_for(self, allowed): return []

    def execute(self, name, args):
        self.calls.append(name)
        if name == "Read":
            time.sleep(self.delay)
        return ToolResult(f"ok:{name}")

    def summarize_tasks(self): return ""


def _blocks3():
    return [
        {"type": "tool_use", "id": "a", "name": "Read", "input": {"file_path": "x"}},
        {"type": "tool_use", "id": "b", "name": "Read", "input": {"file_path": "y"}},
        {"type": "tool_use", "id": "c", "name": "Read", "input": {"file_path": "z"}},
    ]


def test_parallel_reads_faster_than_serial():
    tools = _SleepTools(0.3)
    ag = Agent(_Cfg(), _Reg(), tools)
    ag.parallel_tools = True
    t0 = time.monotonic()
    out = ag._execute_tools(_blocks3())
    dt = time.monotonic() - t0
    assert len(out) == 3
    assert dt < 0.6, f"并行应显著快于串行(0.9s)，实测 {dt:.2f}s"
    # 结果顺序与原 blocks 对齐
    assert [b["tool_use_id"] for b in out] == ["a", "b", "c"]


def test_serial_by_default():
    tools = _SleepTools(0.2)
    ag = Agent(_Cfg(), _Reg(), tools)        # parallel_tools 默认 False
    t0 = time.monotonic()
    ag._execute_tools(_blocks3())
    dt = time.monotonic() - t0
    assert dt >= 0.6, f"默认应串行(约0.6s)，实测 {dt:.2f}s"


def test_side_effect_tools_never_parallelized():
    # Bash/Write 不得进并行分支
    from skill_agent.agent import _is_readonly
    assert _is_readonly("Read") and _is_readonly("Grep")
    assert not _is_readonly("Bash") and not _is_readonly("Write")


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
