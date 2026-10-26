"""① 「对话框没解放」的后端判据探针：三条独立成因，逐条以**反例**形式给出。

用户问题（逐字）：
  「为什么我的skill在运行的过程中，我的对话框没有解放出来，我skill中应该有写在
    运行过程中，对话框需要解放，因为生信分析领域，整个分析的时间可能会很长」

探针测的是「一条 Bash 调用会不会把这一轮占住」，用真实耗时做区分度：
  B1 参数名：skill 文档写 `run_in_background`，工具 schema 只认 `background`
     → 反例：按文档写的那次会**阻塞满整个命令时长**
  B2 重定向：skill 文档里的 `nohup ... &` 不重定向输出
     → 反例：子进程持有 stdout 管道，subprocess.run(capture_output=True) 等到 EOF 才返回
  B3 轮询出口：skill 让模型用 `CronCreate` 定时查 .done
     → 反例：该工具在本仓库**根本不存在**，模型只能在同一轮里 sleep 轮询

跑法：
    /home/chenyuxi/anaconda3/envs/Agent/bin/python -I tools/concurrency/blocking_probe.py
"""
import json
import os
import subprocess
import sys
import time
import tempfile

sys.path.insert(0, "/home/chenyuxi/skill/agent")

log = []
SLEEP = 6          # 被启动的命令时长；判据看「Bash 调用是否等满它」


def ok(name, cond, extra=""):
    log.append(("PASS" if cond else "FAIL") + " " + name)
    print(("  [PASS] " if cond else "  [缺陷] ") + name, flush=True)
    if extra:
        print("         " + extra, flush=True)


def bash_via_toolset(args: dict, cwd: str) -> tuple[str, float]:
    """走真实 ToolSet 路径执行 Bash（不是自己 subprocess），返回 (输出前 200 字, 耗时秒)。"""
    from skill_agent.config import Config
    from skill_agent.skills import SkillRegistry
    from skill_agent.tools import ToolSet

    cfg = Config.load()
    reg = SkillRegistry(os.path.join(cfg.cwd, "skills"))
    ts = ToolSet(reg, cwd, permission="auto", verbose=False)
    t0 = time.monotonic()
    tr = ts.execute("Bash", args) if hasattr(ts, "execute") else None
    if tr is None:                      # 兼容不同方法名
        for meth in ("run", "call", "invoke"):
            if hasattr(ts, meth):
                tr = getattr(ts, meth)("Bash", args)
                break
    dt = time.monotonic() - t0
    return str(getattr(tr, "content", tr))[:200], dt


# ---------------- B1：参数名 ----------------
print(f"\nB1  skill 文档写 `run_in_background`，工具 schema 只认 `background`"
      f"（反例：按文档写会阻塞满 {SLEEP}s）", flush=True)
tmp = tempfile.mkdtemp(prefix="blockprobe_")

try:
    out_doc, dt_doc = bash_via_toolset(
        {"command": f"sleep {SLEEP}", "description": "doc-spelling",
         "run_in_background": True}, tmp)
    blocked_doc = dt_doc >= SLEEP * 0.8
    ok(f"B1a 按文档写法 `run_in_background=true` —— 实测确实阻塞",
       blocked_doc,
       f"耗时 {dt_doc:.2f}s（命令 {SLEEP}s）→ 阻塞={blocked_doc}；返回：{out_doc[:80]!r}")

    out_ok, dt_ok = bash_via_toolset(
        {"command": f"sleep {SLEEP}", "description": "schema-spelling",
         "background": True}, tmp)
    fast = dt_ok < 1.0
    ok("B1b [对照] 按 schema 写法 `background=true` —— 立即返回",
       fast, f"耗时 {dt_ok:.2f}s → 立即返回={fast}；返回：{out_ok[:90]!r}")
except Exception as e:
    ok("B1a/B1b 走真实 ToolSet 执行", False, f"执行失败：{type(e).__name__}: {e}")

# schema 里到底认哪个键（直接读工具定义，不靠猜）
try:
    from skill_agent.config import Config
    from skill_agent.skills import SkillRegistry
    from skill_agent.tools import ToolSet
    cfg = Config.load()
    ts = ToolSet(SkillRegistry(os.path.join(cfg.cwd, "skills")), tmp, permission="auto")
    schemas = ts.schemas_for(None) if hasattr(ts, "schemas_for") else []
    bash_schema = next((s for s in schemas
                        if (s.get("name") or s.get("function", {}).get("name")) == "Bash"), {})
    props = bash_schema.get("input_schema", {}).get("properties", {}) or \
        bash_schema.get("function", {}).get("input_schema", {}).get("properties", {})
    ok("B1c Bash 工具的 schema 里有 `background`、没有 `run_in_background`",
       "background" in props and "run_in_background" not in props,
       f"schema 属性键 = {sorted(props)}")
except Exception as e:
    ok("B1c 读 Bash schema", False, f"{type(e).__name__}: {e}")

