"""M12 摘要式压缩单测：默认关=旧行为；开启后满足配对/角色不变量并保留关键字段。"""
import json
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


# ---------------- 压缩必须能挺过 resume（「继续这个对话框」的硬前提） ----------------
# 这一组是 G5 的常驻回归：压缩若只活在内存里，resume 会原样读回压缩前的历史，
# 用户重开对话就退回压缩前 → 迟早撞上下文上限 → 对话无法继续。


def _store_agent(tmp_path, sid="cmp"):
    """带持久化的 agent：history 与磁盘同步（复刻运行期落盘）。"""
    from skill_agent.session_store import SessionStore
    st = SessionStore(tmp_path)
    ag = Agent(_Cfg(), _Reg(), _Tools())
    ag.store = st
    ag.session_id = sid
    return ag, st


def _persist(ag, hist):
    for m in hist:
        ag.store.append(ag.session_id, m)
    ag.history = list(hist)


def _resume_count(st, sid):
    return len(st.load(sid)["messages"])


def test_drop_compaction_survives_resume(tmp_path):
    """丢弃式压缩（默认路径）必须落锚点，否则 resume 撤销压缩。"""
    ag, st = _store_agent(tmp_path)
    _persist(ag, _long_history())
    n_disk_before = _resume_count(st, "cmp")

    ag._maybe_compact()
    assert ag._compacted and len(ag.history) < n_disk_before

    # 压缩后接着聊两条（用户诉求：压缩后**继续**这个对话框）
    for role, content in (("user", "继续"),
                          ("assistant", [{"type": "text", "text": "好的"}])):
        ag.history.append({"role": role, "content": content})
        ag.store.append("cmp", {"role": role, "content": content})

    n_mem = len(ag.history)
    assert _resume_count(st, "cmp") == n_mem, \
        "resume 结果必须等于压缩后的内存窗口（否则压缩被静默撤销）"
    assert n_mem < n_disk_before            # 确实是被压缩了，不是碰巧相等


def test_summary_compaction_survives_resume_with_summary(tmp_path):
    """摘要式压缩：整窗落盘，resume 后摘要与保留窗口**都**要在。"""
    ag, st = _store_agent(tmp_path, sid="cmps")
    ag.summary_compressor = SummaryCompressor()
    ag.summarize = lambda p: "【前情摘要】阶段 S3 完成；task_id=T7 pid=12345"
    _persist(ag, _long_history())
    n_disk_before = _resume_count(st, "cmps")

    ag._maybe_compact()
    assert ag._compacted
    ag.history.append({"role": "user", "content": "继续"})
    ag.store.append("cmps", {"role": "user", "content": "继续"})
    ag.history.append({"role": "assistant", "content": [{"type": "text", "text": "好"}]})
    ag.store.append("cmps", {"role": "assistant", "content": [{"type": "text", "text": "好"}]})

    n_mem = len(ag.history)
    assert _resume_count(st, "cmps") == n_mem
    assert n_mem < n_disk_before
    resumed = st.load("cmps")["messages"]
    assert any("前情摘要" in str(m.get("content", "")) for m in resumed), \
        "摘要必须留在 resume 后的历史里"
    _valid_history(resumed)


def test_prefix_window_form_is_read_back_whole(tmp_path):
    """`_prefix` 新形态（messages=[...]）必须整窗读回，而不是只留第一条。

    反例（旧实现）：只把边界那一条当起点 → 锚点之前落盘的保留窗口全被覆盖丢弃
    （实测摘要式压缩后内存 14 条、resume 只回 3 条）。
    """
    from skill_agent.session_store import SessionStore
    st = SessionStore(tmp_path)
    st.append_meta("pfx", {"id": "pfx"})
    st.append("pfx", {"role": "user", "content": "很久以前的问题"})
    st.append("pfx", {"role": "assistant", "content": [{"type": "text", "text": "很久以前"}]})
    window = [{"role": "user", "content": "保留窗口 1"},
              {"role": "assistant", "content": [{"type": "text", "text": "保留 1"}]},
              {"role": "user", "content": "保留窗口 2"},
              {"role": "assistant", "content": [{"type": "text", "text": "保留 2"}]}]
    st.append_prefix("pfx", window)
    st.append("pfx", {"role": "user", "content": "锚点之后"})
    st.append("pfx", {"role": "assistant", "content": [{"type": "text", "text": "之后"}]})

    got = st.load("pfx")["messages"]
    assert [m.get("content") for m in got[:4]] == [m["content"] for m in window], \
        f"整窗未读回：{got[:4]}"
    assert len(got) == 6                    # 4 条窗口 + 2 条锚点之后
    assert all("很久以前" not in str(m) for m in got)   # 锚点之前确实被覆盖


def test_legacy_single_message_prefix_still_reads(tmp_path):
    """旧日志里的单条 `_prefix` 形态必须仍按「单条起点」解析（向后兼容）。"""
    from skill_agent.session_store import SessionStore
    st = SessionStore(tmp_path)
    st.append_meta("leg", {"id": "leg"})
    st.append("leg", {"role": "user", "content": "旧的"})
    st.append("leg", {"role": "assistant", "content": [{"type": "text", "text": "旧的答"}]})
    st.append("leg", {"_prefix": True, "message": {"role": "user", "content": "边界"}})
    st.append("leg", {"role": "assistant", "content": [{"type": "text", "text": "之后"}]})

    got = st.load("leg")["messages"]
    assert len(got) == 2, f"旧形态应解析为 2 条（边界 + 之后），实际 {len(got)}"
    assert got[0]["content"] == "边界"


# ---------------- 上下文用量判据（token 口径，非固定轮次） ----------------


