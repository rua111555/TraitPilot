"""G5 判据：**压缩结果能否挺过一次 resume**（用户诉求「继续这个对话框」的硬前提）。

用户诉求（逐字）：
  「我不想太固定最大轮次，而是每次快达到上下文上限的时候，进行上下文的压缩，
    然后继续这个对话框」

「继续这个对话框」= 压缩后接着聊。若压缩只发生在**内存**里、resume 时被磁盘原始
历史覆盖，那么每次重开都会退回压缩前 → 迟早撞上上下文上限 → 该对话**无法继续**。
所以本探针问的不是「压缩函数返回了什么」，而是**磁盘上最终留下了什么**。

两条分支都测（它们是不同代码路径）：
  drop    丢弃式（默认路径：summary_compaction=False，web 默认走这条）
  summary 摘要式（summary_compaction=True）

数据流（不带我的修）：
  触发压缩 → history 从 N 缩到 M（内存）
            → 而 `<sid>.jsonl` 里**新消息仍追加在旧消息之后**
            → load() 读原始 jsonl → _analyze 截断 → 返回 N 条 → resume 覆盖内存
  即：压缩被 resume 撤销。

关键机制（_read_raw 第 141-146 行）：
  `_prefix` 不是「追加锚点」而是**覆盖点**：`raw = [prefix]`，其**之前**的行全部丢弃。
  所以 _prefix 只能作为**最后一个锚点**使用，或必须自带整个保留窗口。

跑法（不联网、不写源码）：
    /home/chenyuxi/anaconda3/envs/Agent/bin/python -I tools/concurrency/compact_resume_probe.py
"""
import os
import sys
import tempfile

sys.path.insert(0, "/home/chenyuxi/skill/agent")

from skill_agent.agent import Agent  # noqa: E402
from skill_agent.compaction import SummaryCompressor  # noqa: E402
from skill_agent.session_store import SessionStore  # noqa: E402

log = []
N_ROUNDS = 25          # 25 轮 → 100 条消息，> COMPACT_MIN_MESSAGES(40)
KEEP_RECENT = 12       # COMPACT_KEEP_RECENT


def ok(name, cond, extra=""):
    log.append(("PASS" if cond else "FAIL") + " " + name)
    print(("  [PASS] " if cond else "  [缺陷] ") + name, flush=True)
    if extra:
        print("         " + extra, flush=True)


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


def _history(n_rounds):
    """工具轮次形态的历史：user → assistant(tool_use) → user(tool_result) → assistant(text)。"""
    msgs = []
    for i in range(n_rounds):
        msgs.append({"role": "user", "content": f"q{i}"})
        msgs.append({"role": "assistant", "content": [
            {"type": "tool_use", "id": f"t{i}", "name": "Bash",
             "input": {"command": f"cmd{i}"}}]})
        msgs.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": f"t{i}", "content": "ok"}]})
        msgs.append({"role": "assistant", "content": [{"type": "text", "text": f"a{i}"}]})
    return msgs


def _make_agent(store, sid):
    ag = Agent(_Cfg(), _Reg(), _Tools())
    ag.store = store
    ag.session_id = sid
    return ag


def _persist_all(ag, hist):
    """忠实复刻运行期落盘：每条消息都 append（agent._persist）。"""
    for m in hist:
        ag.store.append(ag.session_id, m)
    ag.history = list(hist)


def run_branch(label, use_summary):
    """跑一条分支，返回 (内存条数, 磁盘 resume 条数, 保留=?)"""
    tmp = tempfile.mkdtemp(prefix=f"cresume_{label}_")
    st = SessionStore(tmp)
    sid = "s_" + label
    st.append_meta(sid, {"id": sid, "model": "m"})

    ag = _make_agent(st, sid)
    if use_summary:
        ag.summary_compressor = SummaryCompressor()
        ag.summarize = lambda p: (
            "【前情摘要】已完成 25 轮质控。task_id=T7 pid=12345 "
            "标记文件 /data/tasks/T7.done 路径 /data/qc/clean.vcf.gz")
    hist = _history(N_ROUNDS)
    _persist_all(ag, hist)

    n_before = len(ag.history)
    n_disk_before = len(st.load(sid)["messages"])
    ag._maybe_compact()
    n_mem = len(ag.history)

    # ---- 模拟「压缩之后用户接着说了一句」----
    ag.history.append({"role": "user", "content": "继续"})
    ag.store.append(sid, {"role": "user", "content": "继续"})
    ag.history.append({"role": "assistant", "content": [{"type": "text", "text": "好的"}]})
    ag.store.append(sid, {"role": "assistant",
                          "content": [{"type": "text", "text": "好的"}]})
    n_mem_after = len(ag.history)

    # ---- resume：新 agent 从磁盘恢复 ----
    ag2 = _make_agent(st, sid)
    data = st.load(sid)
    ag2.restore(data["messages"])
    n_disk_after = len(ag2.history)

    # 「压缩保住」的判据：磁盘恢复出来的历史**短于**压缩前的原始落盘量，
    # 且**长于等于**压缩后的内存量（保留了摘要 + 后续对话）。
    kept = n_disk_after < n_disk_before
    print(f"\n[{label}] 压缩前 内存={n_before} 磁盘={n_disk_before}"
          f" → 压缩后 内存={n_mem} → 又聊了 2 条 内存={n_mem_after}"
          f" → resume 恢复 磁盘={n_disk_after}", flush=True)
    # 摘要是否还在
    has_summary = any("前情摘要" in str(m.get("content", "")) for m in ag2.history)
    print(f"     摘要是否出现在 resume 后的历史里 = {has_summary}", flush=True)
    # ⚠️ 判据的演化（诚实记录）：第一版只问「磁盘条数 < 压缩前落盘量 且 摘要还在」——
    #    **没有区分度**：摘要式压缩下 resume 只回来 3 条（内存里明明有 14 条）
    #    也照样判 PASS，因为「比 100 小」且「摘要还在」两条都成立。
    #    真正要问的是**内存与磁盘是否一致**：resume 回来的历史必须等于
    #    「压缩后的窗口 + 压缩之后追加的消息」，一条不多一条不少。
    consistent = (n_disk_after == n_mem_after)
    print(f"     内存={n_mem_after} 条 vs resume={n_disk_after} 条 → 一致={consistent}"
          f"（差 {n_mem_after - n_disk_after} 条）", flush=True)
    return (n_disk_before, n_mem, n_disk_after, kept, has_summary,
            consistent, n_mem_after, tmp, st, sid)


