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
            def create(self, *, system, tools, messages, stream, on_delta, on_thinking=None):
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


# ---------------- 归档 / 删除 ----------------

def test_archive_moves_out_of_listing():
    with tempfile.TemporaryDirectory() as d:
        st = SessionStore(Path(d))
        st.append("s1", {"role": "user", "content": "hi"})
        st.append("s1", {"role": "assistant", "content": [{"type": "text", "text": "yo"}]})
        st.append("s2", {"role": "user", "content": "yo"})
        st.append("s2", {"role": "assistant", "content": [{"type": "text", "text": "ok"}]})
        assert {s["id"] for s in st.sessions()} == {"s1", "s2"}
        assert st.archive("s1") is True
        assert {s["id"] for s in st.sessions()} == {"s2"}          # 归档后默认列表不含
        assert [s["id"] for s in st.sessions(archived=True)] == ["s1"]
        assert st.is_archived("s1") and st.exists("s1") is False
        # 归档文件仍可读（历史查看）
        assert st.load("s1")["messages"]


def test_unarchive_restores():
    with tempfile.TemporaryDirectory() as d:
        st = SessionStore(Path(d))
        st.append("s1", {"role": "user", "content": "hi"})
        st.archive("s1")
        assert st.unarchive("s1") is True
        assert [s["id"] for s in st.sessions()] == ["s1"]
        assert st.sessions(archived=True) == []


def test_delete_removes_file():
    with tempfile.TemporaryDirectory() as d:
        st = SessionStore(Path(d))
        st.append("s1", {"role": "user", "content": "hi"})
        assert st.delete("s1") is True
        assert st.sessions() == []
        assert st.delete("nope") is False


def test_archive_and_delete_reject_bad_sid():
    with tempfile.TemporaryDirectory() as d:
        st = SessionStore(Path(d))
        assert st.archive("../evil") is False
        assert st.delete("../evil") is False
        assert st.unarchive("../evil") is False


# ---------------- 展示视图 load_view（切历史会话看不到内容） ----------------

def test_load_view_keeps_unanswered_user_while_load_drops_it():
    """用户问了、模型从未回复 → 续跑视图整段清空，展示视图必须保留用户那句话。"""
    with tempfile.TemporaryDirectory() as d:
        st = SessionStore(Path(d))
        st.append("s1", {"role": "user", "content": "数据库里有什么？"})
        # 续跑视图：仍按边界清空（续跑不变量，不能动）
        assert st.load("s1")["messages"] == []
        assert st.load("s1")["discarded"] == 1
        # 展示视图：磁盘上有的都回放，并说明原因
        v = st.load_view("s1")
        assert len(v["messages"]) == 1 and v["messages"][0]["role"] == "user"
        assert v["reason"] == "no_completed_assistant"
        assert v["resume_count"] == 0 and v["discarded"] == 1


def test_load_view_keeps_incomplete_trailing_turn():
    """尾轮未完成（悬空 tool_use）→ 展示视图含该轮，load 截断到上一完成轮。"""
    with tempfile.TemporaryDirectory() as d:
        st = SessionStore(Path(d))
        st.append("s2", {"role": "user", "content": "q1"})
        st.append("s2", {"role": "assistant", "content": [{"type": "text", "text": "a1"}]})
        st.append("s2", {"role": "user", "content": "q2"})
        st.append("s2", {"role": "assistant", "content": [
            {"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}]})
        assert len(st.load("s2")["messages"]) == 2          # 续跑：截到 a1
        v = st.load_view("s2")
        # 展示：q1/a1/q2 全在，连那条只剩悬空 tool_use 的 assistant 也保留
        assert [m["role"] for m in v["messages"]] == ["user", "assistant", "user", "assistant"]
        assert v["messages"][2]["content"] == "q2"
        assert v["resume_count"] == 2 and v["discarded"] == 1
        assert v["reason"] == "incomplete_turn"
        assert len(v["incomplete"]) == 2

def test_load_view_marks_complete_session_clean():
    with tempfile.TemporaryDirectory() as d:
        st = SessionStore(Path(d))
        st.append("s3", {"role": "user", "content": "q"})
        st.append("s3", {"role": "assistant", "content": [{"type": "text", "text": "a"}]})
        v = st.load_view("s3")
        assert v["reason"] is None and v["discarded"] == 0
        assert v["resume_count"] == len(v["messages"]) == 2


def test_load_view_covers_multi_tool_round_session():
    """整段会话每一轮都带 tool_use（常见于「只读数据、还没总结」）→ 以前整段空白。"""
    with tempfile.TemporaryDirectory() as d:
        st = SessionStore(Path(d))
        st.append("s4", {"role": "user", "content": "看看库"})
        st.append("s4", {"role": "assistant", "content": [
            {"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}]})
        st.append("s4", {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]})
        st.append("s4", {"role": "assistant", "content": [
            {"type": "tool_use", "id": "t2", "name": "Read", "input": {}}]})
        assert st.load("s4")["messages"] == []              # 续跑：整段清空（无完成边界）
        v = st.load_view("s4")
        # 展示：4 条全在（悬空 tool_use 也保留，用户能看到「调了工具但中断」）
        assert [m["role"] for m in v["messages"]] == ["user", "assistant", "user", "assistant"]
        assert v["reason"] == "no_completed_assistant"
        assert v["resume_count"] == 0                       # 全都不会被续跑带上
        assert len(v["incomplete"]) == 4


def test_load_view_tolerates_missing_and_bad_sid():
    with tempfile.TemporaryDirectory() as d:
        st = SessionStore(Path(d))
        v = st.load_view("nope")
        assert v["found"] is False and v["messages"] == []
        assert st.load_view("../evil")["messages"] == []


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
