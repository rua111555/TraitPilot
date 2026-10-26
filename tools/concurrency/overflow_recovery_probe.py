"""G7 判据：上下文溢出（400）时必须**压缩后继续**，而不是把这一轮打死。

用户诉求（逐字）：
  「我不想太固定最大轮次，而是每次快达到上下文上限的时候，进行上下文的压缩，
    然后继续这个对话框」

为什么单列这一条：client 把 400 归为**不可重试**（`_retryable_status` 只认
429/529/5xx），所以上下文溢出会直接终止整轮 —— 用户看到的就是「对话进行不下去」。
而历史在本地是可裁的：裁掉旧内容再发一次就能继续。

  G7a 溢出错误 → 压缩后重试，本轮**继续**（不是抛错结束）
  G7b [阴性对照] 别的 400（如参数错误）→ **必须照旧抛出**，不许吞成「重试」
  G7c 裁不动时（历史不可再分）→ 如实报错，不许假装成功
  G7d 判据本身：错误文本分类的区分度（含/不含超长措辞）

G7b 是关键阴性对照：只有 G7a 的判据会把「所有 400 都吞掉」判成合格，那是危险的
（真参数错误会被伪装成「内部已处理」）。

跑法（不联网：用桩 client 构造 400）：
    /home/chenyuxi/anaconda3/envs/Agent/bin/python -I tools/concurrency/overflow_recovery_probe.py
"""
import os
import sys
import tempfile

sys.path.insert(0, "/home/chenyuxi/skill/agent")

from skill_agent.agent import (  # noqa: E402
    Agent, _is_context_overflow, CONTEXT_LIMIT_FALLBACK,
)
from skill_agent.client import APIError  # noqa: E402
from skill_agent.session_store import SessionStore  # noqa: E402

log = []
OVERFLOW_MSG = ("This model's maximum context length is 286720 tokens. However, "
                "you requested 1 output tokens and your prompt contains at least "
                "286720 input tokens, for a total of at least 286721 tokens. "
                "(parameter=input_tokens, value=286720)")
BADPARAM_MSG = ("messages.1.content: Input should be a valid list "
                "(parameter=messages, value=oops)")


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


# ---------------- G7d：先证明分类判据本身有区分度 ----------------
print("G7d 错误文本分类判据（含/不含超长措辞）", flush=True)
ov = APIError(OVERFLOW_MSG, status=400, retryable=False)
bp = APIError(BADPARAM_MSG, status=400, retryable=False)
ok("G7d-1 溢出文本 → 判为「可压缩后重试」",
   _is_context_overflow(ov), f"识别={_is_context_overflow(ov)}")
ok("G7d-2 [阴性对照] 参数错误文本 → **不**判为溢出（不吞真错误）",
   not _is_context_overflow(bp), f"识别={_is_context_overflow(bp)}")
ok("G7d-3 非 400（如 500 已可重试）→ 不走溢出分支",
   not _is_context_overflow(APIError(OVERFLOW_MSG, status=500, retryable=True)),
   "status=500 时识别="
   f"{_is_context_overflow(APIError(OVERFLOW_MSG, status=500, retryable=True))}")


# ---------------- 桩 client：第一次抛溢出，第二次成功 ----------------
class _StubClient:
    """复刻真实 client 的接口面：create() + last_input_tokens。"""

    def __init__(self, fail_times=1, err=None):
        self.calls = []
        self.remaining_failures = fail_times
        self.err = err or APIError(OVERFLOW_MSG, status=400, retryable=False)
        self.last_input_tokens = 0

    def create(self, *, system, tools, messages, stream=True,
               on_delta=None, on_thinking=None):
        self.calls.append({"n_messages": len(messages),
                           "first_role": messages[0].get("role") if messages else None})
        if self.remaining_failures > 0:
            self.remaining_failures -= 1
            raise self.err
        return ([{"type": "text", "text": "好的，继续。"}], "end_turn", 5)


def _mk(hist, stub, sid="ovf"):
    st = SessionStore(tempfile.mkdtemp(prefix="ovfrec_"))
    ag = Agent(_Cfg(), _Reg(), _Tools())
    ag.store = st
    ag.session_id = sid
    ag.client = stub
    ag.history = list(hist)
    for m in hist:
        st.append(sid, m)
    return ag, st


def _rounds(n):
    out = []
    for i in range(n):
        out.append({"role": "user", "content": f"q{i}"})
        out.append({"role": "assistant", "content": [
            {"type": "tool_use", "id": f"t{i}", "name": "Bash",
             "input": {"command": f"c{i}"}}]})
        out.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": f"t{i}", "content": "ok"}]})
        out.append({"role": "assistant", "content": [{"type": "text", "text": f"a{i}"}]})
    return out


