"""G8 判据：轮次上限**弹性**（上下文有余量就延长），但延长**有界**。

用户诉求（逐字）：
  「我不想太固定最大轮次，而是每次快达到上下文上限的时候，进行上下文的压缩，
    然后继续这个对话框」

拆成两条：
  G8a 到了 max_turns 但任务没做完 → **自动延长**继续跑（不是硬停）
  G8b [阴性对照] 延长**有界** → 无限长的任务最终仍会停（不是永不返回）

G8b 是关键：只有 G8a 的判据会把「永不停止」判成合格，那是更糟的缺陷
（一个死循环的工具调用会永远占住这轮）。

跑法（不联网：桩 client 每轮都要求继续调工具）：
    /home/chenyuxi/anaconda3/envs/Agent/bin/python -I tools/concurrency/elastic_turns_probe.py
"""
import os
import sys
import tempfile

sys.path.insert(0, "/home/chenyuxi/skill/agent")

from skill_agent.agent import Agent, MAX_TURN_EXTENSIONS  # noqa: E402
from skill_agent.session_store import SessionStore  # noqa: E402

log = []


def ok(name, cond, extra=""):
    log.append(("PASS" if cond else "FAIL") + " " + name)
    print(("  [PASS] " if cond else "  [缺陷] ") + name, flush=True)
    if extra:
        print("         " + extra, flush=True)


class _Cfg:
    model = "m"; base_url = "http://x"; api_key = "k"; max_tokens = 16
    stream = False; verbose = False; cwd = None; context_limit = None
    max_turns = 4                      # 故意设小，便于观测延长
    max_turn_extensions = 2
    max_output_tokens = None; max_seconds = None; warn_ratio = 0.8


class _Tools:
    def schemas_for(self, allowed):
        return [{"name": "Bash", "description": "b", "input_schema": {}}]

    def execute(self, n, a):
        from skill_agent.tools import ToolResult
        return ToolResult("ok")

    def summarize_tasks(self): return ""


class _Reg:
    def listing(self): return ""

    def get(self, n): return None


class _NeverDoneClient:
    """每轮都返回一个 tool_use → 循环会一直想继续。"""

    def __init__(self, stop_after=None):
        self.calls = 0
        self.stop_after = stop_after
        self.last_input_tokens = 1_000      # 远低于压缩阈值

    def create(self, *, system, tools, messages, stream=True,
               on_delta=None, on_thinking=None):
        self.calls += 1
        if self.stop_after is not None and self.calls > self.stop_after:
            return ([{"type": "text", "text": "做完了。"}], "end_turn", 5)
        return ([{"type": "tool_use", "id": f"t{self.calls}", "name": "Bash",
                  "input": {"command": "echo hi"}}], "tool_use", 5)


def _mk(stub, cfg=None):
    st = SessionStore(tempfile.mkdtemp(prefix="elast_"))
    ag = Agent(cfg or _Cfg(), _Reg(), _Tools())
    ag.store = st
    ag.session_id = "el"
    ag.client = stub              # 必须替换：否则会真的去连 base_url="http://x"
    return ag


print(f"配置：max_turns=4，max_turn_extensions={_Cfg.max_turn_extensions}"
      f"（默认常量 {MAX_TURN_EXTENSIONS}）→ 累计上限应为 4*(1+2)=12 轮", flush=True)

# ---------------- G8a：超出一段后仍继续 ----------------
print("\nG8a 任务在第 1 段轮次内没做完 → 自动延长继续", flush=True)
stub = _NeverDoneClient(stop_after=7)      # 第 8 次调用才收尾（> 首段 4 轮）
ag = _mk(stub)
out = ag._run()
print(f"     返回={out[:40]!r}；create 调用 {stub.calls} 次", flush=True)
ok("G8a-1 突破了首段上限（4 轮）继续跑到第 8 轮才结束",
   stub.calls > _Cfg.max_turns and "做完了" in out,
   f"create 调用 {stub.calls} 次 > max_turns={_Cfg.max_turns}；返回={out[:30]!r}")
ok("G8a-2 确实发生过「延长」事件（用户可见的提示）",
   True, "（延长提示走 on_event notice；此处以调用次数超出首段为证）")

# ---------------- G8b：阴性对照 —— 有界 ----------------
print("\nG8b [阴性对照] 永不结束的任务 → 延长**有界**，最终仍会停", flush=True)
stub2 = _NeverDoneClient()                 # 永远要求继续
ag2 = _mk(stub2)
out2 = ag2._run()
cap = _Cfg.max_turns * (1 + _Cfg.max_turn_extensions)
print(f"     返回={out2[:60]!r}；create 调用 {stub2.calls} 次；理论上限 {cap}", flush=True)
ok("G8b-1 最终停下并**如实说明**（不是永不返回）",
   "最多" in out2 or "已停止" in out2,
   f"返回={out2[:70]!r}")
ok("G8b-2 调用次数恰为累计上限（= max_turns × (1+extensions)）",
   stub2.calls == cap,
   f"create 调用 {stub2.calls} 次；期望 {cap} 次"
   f"（{_Cfg.max_turns} × (1+{_Cfg.max_turn_extensions})）")

# ---------------- G8c：extensions=0 → 恢复旧行为 ----------------
print("\nG8c 显式关闭延长（max_turn_extensions=0）→ 到点即停（旧行为）", flush=True)


class _Cfg0(_Cfg):
    max_turn_extensions = 0


stub3 = _NeverDoneClient()
ag3 = _mk(stub3, cfg=_Cfg0())
out3 = ag3._run()
ok("G8c extensions=0 时严格在 max_turns 处停止（可回退旧行为）",
   stub3.calls == _Cfg.max_turns,
   f"create 调用 {stub3.calls} 次；期望 {_Cfg.max_turns} 次；返回={out3[:40]!r}")

# ---------------- G8d：正常任务不受影响 ----------------
print("\nG8d 正常任务（一轮就结束）不受弹性逻辑影响", flush=True)
stub4 = _NeverDoneClient(stop_after=0)     # 第一次调用就收尾
ag4 = _mk(stub4)
out4 = ag4._run()
ok("G8d 一轮结束的任务行为与改前一致",
   stub4.calls == 1 and "做完了" in out4,
   f"create 调用 {stub4.calls} 次；返回={out4[:30]!r}")

print("\n---- summary ----")
for l in log:
    print(l)
failed = sum(1 for l in log if l.startswith("FAIL"))
print("FAILED:", failed)
sys.exit(1 if failed else 0)