# ---------------- B2：nohup 不重定向 ----------------
print(f"\nB2  文档里的 `nohup ... &` 未重定向输出（反例：子进程持有管道 → 等满 {SLEEP}s）", flush=True)


def raw_bash(cmd: str, cwd: str) -> tuple[int, float]:
    """复刻 tools.py 前台分支：subprocess.run(capture_output=True)。"""
    t0 = time.monotonic()
    p = subprocess.run(["/bin/bash", "-lc", cmd], cwd=cwd,
                       capture_output=True, text=True)
    return p.returncode, time.monotonic() - t0


# ⚠️ 本机是**重负载**的（实测纯前台 `sleep 6` 墙钟 8.19s，多出的 ~2.2s 是 bash 启动 +
#    profile 开销）。所以判据不能写「耗时 < 1s」这种绝对阈值 —— 那会把「立即返回」
#    误判成阻塞（上一版 B2b 就是这么假失败的）。
#    正确做法：先用同一条链路量出**纯开销**，再与被测命令比。
#      · 纯开销 overhead = 前台 `sleep N` 耗时 - N
#      · 未重定向 ≈ overhead + N（等满整个子进程）
#      · 已重定向 ≈ overhead     （立刻返回，只付 shell 启动的钱）
_ovh_samples = [raw_bash(f"sleep {SLEEP}", tmp)[1] for _ in range(3)]
OVERHEAD = min(_ovh_samples) - SLEEP
if OVERHEAD < 0:
    OVERHEAD = 0.0

_, dt_unredir = raw_bash(f"nohup sleep {SLEEP} &", tmp)
waited_u = dt_unredir - OVERHEAD
blocked_u = waited_u >= SLEEP * 0.8
ok(f"B2a 未重定向的 `nohup sleep {SLEEP} &` —— 实测确实阻塞",
   blocked_u,
   f"耗时 {dt_unredir:.2f}s − 纯开销 {OVERHEAD:.2f}s = 等子进程 {waited_u:.2f}s"
   f"（命令 {SLEEP}s）→ 阻塞={blocked_u}")

_, dt_redir = raw_bash(f"nohup sleep {SLEEP} >/dev/null 2>&1 &", tmp)
waited_r = dt_redir - OVERHEAD
fast_r = waited_r < SLEEP * 0.3        # 相对判据：没等子进程，只付了 shell 启动
ok("B2b [对照] 重定向后的同一命令 —— 立即返回（未等子进程）",
   fast_r,
   f"耗时 {dt_redir:.2f}s − 纯开销 {OVERHEAD:.2f}s = 等子进程 {waited_r:.2f}s"
   f"（相对未重定向的 {waited_u:.2f}s）→ 立即返回={fast_r}")

# ---------------- B3：skill 指明的轮询出口是否存在 ----------------
print("\nB3  skill 让模型用 `CronCreate` 定期查 .done（反例：该工具不存在）", flush=True)
try:
    names = set()
    for s in schemas:
        n = s.get("name") or s.get("function", {}).get("name")
        if n:
            names.add(n)
    has_cron = any(n.startswith("Cron") for n in names)
    # 极性说明：这里要确认的是**缺陷**（文档让模型用不存在的工具），
    # 所以「没有 Cron 工具」= 缺陷成立 = PASS。上一版把极性写反了。
    ok("B3a [缺陷确认] 工具集里**没有** Cron* 工具 —— 文档指向的能力不存在",
       not has_cron,
       f"实际工具 = {sorted(names)}（无任何 Cron*）")
    # 文档里的指示
    hits = []
    for root, _dirs, files in os.walk("/home/chenyuxi/skill/agent/skills"):
        for f in files:
            if f.endswith(".md"):
                p = os.path.join(root, f)
                for i, line in enumerate(open(p, encoding="utf-8", errors="replace"), 1):
                    if "CronCreate" in line or "CronList" in line:
                        hits.append(f"{p.replace('/home/chenyuxi/skill/agent/','')}:{i}")
    ok("B3b skill 文档确实指示了 CronCreate（即：文档与实现不一致）",
       bool(hits), f"文档引用位置 = {hits}")
except Exception as e:
    ok("B3 轮询出口", False, f"{type(e).__name__}: {e}")

# ---------------- B4：历史日志里模型实际怎么写 ----------------
print("\nB4  真实会话日志：模型实际传的是哪个键（区分「文档错」与「实际踩坑」）", flush=True)
import glob  # noqa: E402
import re  # noqa: E402
ctx = "/home/chenyuxi/.skill-agent/contexts"
n_bg = n_rib = n_nohup = n_unredir = 0
unredir_samples = []
files = glob.glob(os.path.join(ctx, "*.jsonl"))