def _big_history(n_rounds, pad):
    msgs = []
    for i in range(n_rounds):
        msgs.append({"role": "user", "content": f"q{i}" + "x" * pad})
        msgs.append({"role": "assistant", "content": [
            {"type": "tool_use", "id": f"t{i}", "name": "Bash",
             "input": {"command": f"c{i}"}}]})
        msgs.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": f"t{i}", "content": "ok"}]})
        msgs.append({"role": "assistant", "content": [{"type": "text", "text": f"a{i}"}]})
    return msgs


def test_context_pressure_triggers_compaction():
    """真实用量到阈值、条数**未超** → 仍压缩（证明是 token 口径在起作用）。

    构造必须**隔离** token 判据：
      · 条数 < COMPACT_MIN_MESSAGES  → 排除「条数判据」
      · 总字符 < COMPACT_MAX_CHARS   → 排除「字符兜底」（否则是它在裁，不是 token）
      · 总字符 > COMPACT_KEEP_BYTES  → 保证按字节确实**有得裁**（否则「不裁」也合法）
    """
    from skill_agent.agent import (
        COMPACT_AT_CONTEXT_RATIO, COMPACT_KEEP_BYTES, COMPACT_MAX_CHARS,
        CONTEXT_LIMIT_FALLBACK,
    )
    ag = _agent()
    ag.history = _big_history(9, 40_000)           # 36 条，约 360k 字符
    n0 = len(ag.history)
    total_chars = sum(len(json.dumps(m, ensure_ascii=False)) for m in ag.history)
    assert n0 < COMPACT_MIN_MESSAGES, "构造前提：条数未超"
    assert total_chars < COMPACT_MAX_CHARS, \
        f"构造前提：字符数须低于 {COMPACT_MAX_CHARS}（否则字符兜底会替 token 判据做功）"
    assert total_chars > COMPACT_KEEP_BYTES, "构造前提：须有得裁"

    ag.client.last_input_tokens = int(CONTEXT_LIMIT_FALLBACK * COMPACT_AT_CONTEXT_RATIO) + 1
    ag._maybe_compact()
    assert ag._compacted and len(ag.history) < n0


def test_no_compaction_when_context_is_small():
    """阴性对照：用量远低于阈值、条数未超、字符未超 → **不许裁**。

    与上一条用**同一个历史**（同样「有得裁」），只把用量换成低值 ——
    这样差异只可能来自 token 判据本身。没有这条，上一条会退化成
    「永远裁」，那压缩就成了纯粹的上下文丢失。
    """
    from skill_agent.agent import (
        COMPACT_AT_CONTEXT_RATIO, COMPACT_KEEP_BYTES, COMPACT_MAX_CHARS,
        CONTEXT_LIMIT_FALLBACK,
    )
    ag = _agent()
    ag.history = _big_history(9, 40_000)           # 与上一条同一构造
    n0 = len(ag.history)
    total_chars = sum(len(json.dumps(m, ensure_ascii=False)) for m in ag.history)
    assert n0 < COMPACT_MIN_MESSAGES and COMPACT_KEEP_BYTES < total_chars < COMPACT_MAX_CHARS
    ag.client.last_input_tokens = 5_000            # 远低于阈值
    ag._maybe_compact()
    assert len(ag.history) == n0 and not ag._compacted, \
        f"低用量被误判为需压缩（{n0} → {len(ag.history)}）；阈值=" \
        f"{int(CONTEXT_LIMIT_FALLBACK * COMPACT_AT_CONTEXT_RATIO)}"


def test_few_but_huge_messages_still_compact_and_do_not_empty():
    """条数很少但每条极大（生信典型：一条 VCF 输出）→ 能裁且**不清空**。

    两个反例：① 按条数算裁点 → len(h)-KEEP_RECENT ≤ 0 → 裁不动；
              ② 裁到 len(h) → 历史被清空（比不裁更糟：丢失本轮全部前情）。
    """
    from skill_agent.agent import COMPACT_AT_CONTEXT_RATIO, CONTEXT_LIMIT_FALLBACK
    ag = _agent()
    hist = []
    for i in range(6):                              # 12 条，远小于 40
        hist.append({"role": "user", "content": f"q{i}"})
        hist.append({"role": "assistant",
                     "content": [{"type": "text", "text": "blob" * 50_000}]})
    ag.history = hist
    n0 = len(ag.history)
    ag.client.last_input_tokens = int(CONTEXT_LIMIT_FALLBACK * COMPACT_AT_CONTEXT_RATIO) + 1
    ag._maybe_compact()
    assert len(ag.history) < n0, "应能裁动"
    assert len(ag.history) >= 2, "绝不能把历史清空"
    _valid_history(ag.history)


def test_unknown_usage_is_not_treated_as_full():
    """拿不到 usage（=0）时不许当成「已满」—— 否则会话一开始就被裁。"""
    ag = _agent()
    ag.history = _big_history(9, 200)               # 小历史，字节也不超
    n0 = len(ag.history)
    ag.client.last_input_tokens = 0
    ag._maybe_compact()
    assert len(ag.history) == n0 and not ag._compacted


def test_safe_cut_returns_negative_when_no_valid_boundary():
    """找不到合法边界必须返回 -1，而不是 start。

    返回 start 会让调用方执行 h[start:]，留下「以 assistant 开头」的畸形历史
    （Messages API 直接 400）；返回 len(h) 则会把历史整段清空。
    """
    ag = _agent()
    ag.history = [{"role": "user", "content": "u"},
                  {"role": "assistant", "content": [
                      {"type": "tool_use", "id": "t", "name": "Bash", "input": {}}]}]
    assert ag._safe_cut(1) == -1            # 下标 1 是 assistant，之后无合法边界
    assert ag._safe_cut(len(ag.history)) == -1


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