print("== 分支一：丢弃式压缩（web 默认路径） ==", flush=True)
b1 = run_branch("drop", use_summary=False)
ok("G5-drop 压缩能挺过 resume（磁盘恢复 == 压缩后的内存窗口）",
   b1[5],
   f"压缩前落盘 {b1[0]} 条 → 压缩后内存 {b1[6]} 条 → resume 恢复 {b1[2]} 条"
   f" → 一致={b1[5]}"
   + ("" if b1[5] else "：磁盘仍是压缩前的原始历史，内存里的压缩**被 resume 撤销**"))

print("\n== 分支二：摘要式压缩（summary_compaction=True） ==", flush=True)
b2 = run_branch("summary", use_summary=True)
ok("G5-summary 压缩能挺过 resume（内存 == 磁盘，且摘要仍在）",
   b2[5] and b2[4],
   f"压缩前落盘 {b2[0]} 条 → 压缩后内存 {b2[6]} 条 → resume 恢复 {b2[2]} 条"
   f"；摘要仍在={b2[4]} → 一致={b2[5]}"
   + ("" if b2[5] else f"：**保留了摘要却丢了 {b2[6] - b2[2]} 条保留窗口**"
      "（_prefix 的 raw=[prefix] 是覆盖点，其之前追加的窗口被丢弃）"))

# ================= 修法的原型验证（不改源码，只在子类里模拟） =================
print("\n== 修法原型：_prefix 携带**整个保留窗口**（追加式，不改 store 的 append-only 语义） ==",
      flush=True)


