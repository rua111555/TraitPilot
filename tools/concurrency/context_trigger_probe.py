"""G6 判据：压缩由**真实上下文用量**触发，而不是固定轮次/条数。

用户诉求（逐字）：
  「我不想太固定最大轮次，而是每次快达到上下文上限的时候，进行上下文的压缩，
    然后继续这个对话框」

所以要证明两件事，缺一不可：
  G6a 真实用量到阈值时**确实触发**压缩            —— 反例：用量再高也不裁（阈值没接上）
  G6b 用量远低于阈值时**不触发**（不误裁）        —— 反例：会话一开始就把历史裁掉
  G6c 「条数少但每条极大」也能裁（生信常见形态）  —— 反例：靠 len(h) 判断 → 裁不动

G6b 是阴性对照：只有 G6a 的判据会退化成「永远裁」，那是废判据。

跑法（不联网：用量直接注入 client 属性，走真实 _maybe_compact）：
    /home/chenyuxi/anaconda3/envs/Agent/bin/python -I tools/concurrency/context_trigger_probe.py
"""
import os
import sys
import tempfile

sys.path.insert(0, "/home/chenyuxi/skill/agent")

from skill_agent import agent as A  # noqa: E402
from skill_agent.agent import Agent, COMPACT_MIN_MESSAGES  # noqa: E402
from skill_agent.session_store import SessionStore  # noqa: E402

log = []
LIMIT = A.CONTEXT_LIMIT_FALLBACK
RATIO = A.COMPACT_AT_CONTEXT_RATIO


def ok(name, cond, extra=""):
    log.append(("PASS" if cond else "FAIL") + " " + name)
    print(("  [PASS] " if cond else "  [缺陷] ") + name, flush=True)
    if extra:
        print("         " + extra, flush=True)


class _Cfg:
    model = "m"; base_url = "http://x"; api_key = "k"; max_tokens = 16
    stream = False; verbose = False; cwd = None; context_limit = None


class _Tools:
    def schemas_for(self, allowed): return []

    def execute(self, n, a):
        from skill_agent.tools import ToolResult
        return ToolResult("ok")

    def summarize_tasks(self): return ""


class _Reg:
    def listing(self): return ""

    def get(self, n): return None


def _mk(hist, sid="ctx"):
    st = SessionStore(tempfile.mkdtemp(prefix="ctxtrig_"))
    ag = Agent(_Cfg(), _Reg(), _Tools())
    ag.store = st
    ag.session_id = sid
    ag.history = list(hist)
    for m in hist:
        st.append(sid, m)
    return ag, st


def _rounds(n, pad=4):
    """n 轮工具往返；pad 控制单条大小。"""
    out = []
    for i in range(n):
        out.append({"role": "user", "content": f"q{i}" + "x" * pad})
        out.append({"role": "assistant", "content": [
            {"type": "tool_use", "id": f"t{i}", "name": "Bash",
             "input": {"command": f"c{i}"}}]})
        out.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": f"t{i}", "content": "ok"}]})
        out.append({"role": "assistant", "content": [{"type": "text", "text": f"a{i}"}]})
    return out


print(f"阈值：上下文上限 {LIMIT} × 比例 {RATIO} = 触发点 {int(LIMIT * RATIO)} token", flush=True)

# ---------------- G6a：真实用量到阈值 → 必须裁 ----------------
print("\nG6a 真实用量已达阈值（条数**未**超 40）—— 反例：用量再高也不裁", flush=True)
# 构造要点：条数 < 40（排除条数判据），但总字节数 > COMPACT_KEEP_BYTES
# （排除「字节预算内无需裁」这条合法的不裁理由）。否则测的就不是用量判据了。
ag, st = _mk(_rounds(9, pad=120_000))    # 36 条，每条 ~120KB → 总计 ~4MB
n0 = len(ag.history)
ag.client.last_input_tokens = int(LIMIT * RATIO) + 1_000
assert n0 < COMPACT_MIN_MESSAGES, "构造前提：条数必须未超阈值"
ag._maybe_compact()
ok("G6a 用量到阈值、条数未超 → 确实压缩（证明用量判据接上了）",
   len(ag.history) < n0 and ag._compacted,
   f"条数 {n0}（< {COMPACT_MIN_MESSAGES}）→ 压缩后 {len(ag.history)} 条；"
   f"用量={ag.client.last_input_tokens} ≥ {int(LIMIT * RATIO)}")