def _is_bg_launch(cmd: str) -> bool:
    """`nohup ... &` —— nohup 后面确实以 `&` 结尾（真后台作业），而不是仅仅提到 nohup。"""
    if "nohup" not in cmd:
        return False
    seg = cmd[cmd.index("nohup"):].replace("2>&1", "2>x").replace("&>", "y>")
    return re.search(r"&\s*($|\||;|\n)", seg) is not None or seg.rstrip().endswith("&")


def _has_bg_redirect(cmd: str) -> bool:
    """`cmd &` 这条后台作业是否把 stdout+stderr 都重定向走了。

    只看 `nohup ... &` 这一段：必须同时有 stdout 与 stderr 的去向，
    否则子进程会持有 subprocess.run 的管道 → 父进程等到 EOF 才返回。

    ⚠️ 两个已踩过的坑（都由负控抓出）：
      1) 上一版只认 `>/dev/null` 字面量，把 `> logs/nohup.out 2>&1` 这种**正确**写法
         误判成未重定向 —— 假缺陷，导致结论完全反了（82% 未重定向）。
      2) 直接 `split("&")` 会被 `2>&1` 里的 `&` 提前截断，于是 stderr 判断永远失败。
         先把重定向运算符**占位保护**起来，再切后台作业的 `&`。
    """
    seg = cmd
    if "nohup" in seg:
        seg = seg[seg.index("nohup"):]
    # 占位保护：这些 '&' 属于重定向语法，不是后台作业分隔符
    seg = seg.replace("2>&1", "2>\x01").replace("2>&2", "2>\x02")
    seg = seg.replace(">&-", ">\x03").replace("&>", "\x04>")
    seg = seg.split("&")[0] if "&" in seg else seg      # 切到后台作业本体
    out_ok = re.search(r">\s*\S", seg) is not None      # 有 stdout 去向
    err_ok = ("\x01" in seg or "\x02" in seg) or re.search(r"2>\s*\S", seg) is not None
    closed = "\x03" in seg or "\x04>" in seg
    return closed or (out_ok and err_ok)


for p in files:
    try:
        for line in open(p, encoding="utf-8", errors="replace"):
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except Exception:
                continue
            if not isinstance(o, dict):
                continue
            c = o.get("content")
            if not isinstance(c, list):
                continue
            for b in c:
                if not isinstance(b, dict) or b.get("type") != "tool_use":
                    continue
                if b.get("name") != "Bash":
                    continue
                inp = b.get("input") or {}
                if "background" in inp:
                    n_bg += 1
                if "run_in_background" in inp:
                    n_rib += 1
                cmd = str(inp.get("command", ""))
                # 只统计**真正的后台启动**：`nohup ... &`（即以 & 结尾的作业）。
                # 否则把「提到 nohup 的轮询/检查命令」也算进来 —— 那类命令本来就没有
                # 重定向，会被误记为「未重定向的后台启动」（本探针第一版就这么高估到 82%）。
                if "nohup" in cmd and _is_bg_launch(cmd):
                    n_nohup += 1
                    if not _has_bg_redirect(cmd):
                        n_unredir += 1
                        if len(unredir_samples) < 3:
                            unredir_samples.append(cmd[:160].replace("\n", " | "))
    except Exception:
        continue
# 先给分类器本身做负控：正确写法必须判为「有重定向」，否则 B4c 无区分度
_cases = [("nohup bash x.sh > logs/o 2>&1 &", True),
          ("nohup bash x.sh > /dev/null 2>&1 &", True),
          ("nohup bash x.sh &> /dev/null &", True),
          ("nohup bash x.sh &", False),
          ("nohup conda run -n Agent bash x.sh &", False)]
_bad = [(c, w) for c, w in _cases if _has_bg_redirect(c) != w]
ok("B4c 负控：重定向分类器本身正确（正确写法判为有重定向）",
   not _bad, f"误判的样例 = {_bad or '无'}；全部 {len(_cases)} 例分类正确")

ok("B4a 模型实际用过 `background`（说明它自己猜对了 / 被 system prompt 教对了）",
   n_bg > 0, f"扫描 {len(files)} 个会话文件：background={n_bg} 次，"
             f"run_in_background={n_rib} 次")
ok("B4b 模型从未按 skill 文档传 `run_in_background`（文档是**潜在的**坑）",
   n_rib == 0, f"run_in_background 出现 {n_rib} 次")
ok("B4d 模型用 nohup 时基本都做了重定向（B2 也是潜在坑）",
   n_nohup == 0 or n_unredir / max(n_nohup, 1) < 0.5,
   f"nohup 调用 {n_nohup} 次，其中未重定向 {n_unredir} 次"
   f"（{n_unredir / max(n_nohup,1):.0%}）" +
   (f"；样例：{unredir_samples[0]!r}" if unredir_samples else ""))

print("\n---- summary ----")
for l in log:
    print(l)
failed = sum(1 for l in log if l.startswith("FAIL"))
print("FAILED:", failed)
sys.exit(1 if failed else 0)
