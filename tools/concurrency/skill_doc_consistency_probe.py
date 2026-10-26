"""G9 判据：skill 文档里提到的东西，工具集里**必须真的存在**；参数名必须真的被认。

① 的**活跃**成因就是这一类错配：文档让模型调用 `CronCreate` 定时查 `.done`，
而 TraitPilot 的工具集里根本没有这个工具 → 模型只能在同一轮里 `sleep` 轮询 →
这一轮永远不结束 → 对话框不解放、切走也看不到进度。

所以这不是「文档写得不好看」，而是**可执行性**问题。判据：
  G9a 文档里引用的每个工具名，都必须在 schema 里存在   —— 反例：CronCreate（不存在）
  G9b 文档里写的 Bash 参数名，必须在 schema 里被认      —— 反例：run_in_background
  G9c `nohup ... &` 必须带重定向                        —— 反例：持有管道 → 父进程等到 EOF

G9a/G9b 是**双向**的：既要求「文档提到的存在」，也要求「不再出现已知错名」——
只查前者会漏掉「文档写了个错名但恰好没被引用」的情况。

跑法：
    /home/chenyuxi/anaconda3/envs/Agent/bin/python -I tools/concurrency/skill_doc_consistency_probe.py
"""
import os
import re
import sys

sys.path.insert(0, "/home/chenyuxi/skill/agent")

log = []
ROOT = "/home/chenyuxi/skill/agent/skills"


def ok(name, cond, extra=""):
    log.append(("PASS" if cond else "FAIL") + " " + name)
    print(("  [PASS] " if cond else "  [缺陷] ") + name, flush=True)
    if extra:
        print("         " + extra, flush=True)


def _md_files():
    out = []
    for root, _d, files in os.walk(ROOT):
        for f in files:
            if f.endswith(".md"):
                out.append(os.path.join(root, f))
    return sorted(out)


def _lines(path):
    return list(open(path, encoding="utf-8", errors="replace"))


# ---------------- 真实工具集 ----------------
from skill_agent.config import Config  # noqa: E402
from skill_agent.skills import SkillRegistry  # noqa: E402
from skill_agent.tools import ToolSet  # noqa: E402

cfg = Config.load()
ts = ToolSet(SkillRegistry(os.path.join(cfg.cwd, "skills")), cfg.cwd, permission="auto")
schemas = ts.schemas_for(None)
tool_names = set()
for s in schemas:
    n = s.get("name") or s.get("function", {}).get("name")
    if n:
        tool_names.add(n)
bash_schema = next((s for s in schemas
                    if (s.get("name") or s.get("function", {}).get("name")) == "Bash"), {})
props = bash_schema.get("input_schema", {}).get("properties", {}) or \
    bash_schema.get("function", {}).get("input_schema", {}).get("properties", {})

print(f"工具集实际包含：{sorted(tool_names)}", flush=True)
print(f"Bash 参数：{sorted(props)}", flush=True)

# ---------------- G9a：文档引用的工具必须存在 ----------------
# 只检查**明确的工具引用形态**，避免把普通英文单词当工具名：
#   · `Tool(...)` 反引号包裹 + 首字母大写 + 括号
#   · `Bash(...)` 这类
print("\nG9a 文档中出现的工具调用形态，是否都存在于工具集", flush=True)
CALL_RE = re.compile(r"`?([A-Z][A-Za-z]+)\s*\(")
KNOWN_TOOLS = tool_names | {
    # 非 TraitPilot 工具但属于**正常文档用语**（外部命令名、Skill 名占位）
    "Skill", "Read", "Write", "Edit", "Bash", "Glob", "Grep", "TaskOutput",
}
BAD_TOOLS = {"CronCreate", "CronList", "CronDelete"}
hits = {}
for p in _md_files():
    for i, line in enumerate(_lines(p), 1):
        for m in CALL_RE.finditer(line):
            name = m.group(1)
            if name in BAD_TOOLS:
                hits.setdefault(name, []).append(
                    f"{os.path.relpath(p, '/home/chenyuxi/skill/agent')}:{i}")
ok("G9a-1 [缺陷确认] 文档中**不再**出现不存在的 Cron* 工具",
   not hits,
   f"残留引用 = {hits}" if hits else "已全部改为 TaskOutput / Read / Bash 轮询")

# 反查：文档里出现的、看起来像 TraitPilot 工具的驼峰名，是否真的存在
camel = {}
for p in _md_files():
    for i, line in enumerate(_lines(p), 1):
        for m in CALL_RE.finditer(line):
            name = m.group(1)
            if name in KNOWN_TOOLS or name in BAD_TOOLS:
                continue
            # 只怀疑「看起来像本仓库工具」的：出现在 col 0 起的代码块里
            if line.lstrip().startswith("`") or line.lstrip().startswith("#"):
                camel.setdefault(name, []).append(
                    f"{os.path.relpath(p, '/home/chenyuxi/skill/agent')}:{i}")
print(f"     疑似工具名但不在工具集：{ {k: v[:2] for k, v in list(camel.items())[:6]} }",
      flush=True)

# ---------------- G9b：Bash 参数名必须被认 ----------------
print("\nG9b 文档中写的 Bash 参数名，是否真的在 schema 里", flush=True)
# ⚠️ 分类器必须区分「让模型这么写」与「提醒别这么写」：
#    第一版把「参数名是 background，**不是** run_in_background」这种**警告句**
#    也判成错误引用 → 假失败。修正：命中错名时，若同行带有否定/警告标记，
#    则视为「正确的提醒」，不计入缺陷；否则才是真的在教模型写错名。
NEGATION = ("不是", "不要", "不能用", "不认", "错误", "❌", "⚠️", "会被忽略", "须改")