# ---------------- G6b：阴性对照 —— 用量低 → 不许裁 ----------------
print("\nG6b [阴性对照] 用量远低于阈值 —— 反例：会话一开始就被裁", flush=True)
ag2, _ = _mk(_rounds(9))
n2 = len(ag2.history)
ag2.client.last_input_tokens = 5_000     # 远低于阈值
ag2._maybe_compact()
ok("G6b 用量低、条数未超 → **不裁**（判据有区分度，不是恒裁）",
   len(ag2.history) == n2,
   f"条数 {n2} → {len(ag2.history)}；用量 5,000 < {int(LIMIT * RATIO)}")

# ---------------- G6c：条数少但每条极大 → 也必须裁 ----------------
print("\nG6c 「条数很少但每条极大」（生信常见：一条 VCF 输出）—— 反例：裁不动", flush=True)
big = []
for i in range(6):                       # 12 条，远小于 40
    big.append({"role": "user", "content": f"q{i}"})
    big.append({"role": "assistant", "content": [
        {"type": "text", "text": "blob" * 50_000}]})   # 每条 ~200k 字符
ag3, _ = _mk(big)
n3 = len(ag3.history)
ag3.client.last_input_tokens = int(LIMIT * RATIO) + 1
ag3._maybe_compact()
# ⚠️ 判据的演化（诚实记录）：第一版只问「裁完比裁前短」——
#    **没有区分度**：把历史整段清空（0 条）也算「变短」，照样判 PASS。
#    而「上下文被清空」与「压缩」是相反的结果：前者等于丢失本轮全部前情。
#    所以必须同时要求**留下了内容**（≥2 条：至少一条边界 + 一条 assistant）。
ok("G6c 条数远低于 40 但用量到阈值 → 仍能裁出合法边界，且**不清空**",
   len(ag3.history) < n3 and len(ag3.history) >= 2,
   f"条数 {n3}（远 < {COMPACT_MIN_MESSAGES}）→ 压缩后 {len(ag3.history)} 条"
   + ("" if 2 <= len(ag3.history) < n3
      else ("：**裁不动** —— 压缩对这类会话形同不存在" if len(ag3.history) >= n3
            else "：**把历史清空了** —— 比不裁更糟，等于丢失本轮全部前情")))

# ---------------- G6d：修完落盘，resume 一致 ----------------
print("\nG6d 用量触发的压缩同样要能挺过 resume（与 G5 串起来）", flush=True)
ag4, st4 = _mk(_rounds(10), sid="ctx4")
ag4.client.last_input_tokens = int(LIMIT * RATIO) + 1
ag4._maybe_compact()
n_mem = len(ag4.history)
ag4.history.append({"role": "user", "content": "继续"})
ag4.store.append("ctx4", {"role": "user", "content": "继续"})
ag4.history.append({"role": "assistant", "content": [{"type": "text", "text": "好"}]})
ag4.store.append("ctx4", {"role": "assistant", "content": [{"type": "text", "text": "好"}]})
d = st4.load("ctx4")
ok("G6d 用量触发路径的压缩结果 = resume 结果",
   len(d["messages"]) == n_mem + 2,
   f"内存={n_mem + 2}（压缩后 {n_mem} + 2 条后续），resume={len(d['messages'])} 条")

# ---------------- G6e：量不到用量时不许误判为「已满」 ----------------
print("\nG6e 拿不到 usage（last_input_tokens=0）—— 反例：误判为已满而清空历史", flush=True)
ag5, _ = _mk(_rounds(9))
n5 = len(ag5.history)
ag5.client.last_input_tokens = 0
ag5._maybe_compact()
ok("G6e 用量未知 → 不裁（回退到字符/条数判据，绝不按「已满」处理）",
   len(ag5.history) == n5,
   f"条数 {n5} → {len(ag5.history)}")

print("\n---- summary ----")
for l in log:
    print(l)
failed = sum(1 for l in log if l.startswith("FAIL"))
print("FAILED:", failed)
sys.exit(1 if failed else 0)
