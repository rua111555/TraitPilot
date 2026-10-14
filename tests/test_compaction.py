"""M12 摘要式压缩单测：默认关=旧行为；开启后满足配对/角色不变量并保留关键字段。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from skill_agent.agent import Agent, COMPACT_MIN_MESSAGES  # noqa: E402
from skill_agent.compaction import SummaryCompressor  # noqa: E402


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
    return c if isinstance(c, list) else ([] if not c else [{"type": "text", "text": c}])


def _valid_history(history):
    """校验 Messages API 不变量：首条 user、角色交替、tool_use/tool_result 严格配对。"""
    assert history, "empty"
    assert history[0]["role"] == "user", "首条必须是 user"
    for a, b in zip(history, history[1:]):
        assert a["role"] != b["role"], "连续两条同 role"
    # 每个 tool_use 必须在紧随的 user 消息里有且仅有对应 tool_result
    for i, m in enumerate(history):
        tus = [x["id"] for x in _blocks(m) if x.get("type") == "tool_use"]
        if not tus:
            continue
        assert i + 1 < len(history) and history[i + 1]["role"] == "user"
        trs = {x.get("tool_use_id") for x in _blocks(history[i + 1])
               if x.get("type") == "tool_result"}
        assert set(tus) == trs, f"配对不符：{tus} vs {trs}"


def _long_history(n_rounds=25):
    msgs = []
    for i in range(n_rounds):
        msgs.append({"role": "user", "content": f"q{i}"})
        msgs.append({"role": "assistant", "content": [
            {"type": "tool_use", "id": f"t{i}", "name": "Bash", "input": {"command": f"c{i}"}}]})
        msgs.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": f"t{i}", "content": "ok"}]})
        msgs.append({"role": "assistant", "content": [{"type": "text", "text": f"a{i}"}]})
    return msgs


def _agent():
    return Agent(_Cfg(), _Reg(), _Tools())


def test_default_off_behaves_like_old_drop():
    ag = _agent()
    ag.history = _long_history()
    n0 = len(ag.history)
    ag._maybe_compact()                        # 无 compressor/summarize → 旧逻辑
    assert len(ag.history) < n0
    assert ag._compacted
    assert not ag.history[0].get("content", "").__str__().startswith("【前情摘要")
    _valid_history(ag.history)


def test_summary_compaction_preserves_invariants_and_fields():
    ag = _agent()
    ag.summary_compressor = SummaryCompressor()
    captured = {}

    def fake_summarize(prompt):
        captured["prompt"] = prompt
        return "阶段 S3：--geno 0.2 -lmm 4；输出 /p/grm.txt；后台 task_id=abc .done=/p/x.done"

    ag.summarize = fake_summarize
    ag.history = _long_history()
    n0 = len(ag.history)
    ag._maybe_compact()

    assert ag._compacted
    assert len(ag.history) < n0
    _valid_history(ag.history)                                  # 不变量
    assert ag.history[0]["role"] == "user"
    assert "【前情摘要" in ag.history[0]["content"]              # 摘要并入首条 user
    assert len([m for m in ag.history[:2] if m["role"] == "user"]) == 1   # 未造连续 user


def test_summary_failure_falls_back_to_drop():
    ag = _agent()
    ag.summary_compressor = SummaryCompressor()
    ag.summarize = lambda p: None           # 摘要失败
    ag.history = _long_history()
    n0 = len(ag.history)
    ag._maybe_compact()
    assert len(ag.history) < n0
    _valid_history(ag.history)
    assert "【前情摘要" not in str(ag.history[0])   # 回退旧逻辑，无摘要前缀


def test_summary_prompt_mentions_required_fields():
    ag = _agent()
    ag.summary_compressor = SummaryCompressor()
    seen = {}
    ag.summarize = lambda p: (seen.setdefault("p", p), "s")[1]
    ag.history = _long_history()
    ag._maybe_compact()
    p = seen["p"]
    for kw in ("阶段号", "后台任务", "审批结论", "log_path"):
        assert kw in p


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