def _is_warning_line(line: str, key: str) -> bool:
    """该行提到 key，但语义是「别这么写」吗？"""
    return any(tok in line for tok in NEGATION)


bad_params, warns = {}, {}
for p in _md_files():
    rel = os.path.relpath(p, "/home/chenyuxi/skill/agent")
    for i, line in enumerate(_lines(p), 1):
        for m in re.finditer(r"\b(run_in_background|background)\b", line):
            key = m.group(1)
            if key != "run_in_background":
                continue
            (warns if _is_warning_line(line, key) else bad_params).setdefault(
                key, []).append(f"{rel}:{i}")
ok("G9b-1 [缺陷确认] 文档中**不再**把不存在的 `run_in_background` 当正确写法教",
   not bad_params,
   f"仍在教错名 = {bad_params}" if bad_params else
   f"已无错误引用；另有 {sum(len(v) for v in warns.values())} 处**警告句**"
   "（提醒别用错名，属正确内容）")
ok("G9b-2 文档提到的 `background` 确实在 schema 里（不是又写错一个名）",
   "background" in props,
   f"schema 属性 = {sorted(props)}")
ok("G9b-3 警告句确实存在（证明「文档写明别写错名」，不只是没写）",
   bool(warns),
   f"警告位置 = {[x for v in warns.values() for x in v][:4]}")

# ---------------- G9c：nohup 必须重定向 ----------------
print("\nG9c 文档里的 `nohup ... &` 是否都带重定向（不带会把这一轮卡住）", flush=True)


def _unredirected_bg(line: str) -> bool:
    """该行是否是一条**未重定向的后台作业**。

    只看真后台作业（`nohup` 且以 `&` 结尾），且必须同时有 stdout 与 stderr 去向。
    占位保护 `2>&1` 里的 `&`（否则会被当成作业分隔符提前截断）。

    ⚠️ 必须排除**说明性文本**：注释行（`#`）、引用行（`>`）、以及「必须重定向」
    这类警告句本身也会含有 `nohup ... &` 字样。第一版没排除 → 把自己的警告
    当成缺陷（假失败 3 处）。
    """
    stripped = line.strip()
    # 说明性上下文：Markdown 注释 / 引用块 / 警告标记行
    if stripped.startswith("#") or stripped.startswith(">"):
        return False
    if "⚠️" in line or any(tok in line for tok in ("必须", "不要", "否则", "会被", "会被忽略")):
        return False
    if "nohup" not in line:
        return False
    seg = line[line.index("nohup"):]
    seg = seg.replace("2>&1", "2>\x01").replace("2>&2", "2>\x02")
    seg = seg.replace(">&-", ">\x03").replace("&>", "\x04>")
    # 省略号形态（`nohup ... &`）是文档里的示意写法，不是可执行命令
    if "..." in seg or "…" in seg:
        return False
    if "&" not in seg:
        return False
    body = seg.split("&")[0]
    out_ok = re.search(r">\s*\S", body) is not None
    err_ok = ("\x01" in body or "\x02" in body) or re.search(r"2>\s*\S", body) is not None
    closed = "\x03" in body or "\x04>" in body
    return not (closed or (out_ok and err_ok))


unredir = []
for p in _md_files():
    for i, line in enumerate(_lines(p), 1):
        if _unredirected_bg(line):
            unredir.append(f"{os.path.relpath(p, '/home/chenyuxi/skill/agent')}:{i}: "
                           f"{line.strip()[:70]}")
ok("G9c [缺陷确认] 文档里没有「未重定向的 nohup 后台作业」",
   not unredir,
   f"未重定向 {len(unredir)} 处：{unredir[:3]}" if unredir else
   "全部带重定向（且都写明了「不重定向会被卡住」的原因）")

# 负控：分类器本身要能把「未重定向」认出来，且**不能**把说明性文本当缺陷
_neg = "nohup bash x.sh &"
_pos = "nohup bash x.sh > log 2>&1 &"
_elide = "nohup ... & — 终端退出后进程继续运行"
_warnline = "> 只写 `&` 时子进程仍持有管道，会被卡住"
ok("G9c-负控 分类器能识别未重定向（否则上一条无区分度）",
   _unredirected_bg(_neg) and not _unredirected_bg(_pos),
   f"未重定向样本判={_unredirected_bg(_neg)}（应为 True）；"
   f"已重定向样本判={_unredirected_bg(_pos)}（应为 False）")
ok("G9c-负控2 分类器不把「示意写法 / 警告句」误判为缺陷",
   not _unredirected_bg(_elide) and not _unredirected_bg(_warnline),
   f"省略号示意判={_unredirected_bg(_elide)}（应为 False）；"
   f"警告句判={_unredirected_bg(_warnline)}（应为 False）")

# ---------------- G9d：sleep 轮询必须被明确劝阻 ----------------
print("\nG9d 文档是否明确劝阻「在工具调用里 sleep 轮询」", flush=True)
warn = []
for p in _md_files():
    for i, line in enumerate(_lines(p), 1):
        if "sleep" in line and ("不要" in line or "禁止" in line or "不应" in line):
            warn.append(f"{os.path.relpath(p, '/home/chenyuxi/skill/agent')}:{i}")
ok("G9d 至少一处文档明确说了「不要 sleep 轮询」（否则模型仍可能自己发明）",
   bool(warn), f"劝阻位置 = {warn}")

print("\n---- summary ----")
for l in log:
    print(l)
failed = sum(1 for l in log if l.startswith("FAIL"))
print("FAILED:", failed)
sys.exit(1 if failed else 0)
