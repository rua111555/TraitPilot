"""M13 会话持久化/恢复单测：落盘往返、悬挂 tool_use 修复、路径安全、resume 集成。"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pathlib import Path  # noqa: E402

from skill_agent.agent import Agent  # noqa: E402
from skill_agent.session import resume_into  # noqa: E402
from skill_agent.session_store import SessionStore, new_session_id  # noqa: E402


class _Cfg:
    model = "m"; base_url = "http://x"; api_key = "k"; max_tokens = 16
    stream = False; verbose = False; cwd = None; session_id = None; persist = False


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


# ---------------- 存储往返 ----------------

def test_append_and_load_roundtrip():
    with tempfile.TemporaryDirectory() as d:
        st = SessionStore(Path(d))
        sid = new_session_id()
        st.append_meta(sid, {"model": "m"})
        st.append(sid, {"role": "user", "content": "hi"})
        st.append(sid, {"role": "assistant", "content": [{"type": "text", "text": "yo"}]})
        data = st.load(sid)
        assert data["meta"]["model"] == "m"
        assert [m["role"] for m in data["messages"]] == ["user", "assistant"]
        assert data["discarded"] == 0


def test_repair_drops_dangling_tool_use():
    with tempfile.TemporaryDirectory() as d:
        st = SessionStore(Path(d))
        sid = "s1"
        # 完整一轮 + 悬挂轮（assistant 带 tool_use，但 tool_result 未落盘 = 崩溃点）
        st.append(sid, {"role": "user", "content": "q0"})
        st.append(sid, {"role": "assistant", "content": [
            {"type": "tool_use", "id": "t0", "name": "Bash", "input": {}}]})
        st.append(sid, {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t0", "content": "ok"}]})
        st.append(sid, {"role": "assistant", "content": [{"type": "text", "text": "final"}]})
        st.append(sid, {"role": "user", "content": "q1"})
        st.append(sid, {"role": "assistant", "content": [
            {"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}]})  # 悬挂

        data = st.load(sid)
        msgs = data["messages"]
        assert data["discarded"] == 1
        # 恢复到安全边界：最后一条必须是「不含 tool_use 的 assistant」
        assert msgs[-1]["role"] == "assistant"
        assert not any(b.get("type") == "tool_use" for b in _blocks(msgs[-1]))
        assert "final" in str(msgs[-1])


def test_repair_preserves_text_when_stripping_tool_use():
    with tempfile.TemporaryDirectory() as d:
        st = SessionStore(Path(d))
        sid = "s2"
        st.append(sid, {"role": "user", "content": "q"})
        st.append(sid, {"role": "assistant", "content": [
            {"type": "text", "text": "我先看看"},
            {"type": "tool_use", "id": "tX", "name": "Read", "input": {}}]})
        st.append(sid, {"role": "assistant", "content": [{"type": "text", "text": "end"}]})
        msgs = st.load(sid)["messages"]
        # 悬挂的 assistant 被整体丢弃（回到边界）；text-only 的助手保留
        assert msgs[-1]["role"] == "assistant"
        assert "end" in str(msgs[-1])
        for m in msgs:
            assert not any(b.get("type") == "tool_use" for b in _blocks(m))


# ---------------- 路径安全 ----------------

def test_path_traversal_rejected():
    with tempfile.TemporaryDirectory() as d:
        st = SessionStore(Path(d))
        st.append("../../etc/passwd", {"role": "user", "content": "x"})
        assert st.load("../../etc/passwd")["messages"] == []
        assert st.exists("../evil") is False and st.sessions() == []


def test_tolerates_corrupt_tail_line():
    with tempfile.TemporaryDirectory() as d:
        st = SessionStore(Path(d))
        sid = "s3"
        st.append(sid, {"role": "user", "content": "a"})
        st.append(sid, {"role": "assistant", "content": [{"type": "text", "text": "done"}]})
        with open(Path(d) / f"{sid}.jsonl", "a") as f:
            f.write('{"role": "assistant", "content": [{"type": "text", ')  # 半截（崩溃）
        msgs = st.load(sid)["messages"]        # 不抛异常，跳过坏行
        assert [m["role"] for m in msgs] == ["user", "assistant"]
        assert "done" in str(msgs[1])


# ---------------- resume 集成 ----------------

def test_agent_persist_then_resume_roundtrip():
    with tempfile.TemporaryDirectory() as d:
        st = SessionStore(Path(d))
        sid = "sess-1"

        # 第一条消息经 answer 落盘（不触网：stub client）
        ag = Agent(_Cfg(), _Reg(), _Tools())
        ag.store = st
        ag.session_id = sid

        class _Client:
            def create(self, *, system, tools, messages, stream, on_delta):
                return ([{"type": "text", "text": "Z"} if not messages or True else ""], "end_turn", 1)

        ag.client = _Client()
        ag.answer("hello")              # 落盘 user + assistant
        # 至少 user 与 assistant 各落一条
        msgs = st.load(sid)["messages"]
        assert msgs[0]["role"] == "user"

        # 新 agent 恢复
        ag2 = Agent(_Cfg(), _Reg(), _Tools())
        info = resume_into(ag2, st, sid)
        assert info["restored"] >= 1
        assert ag2.history and ag2.history[0]["role"] == "user"


def test_prefixed_boundary_overrides_prefix_messages():
    with tempfile.TemporaryDirectory() as d:
        st = SessionStore(Path(d))
        sid = "s4"
        st.append(sid, {"role": "user", "content": "old"})
        st.append(sid, {"role": "assistant", "content": [{"type": "text", "text": "old-a"}]})
        # 压缩边界：前缀消息应覆盖之前的一切
        st.append(sid, {"_prefix": True,
                        "message": {"role": "user", "content": "【前情摘要】S3 已完成"}})
        st.append(sid, {"role": "assistant", "content": [{"type": "text", "text": "new"}]})
        msgs = st.load(sid)["messages"]
        assert len(msgs) == 2
        assert "前情摘要" in msgs[0]["content"] and "old" not in msgs[0]["content"]


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