# ---------------- G7a：溢出 → 压缩后继续 ----------------
print("\nG7a 上下文溢出（400）→ 压缩后重试，本轮继续", flush=True)
stub = _StubClient(fail_times=1)
ag, _ = _mk(_rounds(8), stub)                # 32 条
n0 = len(ag.history)
out = ag._run()
print(f"     返回={out[:40]!r}", flush=True)
print(f"     client.create 调用 {len(stub.calls)} 次；"
      f"各次的 messages 条数={[c['n_messages'] for c in stub.calls]}", flush=True)
ok("G7a-1 溢出后**确实重试**（create 被调用 2 次）",
   len(stub.calls) == 2, f"调用次数={len(stub.calls)}")
ok("G7a-2 重试时用的是**压缩后**的历史（更短）",
   len(stub.calls) == 2 and stub.calls[1]["n_messages"] < n0,
   f"首次 {n0} 条 → 重试 {stub.calls[1]['n_messages'] if len(stub.calls) > 1 else '—'} 条")
# ⚠️ 判据修正（诚实记录）：第一版只查 content[0] 是不是回复文本 → 假失败。
#    压缩后的历史**以 assistant 结尾**（这正是续跑不变量），于是新回复会走既有的
#    「合并进上一条 assistant」路径（与 pause_turn 续推同一条路），落在 content[1:]。
#    要问的是「回复有没有进历史且角色不变形」，不是「它在第几个 block」。
_last = ag.history[-1] if ag.history else {}
_last_blocks = _last.get("content") if isinstance(_last.get("content"), list) else []
_last_text = "".join(b.get("text", "") for b in _last_blocks if isinstance(b, dict))
ok("G7a-3 本轮**正常结束**：模型回复已入历史，且角色未变形（无连续同 role）",
   "继续" in _last_text and _last.get("role") == "assistant"
   and all(a.get("role") != b.get("role") for a, b in zip(ag.history, ag.history[1:])),
   f"末条 role={_last.get('role')} 文本={_last_text!r}；"
   f"角色交替={'无连续同 role' if all(a.get('role') != b.get('role') for a, b in zip(ag.history, ag.history[1:])) else '**出现了连续同 role**'}")

# ---------------- G7b：阴性对照 —— 别的 400 必须照旧抛出 ----------------
print("\nG7b [阴性对照] 非溢出类 400 → **必须抛出**（不许吞成「已处理」）", flush=True)
stub2 = _StubClient(fail_times=1,
                    err=APIError(BADPARAM_MSG, status=400, retryable=False))
ag2, _ = _mk(_rounds(8), stub2)
raised = None
try:
    ag2._run()
except Exception as e:
    raised = e
ok("G7b 参数错误 400 如实抛出（未被伪装成「压缩后重试」）",
   raised is not None and len(stub2.calls) == 1,
   f"抛出={type(raised).__name__ if raised else None}: {str(raised)[:60]!r}；"
   f"create 调用 {len(stub2.calls)} 次（应为 1，不重试）")

# ---------------- G7c：裁不动 → 如实报错 ----------------
print("\nG7c 历史短到无法再裁（溢出仍发生）→ 如实报错，不假装成功", flush=True)
stub3 = _StubClient(fail_times=99)           # 永远溢出
ag3, _ = _mk([{"role": "user", "content": "只有一条"}], stub3)
raised3 = None
try:
    ag3._run()
except Exception as e:
    raised3 = e
ok("G7c 无余量可裁时如实抛出（不吞、不空转重试）",
   raised3 is not None and len(stub3.calls) == 1,
   f"抛出={type(raised3).__name__ if raised3 else None}；"
   f"create 调用 {len(stub3.calls)} 次")

# ---------------- G7e：重试后仍溢出 → 不无限重试 ----------------
print("\nG7e 压缩后**仍**溢出 → 只重试一次，然后如实报错（防死循环）", flush=True)
stub4 = _StubClient(fail_times=99)
ag4, _ = _mk(_rounds(8), stub4)
raised4 = None
try:
    ag4._run()
except Exception as e:
    raised4 = e
ok("G7e 重试上限 = 1 次（create 共 2 次），随后抛出",
   raised4 is not None and len(stub4.calls) == 2,
   f"create 调用 {len(stub4.calls)} 次（期望 2）；抛出={type(raised4).__name__ if raised4 else None}")

print("\n---- summary ----")
for l in log:
    print(l)
failed = sum(1 for l in log if l.startswith("FAIL"))
print("FAILED:", failed)
sys.exit(1 if failed else 0)