class _FixedStore(SessionStore):
    """给 _prefix 增加 payloads 形态：一次写入整个保留窗口。

    向后兼容：老形态 {"_prefix":True,"message":m} 仍然等价于 raw=[m]。
    """

    def append_prefix(self, sid, messages):
        if not self.enabled or not self._valid(sid):
            return
        p = self.root / f"{sid}.jsonl"
        import json as _json
        line = _json.dumps({"_prefix": True, "messages": list(messages)},
                           ensure_ascii=False)
        with self._lock:
            try:
                with open(p, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
                    f.flush()
                    os.fsync(f.fileno())
            except OSError:
                pass


def _read_raw_patched(self, sid):
    """支持新形态 payloads 的 _read_raw（只在探针里模拟）。"""
    import json as _json
    if not self.enabled or not self._valid(sid):
        return [], {}, False
    p = self.root / f"{sid}.jsonl"
    if not p.is_file():
        return [], {}, False
    raw, meta = [], {}
    try:
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = _json.loads(line)
            except _json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            if obj.get("_meta"):
                meta = obj
            elif obj.get("_merge"):
                if raw and raw[-1].get("role") == "assistant":
                    raw[-1]["content"] = list(raw[-1].get("content") or []) + \
                        list(obj.get("content") or [])
            elif obj.get("_prefix"):
                pm = obj.get("messages")
                if isinstance(pm, list) and pm:
                    raw = list(pm)                      # 新形态：整个窗口
                else:
                    m = obj.get("message")
                    if isinstance(m, dict) and m.get("role"):
                        raw = [m]                       # 老形态：单条
            elif obj.get("role"):
                raw.append(obj)
    except OSError:
        return [], {}, False
    return raw, meta, True


class _FixedAgent(Agent):
    """只覆盖丢弃分支：压缩后把**整个**保留窗口作为一个 _prefix 锚点落盘。"""

    def _maybe_compact(self):
        h = self.history
        if not h:
            return
        from skill_agent.agent import COMPACT_MIN_MESSAGES, COMPACT_MAX_CHARS, _msg_bytes
        budget_hit = sum(_msg_bytes(m) for m in h) > COMPACT_MAX_CHARS
        over_msgs = len(h) > COMPACT_MIN_MESSAGES
        if (over_msgs or budget_hit) and self.summary_compressor is not None \
                and self.summarize is not None:
            if self._summary_compact():
                return
        if over_msgs or budget_hit:
            cut = self._safe_cut(len(h) - COMPACT_MIN_MESSAGES) if over_msgs else 0
            if budget_hit and cut > 0:
                cut = self._safe_cut(cut)
            if cut > 0:
                self.history = h[cut:]
                self._compacted = True
                if self.store is not None and self.session_id:
                    try:
                        self.store.append_prefix(self.session_id, self.history)
                    except Exception:
                        pass


tmp = tempfile.mkdtemp(prefix="cresume_fixed_")
st = _FixedStore(tmp)
sid = "s_fixed"
st.append_meta(sid, {"id": sid, "model": "m"})
ag = _FixedAgent(_Cfg(), _Reg(), _Tools())
ag.store = st
ag.session_id = sid
hist = _history(N_ROUNDS)
_persist_all(ag, hist)
n_before = len(st.load(sid)["messages"])
ag._maybe_compact()
n_mem = len(ag.history)
ag.history.append({"role": "user", "content": "继续"})
ag.store.append(sid, {"role": "user", "content": "继续"})
ag.history.append({"role": "assistant", "content": [{"type": "text", "text": "好的"}]})
ag.store.append(sid, {"role": "assistant", "content": [{"type": "text", "text": "好的"}]})

# 用打过补丁的 _read_raw 读回（模拟修好后的 store）
data = st.load(sid)          # 老 load：只认 message 形态 → 期望 raw=[]（因为新形态无 message）
raw, _meta, _found = _read_raw_patched(st, sid)
from skill_agent.session_store import _analyze  # noqa: E402
resumable, tail, disc, reason = _analyze(raw)
print(f"\n[修法] 压缩前落盘={n_before} 压缩后内存={n_mem} → 新形态读回 raw={len(raw)} "
      f"resumable={len(resumable)} discarded={disc} reason={reason}", flush=True)
print(f"     （对照）老 load() 读同一文件 = {len(data['messages'])} 条"
      f" → 说明新形态对**旧代码**不可见，需同时改 _read_raw", flush=True)
ok("修法原型：整个保留窗口随 _prefix 落盘，resume 后 = 压缩后内存 + 后续对话",
   len(resumable) == n_mem + 2,
   f"resumable={len(resumable)}，期望={n_mem + 2}（={n_mem} 压缩窗口 + 2 条后续）"
   f"；reason={reason}")
ok("修法原型：resume 后的历史满足续跑不变量（以「不含 tool_use 的 assistant」结尾）",
   bool(resumable) and resumable[-1].get("role") == "assistant"
   and not any(b.get("type") == "tool_use"
               for b in (resumable[-1]["content"] if isinstance(resumable[-1]["content"], list)
                         else [])),
   f"末条 role={resumable[-1].get('role') if resumable else None}")

# ============ 向后兼容：旧日志里的单条 `message` 形态必须仍能读 ============
# 旧版本 `_persist_prefix` 写的是 {"_prefix":True,"message":m}（单条）。磁盘上可能
# 已经有这种日志（summary_compaction 曾开启过的会话），改读侧时不能把它们读坏。
print("\n== 向后兼容：旧形态 `_prefix`（单条 message）仍按「单条起点」解析 ==", flush=True)
tmp = tempfile.mkdtemp(prefix="cresume_legacy_")
st = SessionStore(tmp)
sid = "s_legacy"
st.append_meta(sid, {"id": sid, "model": "m"})
for i in range(6):
    st.append(sid, {"role": "user", "content": f"old_u{i}"})
    st.append(sid, {"role": "assistant", "content": [{"type": "text", "text": f"old_a{i}"}]})
st.append(sid, {"_prefix": True, "message": {"role": "user", "content": "压缩边界"}})
st.append(sid, {"role": "assistant", "content": [{"type": "text", "text": "边界之后"}]})
leg = st.load(sid)
ok("向后兼容：旧单条形态仍解析为「以该条为起点」（=2 条），不回退成整段 14 条",
   len(leg["messages"]) == 2,
   f"读回 {len(leg['messages'])} 条（期望 2：边界条 + 其后 1 条）；"
   f"内容首条={str(leg['messages'][0].get('content'))[:20]!r}")

# 同一条日志用**展示视图**读，必须与续跑视图一致（否则用户切过去看到的是压缩前全文）
view = st.load_view(sid)
ok("向后兼容：展示视图同样尊重旧形态锚点（resume_count 与续跑条数一致）",
   view["resume_count"] == len(leg["messages"]),
   f"load_view.resume_count={view['resume_count']}，load 条数={len(leg['messages'])}")

print("\n---- summary ----")
for l in log:
    print(l)
failed = sum(1 for l in log if l.startswith("FAIL"))
print("FAILED:", failed)
sys.exit(1 if failed else 0)
