# -*- coding: utf-8 -*-
"""**缺陷类**回归防线：按"失效模式"扫全部 skill 文档，而不是按"我改过的文件"。

为什么需要这个文件（不是重复 `test_stage_gate_template.py`）：
第二轮对抗审计抓到 6 条发现，其中 **3 条是已修缺陷的"漏网复发"**：

  · A5（`${VERIFIER:?}` / 缺 `SAMPLES_CSV` 兜底）→ 编排层 §2.2 **权威模板**里没修
  · A7（裸 `tabix` 在 `set -e` 下杀死脚本）→ joint-genotyping 是**第三处**，当时只修了两处
  · A2（pbrun 常量读组 `SM:sample`）→ `usage-guide.md` 里还留着旧结论

根因不是"修得不仔细"，而是**修复的作用域错了**：我按"这次改到的文件"逐个改，
而缺陷是按"**写法**"在仓库里复发的。所以这里改成按写法扫描 —— 扫全部 skill 文档，
将来新增的文件自动被覆盖，不需要有人记得"这里也要改"。

每条防线的判据都是**结构**（某写法不得出现 / 某兜底必须存在），不是"某个字符串等于某值"。

---

**本文件自身也受"纸面具备"的威胁**：一条正则若写得太松（或文档里恰好没有该写法），
它会**永远绿着却什么也没断言** —— 与它要防的缺陷形态一模一样。故每条判据都拆成
模块级 `_pred_*` 谓词，并由 `test_guards_actually_discriminate` 用**构造的坏样本**
证明它们真的会红、用**构造的好样本**证明它们不误伤。改正则前先看那组自检。
"""
import ast
import glob
import os
import re

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKILLS = os.path.join(REPO, "skills")
# 校验器所在目录（与 test_stage_verify.py 的 SV_PATH 同一约定）——
# 「哪些 stage 消费 --prev-reads-file」由该文件源码推导，不写死清单。
SV_PATH = os.path.join(SKILLS, "wgs-expressive-pipeline", "scripts")


def all_skill_docs():
    """全部 SKILL.md 与辅助 md（usage-guide / references 等）——**自动发现**，不写死清单。"""
    pats = [os.path.join(SKILLS, "*", "SKILL.md"),
            os.path.join(SKILLS, "*", "*.md"),
            os.path.join(SKILLS, "*", "references", "*.md")]
    seen = {}
    for p in pats:
        for f in sorted(glob.glob(p)):
            seen[os.path.relpath(f, REPO)] = f
    return seen


def _repo_py_sources():
    """自动发现 `skills/**/*.py`，**跳过符号链接**（多个扫描面共用）。

    ⚠️ 本仓有一个**跟踪中的符号链接**指向仓外：
        skills/wgs-express-params-propose/scripts/db_query.py
          -> /home/chenyuxi/skill/.claude/skills/wgs-expressive-pipeline/scripts/db_query.py
    它指向**部署副本**，而副本会独立漂移 —— 本轮实测：副本的 `DEFAULT_DB` 写死
    `/data1/chenyx/Pig_geno/pipeline.db`，而仓内规范文件用的是 `os.environ` + 展开的家目录
    （也就是说副本比仓内文件**旧**，并非同步产物）。

    为什么必须排除它（本轮实测踩到）：
      · **判据对象错位** —— 扫链接等于用**仓外**文件的内容去判一个**仓内**路径。副本哪天
        多出一句违例，报错会指向一个本身没问题的仓内文件，且**不可复现**（结果取决于仓外
        那份副本的当前状态）。
      · **写测试会打到仓外** —— 变异验证/整块重写这类操作**穿透符号链接**写进副本；
        而 `git checkout` 恢复不了它（git 只跟踪链接本身，未跟踪目标）。
        本轮实测：一次变异把一行注进了仓外的部署副本，`git status` 全干净、
        仓内文件也没变，**没有任何信号**——直到全量回归报出两个红。
    同内容的检查由 `wgs-expressive-pipeline/scripts/db_query.py`（普通文件）覆盖，不丢覆盖。
    """
    out = []
    for p in sorted(glob.glob(os.path.join(SKILLS, "**", "*.py"), recursive=True)):
        if os.path.islink(p):
            continue
        out.append(p)
    return out


DOCS = all_skill_docs()

# 「**能力断言**」文档 = skill 文档 + 两份**维护中的**能力台账。
#
# 为什么不能直接用 `DOCS`（本轮实测踩到）：`DOCS` 只含 `skills/`，而一条**已被推翻的
# 边界断言**恰恰写在 `docs/test-evidence.md` 里 —— 于是守着它的防线**扫不到它**，
# 全绿，什么也没守。防线自己成了"纸面具备"，与它要防的缺陷同形。
#
# 为什么也不能把 `docs/` 整个并进 `DOCS`：其余几份是 **dated 事件报告**
# （test-fix-report / session-concurrency-fix / rename-to-traitpilot /
# harness-gap-analysis / dialog-liberation…），记的是"某次运行发生过什么"，
# 里面**必然**含有当时写死的本机路径、当时的期望值 —— 实测 `_pred_hardcoded_conda_path`
# 会在这类文档上命中 44 行。那些是忠实的历史记录，改它们等于篡改记录。
#
# `test-evidence.md` / `test-matrix.md` 不同：它们**持续维护**，且写明用途是
# 「记录 XX 功能到底**现在**具备什么、边界在哪」（evidence 开头即写"后续每跑一条测试
# 就往本文档追加一条"）。审计者据以判断系统能力，所以**能力断言**必须在这两份上也成立。
#
# ⚠️ 只有**「能力断言」类**的谓词该用 `CAPABILITY_DOCS`，不是全体谓词都改过去。
# 判据是「这条谓词打的是**对系统能力的错断言**，还是**历史记录的写法**」：
#   · 该用（能力断言）：`_pred_order_proven_by_header_or_index` —— "有 SO/有索引即有序"
#     是**关于系统能力**的断言，台账里写着它，读者就会据此认为该形态不存在。
#   · 不该用（历史写法）：`_pred_hardcoded_conda_path`（实测在台账上命中 **29 行**）、
#     `_pred_pytest_recipe_missing_tool_env`（15 行）—— 那些是"当时用了哪条命令/
#     当时期望多少 passed"的**忠实记录**，本机绝对路径在记录里是**正确**的，
#     按今天的写法规范去改等于篡改记录。`_pred_bare_tabix` 同理（台账里是在
#     **复述**"裸 tabix 会死"这条已修缺陷，不是让读者去执行它）。
# 换句话说：射程取决于**谓词在问什么**，不是"台账要不要一起扫"。
CAPABILITY_DOCS = sorted(DOCS) + ["docs/test-evidence.md", "docs/test-matrix.md"]


def _bash_fences(text):
    """抽出 markdown 里所有 ```bash 围栏代码块。"""
    out, cur, inside = [], [], False
    for line in text.splitlines():
        if line.strip().startswith("```"):
            if inside:
                out.append("\n".join(cur))
                cur, inside = [], False
            else:
                inside = line.strip().lower() in ("```bash", "```sh")
            continue
        if inside:
            cur.append(line)
    if cur:
        out.append("\n".join(cur))
    return out


# ============ 失效模式：面向读者的「怎么跑」配方不自足（2026-10-28 新增） ============
# 与上面几条的区别：那几条扫 **skill 文档**（LLM 照抄去生成阶段脚本），
# 这一条扫**仓库自己的运行配方**（人照抄去跑回归）。缺陷形态同源 ——
# 「权威模板自足」：配方缺了必不可少的一步，照抄即错。
#
# 本次的触发点：v0.3.0 把 `tests/test_stage_verify.py` 从「缺工具就静默 skip」
# 改成「缺工具就在收集期报错」后，`docs/test-matrix.md` 第 1 步那条配方
# （`python -m pytest tests/ -q`，没有 `export PATH=...`）当场变成跑不通 ——
# WGS 门禁用例需要真实 samtools/bcftools/bgzip/tabix，而本机默认 PATH 上四个都没有。
#
# ⚠️ 这里**只扫面向读者的「怎么跑」文档**，不扫 dated 的事件报告
# （test-fix-report / session-concurrency-fix / rename-to-traitpilot / harness-gap-analysis）：
# 那些是"某次运行发生过什么"的记录，其中的期望值（如"期望 196 passed"）本就属于当时，
# 改它们等于篡改记录。测的也不含"输出多少次通过"，只测**工具从哪来有没有说**。
RUN_RECIPE_DOCS = ["docs/test-matrix.md", "docs/test-evidence.md"]


def _pred_pytest_recipe_missing_tool_env(block):
    """该 bash 块会**加载 WGS 门禁用例**（跑整个 `tests/` 目录或点名该文件），
    却没说 samtools/bcftools/bgzip/tabix 从哪来 → True（不自足）。

    只认「整目录」目标（`tests` / `tests/`）与点名的 `test_stage_verify`。
    点名单个**非 WGS** 文件（`pytest tests/test_stops.py`）不在射程内 ——
    它不加载那个文件，自然不需要工具环境。这条界限是刻意的，不是漏判。
    """
    if re.search(r'\bPATH=', block) or "SKILL_AGENT_ALLOW_SKIP_WGS_TOOLS" in block:
        return False  # 已声明工具来源（或显式声明本次跳过）
    for line in block.splitlines():
        code = line.split("#", 1)[0]  # 去掉行尾注释，避免注释里的 pytest 误命中
        if "pytest" not in code:
            continue
        if "test_stage_verify" in code:
            return True
        # `tests` / `tests/` 作为**独立词**出现（后面紧跟空白或行尾）才算整目录；
        # `tests/test_stops.py` 的 `tests/` 后面跟的是文件名，不算 —— 否则
        # 每条点名单文件的配方都会被误伤。
        if re.search(r'(?:^|\s)tests/?\s*(?=$|\s)', code):
            return True
    return False


@pytest.mark.parametrize("rel", RUN_RECIPE_DOCS)
def test_run_recipes_declare_where_the_wgs_tools_come_from(rel):
    """「怎么跑」配方必须自足：跑整套测试就得说清工具从哪来。

    **为什么这条重要而不只是洁癖**：配方是给人逐字照抄的。缺 `export PATH`
    时它**跑不通**（v0.3.0 起收集期报错、退出码 2）—— 这是"响亮地失败"，
    比静默跳过好得多，但仍是缺陷：读者拿到一条照着敲就报错的命令。
    真正要防的回归是有人把 `export PATH` 又删掉。
    """
    path = os.path.join(REPO, rel)
    assert os.path.isfile(path), f"配方文档不见了：{rel}（护栏在扫空集）"
    blocks = _bash_fences(open(path, encoding="utf-8").read())
    recipes = [b for b in blocks if "pytest" in b]
    assert recipes, (
        f"{rel} 里一条 pytest 配方都没扫到 —— 围栏标记或文档结构变了，"
        f"本护栏正在空转（这正是它要防的「纸面具备」）")
    offenders = [b for b in recipes if _pred_pytest_recipe_missing_tool_env(b)]
    assert not offenders, (
        f"{rel} 有 {len(offenders)} 条整目录 pytest 配方没说工具从哪来，"
        f"照抄会因缺 samtools/bcftools/bgzip/tabix 在收集期报错（VERIFY-25）：\n"
        + "\n---\n".join(b.strip() for b in offenders))


# ============ 失效模式：resume 判据比门禁的存在判据松（G03，2026-10-29 新增） ============
# 缺陷形态（根因 D 的又一具体面）：**同一个断点的两端各说各话** ——
# resume 用 `[ -f ... ]`（存在）判"产物好了，跳过"，而门禁的 `have()` 判"存在"
# 是 `isfile ∧ 大小 > 0`（**0 字节算不存在**）。两端对"存在"的定义不一致，于是
# 0 字节/截断的产物让 resume 跳过、门禁又 hard FAIL → **重跑逐字相同、永久卡死**
# （实测：0 字节 BQSR BAM / 0 字节 GVCF 三次重跑输出完全一致）。
#
# 权威定义在**校验器源码**里（`have()`），不在文档里 —— 故本防线从那里推导
# "存在"的判据含"非空"，再要求所有"产物在就跳过"的守卫与之同级或更严（`-s`）。
# 这样将来门禁若把存在判据改得更严（例如加"可解析"），这条防线会跟着要求文档侧
# 重新对齐 —— 而不是像现在这样两端各自漂移。
#
# 作用域 = **产物**守卫（`continue` / "skipping" 语境里的 `[ -f/-s ... ]`）。
# 排除三类**不是**产物的检查（实测标定，避免误伤）：
#   · 标记文件（`.done` / `.failed` / `.pid` / `.verify/` 下的）—— 它们是状态，
#     不是被门禁判"非空"的阶段产物；
#   · 进程检查行（`kill -0` / `pgrep`）—— 判的是"活着"，与文件大小无关；
#   · 注释行与字符串 —— 说明文字里的引用不是守卫。
_RESUME_MARKER_PATH = re.compile(r"\.(?:done|failed|pid)\b|/\.(?:done|failed|pid|verify)/")
_RESUME_NON_PRODUCT_VAR = re.compile(r"_(?:pid|done|failed|log|report|json)\b", re.I)


def _resume_guards(text):
    """抽出"产物在就跳过"的守卫：返回 [(块序号, 块内行号, 判据标志, 目标)]。

    "产物在就跳过"的结构签名：`[ -f/-s "X" ]` 之后 ≤4 行内出现 `continue`，
    或同一窗口里出现 skipping/跳过 字样。按窗口而不是按整块，是因为一个块里
    可能同时有 resume 守卫与别的判据。
    """
    out = []
    for bi, blk in enumerate(_bash_fences(text)):
        lines = [_strip_comment(ln) for ln in blk.splitlines()]
        for i, line in enumerate(lines):
            if re.search(r"kill\s+-0|pgrep|\bps\s", line):
                continue                       # 进程检查，不是产物检查
            m = re.search(r'\[\s+(-[fs])\s+"([^"]+)"', line)
            if not m:
                continue
            flag, target = m.group(1), m.group(2)
            if _RESUME_MARKER_PATH.search(target) or _RESUME_NON_PRODUCT_VAR.search(target):
                continue
            win = "\n".join(lines[i:i + 5])
            if not (re.search(r"\bcontinue\b", win) or re.search(r"skipping|跳过", win)):
                continue
            out.append((bi, i + 1, flag, target))
    return out


def _block_loose_resume_guards(text):
    """该文档里"产物在就跳过"却用 `-f`（而非 `-s`）的守卫 ⇒ 返回命中清单。"""
    return [(bi, ln, tgt) for bi, ln, flag, tgt in _resume_guards(text) if flag == "-f"]


@pytest.mark.parametrize("rel", sorted(DOCS))
def test_resume_guards_are_at_least_as_strict_as_the_gate(rel):
    """**G03 防线**：凡"产物在就跳过"的 resume 守卫，判据不得比门禁的存在判据松。

    门禁的"存在"是 `have()` = `isfile ∧ 大小 > 0`（**0 字节算不存在**）——
    本测试直接从校验器源码推导这一事实，而不是把 `-s` 写死在注释里。
    守卫用 `-f` 时，0 字节/截断产物 → resume 跳过 → 门禁 hard FAIL → 重跑逐字
    相同、永久卡死（实测复现，见 docs/test-evidence.md 的 G03 记录）。
    """
    have_src = open(os.path.join(SV_PATH, "stage_verify.py"), encoding="utf-8").read()
    m = re.search(r"def have\(path\):\s*\n\s*return ([^\n]+)", have_src)
    assert m, "从 stage_verify.py 读不到 have() 的定义 —— 防线在空转（推导源变了）"
    assert "getsize" in m.group(1) and "> 0" in m.group(1), (
        f"have() 的实现变了：{m.group(1)!r} —— 若它不再要求「非空」，"
        f"本防线的前提（存在=非空）不再成立，需重新裁定 resume 的严格度要求")

    text = open(DOCS[rel], encoding="utf-8").read()
    bad = _block_loose_resume_guards(text)
    assert not bad, (
        f"{rel} 有 {len(bad)} 处 resume 守卫用 `[ -f ]` 判产物存在，而门禁的 "
        f"`have()` 要求**非空**（0 字节算不存在）—— 两端对「存在」的定义不一致：\n  "
        + "\n  ".join(f"第{ln}行 → {tgt}（应改 `-s`）" for _, ln, tgt in bad))


def test_resume_guard_predicate_actually_discriminates():
    """判别力自检：坏样本必红、好样本不误伤、且全仓至少扫到 N 处真守卫（反空转）。"""
    bad = '```bash\nif [ -f "${BAM}" ]; then\n    log_info "exists, skipping"\n    continue\nfi\n```'
    good = '```bash\nif [ -s "${BAM}" ]; then\n    log_info "exists, skipping"\n    continue\nfi\n```'
    marker = '```bash\nif [ -f "${RUN_DIR}/.done/x.done" ]; then\n    continue\nfi\n```'
    proc = '```bash\nif [ -f "${pid_file}" ] && kill -0 "$(cat "${pid_file}")"; then\n    continue\nfi\n```'
    comment = '```bash\n# 早期写法 if [ -f "${GVCF}" ]; then continue\nif [ -s "${GVCF}" ]; then\n    continue\nfi\n```'

    assert _block_loose_resume_guards(bad), "坏样本（-f 守卫）必须报"
    assert _block_loose_resume_guards(good) == [], "好样本（-s 守卫）不得报"
    assert _block_loose_resume_guards(marker) == [], "标记文件（.done）不是产物，不得报"
    assert _block_loose_resume_guards(proc) == [], "进程检查不是产物检查，不得报"
    assert _block_loose_resume_guards(comment) == [], "注释里的引用不得报"

    # 反空转：全仓必须真的扫到"产物在就跳过"的守卫 —— 否则这条防线什么也没守
    total = sum(len(_resume_guards(open(DOCS[r], encoding="utf-8").read())) for r in DOCS)
    assert total >= 5, (
        f"全仓只扫到 {total} 处产物 resume 守卫 —— 抽取器失效了"
        f"（本防线正在空转，这正是它要防的「纸面具备」）")


# ============ 失效模式：文档教的写值被判 hard（G04/G07，2026-10-29 新增） ============
# 缺陷形态（根因 D 的最直接形态）：**同一个断点的两端各说各话** ——
# `params-propose/SKILL.md` 的 schema 字面量教写 `"min_variants": 0`（并在说明里
# 逐字定义"用户明确不做此判定时写 0"），而校验器把 `raw <= 0` 判 hard。于是
# **照文档生成的默认 run.params.json 让 Stage 3/4/5 在健康数据上永远写不出 .done**。
# 两端各自的文本都是"自洽"的，没有一处机制把它们连起来。
#
# 推导源是**双端结构**，两边都不写死：
#   · 代码侧：从 stage_verify.py 抽"校验器会读的 run.params.json 键"——
#     凡出现 `params["<key>"]` / `"<key>" in params` 的键名，就是它会判的键。
#     （不抽 `--min-variants` 这类 CLI 参数：它们不是 run.params.json 的键。）
#   · 文档侧：从 params-propose SKILL.md 的 ```json 围栏（schema 字面量）抽键名。
# 差集 = 缺口（G07：`min_variants_indel` 曾被校验器读、而 schema 里没有）。
#
# 第二条判据（G04 的核心）：凡文档**明文教写某个值**（"写 0"句式），该值不得被
# 校验器判 hard。判据是文档句子 → 该键 → 校验器该分支的**实测行为**，不是文本比对。
PARAMS_PROPOSE_REL = "skills/wgs-express-params-propose/SKILL.md"


def _verifier_params_keys():
    """从 stage_verify.py 抽「校验器会读的 run.params.json 键名」。

    两种读取形态都认：`params["<key>"]` 与 `"<key>" in params`。
    **不写死键名** —— 将来新增一个 `params["xxx"]` 就自动进射程。
    """
    src = open(os.path.join(SV_PATH, "stage_verify.py"), encoding="utf-8").read()
    keys = set(re.findall(r'\bparams\["([a-z_][a-z0-9_]*)"\]', src))
    keys |= set(re.findall(r'"([a-z_][a-z0-9_]*)"\s+in\s+params\b', src))
    return keys


def _schema_params_keys():
    """从 params-propose SKILL.md 的 json 围栏（schema 字面量）抽键名。

    只抽**顶层**键（缩进 ≤ 2 空格的行），因为对账的单位是 run.params.json 的顶层字段。
    """
    text = open(os.path.join(REPO, PARAMS_PROPOSE_REL), encoding="utf-8").read()
    keys = set()
    for m in re.finditer(r"```json\n(.*?)```", text, re.S):
        for line in m.group(1).splitlines():
            mm = re.match(r'^ {2}"([a-z_][a-z0-9_]*)"\s*:', line)
            if mm:
                keys.add(mm.group(1))
    return keys


def test_every_verifier_params_key_is_declared_in_the_schema():
    """**G07 防线**：校验器会读的每个 `run.params.json` 键，都必须在 Stage 0 的
    schema 里有声明 —— 否则"正规通路无法声明它"（G07 就是 `min_variants_indel`：
    校验器读它、自己在 warn 文案里教怎么写，而 schema / SKILL.md / workflow.md
    三处文档都只写 SNP 键）。

    反空转：两侧推导都必须非空，且**差集为空**才算通过（下方断言）。
    """
    verifier_keys = _verifier_params_keys()
    schema_keys = _schema_params_keys()
    assert verifier_keys, (
        "从 stage_verify.py 抽不到任何 run.params.json 键 —— 读取形态变了，"
        "本防线在空转（这正是它要防的「纸面具备」）")
    assert len(schema_keys) >= 5, (
        f"从 params-propose 的 json 围栏只抽出 {len(schema_keys)} 个顶层键 —— "
        f"schema 结构或围栏标记变了，本防线在空转")
    missing = sorted(verifier_keys - schema_keys)
    assert not missing, (
        f"校验器会读这些 run.params.json 键，而 Stage 0 的 schema 里没有声明它们："
        f"{missing} —— 用户/LLM 照 schema 写参数时**无从知道有这些键**，"
        f"该判据的正规声明通路不存在（G07 形态）")


def test_every_key_the_schema_has_value_for_is_accepted_by_the_verifier():
    """**G04 防线**：schema 字面量里**示范了取值**的键，其示范值不得被校验器判 hard。

    这是"文档教 A、代码判 B"的**行为级**对账：不看文本措辞，而是把 schema 里
    的字面量喂给校验器的解析函数，断言它不被判畸形。`min_variants: 0` 就是被
    这条抓到的（文档明文定义 0 = 显式不做，实现却判 hard）。

    实现方式：用 `ast` 从源码抽出 `_resolve_min_variants` / `_resolve_min_variants_indel`
    的**判定边界**（哪些值会进"畸形"分支），而不是真的 import 进来跑 —— 那需要
    构造 args/文件系统。这里断言的是**结构性事实**：源码里"显式不做"的豁免判据
    必须存在且含 `isinstance(..., int)`（挡 `True`）与 `raw == 0`。
    """
    src = open(os.path.join(SV_PATH, "stage_verify.py"), encoding="utf-8").read()
    for fn in ("_resolve_min_variants", "_resolve_min_variants_indel"):
        m = re.search(rf"def {fn}\(.*?\n(.*?)(?=\ndef |\Z)", src, re.S)
        assert m, f"找不到 {fn} —— 防线在空转（函数改名了？）"
        body = m.group(1)
        assert re.search(r"raw == 0", body), (
            f"{fn} 里没有「显式不做（= 0）」的豁免分支 —— 若 Schema 仍教写 0，"
            f"两端又不一致了（G04/G07 形态）")
        assert re.search(r"isinstance\(raw, bool\)", body), (
            f"{fn} 的 0 豁免没有排除 bool —— `False == 0` 为真，"
            f"`\"min_variants\": false`（类型写错）会被静默当成豁免")

    # 正控：schema 里真的教了 0（否则上述断言守的是个不存在的约定）
    schema_text = open(os.path.join(REPO, PARAMS_PROPOSE_REL), encoding="utf-8").read()
    assert re.search(r'"min_variants":\s*0\b', schema_text), (
        "params-propose 的 schema 不再示范 min_variants: 0 —— 若约定变了，"
        "本防线的正控前提失效，需重新裁定（而不是让它继续守着旧约定）")


def test_params_reconciliation_predicates_actually_discriminate():
    """判别力自检：键名推导两侧都必须真的抽得到东西，且能识别缺口。"""
    vk = _verifier_params_keys()
    sk = _schema_params_keys()
    # 真实存在的键必须被抽到（正控）
    assert "min_variants" in vk, f"没抽到 min_variants（抽到的是 {sorted(vk)}）"
    assert "min_variants_indel" in vk, f"没抽到 min_variants_indel（G07 的键）"
    assert "min_variants" in sk and "verifier_path" in sk, \
        f"schema 侧抽到的键不对劲：{sorted(sk)}"
    # 负控：构造一个"校验器读、schema 没有"的键，差集必须报出来
    fake_key = "phantom_params_key"
    assert {fake_key} - sk, "差集判据失效（负控没能被识别为缺口）"


def test_doc_discovery_is_not_empty():
    """前提自检：自动发现若扫不到文件，下面每条防线都会空转"全绿"。"""
    assert len(DOCS) >= 10, f"只发现 {len(DOCS)} 份文档，路径规则变了？{sorted(DOCS)[:5]}"


@pytest.mark.parametrize("rel", sorted(DOCS))
def test_bash_fences_do_not_reference_undefined_names(rel):
    """**G02 防线**：给 LLM 照抄的 bash 围栏不得引用"自己从没定义"的变量/函数。

    三条判据都窄而准（标定见 `_pred_fence_uses_var_without_definition` 上方注释）：
      (A) `${X_DIR}` 无赋值而同前缀 `${X_FILE}` 有赋值 —— "改名改了一半"；
      (B) 调用 `log_*` 而**该名字**全文档无定义（按名匹配、允许缩进）；
      (C) 调用了 `skills/_shared/env.sh` 的函数却没 source —— 名字集从该文件推导。
    另有一条**模板专用**判据：
      (D) 「完整阶段模板」围栏（shebang + 自声明 `RUN_DIR`）里用了既非块级也非
          文档级声明的 `${VAR}` —— 这类围栏是**逐字照抄**面，占位符必须自带声明行。

    严重性依据：这些围栏是 `set -euo pipefail` 的模板，未定义名字**一定**以 rc=1
    硬炸（不是警告），且炸在**写任何标记之前** —— 四态判定看不到它。
    """
    doc = open(DOCS[rel], encoding="utf-8").read()
    problems = []
    for blk in _bash_fences(doc):
        for v in _pred_fence_uses_var_without_definition(blk, doc):
            problems.append(f"用了 ${{{v}}} 但只定义过同前缀的 *_FILE（改名改了一半？）")
        for fn in _pred_fence_calls_undefined_log_fn(blk, doc):
            problems.append(f"调用了未定义的函数 {fn}")
        for fn in _pred_fence_calls_unsourced_shared_fn(blk, doc):
            problems.append(f"调用了 {fn}，但本围栏没有 source skills/_shared/env.sh")
        for v, ln in _pred_template_fence_uses_undeclared_var(blk, doc):
            problems.append(
                f"照抄模板第 {ln} 行用了 ${{{v}}}，既没在本围栏声明、全文档也没有它的声明行")
    assert not problems, (
        f"{rel} 的 bash 围栏引用了从未定义的名字，照抄生成的脚本在 "
        f"`set -euo pipefail` 下当场 rc=1、且炸在写任何标记之前：\n  "
        + "\n  ".join(problems))


def test_bash_fence_name_predicates_actually_discriminate():
    """判别力自检：两个谓词必须真的会红（坏样本）、且不误伤（好样本）。

    ⚠️ 负控用的字面量在**运行时拼出来** —— 直接写进源码会被谓词扫到自己。
    """
    # 坏样本 1：X_DIR / X_FILE 改名改了一半
    bad1 = 'LOG_FILE="${RUN_DIR}/logs/01.log"\nmkdir -p "${LOG_DIR}"'
    # 坏样本 2：调用未定义的 log_*（文档里没有任何 log_x() 定义）
    bad2 = 'log_info "SNPs: ${n}"\ndate > "${DONE_FILE}"'
    good1 = 'LOG_FILE="${RUN_DIR}/logs/01.log"\nmkdir -p "$(dirname "${LOG_FILE}")"'
    good2 = 'LOG_DIR="${RUN_DIR}/logs"\nmkdir -p "${LOG_DIR}"'

    assert _pred_fence_uses_var_without_definition(bad1, bad1) == ["LOG_DIR"]
    assert _pred_fence_uses_var_without_definition(good1, good1) == []
    assert _pred_fence_uses_var_without_definition(good2, good2) == [], \
        "本围栏里定义过 LOG_DIR 就不该报"

    # 函数名运行时拼出，避免自命中
    fn = "log_" + "info"
    fn_def = fn + "()  { echo hi; }"
    assert _pred_fence_calls_undefined_log_fn(f'{fn} "x"', "no def here"), \
        "未定义就调用必须报"
    assert _pred_fence_calls_undefined_log_fn(f'{fn} "x"', fn_def) == [], \
        "文档别处定义过（哪怕在另一围栏）就不该报"
    assert _pred_fence_calls_undefined_log_fn(f'# {fn} "x" 是注释', "no def") == [], \
        "注释里的调用不该报"
    assert _pred_fence_calls_undefined_log_fn('echo "' + fn + ' 不是调用"', "no def") == [], \
        "字符串里出现的名字不该报"

    # ⚠️ 2026-10-30：下面三组是**先前漏掉的真实形态**。缺了它们，
    #    这条自检只覆盖了"无缩进 + 无任何定义"这一种写法，而真实调用全是缩进的。
    indented = 'if [[ ! -f "${X}" ]]; then\n    ' + fn + ' "缺 X"; exit 1\nfi'
    assert _pred_fence_calls_undefined_log_fn(indented, "no def"), \
        "**缩进**的调用（失败分支的常态）漏报 —— 全仓 41 处真实调用里 33 处这样写"
    other = "log_" + "error" + "()  { echo e; }"
    assert _pred_fence_calls_undefined_log_fn('log_' + 'warn "a"', other), \
        "文档里有**别的** log_* 定义不得成为放行理由 —— 必须**按名**匹配"
    assert _pred_fence_calls_undefined_log_fn('if x; then ' + fn + ' "y"; fi', fn_def) == [], \
        "同行 if/then 之后调用**已定义**的函数不该报"

    # 判据 (C)：env.sh 里定义的函数，围栏用了却没 source
    sk_fns = sorted(_shared_env_functions())
    sk_fn = sk_fns[0] if sk_fns else None
    if sk_fn:
        assert _pred_fence_calls_unsourced_shared_fn(f"{sk_fn}\necho ok", "x"), \
            f"调用 {sk_fn} 却没 source env.sh 必须报"
        assert _pred_fence_calls_unsourced_shared_fn(
            f"source skills/_shared/env.sh\n{sk_fn}\necho ok", "x") == [], \
            "source 过就不该报（哪怕是相对路径）"
        assert _pred_fence_calls_unsourced_shared_fn(f"# {sk_fn} 定义见 env.sh", "x") == [], \
            "只出现在注释里的名字不该报"

    # 判据 (D)：照抄模板里的未声明占位符
    tmpl_bad = '#!/bin/bash\nset -euo pipefail\nRUN_DIR="..."\nX="${MY_PREFIX}.bed"\n'
    tmpl_ok = ('#!/bin/bash\nset -euo pipefail\nRUN_DIR="..."\n'
               'MY_PREFIX="..."   # 由编排层替换\nX="${MY_PREFIX}.bed"\n')
    assert _pred_template_fence_uses_undeclared_var(tmpl_bad, tmpl_bad), \
        "模板里的未声明占位符必须报"
    assert _pred_template_fence_uses_undeclared_var(tmpl_ok, tmpl_ok) == [], \
        "自己声明过就不该报"
    assert _pred_template_fence_uses_undeclared_var(
        'BFILE="${PREFIX}.bed"\n', 'PREFIX="..."   # 文档级声明\n') == [], \
        "文档别处（如共用的占位说明段）声明过就不该报"
    assert _pred_template_fence_uses_undeclared_var(tmpl_bad.replace("#!/bin/bash\n", ""), "") == [], \
        "不带 shebang 的片段示例不在射程内（否则 gwas-format-convert 的 PLINK 片段会误伤）"
    assert _pred_template_fence_uses_undeclared_var("#!/bin/bash\nX=\"${PATH}/y\"\n", "") == [], \
        "shell 自带的环境变量不该报"

    # 反空转：全仓必须真的有围栏被扫到（否则谓词再准也没意义）
    total = sum(len(_bash_fences(open(DOCS[r], encoding="utf-8").read()))
                for r in DOCS)
    assert total >= 20, f"全仓只扫到 {total} 个 bash 围栏 —— 抽取器失效了？"
    tmpl = [b for r in DOCS for b in _bash_fences(open(DOCS[r], encoding="utf-8").read())
            if _is_stage_template_fence(b)]
    assert len(tmpl) >= 8, \
        f"全仓只认出 {len(tmpl)} 个『完整阶段模板』围栏 —— 判据 (D) 在空转"
    assert sk_fns, "skills/_shared/env.sh 里一个函数都没扫到 —— 判据 (C) 在空转"


# ======================= 谓词：真 ⇒ 该行/该块**有缺陷** =======================

def _pred_bare_tabix(line):
    """裸 `tabix -p vcf`（无 `-f`）且不在"告诫读者不要这么写"的语境里。"""
    if not re.search(r"\btabix\b", line):
        return False
    if re.search(r"不要|勿|别|禁止|⚠|无需|wave|自动写|自动产出|GATK 自动", line):
        return False
    m = re.search(r"tabix\s+(-[A-Za-z]+\s+)*-p\s+vcf", line)
    return bool(m) and " -f " not in line and not re.search(r"tabix\s+-f", line)


def _pred_readgroup_auto_claim(line):
    """正面断言"读组会被自动配好/且符合 SM"（而非任何提到 read group 的行）。"""
    if not re.search(r"read.?group|读组", line, re.I):
        return False
    # 已被标注为"旧结论/错的"的行属于**纠正叙述**，不是缺陷本身
    if re.search(r"不要|⚠|并不|不是自动|常量|写死|SM:sample|必须显式|错的|旧版本", line):
        return False
    # 中文正面论断
    if re.search(r"自动(生成|加上|写入|配好)", line) and \
            re.search(r"符合\s*`?SM`?|无需|即可|不必|不用指定|automatic", line, re.I):
        return True
    # 英文旧结论（原句）
    return bool(re.search(r"generates the read group automatically", line, re.I) and
                not re.search(r"wrong|incorrect|错", line))


def _pred_hardcoded_conda_path(line):
    """写死本机 conda 绝对路径，或**建议**用 conda 绝对路径（违反硬规则 #5.1）。"""
    if re.search(r"/(home|Users|data\d?)/[\w.\-]+/(anaconda3|miniconda3|mambaforge)/envs/", line):
        return not re.search(r"实测|复现|本机|例如|如：|例：|环境变量", line)
    if re.search(r"conda absolute path|写死.*绝对路径|绝对路径.*工具", line, re.I):
        return not re.search(r"禁止|不要|勿|别|硬规则|command -v|探测|旧版本|错", line)
    return False


# ============ 失效模式：围栏引用了自己从没定义的名字（G02，2026-10-29 新增） ============
# 缺陷形态（根因 D 的一个具体面）：围栏是给 LLM **逐字照抄**的模板，而模板里用了
# 从未赋值的变量、从未定义的函数 —— 照抄生成的脚本在 `set -euo pipefail` 下当场
# 以 rc=1 退出，磁盘上既无 .done 也无 .failed（若该流水线还有四态判定，会被误归
# 为"已中断"）。这正是 G02 的原始形态：`gwas-format-convert` 的围栏里
# `mkdir -p "${LOG_DIR}" "${DONE_DIR}" "${PID_DIR}"` 用了三个从未赋值的 `*_DIR`，
# 而同一围栏只定义了 `LOG_FILE`/`DONE_FILE`/`PID_FILE`；末尾还有 `log_info "..."`，
# 全文档没有任何定义。
#
# ⚠️ **为什么判据必须窄**（本轮实测标定）：先试过宽版"围栏里的 ${VAR} 必须在
# 同文档有赋值"——实测**三个口径都很大**：253 次出现 / 97 个 (文档,变量) 对 /
# 213 行（自测过的抽取器重算，2026-10-30）。绝大多数是**片段式示例**：
# 对齐/排序等文档里的围栏是"关键段落摘录"（`bwa mem ... "${R1}" "${R2}"` 的
# R1/R2 在样本循环里赋值，不在这段里），或是给编排层填的模板占位。宽版会让防线
# 变成一串误报，下一个维护者只能把它关掉 —— 那就是"纸面具备"的另一种死法。
#
# ⚠️ **2026-10-30 订正**：此处原文写"全仓 137 处命中"，该数字**不可复现**（另一次
# 独立测量得 255–294 / 96–114 / 214，同样对不上）。按上面的方式重算并改掉。
# 教训与 §11.12 同：**写进代码注释的"实测数字"也是断言**，它同样会被后人引用，
# 同样必须能被当场重跑出来。
#
# 窄版只认三条**结构性**判据（都实测零误报）：
#   (A) `X_DIR` 无赋值而同名前缀的 `X_FILE` 有赋值 —— 这是"改名改了一半"的
#       签名：作者把 LOG_FILE 写对了，却把 mkdir 的目标写成了 LOG_DIR。
#       兄弟 skill 全部用 `mkdir -p "$(dirname "${LOG_FILE}")" ...`，该写法
#       天然不引用 `*_DIR`，所以命中的就是真缺陷。
#   (B) 调用 `log_*` 而**该名字**在全文档无定义。⚠️ 2026-10-30 修：先前版本写成
#       "文档里有**任意** log_*() 定义就放行"，且正则带 `^` 锚点要求 token 在
#       **行首** —— 两处叠加后判别力实测为零：全仓 41 处真实调用里有 33 处是
#       **缩进**的（全在失败分支里），一个都匹配不到；把 `wgs-express-filtering`
#       的整段定义删掉，谓词仍返回 `[]`。判别力自检的坏样本恰好是无缩进的写法，
#       于是"自检绿、真实形态盲"。函数名不是数据，不存在"片段示例里在别处赋值"
#       的可能，所以按名匹配是安全的下界。
#   (C) 调用了 `skills/_shared/env.sh` 里定义的函数却没 source 它。判据是**推导**的：
#       函数名集合从 env.sh 扫出，不写死清单。

# 从 `skills/_shared/env.sh` **推导**可调用的函数名集合（判据 C 用）。
_SHARED_ENV_SH = os.path.join(SKILLS, "_shared", "env.sh")


def _shared_env_functions():
    """`{函数名}`：从 `skills/_shared/env.sh` 的 `name() {` 定义里扫出来。

    判据 C 的名字面**必须**推导而非写死：将来 env.sh 里加了新工具函数，
    防线要自动跟着把它纳入，否则新函数又是一处无守护的引用点。
    """
    try:
        src = open(_SHARED_ENV_SH, encoding="utf-8").read()
    except OSError:
        return set()
    return {m.group(1) for m in re.finditer(r"^\s*(sk_\w+)\s*\(\s*\)", src, re.M)}


def _pred_fence_uses_var_without_definition(block, doc_text):
    """围栏里出现 `${X_DIR}` 却只定义过 `${X_FILE}`（同前缀）⇒ 返回命中的变量列表。

    判据是**同名前缀的两端**，不是"任何未定义变量"：前者是"改名改了一半"的
    结构性签名（作者心里的名字是 `X_DIR`，笔下的定义是 `X_FILE`），后者会把
    片段式示例全部误伤（实测 253 次出现 / 97 对 / 213 行，见上方说明）。
    """
    hits = []
    for m in re.finditer(r"\$\{(\w+)_DIR\}", block):
        if re.search(rf'^\s*(?:export\s+)?{m.group(1)}_DIR=', block, re.M):
            continue                       # 本围栏里定义过，正常
        if re.search(rf'^\s*(?:export\s+)?{m.group(1)}_FILE=', block, re.M):
            hits.append(m.group(1) + "_DIR")   # 只定义过 *_FILE —— 改名改了一半
    return hits


def _pred_fence_calls_undefined_log_fn(block, doc_text):
    """围栏里调用 `log_*`（命令位，**允许缩进**），而**该名字**全文档无 `log_x() {` 定义。

    为什么把"全文档"而不是"本围栏"作为定义面：wgs-express 系列把日志函数定义
    在一个独立的 ```bash 围栏（「日志函数」段）里，使用在另一个围栏 —— 那是
    **正常的**分块写法。真正的缺陷是**全文档都没有**定义。

    ⚠️ 两处必须同时成立（2026-10-30 实测修正）：
      1. **按名匹配**：先前版本只要有**任意** `log_*()` 定义就整体放行，
         于是"调用 log_warn + 文档里定义了 log_error"这种改错名字的形态漏报。
      2. **匹配允许缩进**：先前正则 `(?:^|[;&|]\\s*|\\bthen\\s+)` 里的 `^` 要求
         命令名出现在**行首**，而真实调用 41 处里 33 处是缩进的（失败分支），
         一个都抓不到 —— 把 `wgs-express-filtering` 的整段定义删掉，本函数
         仍返回 `[]`。判别力自检原先用的坏样本恰好是无缩进写法，掩盖了它。
    """
    defined = {m.group(1) for m in re.finditer(
        r"^\s*(?:function\s+)?(log_\w+)\s*\(\s*\)", doc_text, re.M)}
    hits = []
    for ln, line in enumerate(block.splitlines(), 1):
        code = _strip_comment(line)
        for m in re.finditer(
                r"(?:^|[;&|]|\bthen\b|\bdo\b|\belse\b|&&|\|\|)\s*(log_\w+)\s+[\"'$]", code):
            if m.group(1) not in defined:
                hits.append(f"{m.group(1)} (块内第 {ln} 行)")
    return hits


def _pred_fence_calls_unsourced_shared_fn(block, doc_text):
    """围栏调用了 `skills/_shared/env.sh` 里定义的函数，却没在本围栏 source 它。

    与判据 (B) 同形，但**更硬**：`env.sh` 的函数全在仓库里，名字集合可从文件本身
    推导；而被调用的那个围栏（`gwas-ld-block`）既没 source、也没内联实现 ——
    照抄在 `set -euo pipefail` 下当场 rc=127（`command not found`）。

    刻意**不**扩展到"跨文档引用"：`wgs-express-align-sort` 的散文里写「可
    `source skills/_shared/env.sh` 后用 `sk_tool bwa`」是对**交互式读者**的说明，
    那段话自己就给了 source 指令，不是给照抄模板的缺口。
    """
    fns = _shared_env_functions()
    if not fns:
        return []
    if re.search(r"(?:^|[;&|]|\bthen\b|\bdo\b)\s*(?:source|\.)\s+[^\n]*_shared/env\.sh", block, re.M):
        return []
    hits = []
    for ln, line in enumerate(block.splitlines(), 1):
        code = _strip_comment(line)
        for m in re.finditer(r"(?:^|[;&|]|\bthen\b|\bdo\b|\belse\b|&&|\|\|)\s*(sk_\w+)\b", code):
            if m.group(1) in fns:
                hits.append(f"{m.group(1)}() (块内第 {ln} 行)")
    return hits


# 「完整阶段模板」围栏的**结构签名**：自带 shebang（自称是可直接执行的完整脚本）
# 且自己声明 `RUN_DIR`（六个阶段模板共有的那一行）。两者缺一就不算模板 ——
# 只带 shebang 的独立小工具（如 `phenotype_extract.py` 的示范）也不该被当模板。
def _is_stage_template_fence(block):
    ls = block.split("\n")
    return bool(ls) and ls[0].lstrip().startswith("#!") \
        and bool(re.search(r"^\s*RUN_DIR=", block, re.M))


# bash 里"声明过"的写法（赋值 / read / for / local）。**保守取值**：只用来判"有没有"，
# 不用来判作用域 —— 宁可漏报（把文档级声明当成块级）也不误报。
_DECL_RE = re.compile(
    r"(?:^|[;&|(]|\bthen\b|\bdo\b|\belse\b|&&|\|\|)\s*(?:export\s+|local\s+|declare\s+[-\w]*\s*)?"
    r"([A-Za-z_]\w*)=", re.M)
_READ_RE = re.compile(r"(?:^|[;&|(]|\bthen\b|\bdo\b)\s*read\s+(?:-\w+\s+)*([A-Za-z_]\w*)", re.M)
_FOR_RE = re.compile(r"(?:^|[;&|(]|\bthen\b|\bdo\b)\s*for\s+([A-Za-z_]\w*)\s+in\b", re.M)

# 这些名字由 shell / conda 从外部保证，不要求文档自己声明。
_SHELL_PROVIDED = {
    "PATH", "HOME", "PWD", "USER", "SHELL", "TMPDIR", "IFS", "BASH_SOURCE", "LINENO",
    "HOSTNAME", "CONDA_PREFIX", "CONDA_DEFAULT_ENV", "LC_ALL", "LANG", "RANDOM",
    "EPOCHSECONDS", "BASHPID", "UID", "OLDPWD", "FUNCNAME", "BASH_VERSION", "PS1",
    "PIPESTATUS", "SECONDS",
}


def _sh_declared_names(text):
    """从一段 bash 里扫出"被声明过"的名字集合（赋值 / read / for 的循环变量）。"""
    return ({m.group(1) for m in _DECL_RE.finditer(text)}
            | {m.group(1) for m in _READ_RE.finditer(text)}
            | {m.group(1) for m in _FOR_RE.finditer(text)})


def _pred_template_fence_uses_undeclared_var(block, doc_text):
    """**完整阶段模板**围栏里用了既非块级、也非文档级声明的 `${VAR}` ⇒ 返回 (变量, 行号)。

    范围**必须**收在 `_is_stage_template_fence` 上（实测标定）：放宽到一个 `## 脚本模板`
    小节下的围栏，`gwas-format-convert` 的"直接 VCF → PLINK"示例（`BFILE`/`OUT`/`FAM`
    三个占位、上方散文有说明）会被误伤 —— 那是给读者看的片段，不是照抄模板。

    标定记录（2026-10-30）：修前命中 6 个 gwas 阶段模板、共 41 处 / 17 个变量名
    （`PREFIX` `BFILE` `CHR` `START` `END` `GFF` `CUTLINE` `TOPSITE` `OUTPREFIX`
    `GWAS_PVALUE` `COVAR_TXT` `PHE_TXT` `C_TXT` `SIG_TXT` …）；两个 wgs-express
    模板（align-sort #2 / fastp-qc #2）与带 shebang 的独立工具围栏**零误伤**。
    修复方式：与 `gwas-format-convert` 已落地的写法一致，在模板里补一行
    `X="..."  # 由 gwas-pipeline 生成脚本时替换为实际值`。
    """
    if not _is_stage_template_fence(block):
        return []
    front = _sh_declared_names(doc_text)
    decl = _sh_declared_names(block)
    hits = []
    seen = set()
    for ln, line in enumerate(block.splitlines(), 1):
        for m in re.finditer(r"\$\{(\w+)\}", _strip_comment(line)):
            v = m.group(1)
            if v in _SHELL_PROVIDED or v.isdigit() or v in decl or v in front:
                continue
            if v in seen:
                continue
            seen.add(v)
            hits.append((v, ln))
    return hits


def _pred_idxstats_as_judgment_source(line):
    """正面推荐用 `idxstats`（**派生数据**）去取得/判定 reads 数、比对率。

    这是根因 A（判据读派生数据不读本体）在**文档层**的复发形态：SKILL.md 是 LLM
    照抄生成阶段脚本的来源，这里推荐 `idxstats`，生成的脚本就会用它 —— 于是三个
    已实测的失效形态会在新脚本里原样重现（陈旧/外来索引撒谎、GPU 后端漏计）。
    代码侧的 `check_bam` 已改为读本体，但那只堵住了校验器自己；**文档改了才是堵住源头**。
    """
    if not re.search(r"idxstats", line):
        return False
    if not re.search(r"reads?\s*数|read_count|条数|比对率|mapped|空\s*BAM|判(定|断)|取(值|数)",
                     line, re.I):
        return False
    if re.search(r"不要|勿|别|禁止|⚠|不能|不得|而非|不是|错的|旧版本|只作|旁证|派生数据", line):
        return False
    return True


def _pred_order_proven_by_header_or_index(line):
    """断言**排序状态可由头部标签或"索引存在"证明**（F18 残余在文档层的复发形态）。

    ⚠️ 这是本轮实测推翻的一条**曾被明确登记**的结论，不只是措辞不当：
    实测（40000 条 / 126 个 BGZF 块，交换两个相邻数据块，**保留原有 .bai**）：
        文件字节数不变、quickcheck rc=0、`view -c`=40000（与完整文件同）、
        idxstats=40000、@HD 仍写 `SO:coordinate`
        ⇒ 门禁 `✅ 全部检查通过` rc=0，而本体坐标从 999993 跳回 988415。
    `samtools index` 只在**你试图建/重建索引**时检查顺序 —— 对一个**已有** .bai 的
    BAM 做"字节数不变的就地篡改"，索引还在、乱序也在。故：
      · "乱序且带索引"在现实中**是存在的**（此前的边界登记已作废）；
      · `@HD SO` 是**头部自称**，与本体无关，不能作为排序的证据。
    只有"拿本体重建一次索引"能证明本体有序（校验器里叫 `body_indexable`）。

    判据只打"断言它**能证明**"的行 —— 正确写法（"SO 只是自称、须重建索引复核"）
    不得被误伤。

    ⚠️ 三个 token 集合都是**实测标定**的，不是想当然写的（改动前先看
    `test_guards_actually_discriminate` 里那十几条边界样本）：
      · 只认 `SO:coordinate`/`@HD` 会漏掉"由**索引存在**推断有序"（坏样本 3、5）——
        那正是本形态的另一半，必须把 `.bai`/`.csi`/索引/index 一并算作"证据来源"。
      · 结论必须挂在**证据**上：光有过渡词（即/故/说明…）不算，得同时出现
        "有序/排序/coordinate/正确"，或"乱序…不存在"这类**否定式断言**
        （被推翻的那条原文正是这种写法，早期的正则漏了它）。
      · 「不是」**不能**当豁免词 —— 它常出现在"…说明该 BAM **不是**乱序的"这种
        **正面论断**里（坏样本 4）。豁免的是"不能/不得/并非"这类否定**态度**。
      · 「而非/而不是」**不进豁免词表**，改为按**作用方向**处理：它否定的是其后
        那一侧。"判定有序，而非以 SO 标签"= 纠正叙述（豁免）；
        "以 SO 标签判定有序，而非重建索引"= 仍在断言 SO 能证明（不豁免）。
        一律豁免会漏报，一律不豁免会误伤 —— 只有看方向才对。
      当前集合是逐词在真实语料（skills/ + docs/ 共 34 份文档、13667 行）上量出来的：
      命中行数恰好等于已知真缺陷数（1 行，即 test-evidence.md §11.2 那条），
      说明这些豁免词在现有语料上不吞真缺陷，只是为将来的纠正叙述留出余地。
    """
    # 已被标注为"推翻/作废/自称/派生数据"的行是**纠正叙述**，不是缺陷本身
    if re.search(r"推翻|作废|订正|证伪|已失效|错的|旧版本|旧结论"
                 r"|自称|不验本体|与本体无关|派生数据|不参与判定|只作旁证|旁证", line):
        return False
    # 告诫读者"不要这么判"的行同理
    if re.search(r"不要|切勿|禁止|不能|不得|并非|别(?:把|用|靠|拿|以|只|再|据|凭)", line):
        return False
    # 「而非/而不是」按作用方向判：只在其**后**一侧点名证据、且那不是"正确方法"时豁免
    m = re.search(r"而(?:非|不是)\s*(.{0,24})", line)
    if m and (re.search(r"SO\s*[:=]\s*coordinate|SO\s*标签|@HD|索引|\.bai|\.csi|"
                        r"\bbai\b|\bcsi\b|\bindex(?:es)?\b", m.group(1), re.I)
              and not re.search(r"重建|复核|重算|重跑|读本体|取本体|本体验证", m.group(1))):
        return False
    # 证据来源：头部标签**或**索引（派生数据）
    if not re.search(r"SO\s*[:=]\s*coordinate|SO\s*标签|@HD|索引|\.bai|\.csi|"
                     r"\bbai\b|\bcsi\b|\bindex(?:es)?\b", line, re.I):
        return False
    # 由证据**下结论**：过渡词 + 有序/排序，或"乱序…不存在"式否定断言
    return bool(re.search(
        r"(即|故|所以|因此|据此|由此|说明|证明|可判|判定|保证|表明)[^\n]{0,14}"
        r"(有序|排序|sorted|coordinate|正确|(?:不是|非|不会|没有|不存在)乱序)"
        r"|(乱序|未排序|无序|unsorted)[^\n]{0,14}(不存在|不可能|不会出现|不成立)",
        line, re.I))


def _block_missing_samples_csv(blk, doc_has_assignment):
    """块里用了 `${SAMPLES_CSV}` 但全文档都没有它的赋值 ⇒ A5 复发。"""
    if "${SAMPLES_CSV}" not in blk:
        return False
    if re.search(r'^\s*SAMPLES_CSV="\$\(', blk, re.M):
        return False
    return not doc_has_assignment


def _strip_comment(line):
    """去掉 bash 行尾/整行注释 —— **只用于**"这个参数到底传没传"这类**可执行性**判据。

    ⚠️ 为什么必须去注释（变异验证当场抓到的洞）：align-sort 的块在调用**上方**有一段
    警示注释，里面逐字写着 `--prev-reads-file`。原先的判据是 `"--prev-reads-file" in blk`
    ——**整块子串匹配**，于是把**真正的** `--prev-reads-file "${PREV_QC}"` 那一行删掉、
    只留注释，判据**照样全绿**。那正是本文件要防的"纸面具备"：防线在看说明文字，
    而不是看接线本身。实测：变异①（删真参数、留注释）旧判据 232 passed 不变红。

    注意"#"出现在引号内不算注释（`echo "a # b"`）。此处按 bash 的常见写法做保守处理，
    够用即可 —— 误切只会让判据**更严**（更难通过），不会放过缺陷。
    """
    if line.lstrip().startswith("#"):
        return ""
    out, quote = [], None
    for ch in line:
        if quote:
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch == "#":
            break
        out.append(ch)
    return "".join(out)


def _exec_code_lines(blk):
    """块里**真正会被 shell 执行**的行（已去注释），供"参数传没传"这类判据使用。"""
    return "\n".join(_strip_comment(ln) for ln in blk.splitlines())


def _block_missing_stale_failed_cleanup(blk):
    """块**真的会写** `DONE_FILE` 却没有 `rm -f .../.failed/...` ⇒ 陈旧失败标记会残留。

    ⚠️ 判据必须是「**写入**」而不是「提到」：G05 的开工清理加了 `rm -f "${DONE_FILE}"`
    之后，早先那句 `and "DONE_FILE}" not in blk` 的宽松兜底把它当成了"写 .done 的块"
    ——于是**没有收尾写入**的块也被要求清 `.failed`（实测 3 处误报）。写 .done 的形态
    是**重定向进** DONE_FILE；`rm -f` 是删除，恰好相反。
    """
    writes = [ln for ln in (_strip_comment(l) for l in blk.splitlines())
              if re.search(r'>\s*"?\$\{?DONE_FILE', ln)]
    if not writes:
        return False
    if not re.search(r"\.done", blk):
        return False
    return not re.search(r"rm\s+-f\s+.*\.failed", blk)


def _doc_warn_only_mapped_rate(text):
    """把比对率写成 warn 级，却通篇没提 **hard** 的 `mapped_ratio` ⇒ F3 复发。"""
    if "mapped_rate" not in text and "比对率" not in text:
        return False
    mentions_warn_only = re.search(r"比对率[^\n]{0,20}≥\s*90%|mapped_rate[^\n]{0,30}warn", text)
    return bool(mentions_warn_only) and "mapped_ratio" not in text


# ==== 失效模式：pbrun 调用与它自己的参数契约相矛盾（F05 复发，2026-10-28） ====
# 与上面几条的区别：那些扫"对**读者**的断言"，这两条扫"对**工具**的断言" ——
# 判据是 `pbrun <tool>` 的**命令行本身**与 §4.1 速查表记的契约是否自相矛盾。
#
# 为什么按"写法"扫全仓：本条是 F05（Parabricks 三处文档错误）的复发面。修好之后
# `docs/test-evidence.md:651` 声称"文档侧由 test_defect_class_guards.py 的写法扫描覆盖"
# —— 实测 `grep -c num-gpus tests/test_defect_class_guards.py` = **0**，一句空头支票。
# 变异验证（把 `--num-gpus 1` 逐字加回 HEAD 模板模板）：577 passed → 577 passed，
# 整套用例**一条都不红**。即该缺陷可静默复发。这正是本文件要防的"纸面具备"，
# 只不过这次是**防线自己**在声称自己存在。
#
# 契约来源是 `parabricks-setup.md` §4.1 表 —— **推导**而非写死：将来表里新增一个
# 不接受某参数的工具，它的调用点自动进入射程。
PBRUN_SETUP_REL = "skills/wgs-expressive-pipeline/references/parabricks-setup.md"


def _pbrun_contract():
    """从 §4.1 速查表推导「谁接受 / 谁不接受 `--num-gpus`」与工具的必需要求。

    返回 `(rejected, accepted)`：选卡格写「不接受 `--num-gpus`」的进 rejected，
    写着 `--num-gpus` 的进 accepted。**两集合都非空**才算推出来了 —— 否则是本防线
    自己在空转（表格格式改了 / 表没了），必须当场报错而不是静默全绿。
    """
    path = os.path.join(REPO, PBRUN_SETUP_REL)
    assert os.path.isfile(path), f"§4.1 速查表不见了：{PBRUN_SETUP_REL}（防线在扫空集）"
    rejected, accepted = set(), set()
    for line in open(path, encoding="utf-8").read().splitlines():
        if not line.lstrip().startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 2:
            continue
        tool = cells[0].strip("`* ").strip()
        if not re.fullmatch(r"[a-z][a-z0-9_\-]*", tool):
            continue                      # 跳过 `|---|` 分隔行与表头
        if "不接受" in cells[1] and "--num-gpus" in cells[1]:
            rejected.add(tool)
        elif "--num-gpus" in cells[1]:
            accepted.add(tool)
    assert rejected, ("没从 §4.1 表里推出任何「不接受 --num-gpus」的工具 —— "
                      "表格改动或列序变了，本防线正在空转（这正是它要防的「纸面具备」）")
    assert accepted, "没推出任何「接受 --num-gpus」的工具 —— 表格改动，防线在空转"
    assert not (rejected & accepted), f"同一工具既接受又不接受：{rejected & accepted}"
    return rejected, accepted


def _pbrun_logical_lines(text):
    """把 bash 续行（行尾 `\\`）并成**逻辑行**，返回 `[(起始行号, 文本)]`。

    ⚠️ 必须并：`--num-gpus` 常常不在 `pbrun <tool>` 那一行上（模板里它排最后，
    中间隔着五六个参数与若干续行）—— 逐物理行看会**整片漏掉**。
    """
    out, buf, start = [], "", None
    for ln, line in enumerate(text.splitlines(), 1):
        if buf == "":
            start = ln
        if line.rstrip().endswith("\\"):
            buf += line.rstrip()[:-1] + " "
            continue
        out.append((start, buf + line))
        buf = ""
    if buf:
        out.append((start, buf))
    return out


def _pred_rejected_tool_takes_flag(raw, rejected):
    """`pbrun <不接受该参数的工具>` 的调用里**传了** `--num-gpus` ⇒ True。

    ⚠️ 射程的关键是"pbrun 之后**紧跟**该工具名"（中间只允许夹 GPU 索引 token）：
    只要求"同一逻辑行里同现"会把**告诫句**误伤 —— 实测
    「每个 pbrun 任务绑 1 张卡（`--num-gpus 1`；⚠️ 唯一例外是 `genotypegvcf`…）」
    同时含三个 token，却是在教读者**别**加它。而"紧跟"只匹配真正的调用形态。
    """
    if "--num-gpus" not in raw:
        return False
    m = re.search(r"\b(?:pbrun|PBRUN)\b\s+(?:\"?\$\{[^}]*\}\"?\s*)?([A-Za-z][\w\-]*)", raw)
    if not m or m.group(1).lower() not in rejected:
        return False
    # 引用错误用法/报错信息/纠正叙述的语境不算（它们**必须**能把错写法逐字写出来）
    if re.search(r"作废|推翻|已失效|旧版本|错的|早期|历史上|曾经|反例|错误写法"
                 r"|不要|切勿|勿|别|禁止|⚠|❌|✗"
                 r"|unrecognized arguments|rc\s*=\s*2|error:|不接受|无需|不需要|不必"
                 r"|不能|不得|例外|没有", raw):
        return False
    return True


_MD_ITEM = re.compile(r"^\s*(?:\d+\.\s|[-*+]\s|\||```)")
_MD_HEAD = re.compile(r"^#{1,6}\s")
# 该主张**自身**是告诫/纠正/复述报错 ⇒ 不是缺陷断言。
# ⚠️ 这个集合必须**紧**：第一版曾把 `⚠`/`没有`/`例外` 也收进来，变异验证当场打脸 ——
# params-propose 的 `--gpu` 告诫行紧跟在 `--num-gpus` 断言下面，一句无关的「⚠ 没有 --gpu
# 参数」就把整条主张**豁免**掉了（变异 2：删掉例外、全 25 条参数化用例仍绿）。
# 现在只收"明确在否定/订正**这件事**"的词，且必须是**主张窗口内**的。
_CORRECTIVE = re.compile(
    r"作废|推翻|已失效|旧版本|错的|早期|历史上|曾经|反例|错误写法"
    r"|不要|切勿|勿|别(?:按|把|用|加|写|当成|只|再|拿|靠|以|据|凭)|禁止"
    r"|unrecognized arguments|rc\s*=\s*2|error:|不接受")
_DIRECT = re.compile(
    r"用|使用|采用|加|带|传|给|绑|需|应|要|必须|均|皆|一律|统一|全部|所有"
    r"|\buses?\b|\bpass(?:es)?\b|\btakes?\b|\bneeds?\b|\badds?\b"
    r"|\balways\b|\bshould\b|\bmust\b|\bwith\b", re.I)


def _claim_window(lines, i, limit=8):
    """第 i 行的「本条主张」窗口 = 该行 + 后续**续行**（跨行的括号说明会被收进来）。

    ⚠️ 为什么不用**整段**（第一版就是这么写的，被变异当场证伪）：
    params-propose 的 1./2./3. 三条约定之间**没有空行**，整段是一段；其中第 3 条
    下面紧跟着一句无关的 `--gpu` 告诫（含 `⚠`）。按整段判时，那句告诫把整个段落
    **豁免**掉了 —— 删掉 `genotypegvcf` 例外后防线全绿。这正是本文件反复记的
    "一条标记豁免一整片"。改成按**主张**收窗：遇到新的 markdown 条目就断开。
    """
    out = [lines[i]]
    for j in range(i + 1, min(i + 1 + limit, len(lines))):
        nxt = lines[j]
        if not nxt.strip():
            break
        if _MD_ITEM.match(nxt):
            break
        if _MD_HEAD.match(nxt) and not lines[i].lstrip().startswith("#"):
            break                       # markdown 新标题（bash 注释行不算，见下）
        out.append(nxt)
    return "\n".join(out)


def _pred_blanket_flag_claim(lines, i, rejected, accepted):
    """第 i 行把 `--num-gpus` 讲成 pbrun 的**通例**，且整条主张都不给例外 ⇒ True。

    这是 F05 的**叙述式**复发形态，也是它当初实际扩散的方式：三处引导面
    （params-propose 的 wrapper 定义点、parabricks-setup 的 §4 约定、server-detect
    的核心原则）各自写了一句"容器内 pbrun 用 `--num-gpus 1`"，都不带例外。
    各阶段 skill 的 wrapper 注释逐字照抄同一句 —— 于是错写法跟着模板在仓里传，
    且**照抄的 LLM 会把它写进新生成的阶段脚本**。

    例外可以有两种给法，都算已讲清（在**主张窗口内**出现即可）：
      (a) 点名被拒工具（`genotypegvcf`）；或
      (b) 把主语限定到**接受**该参数的具体工具（`pbrun fq2bam`）——
          这样讲的是"该工具怎么调"，不是"pbrun 通例"。
    另外，主张本身是告诫/纠正/复述报错的（`_CORRECTIVE`），本就不是在断言。
    """
    line = lines[i]
    if "--num-gpus" not in line:
        return False
    if not _DIRECT.search(line):
        return False                    # 只是提到，不是给读者的指令
    win = _claim_window(lines, i)
    low = win.lower()
    if any(t in low for t in rejected):        # (a) 已点名例外工具
        return False
    if any(t in low for t in accepted):        # (b) 已限定到接受该参数的具体工具
        return False
    if _CORRECTIVE.search(win):                # (c) 本条主张是告诫/纠正/复述
        return False
    return True



# ======================= 防线：扫全仓 =======================

# ============ F15 残余防线：退化值必须在**门外**也被声明（2026-10-28） ============
# 这里扫的不是文档，是**表型插件源码**（同样自动发现，将来新增的插件自动进射程）。
#
# 要防的形态（第二轮审计 F15 残余，实测已复现）：面积比值型表型在「分子为 0」时
# 产出一个**落在合法值域内**的退化常数（IMF：`脂肪面积_px == 0` → `0.0%`）。
# 插件侧已在分子为 0 时 `raise PhenotypeError` —— 但那只管得住**本次运行**。
# 明细 CSV 一旦落盘，就能被独立复核、被搬运、被下游消费；`validate.py` 作为独立
# CLI 在 GWAS 入口复核一份**已存在**的 CSV 时，插件根本不参与，raise 管不到。
# 实测：修复前一份含 `脂肪面积_px=0`、值 0.0、状态 ok 的明细 CSV 混在 20 个健康
# 样本里走 CLI —— 6 条判据全 PASS、rc=0、零告警。
#
# **判据是推导出来的，不是写死的清单**（否则它只覆盖"我改过的那个插件"）：
#   对一个 `compute` 里出现的量 X，若同时满足
#     (a) X 被 `X <= 0`（或 `== 0`）形式的**守卫**判过并 raise，
#     (b) X **不是**任何除法的除数（⇒ 这个守卫判的是"退化"，
#         不是在防 ZeroDivisionError —— 后者 raise 也无妨，但不是本判据要的东西），
#     (c) X 被写进 `extra` 落盘（有列名），
#   则 X 就是「分子为 0 ⇒ 表型值为退化常数」这类量的签名，该插件**必须**在 TASK 的
#   `validate.degenerate_witness` 里声明它（或显式写 null 表明确无此形态）。
#   (b) 是关键：`meat_px <= 0` 是**分母**的守卫（防 ZeroDivisionError），不算 ——
#   真实跑一遍就能看出两者区别，故这里靠 AST 而非靠人名清单。


def all_phenotype_plugins():
    """自动发现全部表型插件源码（`phenotypes/*.py`，跳过 `_` 开头）。

    与 `all_skill_docs()` 同一约定：不写死清单，新增插件自动进射程。
    """
    pat = os.path.join(SKILLS, "phenotype-extract", "scripts", "phenotypes", "*.py")
    return {os.path.relpath(p, REPO): p for p in sorted(glob.glob(pat))
            if not os.path.basename(p).startswith("_")}


PLUGINS = all_phenotype_plugins()


def _plugin_zero_guarded_witnesses(src):
    """从插件源码推导出「必须声明却没声明」的量名。

    返回 ``[(量名, extra 列名)]`` —— 见上方判据说明。
    """
    tree = ast.parse(src)
    divisors, guards, extra_col = set(), set(), {}
    for n in ast.walk(tree):
        # 除数（`a / X` 与 `a / max(X, 1)` 都要认）
        if isinstance(n, ast.BinOp) and isinstance(n.op, ast.Div):
            for sub in ast.walk(n.right):
                if isinstance(sub, ast.Name):
                    divisors.add(sub.id)
        # `X <= 0` / `X == 0` 形式的守卫，体内 raise
        if isinstance(n, ast.If):
            body = ast.dump(ast.Module(body=n.body, type_ignores=[]))
            if "PhenotypeError" not in body:
                continue
            t = n.test
            for cmp_node in ast.walk(t):
                if not isinstance(cmp_node, ast.Compare):
                    continue
                if not isinstance(cmp_node.left, ast.Name):
                    continue
                if any(isinstance(o, (ast.LtE, ast.Eq)) for o in cmp_node.ops) and \
                        any(isinstance(c, ast.Constant) and c.value == 0
                            for c in cmp_node.comparators):
                    guards.add(cmp_node.left.id)
        # `extra = {...: <Name>}` → 量名 → 落盘列名
        if isinstance(n, ast.Assign) and any(
                getattr(t, "id", "") == "extra" for t in n.targets):
            if isinstance(n.value, ast.Dict):
                for k, v in zip(n.value.keys, n.value.values):
                    if isinstance(v, ast.Name) and isinstance(k, ast.Constant):
                        extra_col[v.id] = k.value
    # (a) 被守卫 + (b) 不是除数 + (c) 有落盘列名
    return [(x, extra_col[x]) for x in sorted(guards - divisors) if x in extra_col]


def _plugin_declares_witness(src, col):
    """插件的 TASK 里是否声明了见证列 `col`（显式 null 豁免**不算**覆盖它）。"""
    tree = ast.parse(src)
    for n in ast.walk(tree):
        if isinstance(n, ast.Assign) and any(
                getattr(t, "id", "") == "TASK" for t in n.targets):
            if not isinstance(n.value, ast.Dict):
                continue
            for k, v in zip(n.value.keys, n.value.values):
                if not (isinstance(k, ast.Constant) and k.value == "validate"):
                    continue
                if not isinstance(v, ast.Dict):
                    continue
                for kk, vv in zip(v.keys, v.values):
                    if isinstance(kk, ast.Constant) and kk.value == "degenerate_witness":
                        # 显式 null = 作者判定「确无此形态」，与本判据的推导冲突时
                        # 由 test_..._declares_... 报出来（不作为"已覆盖"）
                        if isinstance(vv, ast.Constant) and vv.value is None:
                            return False
                        return col in ast.dump(vv)
    return False


@pytest.mark.parametrize("rel", sorted(PLUGINS))
def test_zero_guarded_plugin_quantity_is_declared_as_gate_witness(rel):
    """**F15 残余防线（插件侧）**：分子为 0 ⇒ 退化常数的量，必须声明给门禁。

    推导见上方注释。反例（修复前的 imf.py）：`fat_px <= 0` 已 raise，但 TASK 只声明了
    `value_range` —— 插件的 raise 管不住"独立复核一份已落盘的明细 CSV"那条路径，
    于是 `0.0` 在门外畅通无阻。
    """
    src = open(PLUGINS[rel], encoding="utf-8").read()
    missing = [(q, col) for q, col in _plugin_zero_guarded_witnesses(src)
               if not _plugin_declares_witness(src, col)]
    if missing:
        detail = "、".join(f"`{q}`（落盘列 `{col}`）" for q, col in missing)
        pytest.fail(
            f"{rel}: 量 {detail} 被 `<= 0` 守卫判过并 raise，且写进了 extra 落盘 —— "
            f"即「该量为 0 时表型值是退化常数而非测量结果」。\n"
            f"  但 TASK 的 `validate` 里没有声明对应见证列。\n"
            f"  为什么必须声明：插件的 `raise` 只管得住**本次运行**；明细 CSV 一旦落盘，"
            f"就能被独立复核（`validate.py` 作为 CLI 在 GWAS 入口）、被搬运、被下游消费 ——"
            f"那条路径上插件不参与，raise 管不到，退化常数会伪装成正常值进入 GWAS。\n"
            f"  修法：在 TASK 里加 "
            f'`"degenerate_witness": {{"column": "{missing[0][1]}", "value": 0}}`；'
            f"确无此形态时**显式写 null**（不要删键 —— 删掉与「忘了声明」无从分辨）。")


def test_f15_derivation_is_not_idle():
    """**防空转**：推导必须在本仓库现存插件上真的推出东西来。

    ⚠️ 这条防的是本文件 docstring 点名的那个形态 —— 防线自己成为「纸面具备」。
    `test_zero_guarded_plugin_quantity_is_declared_as_gate_witness` 是对每条
    「推导结果」做断言的：若哪天 AST 推导被改坏（返回空列表），或插件把 extra
    的写法换掉，那条防线会在**整个仓库上全绿**，而它什么也没断言。
    故这里钉住"现存 imf.py 必须能推出 脂肪面积_px 这个量"。

    同理，插件集本身为空时（glob 路径变了）也要报出来。
    """
    assert PLUGINS, ("没发现任何表型插件 —— `phenotypes/*.py` 的 glob 路径变了，"
                     "本组防线正在扫空集")
    derived = {}
    for rel, path in sorted(PLUGINS.items()):
        got = _plugin_zero_guarded_witnesses(open(path, encoding="utf-8").read())
        if got:
            derived[rel] = got
    assert derived, (
        "从任何插件都推不出「分子为 0 ⇒ 退化常数」的量 —— AST 推导失效或插件写法变了，"
        "test_zero_guarded_plugin_quantity_is_declared_as_gate_witness 正在空转")
    imf = [k for k in derived if k.endswith("imf.py")]
    assert imf, f"imf.py 没推出任何量（它确有 fat_px 这种量）：{derived}"
    assert ("fat_px", "脂肪面积_px") in derived[imf[0]], (
        f"imf.py 的推导结果不含预期的 (fat_px, 脂肪面积_px)：{derived[imf[0]]}")


def test_f15_guard_actually_discriminates():
    """负控 + 正控：上面那条防线必须**真的会红**，且不误伤分母守卫。

    没有这组自检，一条写松的推导（例如漏掉「不是除数」这一条）会在整个仓库上全绿 ——
    正是本项目反复出现的「纸面具备」。
    """
    # 坏样本：修复前的真实形状（fat_px 守卫 + 落盘，TASK 只声明 value_range）
    bad = '''
def compute(ctx):
    fat_px = int((seg == 1)[roi].sum())
    if fat_px <= 0:
        raise PhenotypeError("分子为 0")
    ratio = 100.0 * fat_px / meat_px
    extra = {"脂肪面积_px": fat_px}
    return ratio, extra

TASK = {"validate": {"value_range": [0.0, 100.0]}}
'''
    got = _plugin_zero_guarded_witnesses(bad)
    assert got == [("fat_px", "脂肪面积_px")], f"推导失效，实得 {got}"
    assert not _plugin_declares_witness(bad, "脂肪面积_px"), "坏样本被误判为已声明"

    # 好样本 1：**分母**守卫（meat_px 是除数）不得被误伤 —— 它在防 ZeroDivisionError，
    # 真实形态就是 `ratio = 100.0 * fat_px / meat_px` 配 `meat_px <= 0` 的 raise。
    # 若判据漏掉「不是除数」这一条，本仓库现存代码会被整片误报。
    good_divisor = '''
def compute(ctx):
    meat_px = int(roi.sum())
    if meat_px <= 0:
        raise PhenotypeError("分母为 0")
    ratio = 100.0 * fat_px / meat_px
    extra = {"组织面积_px": meat_px}
    return ratio, extra

TASK = {"validate": {"value_range": [0.0, 100.0]}}
'''
    assert _plugin_zero_guarded_witnesses(good_divisor) == [], (
        f"分母守卫被误伤：{_plugin_zero_guarded_witnesses(good_divisor)}")

    # 好样本 2：声明了见证列 → 不算缺陷
    good_declared = bad.replace(
        '"value_range": [0.0, 100.0]',
        '"value_range": [0.0, 100.0], '
        '"degenerate_witness": {"column": "脂肪面积_px", "value": 0}')
    assert _plugin_declares_witness(good_declared, "脂肪面积_px"), \
        "已声明的插件被误判为未声明"

    # 好样本 3：被守卫的量**没有**落盘（不写进 extra）→ 不在射程内。
    # 理由：门禁只能读落盘的列；没落盘 = 门外无从判定，不该要求声明。
    good_unlogged = bad.replace('extra = {"脂肪面积_px": fat_px}',
                                'extra = {"其他": meat_px}')
    assert _plugin_zero_guarded_witnesses(good_unlogged) == [], \
        "未落盘的量不该进射程"

    # 好样本 4：**显式 null 豁免**不得被当成"已覆盖该列"。
    # 否则作者写一个 null 就能让防线闭嘴 —— 而 null 的语义恰恰是"确无此形态"，
    # 与推导出的"有这种量"直接矛盾，必须报出来让人核对。
    null_exempt = bad.replace('"value_range": [0.0, 100.0]',
                              '"value_range": [0.0, 100.0], "degenerate_witness": null')
    assert not _plugin_declares_witness(null_exempt, "脂肪面积_px"), \
        "显式 null 被误判为已声明见证列"


# ============ F4 复发防线：门禁判据与它的文档表必须对得上（2026-10-28） ============
# 这条补的是一张**空头支票**（与 §11.10 的 F05 同一形态，第二次）：
# `docs/test-evidence.md` 的 F4 行"判据落地"栏写着"`test_defect_class_guards.py`
# 扫表型 SKILL.md"—— 实测该文件当时**零处**提到表型（`grep -c "phenotype\|表型"` = 0）。
# 于是"判据表补了 sample_uniqueness"这件事只存在于**当时改过的那一行文本**里：
# 下一个新增的判据（或有人删掉表里某一行）都不会被发现。
#
# F4 的原形态是"hard 判据未进文档"（`sample_uniqueness` 在代码里是 hard，
# 判据表里根本没有它）—— 按**写法**复发时的形态同样是"某条判据没进表"。
# 故判据是**双向对账**，且两边都**推导**
# （不写死清单：判据名从 `validate.py` 的 `report.add(...)` 调用点 AST 抽取，
#   文档侧从 SKILL.md 判据表的第一列抽）：
#   · 代码里有、表里没有 → 读者不知道这条判据存在（F4 原形态）
#   · 表里有、代码里没有 → 读者以为有保护，实际没有（更坏）
PHENO_VALIDATE_REL = "skills/phenotype-extract/scripts/validate.py"
PHENO_SKILL_REL = "skills/phenotype-extract/SKILL.md"


def _gate_judge_names():
    """从 `validate.py` 抽出门禁登记的全部判据名（`report.add("<name>", ...)`）。"""
    path = os.path.join(REPO, PHENO_VALIDATE_REL)
    assert os.path.isfile(path), f"门禁源码不见了：{PHENO_VALIDATE_REL}（防线在扫空集）"
    tree = ast.parse(open(path, encoding="utf-8").read())
    names = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) \
                and n.func.attr == "add" and n.args:
            a0 = n.args[0]
            if isinstance(a0, ast.Constant) and isinstance(a0.value, str):
                names.add(a0.value)
    assert names, (f"没从 {PHENO_VALIDATE_REL} 抽出任何判据名 —— "
                   f"登记写法变了，本防线正在空转（这正是它要防的「纸面具备」）")
    return names


def _documented_judge_names():
    """从表型 SKILL.md 的**判据表**第一列抽出判据名。

    只在 markdown 表格行里找反引号包裹的全小写标识符，且必须在判据表那一节内
    （避免把正文里提到的判据名当成表里已有）。
    """
    text = open(os.path.join(REPO, PHENO_SKILL_REL), encoding="utf-8").read()
    lines = text.splitlines()
    start = None
    for i, ln in enumerate(lines):
        if ln.startswith("| 判据 |") or ln.startswith("| 判据|"):
            start = i
            break
    assert start is not None, (
        f"{PHENO_SKILL_REL} 里找不到判据表表头（`| 判据 |`）—— 表格被改名/删掉了，"
        f"本防线正在空转")
    names = set()
    for ln in lines[start + 1:]:
        if not ln.lstrip().startswith("|"):
            break                       # 表格结束
        cells = [c.strip() for c in ln.strip().strip("|").split("|")]
        if not cells:
            continue
        m = re.fullmatch(r"`([a-z][a-z0-9_]*)`", cells[0])
        if m:
            names.add(m.group(1))
    assert names, f"{PHENO_SKILL_REL} 的判据表一行都没解析出来 —— 本防线在空转"
    return names


def test_gate_judges_and_documented_table_agree():
    """**F4 复发防线**：`validate.py` 登记的判据 ↔ SKILL.md 判据表，双向对账。

    两边都是**推导**的（判据名从源码 AST 抽、文档侧从表格第一列抽），
    故新增一条判据却忘了写进表、或表里留着一个已删掉的判据名，都会被当场报出来。
    """
    code = _gate_judge_names()
    doc = _documented_judge_names()
    missing_in_doc = sorted(code - doc)
    missing_in_code = sorted(doc - code)
    msg = []
    if missing_in_doc:
        msg.append(f"**代码里有、判据表里没有**（读者不知道这条判据存在，F4 原形态）："
                   f"{missing_in_doc}")
    if missing_in_code:
        msg.append(f"**判据表里有、代码里没有**（读者以为有保护，实际没有 —— 更坏）："
                   f"{missing_in_code}")
    assert not msg, (f"{PHENO_VALIDATE_REL} 与 {PHENO_SKILL_REL} 的判据清单不一致：\n  "
                     + "\n  ".join(msg))


def test_judge_table_guard_actually_discriminates():
    """负控 + 正控：对账判据必须真的能发现两侧各自的不一致。"""
    code = _gate_judge_names()
    doc = _documented_judge_names()
    # 现状必须是一致的（否则上一条已经红了）
    assert code == doc, f"code={sorted(code)} doc={sorted(doc)}"
    # 负控：模拟"代码新增一条判据但没进表"
    assert (code | {"新判据"}) - doc == {"新判据"}
    # 负控：模拟"表里留着一条已删掉的判据"
    assert doc - (code - {"value_range"}) == {"value_range"}
    # 正控：两边的推导结果都必须非空（否则对账是"空 vs 空"，恒真）
    assert len(code) >= 6, f"判据名抽得太少（{len(code)} 个），推导可能失效：{sorted(code)}"
    assert len(doc) >= 6, f"文档侧判据名抽得太少（{len(doc)} 个）：{sorted(doc)}"


# ====== 台账自检：点名的测试/谓词必须真的存在（2026-10-28，§11.10 立规、§11.11 执行）======
# §11.10 立了规矩"台账里每句『由 X 覆盖』都要能当场 `grep` 出来"。§11.11 手工执行一次，
# 抓到**一例**（F4 行的"由 test_defect_class_guards.py 扫表型 SKILL.md"，实测命中 0）。
# §11.9 又抓到一例：台账点名的 `test_stage1_gate_passes_prev_reads_file` **从未存在过**
# （`git log --all -S` 无命中）—— 实际承担该职责的是
# `test_no_order_proven_by_header_or_index`。
# 规律：**手工执行一次而没做成自动检查的规矩，下一批就失效** —— 那正是本项目
# 反复出现的"纸面具备"在**规矩**层面的形态。故这里把它做成可执行的防线。
#
# 判据：台账里出现的每个 `test_*` / `_pred_*` / `_block_*` 标识符，
# 都必须能在**别的文件**里 grep 到（台账自己不算 —— 否则写成"某测试存在"就自证了）。
# ⚠️ 必须按**主张窗口**判定，不能逐行：订正叙述的第一行往往只写名字、
#    把"已作废/从未存在"放在后续行（§11.9 那条订正正是这个形状）。
LEDGERS = ["docs/test-evidence.md", "docs/test-matrix.md"]
_IDENT = re.compile(r"\b((?:test|_pred|_block)_[a-z0-9_]+)\b")
# 订正/追溯语境的标记：本条主张是在**订正**一个旧名字，不是在声称它存在。
# 另收「本处没有测试」类语境：缺口表里写 `起 flask test_client，GET /api/config`
# 是在**描述一个待写的测试**（同行明说"无任何用例"），不是在声称 `test_client` 存在
# —— 而那 `test_client` 其实是 Flask 的测试夹具名，本就不是本仓的测试。
_RETRACTED = re.compile(
    r"从未|已订正|已作废|作废|订正|改名|更名|原名|旧名|此前误写|笔误|不存在"
    r"|MISS|找不到|grep|空头支票"
    r"|无任何用例|未测|未覆盖|缺口|待补|尚无")


def _repo_texts(exclude):
    """读全仓文本（跳过 VCS/缓存目录与指定的排除文件）。"""
    out = {}
    for root, dirs, files in os.walk(REPO):
        dirs[:] = [d for d in dirs
                   if d not in (".git", "__pycache__", ".pytest_cache", "node_modules")]
        for fn in files:
            if not fn.endswith((".py", ".md", ".sh", ".json")):
                continue
            p = os.path.join(root, fn)
            rel = os.path.relpath(p, REPO)
            if rel in exclude:
                continue
            try:
                out[rel] = open(p, encoding="utf-8", errors="replace").read()
            except OSError:
                pass
    return out


def _ledger_named_idents():
    """从两份台账抽出点名的标识符 → {名字: [(文件, 行号)…]}（跳过订正语境的条目）。

    ⚠️ 两类要分开，混在一起会整片误报（第一版就是这么写的，实测 10 条全是误伤）：
      · **文件名引用** `tests/test_xxx.py` / `test_xxx.py` —— 判"文件在不在"
        （`tests/test_compaction.py` 的文件内容里**不含** `test_compaction` 字样，
         按函数名去 grep 必然找不到）
      · **函数/谓词名** `test_xxx` / `_pred_xxx` / `_block_xxx`（无 `.py` 后缀）——
        判"能不能在别的文件里 grep 到"
    """
    named = {}
    for rel in LEDGERS:
        path = os.path.join(REPO, rel)
        assert os.path.isfile(path), f"能力台账不见了：{rel}（本防线在扫空集）"
        lines = open(path, encoding="utf-8").read().splitlines()
        for i, line in enumerate(lines):
            if not _IDENT.search(line):
                continue
            # 主张窗口：本行 + 后续续行（空行 / 新条目 / 新标题即断开）
            win = [line]
            for j in range(i + 1, min(i + 9, len(lines))):
                nxt = lines[j]
                if not nxt.strip() or _MD_ITEM.match(nxt) or _MD_HEAD.match(nxt):
                    break
                win.append(nxt)
            if _RETRACTED.search("\n".join(win)):
                continue          # 本条是在**订正**旧名字，不是声称它存在
            for m in _IDENT.finditer(line):
                name = m.group(1)
                # 三类分开判，混在一起会整片误报（第一版实测 10 条全是误伤）：
                #   · `tests/test_xxx.py` / `test_xxx.py`  → 文件引用（判文件在不在）
                #   · `test_xxx` 且仓里**真有** `tests/test_xxx.py`
                #     → 也是文件引用（台账里常省略 `.py` 后缀，如 "4 条 test_config_json
                #       pytest"）。判据取"存在性优先"：文件在就不按函数名去 grep。
                #   · 其余 → 函数/谓词名（判能不能 grep 到）
                tail = line[m.end():m.end() + 3]
                is_file = tail.startswith(".py") or os.path.isfile(
                    os.path.join(REPO, "tests", name + ".py"))
                named.setdefault((name, is_file), []).append((rel, i + 1))
    return named


def _ledger_named_files():
    """从台账抽出点名的测试**文件名** → 是否存在。"""
    missing = []
    for (name, is_file), where in _ledger_named_idents().items():
        if not is_file:
            continue
        if not os.path.isfile(os.path.join(REPO, "tests", name + ".py")):
            missing.append((name + ".py", where))
    return missing


def test_ledger_named_tests_actually_exist():
    """**空头支票防线**：台账点名的每个测试/谓词都必须能在仓里 grep 到。

    §11.10/§11.11/§11.9 手工抓到三例（两次"由 X 覆盖"是假的、一次点名的测试名
    从未存在过）。本防线把那条规矩变成**自动发现**：下一个人写台账时点了不存在的
    名字、或改名后忘了同步台账，当场报出来。
    """
    named = _ledger_named_idents()
    funcs = {k[0] for k in named if not k[1]}
    files = {k[0] for k in named if k[1]}
    assert len(funcs) >= 20, (
        f"只从台账抽出 {len(funcs)} 个函数/谓词名 —— 台账格式变了或抽取失效，"
        f"本防线正在空转（这正是它要防的「纸面具备」）")
    repo = _repo_texts(exclude=set(LEDGERS))
    missing = {k: v for k, v in named.items()
               if not k[1] and not any(k[0] in txt for txt in repo.values())}
    missing_files = _ledger_named_files()
    msg = []
    if missing:
        msg.append("台账点名了仓里不存在的测试/谓词（『由 X 覆盖』的空头支票）：\n  "
                   + "\n  ".join(f"{k[0]}  ← "
                                 f"{', '.join(f'{r}:{l}' for r, l in v[:3])}"
                                 for k, v in sorted(missing.items())))
    if missing_files:
        msg.append("台账点名了不存在的测试文件：\n  "
                   + "\n  ".join(f"tests/{n}  ← "
                                 f"{', '.join(f'{r}:{l}' for r, l in v[:3])}"
                                 for n, v in sorted(missing_files)))
    if msg:
        msg.append("修法：改成真实存在的名字，或（若是在**订正**旧名字）在同一条主张里"
                   "加上「已订正 / 从未 / 原名」等标记。")
    assert not msg, "\n".join(msg)


def test_ledger_guard_actually_discriminates():
    """负控 + 正控：台账防线必须真的能发现"点名了不存在的测试"。"""
    # 负控：一个不可能存在的名字，仓里必然 grep 不到。
    # ⚠️ 名字必须**运行时拼出来** —— 直接写字面量时，本文件自己就含该字符串，
    #    于是"仓里找不到"恒假（第一版踩到：负控自己让负控失败，可见 `assert not True`）。
    phantom = "test_phantom_" + "never_defined_anywhere_xyz"
    repo = _repo_texts(exclude=set(LEDGERS))
    assert not any(phantom in txt for txt in repo.values()), \
        "负控用的名字居然存在于仓里 —— 换个更不可能的名字"
    # 正控：真实存在的名字必须能被找到
    assert any("test_guards_actually_discriminate" in txt for txt in repo.values())
    assert any("_pred_bare_tabix" in txt for txt in repo.values())
    # 正控：抽取器本身在工作（抽得出足够多的名字，两类都抽到）
    named = _ledger_named_idents()
    funcs = {k[0] for k in named if not k[1]}
    files = {k[0] for k in named if k[1]}
    assert len(funcs) >= 20, f"函数名抽得太少：{len(funcs)}"
    assert len(files) >= 3, f"文件名抽得太少：{sorted(files)}"
    # 订正语境必须被豁免（否则 §11.9 那条订正会被当成空头支票反复报出来）
    assert _RETRACTED.search("⚠️ **本句点名的测试名有误，已订正（2026-10-29）**")
    assert _RETRACTED.search(f"`{phantom}` **从未在仓库任何版本存在过**")


@pytest.mark.parametrize("rel", sorted(DOCS))
def test_no_bare_tabix_without_force(rel):
    """**A7 复发防线**：不得出现裸 `tabix -p vcf`（无 `-f`）的**可执行命令**。

    GATK walker 的 `--create-output-variant-index` 默认 true，索引早已存在 →
    裸 tabix **rc=1**（`the index file exists`），`set -euo pipefail` 下当场退出，
    健康数据既不写 `.done` 也不写 `.failed`，编排层误判"已中断"。

    允许出现的地方是**告诫读者不要这么写**的注释/表格（含"不要/勿/别/⚠"等）。
    """
    text = open(DOCS[rel], encoding="utf-8").read()
    for ln, line in enumerate(text.splitlines(), 1):
        if _pred_bare_tabix(line):
            pytest.fail(
                f"{rel}:{ln} 有裸 `tabix -p vcf`（无 -f）—— 索引已存在时 rc=1，"
                f"在 set -e 下会杀死脚本、够不到门禁。\n  {line.strip()[:160]}\n"
                f"确需重建请用 `tabix -f -p vcf`；若只是告诫读者不要写，加「不要/勿」等字样。")


@pytest.mark.parametrize("rel", sorted(DOCS))
def test_no_parabricks_readgroup_auto_claim(rel):
    """**A2 复发防线**：不得再写"Parabricks 会自动生成读组、且符合 SM"这类结论。

    实测（clara-parabricks:4.3.2-1 容器内真跑）：不传 `--read-group-sm` 时 fq2bam
    生成的是**写死的常量读组** `SM:sample` —— 与真实样本号无关，每个样本都是 `sample`。
    门禁 `readgroup`(hard) 于是拦下**每一个**样本，而 parabricks 是 GPU 就绪时的默认后端。

    已实测踩到**四处**（这就是为什么本防线按"写法"扫全仓，而不是逐个文件改）：
    align-sort 的 SKILL.md 正文、错误预防表、与 CPU 路径的差异对照表、`usage-guide.md`。

    判据精确到"断言读组会被自动配好"，而非任何提到 read group 的行 ——
    正确写法（"自动生成的是常量 `SM:sample`，必须显式传"）不得被误伤。
    """
    text = open(DOCS[rel], encoding="utf-8").read()
    for ln, line in enumerate(text.splitlines(), 1):
        if _pred_readgroup_auto_claim(line):
            pytest.fail(
                f"{rel}:{ln} 仍称读组会被「自动配好且符合 SM」—— 实测生成的是常量 "
                f"`SM:sample`，会让默认后端每样本被拦。\n  {line.strip()[:160]}\n"
                f"应写明：必须显式传 --read-group-sm。")


@pytest.mark.parametrize("rel", sorted(DOCS))
def test_no_rejected_pbrun_tool_is_passed_the_flag(rel):
    """**F05 复发防线（命令式）**：不得给「不接受 `--num-gpus`」的 pbrun 子命令传该参数。

    实测（clara-parabricks:4.3.2-1 真容器）：`pbrun genotypegvcf --num-gpus 1` 当场
    `rc=2`、`pbrun: error: unrecognized arguments: --num-gpus 1`、`out/` **零产物**；
    去掉该参数后同一输入 `rc=0` 并产出 VCF + `.tbi`。同一容器里
    `pbrun {fq2bam,haplotypecaller,markdup,bqsr,applybqsr} -h` 的 Run Options
    **每一个都有** `--num-gpus`（默认 1），只有 `genotypegvcf` 没有。

    危害不在"报错"，而在**报错被吞掉之后的两种表现**：若模板做了失败累计，
    脚本继续走到门禁、报 `[FAIL] joint.exists`（留痕但误导方向）；若照抄裸调用，
    `set -euo pipefail` 下当场死在 pbrun 那一行，磁盘上 `.done`/`.failed` **都不存在**，
    编排层四态判定归为"已中断"，把用户引去查一个不存在的进程。

    **作用域自动发现**：被拒工具集从 `parabricks-setup.md` §4.1 表推导，
    将来表里新增一个不接受该参数的工具，它的调用点自动进入射程。
    """
    rejected, _ = _pbrun_contract()
    text = open(DOCS[rel], encoding="utf-8").read()
    for ln, raw in _pbrun_logical_lines(text):
        if _pred_rejected_tool_takes_flag(raw, rejected):
            pytest.fail(
                f"{rel}:{ln} 给「不接受 `--num-gpus`」的 pbrun 子命令传了该参数 —— "
                f"实测 rc=2 `unrecognized arguments` 且零产物。\n  {raw.strip()[:160]}\n"
                f"该参数由 §4.1 表推出对 {'/'.join(sorted(rejected))} 不适用；"
                f"删掉它即可（其余兄弟工具都接受）。")


@pytest.mark.parametrize("rel", sorted(DOCS))
def test_pbrun_flag_claims_name_the_exception(rel):
    """**F05 复发防线（叙述式）**：把 `--num-gpus` 讲成 pbrun **通例**时，必须同时给出例外。

    这是该缺陷当初**实际扩散**的方式：三处引导面（params-propose 的 wrapper 定义点、
    parabricks-setup §4 的约定、server-detect 的核心原则）各写一句
    "容器内 pbrun 用 `--num-gpus 1`"、都不带例外；各阶段 skill 的头部注释再逐字照抄 ——
    错写法跟着模板在仓里传，且**照抄的 LLM 会把它写进新生成的阶段脚本**。

    ⚠️ 只扫**跨工具的**文档：单工具阶段 skill 里的 "pbrun 用 --num-gpus 1" 受
    本 skill 的工具限定（如 align-sort 只讲 fq2bam），不是"通例"。
    但它们的头部注释是**逐字复制的样板**，已统一带上例外 —— 那是照抄面，必须一致。

    ⚠️ 判据建在**单条主张**的窗口上，不是整段：params-propose 的三条约定之间没有空行，
    整段是一段，其中紧跟着一句无关的 `--gpu` 告诫；按整段判时那句告诫会把整片豁免掉
    （变异验证实测：删掉例外后 25 条参数化用例仍全绿）。

    允许出现：告诫/纠正语境，或把主语限定到**接受**该参数的具体工具。
    见 `test_guards_actually_discriminate` 里本条谓词的负控与正控。
    """
    rejected, accepted = _pbrun_contract()
    lines = open(DOCS[rel], encoding="utf-8").read().splitlines()
    for i, line in enumerate(lines):
        if _pred_blanket_flag_claim(lines, i, rejected, accepted):
            pytest.fail(
                f"{rel}:{i + 1} 把 `--num-gpus` 讲成 pbrun 通例，却没给例外 —— "
                f"`{'/'.join(sorted(rejected))}` 不接受该参数（实测 rc=2、零产物），"
                f"照抄生成的阶段脚本会死在那一行，且 `.done`/`.failed` 都不写。\n"
                f"  {line.strip()[:170]}\n"
                f"加法：补一句「⚠️ 唯一例外：`genotypegvcf` 不接受该参数」，"
                f"或把主语限定到具体工具（`pbrun fq2bam …`）。")


@pytest.mark.parametrize("rel", sorted(DOCS))
def test_no_hardcoded_conda_paths(rel):
    """**硬规则 #5.1**：工具用 `command -v` 运行时探测，禁止写死本机 conda 绝对路径。

    复发点实测：`usage-guide.md` 曾写「Use conda absolute paths for all tools」，
    与 SKILL.md 和硬规则直接冲突 —— 照抄生成的脚本换机即失效。
    """
    text = open(DOCS[rel], encoding="utf-8").read()
    for ln, line in enumerate(text.splitlines(), 1):
        if _pred_hardcoded_conda_path(line):
            pytest.fail(f"{rel}:{ln} 写死了/建议使用 conda 绝对路径（违反硬规则 #5.1）："
                        f"\n  {line.strip()[:160]}")


@pytest.mark.parametrize("rel", sorted(DOCS))
def test_every_verifier_call_has_samples_csv_defined(rel):
    """**A5 复发防线**：凡调用 `${VERIFIER} ... --samples "${SAMPLES_CSV}"` 的代码块，
    该块（或同文档别处）必须先有 `SAMPLES_CSV="$(python ...` 的赋值。

    实测复发点：编排层 §2.2 是"完整模板见编排层 SKILL.md"指定的权威版本，
    却漏了六个阶段 skill 都有的这段 —— LLM 照抄即 `SAMPLES_CSV: unbound variable`，
    `set -u` 下当场退出，磁盘上**两个标记都没有**，编排层归为"已中断"。
    """
    text = open(DOCS[rel], encoding="utf-8").read()
    has_assign_anywhere = bool(re.search(r'^\s*SAMPLES_CSV="\$\(', text, re.M))
    for blk in _bash_fences(text):
        if _block_missing_samples_csv(blk, has_assign_anywhere):
            pytest.fail(
                f"{rel}: 有用到 ${{SAMPLES_CSV}} 的 bash 代码块，但全文档都没有它的赋值 —— "
                f"照抄生成的脚本在 set -u 下会 `SAMPLES_CSV: unbound variable` 退出，"
                f"磁盘上既无 .done 也无 .failed。\n块开头：{blk.strip().splitlines()[0][:120]}")


@pytest.mark.parametrize("rel", sorted(DOCS))
def test_every_success_path_clears_stale_failed(rel):
    """**陈旧标记防线**：凡**走结果门禁**的收尾模板，写 `.done` 时必须 `rm -f` 该阶段的 `.failed`。

    不清的话，重跑通过后 `.done` 与 `.failed` 并存，而编排层四态判定**优先读 `.failed`**
    → 已修好并通过的阶段被报成"校验未通过"（实测复现）。

    **作用域**：只覆盖"四态 + 门禁"这条链上的文档（文档里出现 `.failed` / `.verify` /
    门禁字样）。`gwas-*` 是**另一条流水线**：它不读 `.failed`、不跑 `stage_verify.py`、
    只写 `.done`（实测 7 个 gwas skill 的 `.failed` 出现次数均为 0）。对它们套用本条
    判据是**前提错配**而非发现缺陷 —— 那套流水线缺的是"整个门禁"，属于另一件事
    （已记入 docs/test-evidence.md 的已知边界），不该由这条防线伪装成"已覆盖"。
    """
    text = open(DOCS[rel], encoding="utf-8").read()
    if not re.search(r"\.failed|stage_verify|VERIFIER|四态", text):
        return                       # 非门禁链上的文档，见 docstring 的作用域说明
    for blk in _bash_fences(text):
        if _block_missing_stale_failed_cleanup(blk):
            pytest.fail(
                f"{rel}: 有写 ${DONE_FILE} 的收尾块，但没有 `rm -f .../.failed/<stage>.failed` —— "
                f"重跑通过后陈旧 .failed 仍在，四态判定优先读它，会把已通过的阶段报成未通过。")


@pytest.mark.parametrize("rel", sorted(DOCS))
def test_no_idxstats_recommended_as_judgment_source(rel):
    """**根因 A 复发防线**：不得再**推荐**用 `samtools idxstats` 去取得/判定 reads 数。

    为什么按"写法"扫全仓：本缺陷已在代码里出现过**五轮**（F03/F18/F25/F27 + 第四轮
    的 `read_count`），每次都是"这一处改了、下一处还在"。文档是 LLM 生成阶段脚本的
    模板 —— 文档里推荐 idxstats，新生成的脚本就会照用，代码侧的修复一次也管不到它。

    允许出现：告诫语境（"不要/⚠/而非/只作旁证/派生数据"等）。见
    `test_guards_actually_discriminate` 里本条谓词的负控与正控。
    """
    text = open(DOCS[rel], encoding="utf-8").read()
    for ln, line in enumerate(text.splitlines(), 1):
        if _pred_idxstats_as_judgment_source(line):
            pytest.fail(
                f"{rel}:{ln} 推荐用 `samtools idxstats` 取得/判定 reads 数 —— "
                f"它读的是**索引**（派生数据），三种形态都会撒谎：陈旧（重跑/--resume "
                f"跳过 index → 空 BAM 报出上一轮条数）、外来（不打 older than 警告）、"
                f"漏计（GPU 后端 .bai 系统性少计）。应用 `samtools view -c [-F 4/-f 4]` "
                f"读 BAM 本体；若本行是告诫读者不要这么写，请加「不要/⚠/只作旁证」等字样。\n"
                f"  {line.strip()[:160]}")


@pytest.mark.parametrize("rel", sorted(DOCS))
def test_mapped_ratio_not_documented_as_warn_only(rel):
    """**F3 复发防线**：提到 `mapped_rate`/比对率告警的文档，不得让读者以为映射侧只有 warn。

    代码里有**两条**：`mapped_ratio`(hard，全 unmapped 即拦) 与 `mapped_rate`(warn，≥90%)。
    早期编排层表只写「比对率是否 ≥90%」为 warn，读者据此以为"比对率低只是告警"，
    实际整个阶段被判失败；反向地，排查时按 warn 的定位又找不到这条硬失败。
    """
    text = open(DOCS[rel], encoding="utf-8").read()
    if _doc_warn_only_mapped_rate(text):
        pytest.fail(
            f"{rel}: 把比对率写成 warn 级（≥90%），却没提 **hard** 的 `mapped_ratio` "
            f"（全 unmapped = 0 即拦）—— 读者会以为低比对率只收到一行告警，"
            f"实际阶段被判失败停止推进。")


# ==== 失效模式：状态判序让陈旧标记盖过真实状态（G05，2026-10-29） ====
# 缺陷形态：**同一个状态的两个来源各说各话**，而判序让**陈旧的那个**先说话。
#   · 陈旧 `.done`（上一轮成功留下）+ 本轮存活 pid 并存 → 报"已完成"，
#     用户据此推进下一阶段，而上一阶段还在写同一个产物。
#   · 实测（真 bash，脚本逐行取自场景 B 循环 + `nohup sleep`）：
#       有陈旧 .done → `✅ 01_bwa_sort 已完成`；去掉 .done → `🕐 运行中`。
#   · 与 0.3.x 已修的"陈旧 .failed"是**镜像缺陷**：那条管"成功后要清 .failed"，
#     这条管"开工要清 .done" + "判序要 pid 优先"。
#
# 判据从**源码推导**，不写死行号/字面量：先在文档里找到"四态判定"的判序块
# （同时含 `.failed`、`.done`、`kill -0` 三者的 bash 块），再断言 .done 分支的
# 位置**不早于** pid 存活分支。文档改写措辞不会让防线失效，但**把顺序改回去**必然红。

_PID_ALIVE_LINE = re.compile(
    r'\[\s*-[fs]\s+"?\$\{?(?:pid_file|PID_FILE)'      # [ -f "${pid_file}" ]
    r'[^\]]*\]\s*&&\s*kill\s+-0', re.I)

# `.done` 的**判定**行：测试条件里出现 .done 的文件名。两种写法都要认 ——
# 字面量 `"${RUN_DIR}/.done/x.done"`（编排层 SKILL.md）与变量 `"${done_file}"`（workflow.md）。
# ⚠️ 只认字面量是**变异验证当场抓到的洞**：把 workflow.md 的 elif 链换回旧判序，
# 判据照样全绿 —— 因为该块用的是 `${done_file}`。反空转断言只数了"有几个四态块"，
# 而**块内**的分支位置根本没被识别（正是本文件要防的"看着在守、其实什么也没守"）。
_DONE_TEST_LINE = re.compile(
    r'\[\s*-[fs]\s+"?(?:[^\]]*\.done|\$\{?done_file\b|\$done_file\b)')


# `.failed` 的**判定**行：测试条件里出现 .failed 的文件名。与 _DONE_TEST_LINE 对称。
# ⚠️ 必须要求**真的在测这个标记文件**（`[ -f ... .failed ]` / `[ -f "${failed_file}" ]`），
# 不能只匹配字面的 "failed" —— 并发池块里有 `FAILED=0` 这样的累加器变量、
# 注释里也常提 `.failed`，只按词匹配会把两个**样本池**块误当成四态判定块
# （反空转断言当场抓到：4 个块里 2 个是误报）。
_FAILED_TEST_LINE = re.compile(
    r'\[\s*-[fs]\s+"?(?:[^\]]*\.failed|\$\{?failed_file\b|\$failed_file\b)')


def _four_state_order_blocks(text):
    """文档里所有"四态判定"判序块：同时判 `.failed`、`.done`、pid 存活（`kill -0`）。

    ⚠️ 两处识别都要广：`.done` 同时认字面量与 `"${done_file}"`（只认字面量会把
    workflow.md 的 `elif` 链漏掉），`.failed` 必须要求是**标记文件的测试**而不是
    同名词（否则并发池块会混进来）。这两点都是变异验证/反空转断言当场抓到的。
    """
    out = []
    for blk in _bash_fences(text):
        if "kill -0" not in blk:
            continue
        if (not re.search(r"\.done|\bdone_file\b", blk)
                or not _FAILED_TEST_LINE.search(blk)):
            continue
        out.append(blk)
    return out


def _done_branch_precedes_pid_alive(blk):
    """块里 `.done` 判定分支出现在 pid 存活判定**之前** ⇒ 陈旧 .done 会盖过"运行中"。

    只比**有序分支**的位置（`if`/`elif` 行；`continue` 式循环已一并覆盖，因为
    `echo ... 已完成` 与 `kill -0` 的相对次序同样是判序）。注释与说明文字不算 ——
    真正的风险是**执行**顺序，不是文档里提到顺序的先后。

    没有 pid 分支的块返回 False（它压根没做"运行中"这一态的判定 —— 那是**另一条**
    缺陷，不该由本条判据伪装成"已覆盖"）。
    """
    lines = [_strip_comment(ln) for ln in blk.splitlines()]
    pid_ln = done_ln = None
    for i, ln in enumerate(lines):
        if pid_ln is None and _PID_ALIVE_LINE.search(ln):
            pid_ln = i
        if (done_ln is None and pid_ln is None
                and _DONE_TEST_LINE.search(ln)
                and "kill -0" not in ln):
            done_ln = i
    if pid_ln is None or done_ln is None:
        return False
    return done_ln < pid_ln


def _block_missing_stale_done_clear(blk):
    """阶段脚本**前导段**没有开工清陈旧 `.done` ⇒ 陈旧标记会与存活进程并存。

    判据是块里有没有 `DONE_FILE="..."` 的**赋值** **且** 写 pid（`echo $$ >`）——
    那正是"阶段脚本前导段"的签名（配置 + mkdir + 写 pid + 收尾都在一块里）。
    两类块**不该**命中：
      · 收尾片段（只有 `date > "${DONE_FILE}"`、无赋值）→ 不负责开工清理；
      · 编排层 §2.2 的块——它是**收尾模板**（开头即写 `# ─── 阶段收尾 ───`），
        在里面清 .done 发生在所有工作**之后**，挡不住运行期间的竞态窗口。
        那正是本缺陷要防的"纸面具备"：看起来模板修了，实际窗口没变。
    """
    if not re.search(r'^\s*DONE_FILE="', blk, re.M):
        return False
    if not re.search(r"echo\s+\$\$\s*>", blk):
        return False
    if not re.search(r"\.done", blk):
        return False
    return not re.search(r'^\s*rm\s+-f\s+"?\$\{?DONE_FILE', blk, re.M)


@pytest.mark.parametrize("rel", sorted(DOCS))
def test_four_state_verdict_checks_pid_alive_before_done(rel):
    """**G05 复发防线（判序半边）**：四态判定块里 pid 存活必须早于 `.done`。

    陈旧 `.done` 与本轮存活进程并存是**重跑一个曾成功过的阶段**的常态。`.done`
    先判时，正跑着的阶段被报成"已完成"，用户据此推进下一阶段 —— 而上一阶段还在
    写同一个产物，下游读到的是半成品。
    """
    text = open(DOCS[rel], encoding="utf-8").read()
    blocks = _four_state_order_blocks(text)
    for blk in blocks:
        if _done_branch_precedes_pid_alive(blk):
            pytest.fail(
                f"{rel}: 四态判定块里 `.done` 分支排在 pid 存活（`kill -0`）**之前** —— "
                f"重跑阶段时陈旧 .done 与存活进程并存，正跑着的阶段会被报成「已完成」，"
                f"用户据此推进下一阶段。判序恒为 `.failed` → pid 存活 → `.done`。\n"
                f"块开头：{blk.strip().splitlines()[0][:120]}")


@pytest.mark.parametrize("rel", sorted(DOCS))
def test_stage_template_clears_stale_done_at_start(rel):
    """**G05 复发防线（开工清理半边）**：阶段脚本样板写 pid 之前必须清掉自己的 `.done`。

    与已有防线 `test_every_success_path_clears_stale_failed` 是**同一个设计的两半**：
    成功路径清 `.failed`、开工清 `.done`。只做一半，磁盘上就会同时存在两个互相矛盾的
    状态来源，而判序那一半（上一用例）是给**旧版本生成的脚本**兜底的 —— 新样板两半都要有。
    """
    text = open(DOCS[rel], encoding="utf-8").read()
    for blk in _bash_fences(text):
        if _block_missing_stale_done_clear(blk):
            pytest.fail(
                f"{rel}: 阶段脚本样板给 `${DONE_FILE}` 赋了值，"
                f"却没有开工清陈旧 `.done`（`rm -f \"${{DONE_FILE}}\"`）—— "
                f"重跑一个曾成功过的阶段时，上一轮的 .done 与本轮存活的 pid 并存，"
                f"四态判定会把正在跑的阶段报成「已完成」（实测复现）。\n"
                f"块开头：{blk.strip().splitlines()[0][:120]}")


_UNGLOBBED_BARE_GLOB = re.compile(
    r'^\s*for\s+\w+\s+in\s+"?\$\{?\w+\}?"?/[^\s"]*\*\.[a-z]', re.M)


def _block_globs_without_empty_guard(blk):
    """块里用 `for f in "${DIR}"/X*y` 遍历产物，却没有任何"无匹配就跳过"的防护 ⇒ True。

    空目录下 bash **不展开** glob（全仓未设 `nullglob`），字面量 `*.done` 会原样
    进入循环体：gwas 场景 B 实测输出一行 `✅ *.done`，读者据此以为"有个阶段完成了"。

    认三种防护（任一即可）：`[ -e "$f" ]` / `[ -f "$f" ]` 判存在后 `continue`；
    `nullglob`；`compgen -G`。

    ⚠️ 判据只看**会执行的行**（`_strip_comment`）：本块上方的警示注释里逐字写着
    "写成 `shopt -s nullglob` 也一样有效"，整块子串匹配会把**说明文字**当成防护 ——
    变异验证当场抓到：删掉真正的 `[ -e "$f" ] || continue`，判据照样全绿。
    与 `_strip_comment` 的 docstring 里记的是同一类洞：防线在看说明，而不是看接线。
    """
    code = _exec_code_lines(blk)
    if not _UNGLOBBED_BARE_GLOB.search(code):
        return False
    if re.search(r"nullglob|compgen\s+-G", code):
        return False
    # `[ -e "$f" ] || continue` 之类的存在性闸门
    if re.search(r'\[\s*-[ef]\s+"\$\{?\w+\}?"?\s*\]\s*(\|\||&&\s*continue)', code):
        return False
    return True


@pytest.mark.parametrize("rel", sorted(DOCS))
def test_product_globs_guard_against_empty_directory(rel):
    """**G05 第二症状防线**：遍历产物目录的 glob 必须有"无匹配"防护。

    缺陷形态与陈旧标记同源 —— 状态被**误读**：空 `.done/` 目录下，`for f in
    "${RUN_DIR}"/.done/*.done` 输出一行字面量 `✅ *.done`，读者以为某阶段已完成。
    实测（空目录，真 bash）确认。
    """
    text = open(DOCS[rel], encoding="utf-8").read()
    for blk in _bash_fences(text):
        if _block_globs_without_empty_guard(blk):
            pytest.fail(
                f"{rel}: 用 glob 遍历产物目录却没有「无匹配」防护 —— 空目录下 bash "
                f"不展开 glob，字面量（如 `*.done`）会原样进循环体，输出一行 "
                f"`✅ *.done`，读者据此以为某阶段已完成。加 `[ -e \"$f\" ] || continue`、"
                f"`shopt -s nullglob` 或 `compgen -G`。\n"
                f"块开头：{blk.strip().splitlines()[0][:120]}")


# ==== 失效模式：上游声明的下游消费者不存在 / 键名与 schema 对不上（G06，2026-10-29） ====
# 缺陷形态：**同一个交接点的两端各说各话**（根因 D）。
#   · fastp 声明"clean FASTQ 是 Stage 1 的输入"，而 align-sort 的输入契约写**原始**
#     FASTQ —— 下游消费者名不副实；真跑时 Stage 1 静默消费错的那一份，两个阶段的门禁
#     双双放行（实测 1400 对里 400 对被 fastp 剔除，BAM 得 2800 而非 2000）。
#   · fastp 模板从 run.params.json 读 `sample_numbers` —— **该键不存在**（schema 里
#     只有 `samples`/`sample_names`，"sample_numbers" 全仓唯一出处是 db_query.py 的
#     一个函数形参名）。读完 loop 一次都不跑、或 set -u 下当场 unbound variable。
#
# 判据按**写法**扫（与文件其它防线同源）：文档把某个键当 run.params.json 的键来读时，
# 该键必须在 Stage 0 的 schema 字面量里真的存在。schema 由 `_schema_params_keys()`
# 从 params-propose 的 ```json 围栏**推导**，不写死清单 —— 新增键自动被覆盖。

_PARAMS_READ_CLAIM = re.compile(
    r'run\.params\.json[^\n]{0,24}?(?:读取|读出|读回|读|取)(?![不没])([^\n]{0,40})')
# 键名的两种可靠写法：引号/反引号包着的，或**含下划线**的标识符。
# 下划线是键名与散文动词的分界：`sample_numbers` vs `读不出`/`读取`。
_IDENT_WITH_UNDERSCORE = re.compile(r'\b([a-z][a-z0-9]*(?:_[a-z0-9]+)+)\b')
_QUOTED_NAME = re.compile(r'[`"\']([a-z_][a-z0-9_]{2,})[`"\']')
# 引号里/散文里的这些词不是"本 schema 的键"（工具名、布尔字面量、DB 表列名等）
_NON_KEY_WORDS = {"json", "and", "the", "python", "conda", "true", "false",
                  "read1_path", "read2_path"}   # 后两个是 fastq_samples 表的**列**，不是 params 键


def _pred_params_key_not_in_schema(line, schema_keys):
    """行里声称"从 run.params.json 读/取 X"，而 X 不在 schema 键集里 ⇒ True。

    ⚠️ 定标记录（这条谓词改了三版，每版都由实测逼出来）：
      · 只认**同一行** —— 跨行会把整段说明扯进来，误报爆炸；
      · 名字要么带引号、要么**含下划线** —— 不设这个门槛时，"run.params.json
        读不出时 set -e 直接退出"里的 `读不出` 被当成键，全仓 6 处**全是误报**；
      · 否定式 `(?![不没])` 挡住"读不出/读没"这类动词短语。
    定标后 8 个构造样本（含修复前的真缺陷原文 + 6 种散文形态）**全对**，
    见 `test_params_key_predicates_actually_discriminate`。
    """
    if "run.params.json" not in line:
        return False
    m = _PARAMS_READ_CLAIM.search(line)
    if not m:
        return False
    seg = m.group(1)
    names = list(dict.fromkeys(
        _QUOTED_NAME.findall(seg)
        + [n for n in _IDENT_WITH_UNDERSCORE.findall(seg) if "_" in n]))
    bad = [n for n in names if n not in schema_keys and n not in _NON_KEY_WORDS]
    return bool(bad)


def _pred_bash_reads_params_key_not_in_schema(blk, schema_keys):
    """bash 块里 `for i in "${!X[@]}"` 的 X 被当作 run.params.json 的数组读，
    而 X 不在 schema 键集里 ⇒ True。

    数组名就是键名（各阶段模板统一从 run.params.json 读回同名键）。
    """
    names = set(re.findall(r'"\$\{!([a-z_][a-z0-9_]*)\[@\]\}"', blk))
    if not names:
        return False
    # 只判"从 run.params.json 读"语境下的块：块里有 run.params.json 字样
    if "run.params.json" not in blk:
        return False
    local_arrays = set(re.findall(r'^\s*([a-z_][a-z0-9_]*)=\(\)', blk, re.M))
    bad = [n for n in names if n not in schema_keys and n not in local_arrays]
    return bool(bad)


@pytest.mark.parametrize("rel", sorted(DOCS))
def test_params_keys_read_from_docs_are_declared_in_schema(rel):
    """**G06 复发防线（键名半边）**：文档当作 run.params.json 键来读的名字必须在 schema 里。

    反向的"schema 有而校验器不认"由 `test_every_key_the_schema_has_value_for_is_accepted...`
    守着；本条守的是**文档/脚本**侧写的键名 —— 它不经过校验器，写错了只有真跑才暴露。
    """
    schema = _schema_params_keys()
    assert len(schema) >= 5, "从 params-propose 的 json 围栏里解析不到键 —— 本防线正在空转"
    text = open(DOCS[rel], encoding="utf-8").read()
    for ln, line in enumerate(text.splitlines(), 1):
        if _pred_params_key_not_in_schema(line, schema):
            pytest.fail(
                f"{rel}:{ln} 把某个名字当 run.params.json 的键来读，但 Stage 0 的 "
                f"schema 里没有它 —— 照抄生成的脚本会读到空值（`set -u` 下更直接 "
                f"`unbound variable`），而产物缺失要到门禁才发现。\n  {line.strip()[:160]}")
    for blk in _bash_fences(text):
        if _pred_bash_reads_params_key_not_in_schema(blk, schema):
            pytest.fail(
                f"{rel}: bash 块用 `${{!X[@]}}` 遍历数组 X，且块内提到 run.params.json，"
                f"但 schema 里没有键 X —— 该数组读回来必然是空的，循环一次都不跑。\n"
                f"块开头：{blk.strip().splitlines()[0][:120]}")


# ==== 失效模式：声明的下游消费者名不副实（G06 交接半边，2026-10-29） ====
# 缺陷形态比键名那半边更隐蔽：**两端各自都自洽**，只有放一起才矛盾。
#   fastp 的「输出」表产出 `{sample_name}.clean_1/2.fq.gz`，并声明"直接作为 Stage 1 的输入"；
#   align-sort 的「输入」契约写的是 `{sample_number}_1.fq.gz`（原始形态），
#   且**全仓没有任何环节读 clean FASTQ**（实测 `grep -rE 'clean_[12]'` 除门禁/测试
#   自身外零命中，align-sort 里 `fastp` 出现 0 次）。真跑时 Stage 1 按自己的契约拿到
#   原始 FASTQ —— 文件存在、非空、比对成功，**两个阶段的门禁双双放行**，只有 read
#   数对不上账（实测 1400 对里 400 对被 fastp 剔除 → BAM 2800 而非应得的 2000）。
#
# 判据是**结构**：产出方声明的产物名，必须在消费方的输入契约里出现。任一侧改名
# （或删掉那一行声明）都必然红 —— 这正是"改一端忘另一端"的复发形态。

_HANDOFF = {
    # 产出方相对路径 → (消费方相对路径, 产出方声明的产物形态, 消费方必须提到的形态)
    "skills/wgs-express-fastp-qc/SKILL.md": (
        "skills/wgs-express-align-sort/SKILL.md",
        (".clean_1.fq.gz", ".clean_2.fq.gz"),
    ),
}


@pytest.mark.parametrize("producer,spec", sorted(_HANDOFF.items()))
def test_producer_output_form_appears_in_consumer_input_contract(producer, spec):
    """**G06 复发防线（交接半边）**：产出方的产物名必须在消费方的输入契约里出现。"""
    consumer, forms = spec
    p_text = open(os.path.join(REPO, producer), encoding="utf-8").read()
    c_text = open(os.path.join(REPO, consumer), encoding="utf-8").read()
    # 前置：产出方确实还声明着这些产物（否则本防线的前提没了，应报红而不是静默通过）
    for form in forms:
        assert form in p_text, (
            f"{producer} 不再声明产物 `{form}` —— 本防线的前提变了（产出方改名/删表？），"
            f"请同步更新 _HANDOFF 而不是让这条判据静默失效")
        assert form in c_text, (
            f"{producer} 产出 `{form}` 并声明它供下游消费，而消费方 {consumer} 的输入契约里"
            f"**完全没有这个名字** —— 下游按自己的契约会去读另一份文件（原始 FASTQ），"
            f"文件存在、比对成功、门禁全绿，只有 read 数对不上账（实测 2800 vs 2000）。"
            f"修法：消费方的输入契约要写明「跟随 run.params.json 的 fastq_dir」并列出 "
            f"`{form}` 这一形态。")
    # 消费方必须说清"哪一份由什么决定"（根因 D 的通用解药：不能只列形态、不说选择规则）
    assert "fastq_dir" in c_text, (
        f"{consumer} 列了 clean 形态却没说它'由 run.params.json 的 fastq_dir 决定' —— "
        f"读者无法判断本次该用哪一份，等于把交接留成一次猜测")


# ==== 失效模式：断言「Stage 1 的输入是哪一份」而不说选择规则（G06 第三处，2026-10-29） ====
# 交接一致防线只钉住了「产出方声明的名字在消费方出现」这**一对手写的文档**。
# 但同一句断言在本仓还有第三个落点、且它在**校验器的失败文案**里：
#   `_anchor_from_fastq` 判 BAM 的 primary 数与上游 FASTQ 条数不等时，会顺着这条线索
#   告诉用户"本管线 Stage 1 的输入契约是两份 clean FASTQ"。
#   **修 G06 前那句就是这么写的**，而它是**错的**：跳过 Stage 0.5 的常规路径下
#   Stage 1 消费的是原始 FASTQ —— 正好是不该给本阶段传 `--prev-reads-file` 的那种运行。
#   门禁的失败文案是**排查时的第一手线索**，把用户引向一个不存在的契约，比不说更坏。
#
# 判据是**结构**（不是找错的句号）：凡断言"Stage 1 的输入/输入契约/读到什么"的**断言单元**，
# 必须二者之一：
#   · 说出**选择规则** —— 单元里出现 `fastq_dir`（"由 run.params.json 的 fastq_dir 决定"）
#   · 或是在讲**形态**（PE/双端 vs merged/单端）—— 那与 fastq_dir 无关，是另一件事，
#     豁免（两处合法提法：fastp 的 `--merge` 警告、stage_verify 的 merge 模式判据，
#     整段都在讲"合并后的 read 没有环节消费"）
#
# 为什么不能只看"有没有那句错话"：错话可以有无数种写法（实测标定用**修复前逐字原文**
# 作负控，见 `test_stage1_input_claim_predicates_actually_discriminate`）。
# 反过来也不能要求所有提法都带 fastq_dir —— 形态类断言谈的是另一个维度，硬套会误伤。
_STAGE1_INPUT_CLAIM = re.compile(r'Stage\s*1[^\n]{0,24}?(?:的输入|输入契约|读到|消费)')
# 形态维度（PE/双端 vs merged/单端）：与"哪一份由 fastq_dir 决定"正交，不适用本条
_CLAIM_SHAPE_SCOPE = re.compile(r'merge|merged|单端|双端|\bPE\b|\bSE\b')
_CLAIM_RULE_MARKER = "fastq_dir"
# 句末标记：规则必须与断言**同句**，不能靠邻近的其它句子背书
_SENT_END = re.compile(r'[。！？]')


def _paren_depth_delta(s):
    return sum(s.count(c) for c in "([{") - sum(s.count(c) for c in ")]}")


# 续行形态：`+ "..."` / `"..."` / `f"..."` —— 隐式字符串拼接的每一行都括号平衡
_STRING_CONT_LINE = re.compile(r'^[+]?\s*[frbFRB]{0,2}["\']')


def _starts_str_literal(s):
    return bool(_STRING_CONT_LINE.match(s.strip()))


def _ends_str_literal(s):
    t = s.strip()
    return bool(t) and (t.endswith('"') or t.endswith("'"))


def _claim_unit(lines, ln, ext):
    """命中行所属的**断言单元**：md 取同一引用块，py 取同一表达式。

    ⚠️ 必须按单元判、不能按行判：修复后的文案是**多行 f-string 拼接**（一句话串在
    好几行上），只扫命中行会看不到紧跟其后的 `fastq_dir` 选择规则，于是**修好的代码
    反倒报红**。同理也不能扫整个文件 —— 文件别处出现 `fastq_dir` 不能证明本句说了规则。

    ⚠️ .py 侧要两种机制并用，缺一不可（本轮实测踩到）：括号平衡只在**跨行括号**时
    成立，而字符串拼接的每一行**各自都是平衡的**（`"..."` 里没有括号），
    纯括号法在真实文案上退化成单行；故补一条「字符串拼接链」——沿续行形态的相邻行
    双向展开。负控（修复前逐字原文）与正控（现行文案）都要求这条链存在才判得准。
    """
    if ext != ".py":
        if not lines[ln].lstrip().startswith(">"):
            return lines[ln]
        prefix = len(lines[ln]) - len(lines[ln].lstrip(">"))
        lo = ln
        while lo > 0 and lines[lo - 1].lstrip().startswith(">") and \
                len(lines[lo - 1]) - len(lines[lo - 1].lstrip(">")) == prefix:
            lo -= 1
        hi = ln
        while hi + 1 < len(lines) and lines[hi + 1].lstrip().startswith(">") and \
                len(lines[hi + 1]) - len(lines[hi + 1].lstrip(">")) == prefix:
            hi += 1
        return "\n".join(lines[lo:hi + 1])
    lo = hi = ln
    # ① 括号包含：命中行处在未闭合的括号里 ⇒ 扩到整个括号表达式
    d = _paren_depth_delta(lines[ln])
    while d > 0 and hi + 1 < len(lines):
        hi += 1
        d += _paren_depth_delta(lines[hi])
    d = _paren_depth_delta(lines[ln])
    while d > 0 and lo > 0:
        lo -= 1
        d += _paren_depth_delta(lines[lo])
    # ② 字符串拼接链：隐式相接的字面量在每行上都是平衡的，① 抓不到
    while hi + 1 < len(lines) and _starts_str_literal(lines[hi + 1]):
        hi += 1
    while lo - 1 >= 0 and _ends_str_literal(lines[lo - 1]):
        lo -= 1
    return "\n".join(lines[lo:hi + 1])


def _split_sentences(prose):
    """按句末标点切句，返回句子列表。"""
    out, start = [], 0
    for m in _SENT_END.finditer(prose):
        out.append(prose[start:m.end()])
        start = m.end()
    if start < len(prose):
        out.append(prose[start:])
    return out


def _claim_prose(unit, ext):
    """把断言单元还原成**散文**：.py 去掉字符串字面量的语法噪音后逐行相接。

    `f"无 per-mate 读数）—— 本管线 Stage 1 的输入契约是 PE 的两份 "` 这样的行，
    还原后就是它印出来的那句话 —— 判"这句话说没说规则"必须在这层判。
    """
    if ext != ".py":
        return unit
    parts = []
    for line in unit.split("\n"):
        t = line.strip()
        t = re.sub(r'^[+]?\s*[frbFRB]{0,2}["\']', "", t)   # 开头引号（含 + 与前缀）
        t = re.sub(r'["\']\s*[+)]?\s*$', "", t)            # 结尾引号与可能的 `)` / `+`
        parts.append(t)
    return "".join(parts)


def _pred_stage1_input_claim_without_rule(text, ext):
    """断言"Stage 1 的输入是哪一份"却**没说选择规则**、又不是在讲形态 ⇒ True。

    ⚠️ 规则必须与断言**同句**、且是**陈述**（本轮实测的两个漏洞，各有一个反例样本）：
      · 同句之外提到 `fastq_dir` 不算 —— 否则「写死一侧 + 后一句顺带提一句配置字段」
        能溜过去，而读者读到的是那句写死的断言（变异 6）。
      · 同句里只是**举着这个词**也不算 —— 「本阶段消费 `{RUN_DIR}/out/` 下的
        `{sample_name}.clean_1.fq.gz`」里没有任何 `fastq_dir` 字样却**已是**写死一侧；
        反之「由 `fastq_dir` 决定」才是陈述规则。本判据取「同句出现 `fastq_dir`」为
        规则陈述的**充分标记**（本仓所有合规写法都这么写），并要求它不能只出现在
        括号补充或"另见"里 —— 那类由下面的 `_RULE_AS_ASIDE` 排除。
    """
    lines = text.split("\n")
    for m in _STAGE1_INPUT_CLAIM.finditer(text):
        ln = text[:m.start()].count("\n")
        prose = _claim_prose(_claim_unit(lines, ln, ext), ext)
        for seg in _split_sentences(prose):
            if not _STAGE1_INPUT_CLAIM.search(seg):
                continue
            if _CLAIM_SHAPE_SCOPE.search(seg):
                continue
            if _CLAIM_RULE_MARKER in seg and not _RULE_AS_ASIDE.search(seg):
                continue
            return True, ln + 1, lines[ln].strip()
    return False, 0, ""


# 「另见/补充说明」形态：提到 `fastq_dir` 但**不是**在陈述选择规则，不算数
_RULE_AS_ASIDE = re.compile(r'(?:见|详见|参考|另见|参见)[^。；]{0,12}fastq_dir'
                            r'|fastq_dir[^。；]{0,6}(?:字段|键)?\s*[)）]')


def _stage1_input_claim_sources():
    """自动发现扫描面：全部 skill 源码 —— 文档与脚本都会印出这句断言。

    `skills/**/*.md`（SKILL.md 与 usage-guide/references）与 `skills/**/*.py`
    （校验器、db_query 等）。含 `.py` 是本条与其它防线最大的不同：这句话的
    **第三处落点在失败文案里**，是个 .py 文件。

    `.py` 侧跳过符号链接（理由见 `_repo_py_sources`）：扫它等于用仓外的部署副本
    去判一个仓内路径，结果不可复现。
    """
    pats = [os.path.join(SKILLS, "*", "*.md"),
            os.path.join(SKILLS, "*", "references", "*.md")]
    seen = {}
    for p in pats:
        for f in sorted(glob.glob(p, recursive=True)):
            seen[os.path.relpath(f, REPO)] = f
    for f in _repo_py_sources():
        seen[os.path.relpath(f, REPO)] = f
    return seen


@pytest.mark.parametrize("rel", sorted(_stage1_input_claim_sources()))
def test_stage1_input_claim_declares_selection_rule(rel):
    """**G06 复发防线（断言半边）**：说"Stage 1 的输入是哪一份"就必须说清由谁决定。"""
    text = open(os.path.join(REPO, rel), encoding="utf-8").read()
    hit, ln, line = _pred_stage1_input_claim_without_rule(text, os.path.splitext(rel)[1])
    if hit:
        pytest.fail(
            f"{rel}:{ln} 断言了 Stage 1 的输入是哪一份，却**没说由什么决定** —— "
            f"本仓 Stage 1 的输入由 `run.params.json` 的 `fastq_dir` 决定"
            f"（跳过 Stage 0.5 时是原始 FASTQ，跑了才是 clean），"
            f"写死一侧会把用户/读者引向另一半的部署（实测：Stage 1 静默消费错的那一份，"
            f"两个阶段的门禁双双放行，只有 read 数对不上账 2800 vs 2000）。\n"
            f"  要么写明「由 run.params.json 的 fastq_dir 决定」，"
            f"要么若这句讲的是**形态**（PE vs merged/单端）则加一句形态词以示区分。\n"
            f"  {line[:160]}")


def test_stage1_input_claim_predicates_actually_discriminate():
    """负控 + 正控：谓词必须抓住**修复前逐字原文**，且不误伤两处合法的形态类断言。

    负控用真实历史文本（`stage_verify.py` 修复前的 `"本管线 Stage 1 的输入契约是
    两份 clean FASTQ。"`）—— 用构造的近似样本自证不算数，谓词的价值就在于抓真东西。
    """
    sv = "skills/wgs-expressive-pipeline/scripts/stage_verify.py"
    cases = [
        # (样本, 期望命中)
        ('                    f"无 per-mate 读数）—— 本管线 Stage 1 的输入契约是 PE 的两份 "\n'
         '                    f"clean FASTQ，合并后的 read 进了 --merged_out，下游没有任何环节"',
         False),   # 形态类（PE/merged）：豁免
        ('    """本管线 Stage 1 的输入契约是 PE 的两份 clean FASTQ，merged 文件没有任何环节消费：',
         False),   # 形态类：豁免
        ('                + "另一种可能是本阶段消费的**不是** Stage 0.5 的产物"\n'
         '                  "本管线 Stage 1 的输入由 run.params.json 的 `fastq_dir` 决定 —— "',
         False),   # 说出了选择规则：通过
        ("> 现行契约（两侧已对齐）：Stage 1 的输入由 `run.params.json` 的 `fastq_dir` 决定 ——",
         False),   # 说出了选择规则：通过
        # —— 负控：修复前逐字原文（跨行拼接的形态，谓词必须按**单元**判才抓得到）——
        ('                    "另一种可能是本阶段消费的**不是** Stage 0.5 的产物"\n'
         '                  "本管线 Stage 1 的输入契约是两份 clean FASTQ。\n'
         '                  "，而 clean_*.fq.gz 是另一轮的）："',
         True),
        ("> 清洗后的 FASTQ 直接作为 Stage 1（BWA）的输入，不写回数据库。", True),
        # —— 负控：变异 6 的形态 —— 规则**挪到下一句**，断言本身仍写死一侧 ——
        #    按"单元里有没有 fastq_dir"判会漏掉它（本轮实测确实漏过），故必须按句判
        ('"本管线 Stage 1 的输入契约是两份 clean FASTQ。"\n'
         '"（配置细节见 run.params.json 的 fastq_dir 字段。）"',
         True),
        # —— 负控：同句里只是**举着这个词**当"另见"，不是陈述规则 ——
        ('本管线 Stage 1 的输入是 clean FASTQ（见 fastq_dir 字段）。', True),
    ]
    missed = []
    for sample, expected in cases:
        got, ln, _ = _pred_stage1_input_claim_without_rule(sample, ".py")
        if got != expected:
            missed.append(
                f"{'**漏报**' if expected else '**误伤**'}: 期望 {expected} 得到 {got} "
                f"（第 {ln} 行）← {sample[:70]!r}")
    assert not missed, "谓词失去鉴别力（等于纸面具备）：\n  " + "\n  ".join(missed)
    # 反空转：扫描面与活跃断言都必须非空，否则本防线扫了个寂寞
    srcs = _stage1_input_claim_sources()
    assert len(srcs) >= 20, f"扫描面只发现 {len(srcs)} 个文件 —— 本防线正在空转"
    live = [rel for rel in srcs
            if _STAGE1_INPUT_CLAIM.search(
                open(os.path.join(REPO, rel), encoding="utf-8").read())]
    # 底数 = 本仓当前的活跃断言处数（fastp / params-propose / 编排 / 校验器）。
    # 它涨不该报警，跌必然是**有人删了断言**（或正则失配），两者都该被看见。
    assert len(live) >= 4, (
        f"全仓只有 {len(live)} 个文件出现这句断言（预期 ≥4：fastp、params-propose、"
        f"编排、校验器）—— 要么断言被删光，要么 `_STAGE1_INPUT_CLAIM` 已失配，"
        f"本防线正在空转。现存：{live}")
    # 点名钉住**失败文案**那一处：它是排查时的第一手线索，也是本条防线的由来
    assert sv in live, (
        f"{sv} 的失败文案不再提「Stage 1 的输入由什么决定」—— 用户看到 'read 数对不上' "
        f"时最需要的那条线索没了（或者改成了另一句话，请同步 `_STAGE1_INPUT_CLAIM`）")
    assert os.path.exists(os.path.join(REPO, sv)), f"负控出处 {sv} 不存在"


# ==== 失效模式：脚本文档示范了在自己运行环境里跑不通的调用（G08，2026-10-29） ====
# `stage_verify.py` 的 docstring「用法」段曾示范
#     conda run -n Agent python <repo>/.../stage_verify.py --stage ...
# 而本脚本**从不**从交互式 shell 调用：它由阶段脚本的收尾门禁段调用，而阶段脚本已经跑在
# `conda run -n Agent bash` 里。再套一层时 `conda run` 把 PATH 收窄成 envs/Agent/bin +
# 系统目录，那里**没有 conda 可执行文件** → rc=127、校验器根本没启动、`.verify/*.json`
# 未产出 → 数据再健康也走失败分支（实测复现：PIPELINE_RC=127，三个标记目录全不存在）。
#
# ⚠️ **为什么不能一刀切禁掉 `conda run`**（本轮标定的关键）：本仓另有**独立工具**的
# docstring 示范 `conda run -n Agent python db_query.py ...` —— 那是**对的**：它们确实
# 从交互式 shell 直接跑，那里 PATH 里有 conda。禁掉等于误伤正确示范。
# 真正的判据是**同一文档里有没有"被阶段脚本调用"的语境**：
#   自述「由阶段脚本…调用」的脚本，其调用示范必须用裸 `python`（阶段脚本已在 conda 内）；
#   其余（独立工具）用 `conda run` 是规范写法，豁免。
# 这与 G08 的机理一致：矛盾不在"写没写 conda run"，而在**示范与自己声明运行环境的冲突**。
_STAGE_CALLED_MARK = re.compile(
    r'由\s*阶段脚本|阶段脚本(?:在写|的收尾|内部)|收尾门禁段|由编排[^\n]{0,10}调用|'
    r'由阶段[^\n]{0,6}调用')
_CALLS_ITSELF_SUBPROCESS = re.compile(
    r'^\s*(?:#.*)?(?:conda\s+run\s+[^\n]*?)?python\s+["\']?\$\{?VERIFIER', re.M)
_VERIFIER_STANDALONE = re.compile(
    r'^\s*conda\s+run\s+-n\s+\S+\s+python\s+(?:<repo>|\S*/)?stage_verify\.py', re.M)
_DOC_RUN_CALL = re.compile(r'^\s*conda\s+run\s+-n\s+\S+\s+python\s', re.M)


# 注：`_repo_py_sources()`（`skills/**/*.py` 发现助手，跳过符号链接）定义在文件上方
# 与 `all_skill_docs()` 同处 —— 多个扫描面共用，且 parametrize 在导入期求值。


def _script_docstrings():
    """自动发现全部 skill 脚本的 docstring（G08 的扫描面：`DOCS` 只含 markdown）。"""
    out = {}
    for p in _repo_py_sources():
        src = open(p, encoding="utf-8").read()
        try:
            doc = ast.get_docstring(ast.parse(src))
        except SyntaxError:
            continue
        if doc:
            out[os.path.relpath(p, REPO)] = doc
    return out


def _pred_doc_demos_nested_conda_run(doc):
    """docstring 既自述「由阶段脚本调用」、又示范 `conda run … python …` ⇒ True。

    返回 `(命中, 证据行)`；独立工具（无"由阶段脚本调用"语境）不命中。
    """
    if not _STAGE_CALLED_MARK.search(doc):
        return False, ""
    for line in doc.split("\n"):
        if _DOC_RUN_CALL.match(line):
            return True, line.strip()
    return False, ""


@pytest.mark.parametrize("rel", sorted(_script_docstrings()))
def test_nested_conda_run_demos_have_stage_context(rel):
    """**G08 复发防线**：自述"由阶段脚本调用"的脚本，其示范不得自套 `conda run`。

    机理：阶段脚本本身已在 `conda run -n Agent bash` 内，其 PATH 无 conda 可执行文件，
    再嵌套即 rc=127、校验器不启动、`.verify/*.json` 不产出（实测复现）。独立工具的
    `conda run` 示范是对的，豁免 —— 判据是**语境冲突**，不是关键词。
    """
    doc = _script_docstrings()[rel]
    hit, line = _pred_doc_demos_nested_conda_run(doc)
    if hit:
        pytest.fail(
            f"{rel} 的 docstring 一边自述「由阶段脚本调用」（= 已在 conda 环境内），"
            f"一边示范 `conda run -n Agent python …` —— 照抄会以 **rc=127** 失败："
            f"`conda run` 把 PATH 收窄成 envs/Agent/bin + 系统目录，那里没有 conda；"
            f"校验器根本没启动、`.verify/*.json` 未产出，数据再健康也走失败分支。\n"
            f"  改成裸 `python \"${{VERIFIER}}\" …`（见各阶段 SKILL.md 的收尾门禁段）：\n"
            f"  {line[:150]}")


def test_nested_conda_run_predicate_actually_discriminates():
    """负控 + 正控：谓词必须抓住 `stage_verify.py` 修复前的逐字示范，且**不误伤独立工具**。

    误伤检查用的是本仓**真实的**独立工具 docstring（`db_query.py` /
    `phenotype_extract.py`）—— 它们示范 `conda run` 是规范写法，谓词判它们为坏样本
    就等于把防线变成误报机，下一个维护者只会把它关掉。
    """
    docs = _script_docstrings()
    # 负控 1：stage_verify.py **修复前**的逐字示范（自述"由阶段脚本…调用" + 嵌套示范）
    pre_fix = ("用法（由阶段脚本在写 .done 前调用）\n"
               "-----------------------------------\n"
               "    conda run -n Agent python <repo>/skills/wgs-expressive-pipeline/"
               "scripts/stage_verify.py \\\n"
               '        --stage 01_bwa_sort --run-dir "${RUN_DIR}" \\\n'
               '        --samples "sample1,sample2" --json\n')
    got, line = _pred_doc_demos_nested_conda_run(pre_fix)
    assert got, f"漏报：修复前的逐字示范没被抓住 ← {line!r}"
    # 正控 1：现行 docstring（裸 python + 告诫）必须通过
    sv = "skills/wgs-expressive-pipeline/scripts/stage_verify.py"
    assert sv in docs, f"{sv} 没有 docstring —— 扫描面空了"
    got, line = _pred_doc_demos_nested_conda_run(docs[sv])
    assert not got, f"误伤：现行 docstring（裸 python）被拦 ← {line!r}"
    assert _STAGE_CALLED_MARK.search(docs[sv]), (
        "现行 docstring 不再自述「由阶段脚本调用」—— 谓词的语境前提没了，"
        "它对本文件的判别力等于零（请同步 `_STAGE_CALLED_MARK`）")
    # 正控 2（**误伤检查**）：真实独立工具的 docstring 不得命中
    # ⚠️ 用**非符号链接**的路径（另一个 db_query.py 是跟踪中的符号链接，指向仓外部署
    #    副本，已被扫描面排除 —— 见 `_repo_py_sources`）
    for rel, why in [("skills/wgs-expressive-pipeline/scripts/db_query.py",
                      "独立工具，从交互式 shell 调用"),
                     ("skills/phenotype-extract/scripts/phenotype_extract.py",
                      "独立工具，另一 conda 环境")]:
        assert rel in docs, f"{rel} 没有 docstring —— 误伤检查的前提没了"
        hit, line = _pred_doc_demos_nested_conda_run(docs[rel])
        assert not hit, (
            f"误伤：{rel}（{why}）示范 `conda run` 是**对的**，却被判为坏样本 ← {line!r}")
        assert _DOC_RUN_CALL.search(docs[rel]), (
            f"{rel} 不再示范 `conda run` —— 误伤检查失去了对象（样本变成了平凡通过）")
    # 反空转：扫描面必须非空
    assert len(docs) >= 8, f"只发现 {len(docs)} 个带 docstring 的脚本 —— 本防线正在空转"


def test_params_key_predicates_actually_discriminate():
    """负控 + 正控：两条键名谓词必须对坏样本返回 True、对好样本返回 False。

    坏样本 1 用**修复前的原文逐字**（`/home/chenyuxi/tmp/gfix/items/G06.md` 引的 HEAD
    `fastp-qc/SKILL.md:143`）—— 谓词的价值就在于能抓住它，用构造的近似样本自证不算数。
    """
    schema = _schema_params_keys()
    assert len(schema) >= 5, "schema 解析空转"
    # ---- 行级谓词 ----
    assert _pred_params_key_not_in_schema(
        "  # 实际运行时从 run.params.json 读取 sample_numbers 和 sample_names",
        schema) is True, "坏样本（读 sample_numbers，修复前原文）没被抓住"
    for good, why in [
        ("# 从 run.params.json 读取 samples 和 sample_names", "读 samples/sample_names"),
        ("SAMPLES_CSV 从 run.params.json 的 sample_names 读回", "读回 sample_names"),
        ("verifier_path 由 run.params.json 解析后传入", "解析 verifier_path"),
        ("# 同上：加 `|| true`，否则 run.params.json 读不出时 set -e 直接退出、",
         "散文「读不出时」（误报过一次）"),
        ("| `--native-pair-hmm-threads` | 从 `run.params.json` 读取 | PairHMM 线程数 |",
         "散文「读取」+ 非键的选项名（误报过一次）"),
        ("本阶段的产物落在 run.params.json 旁边", "非读取语境"),
    ]:
        assert _pred_params_key_not_in_schema(good, schema) is False, f"好样本被误伤：{why}"
    # ---- bash 数组谓词 ----
    bad_blk = ('# 实际运行时从 run.params.json 读取\n'
               'for i in "${!sample_numbers[@]}"; do echo "${sample_numbers[$i]}"; done\n')
    good_blk = ('# 实际运行时从 run.params.json 读取\n'
                'for i in "${!sample_names[@]}"; do echo "${sample_names[$i]}"; done\n')
    local_blk = ('# 从 run.params.json 建数组\n'
                 'sample_numbers=()\n'
                 'for i in "${!sample_numbers[@]}"; do echo "${sample_numbers[$i]}"; done\n')
    assert _pred_bash_reads_params_key_not_in_schema(bad_blk, schema) is True
    assert _pred_bash_reads_params_key_not_in_schema(good_blk, schema) is False
    assert _pred_bash_reads_params_key_not_in_schema(local_blk, schema) is False, \
        "块内自己声明的数组（非 schema 键）被误伤"


def test_stale_done_guards_actually_discriminate():
    """负控 + 正控：上面几条谓词必须对坏样本返回 True、对好样本返回 False。"""
    bad = ('```bash\n'
           'if [ -f "${RUN_DIR}/.failed/${s}.failed" ]; then echo "❌"; continue; fi\n'
           'if [ -f "${RUN_DIR}/.done/${s}.done" ]; then echo "✅ 已完成"; continue; fi\n'
           'if [ -f "${pid_file}" ] && kill -0 "$(cat "${pid_file}")" 2>/dev/null; then echo "🕐"; continue; fi\n'
           '```\n')
    good = ('```bash\n'
            'if [ -f "${RUN_DIR}/.failed/${s}.failed" ]; then echo "❌"; continue; fi\n'
            'if [ -f "${pid_file}" ] && kill -0 "$(cat "${pid_file}")" 2>/dev/null; then echo "🕐"; continue; fi\n'
            'if [ -f "${RUN_DIR}/.done/${s}.done" ]; then echo "✅ 已完成"; continue; fi\n'
            '```\n')
    b = _four_state_order_blocks(bad)
    g = _four_state_order_blocks(good)
    assert b and g, "判序样本没被识别为四态块 —— 本防线正在空转"
    assert _done_branch_precedes_pid_alive(b[0]) is True, "坏样本（.done 先判）没被抓住"
    assert _done_branch_precedes_pid_alive(g[0]) is False, "好样本（pid 先判）被误伤"

    # 变量写法（workflow.md 的 `elif` 链形态）：坏序与好序都识别得出 ——
    # 旧判据只认字面量 `.done`，这个样本上它会返回 False（变异验证抓到的洞）
    bad_var = ('```bash\n'
               'if [ -f "${failed_file}" ]; then echo "❌"\n'
               'elif [ -f "${done_file}" ]; then echo "✅ 已完成"\n'
               'elif [ -f "${pid_file}" ] && kill -0 "$(cat "${pid_file}")" 2>/dev/null; then echo "🕐"\n'
               'fi\n'
               '```\n')
    good_var = ('```bash\n'
                'if [ -f "${failed_file}" ]; then echo "❌"\n'
                'elif [ -f "${pid_file}" ] && kill -0 "$(cat "${pid_file}")" 2>/dev/null; then echo "🕐"\n'
                'elif [ -f "${done_file}" ]; then echo "✅ 已完成"\n'
                'fi\n'
                '```\n')
    bv = _four_state_order_blocks(bad_var)
    gv = _four_state_order_blocks(good_var)
    assert bv and gv, "变量写法样本没被识别为四态块"
    assert _done_branch_precedes_pid_alive(bv[0]) is True, (
        "变量写法 `${done_file}` 的坏样本没被抓住（旧判据的洞）")
    assert _done_branch_precedes_pid_alive(gv[0]) is False, "变量写法的好样本被误伤"

    # 反引号/说明文字里的顺序不该被当成执行顺序（正控）
    prose = ('```bash\n'
             '# .failed → .done → 运行中（文档里的说明文字）\n'
             'if [ -f "${pid_file}" ] && kill -0 "$(cat "${pid_file}")" 2>/dev/null; then echo "🕐"; continue; fi\n'
             'if [ -f "${RUN_DIR}/.done/${s}.done" ]; then echo "✅"; continue; fi\n'
             'if [ -f "${RUN_DIR}/.failed/${s}.failed" ]; then echo "❌"; fi\n'
             '```\n')
    p = _four_state_order_blocks(prose)
    assert p and _done_branch_precedes_pid_alive(p[0]) is False, (
        "注释里的「.done」字样让判据误判了执行顺序")

    # ---- 开工清理谓词 ----
    tpl_bad = ('```bash\n'
               'DONE_FILE="${DONE_DIR}/01_bwa_sort.done"\n'
               'PID_FILE="${PID_DIR}/01_bwa_sort.pid"\n'
               'mkdir -p "${LOG_DIR}"\n'
               'echo $$ > "${PID_FILE}"\n'
               'trap \'rm -f "${PID_FILE}"\' EXIT\n'
               'date > "${DONE_FILE}"\n'
               '```\n')
    tpl_good = ('```bash\n'
                'DONE_FILE="${DONE_DIR}/01_bwa_sort.done"\n'
                'PID_FILE="${PID_DIR}/01_bwa_sort.pid"\n'
                'mkdir -p "${LOG_DIR}"\n'
                'rm -f "${DONE_FILE}"\n'
                'echo $$ > "${PID_FILE}"\n'
                'trap \'rm -f "${PID_FILE}"\' EXIT\n'
                'date > "${DONE_FILE}"\n'
                '```\n')
    assert _block_missing_stale_done_clear(tpl_bad) is True, "坏样板没被抓住"
    assert _block_missing_stale_done_clear(tpl_good) is False, "好样板被误伤"
    # 收尾片段（没有赋值）不该被这条判据要求开工清理（正控：作用域别过宽）
    tail_only = ('```bash\n'
                 'date > "${DONE_FILE}"\n'
                 'rm -f "${RUN_DIR}/.failed/01_bwa_sort.failed"\n'
                 '```\n')
    assert _block_missing_stale_done_clear(tail_only) is False, (
        "收尾片段被误当成阶段脚本样板")
    # 编排层 §2.2 的收尾模板（有赋值、但开头写明是"阶段收尾"）—— 在里面清 .done
    # 发生在工作**之后**，挡不住竞态窗口，不该被当成"模板已修"（正控：别制造纸面具备）
    tail_template = ('```bash\n'
                     '# ─── 阶段收尾：先校验，再决定写哪个标记 ───\n'
                     'DONE_FILE="${RUN_DIR}/.done/01_bwa_sort.done"\n'
                     'date > "${DONE_FILE}"\n'
                     '```\n')
    assert _block_missing_stale_done_clear(tail_template) is False, (
        "收尾模板被要求做开工清理 —— 那会在工作之后才清，属纸面具备")

    # ---- 反空转：真实仓库上两条判据都确实扫到了东西 ----
    # ⚠️ 只数"有几个四态块"**不够** —— 变异验证当场抓到：workflow.md 的块用 `${done_file}`，
    #    旧判据认不出它的 `.done` 分支，块数照样 ≥2、判据全绿，而改回旧判序**一条都不红**。
    #    故必须断言**块内两个分支都真的被识别了**（每块都能定出 pid 行与 .done 行）。
    order_blocks, resolvable = 0, 0
    for r in DOCS:
        for blk in _four_state_order_blocks(open(DOCS[r], encoding="utf-8").read()):
            order_blocks += 1
            lines = [_strip_comment(ln) for ln in blk.splitlines()]
            if (any(_PID_ALIVE_LINE.search(ln) for ln in lines)
                    and any(_DONE_TEST_LINE.search(ln) and "kill -0" not in ln
                            for ln in lines)):
                resolvable += 1
    assert order_blocks >= 2, (
        f"全仓只找到 {order_blocks} 个四态判定块（预期 ≥2：编排层 SKILL.md + workflow.md）"
        f" —— 本防线正在空转")
    assert resolvable == order_blocks, (
        f"有 {order_blocks - resolvable}/{order_blocks} 个四态块里，pid 分支或 .done 分支"
        f"**没被识别出来** —— 这些块的分支位置根本没被比较，判序改回去也不会红"
        f"（纸面具备）。检查 _PID_ALIVE_LINE / _DONE_TEST_LINE 是否覆盖了该块的文件名写法。")

    # ---- glob 空目录谓词：负控 + 正控 ----
    glob_bad = ('```bash\n'
                'for f in "${RUN_DIR}"/.done/*.done; do echo "✅ $(basename "$f")"; done\n'
                '```\n')
    # 坏样本 2：防护只存在于**注释**里（变异验证抓到的真实形态 —— gwas 修复块上方
    # 的警示注释逐字写着 "写成 shopt -s nullglob 也一样有效"，整块匹配会把它当防护）
    glob_bad_comment = ('```bash\n'
                        '# 写成 shopt -s nullglob 也一样有效；[ -e "$f" ] || continue 也行\n'
                        'for f in "${RUN_DIR}"/.done/*.done; do echo "✅ $f"; done\n'
                        '```\n')
    glob_good_e = ('```bash\n'
                   'for f in "${RUN_DIR}"/.done/*.done; do\n'
                   '    [ -e "$f" ] || continue\n'
                   '    echo "✅ $(basename "$f")"\n'
                   'done\n'
                   '```\n')
    glob_good_n = ('```bash\nshopt -s nullglob\n'
                   'for f in "${RUN_DIR}"/.done/*.done; do echo "✅ $f"; done\n'
                   '```\n')
    for label, blk, want in (("坏样本（无防护）", glob_bad, True),
                             ("坏样本（防护只在注释里）", glob_bad_comment, True),
                             ("[ -e ] 防护", glob_good_e, False),
                             ("nullglob 防护", glob_good_n, False)):
        got = _block_globs_without_empty_guard(blk)
        assert got is want, f"glob 谓词对{label}判断错误（得到 {got}，期望 {want}）"
    # 反空转：真实仓库上这条谓词确实扫到了 glob 循环
    scanned = sum(1 for r in DOCS for blk in _bash_fences(open(DOCS[r], encoding="utf-8").read())
                  if _UNGLOBBED_BARE_GLOB.search(blk))
    assert scanned >= 1, "全仓一个产物 glob 循环都没扫到 —— 本防线正在空转"


@pytest.mark.parametrize("rel", CAPABILITY_DOCS)
def test_no_order_proven_by_header_or_index(rel):
    """**F18 残余复发防线**：不得断言"头部 SO 标签 / 索引存在"能证明 BAM 本体有序。

    这是本轮实测推翻的一条**曾被明确登记为边界**的结论（原文见
    `docs/test-evidence.md`：「samtools 拒绝给乱序 BAM 建索引，故'乱序且带索引'
    在现实中不存在」）—— 前半句正是该形态赖以成立的性质，后半句被块交换 fixture
    直接证伪。文档若照旧写"有 SO/有索引即有序"，生成的阶段脚本就会跳过本体重算，
    而那是**唯一**能发现该形态的手段。

    ⚠️ 扫的是 `CAPABILITY_DOCS` 而非 `DOCS`：那条被推翻的断言就在
    `docs/test-evidence.md` 里，用 `DOCS` 会**扫不到自己该管的文件**（实测过）。

    允许出现：告诫/纠正语境（"自称/不可/无法/作废/实测"等）。
    """
    path = os.path.join(REPO, rel)
    assert os.path.isfile(path), f"能力台账不见了：{rel}（护栏在扫空集）"
    text = open(path, encoding="utf-8").read()
    for ln, line in enumerate(text.splitlines(), 1):
        if _pred_order_proven_by_header_or_index(line):
            pytest.fail(
                f"{rel}:{ln} 断言 `@HD SO`/索引存在**能证明**本体有序 —— 实测已推翻："
                f"交换两个等长 BGZF 数据块并保留原 .bai 后，头部仍写 SO:coordinate、"
                f"索引仍在、quickcheck/`view -c`/idxstats 全通过，而本体坐标已乱序"
                f"（下游按区读取静默取错数据）。SO 是**头部自称**；只有拿本体重建一次"
                f"索引才能证明有序。若本行是告诫读者不要这么想，请加「自称/不可/作废」等字样。\n"
                f"  {line.strip()[:160]}")


def _stages_consuming_prev_reads_file():
    """从**校验器源码**推导出"哪些 stage 真会读 `--prev-reads-file`"（不是写死清单）。

    写死 `("01_bwa_sort", "02_markdup_bqsr")` 会让这条防线在**新增**一个消费该参数的
    stage 时静默失守 —— 那又是"作用域错了"的老问题。故用 `ast` 沿调用图求传递闭包：
    不能只看 stage 入口函数本体，因为 `verify_align` 是通过 `_anchor_from_fastq()`
    间接读的（只看本体会漏掉 01_bwa_sort，实测踩到）。

    用 ast 而非正则，是因为 `ast.Attribute` **匹配不到注释和字符串里的同名文字**：
    校验器里有若干注释/消息文本写着 `--prev-reads-file`，正则会把它们当成"消费"。
    """
    src = open(os.path.join(SV_PATH, "stage_verify.py"), encoding="utf-8").read()
    tree = ast.parse(src)

    funcs, stage_of = {}, {}
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        funcs[node.name] = node
        for dec in node.decorator_list:
            if (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Name)
                    and dec.func.id == "stage" and dec.args
                    and isinstance(dec.args[0], ast.Constant)):
                stage_of[node.name] = dec.args[0].value

    assert stage_of, ("从 stage_verify.py 里一个 `@stage(...)` 都没解析出来 —— "
                      "装饰器写法变了，本防线正在空转")

    def _reads_flag(node):
        return any(isinstance(n, ast.Attribute) and n.attr == "prev_reads_file"
                   for n in ast.walk(node))

    def _consumes(fn):
        seen, stack = {fn}, [fn]
        while stack:
            body = funcs[stack.pop()]
            if _reads_flag(body):
                return True
            for n in ast.walk(body):
                if (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                        and n.func.id in funcs and n.func.id not in seen):
                    seen.add(n.func.id)
                    stack.append(n.func.id)
        return False

    return {sname for fn, sname in stage_of.items() if _consumes(fn)}


def _block_missing_prev_reads_flag(blk, consumers):
    """块点名调用了**消费该参数**的 stage，但**可执行行**里没传 `--prev-reads-file` ⇒ True。

    ⚠️ 判据建在**去注释后的可执行行**上（`_strip_comment`）：调用上方的警示注释里
    就逐字写着这个参数名，整块子串匹配会被注释**掩盖**真缺陷 —— 变异①实测：
    删掉真参数、只留注释，旧判据 232 passed 仍然全绿。
    """
    code = _exec_code_lines(blk)
    stages = set(re.findall(r"--stage\s+(\d\d_\w+)", code))
    if not (stages & consumers):
        return False
    return "--prev-reads-file" not in code


@pytest.mark.parametrize("rel", sorted(DOCS))
def test_gates_pass_prev_reads_file_where_consumed(rel):
    """**F18 残余的接线防线**：消费 `--prev-reads-file` 的 stage，门禁调用必须真传它。

    ⚠️ 为什么"接线"本身要设防线：`reads_anchor` 判据是**唯一**能发现「合法但更短的
    BAM」的东西，但它的基准来自 `--prev-reads-file`。阶段脚本不传这个参数时，
    判据不会报错、也不会失败 —— 它登记一条 warn「未提供基准、未做该判定」然后继续，
    报告照样可能全绿。**判据在、从不生效**，正是本项目"纸面具备"的核心形态，
    而且它的触发条件（有人重写模板时漏掉一行）极常见。

    实测过：把两处调用（align-sort 的 SKILL.md、pipeline 的编排模板）的该参数删掉后，
    所有测试仍然全绿 —— 即此前**没有任何东西**守着这条接线。故补此防线。

    作用域是**自动发现**的：扫全部 skill 文档的 bash 围栏，凡点名调用
    `stage_verify.py --stage NN_...` 的块都要过；而"哪些 stage 该带这个参数"
    由 `_stages_consuming_prev_reads_file()` 从校验器源码推出。这样将来新增一个
    消费该参数的 stage 时，它的调用点**自动**进入射程，不需要有人记得来改这里。

    ⚠️ 判据建在**可执行行**上（经 `_strip_comment`），不是整块子串匹配 ——
    调用上方的警示注释里就写着这个参数名，整块匹配会被注释**掩盖**真缺陷
    （变异①实测：删掉真参数、只留注释，旧判据全绿）。
    """
    consumers = _stages_consuming_prev_reads_file()
    assert consumers, ("没有从校验器源码里推出任何消费 --prev-reads-file 的 stage —— "
                       "要么该参数被删了，要么推导逻辑坏了；本防线在空转")
    text = open(DOCS[rel], encoding="utf-8").read()
    blocks = _bash_fences(text)
    assert blocks, f"{rel} 里一个 bash 围栏都没抽到 —— 围栏标记或文档结构变了"
    for blk in blocks:
        code = _exec_code_lines(blk)
        stages = set(re.findall(r"--stage\s+(\d\d_\w+)", code))
        if not (stages & consumers):
            continue
        assert not _block_missing_prev_reads_flag(blk, consumers), (
            f"{rel}: 调用 `stage_verify.py --stage {'/'.join(sorted(stages & consumers))}` "
            f"时未传 --prev-reads-file（**可执行行**里没有；注释里提到不算）—— "
            f"该校验阶段**确实会读**这个参数（从源码推导），"
            f"不传时它只登记「未提供基准、未做该判定」然后继续，报告照样可能全绿。\n"
            f"  对 01_bwa_sort，这是**唯一**能发现「合法但更短的 BAM」（BGZF 块边界截断后"
            f"贴回 EOF 块，quickcheck/全量计数/idxstats/坐标有序全部通过）的判据。\n"
            f"  应传 --prev-reads-file \"${{RUN_DIR}}/.verify/00_fastp_qc.json\"")



def test_orchestrator_template_is_self_contained():
    """编排层 §2.2 是"完整模板"的权威版本，必须**自足可抄**（不依赖别处补齐变量）。

    判据：该段 bash 块里用到的关键变量，都要在本块内出现定义/确认。
    """
    path = os.path.join(SKILLS, "wgs-expressive-pipeline", "SKILL.md")
    text = open(path, encoding="utf-8").read()
    i = text.index("# ─── 阶段收尾：先校验，再决定写哪个标记 ───")
    start = text.rindex("```bash", 0, i) + len("```bash")
    end = text.index("```", i)
    blk = text[start:end]
    for var in ("VERIFIER", "SAMPLES_CSV", "RUN_DIR", "DONE_FILE"):
        assert re.search(rf'^\s*{var}="?', blk, re.M), (
            f"编排层 §2.2 的权威模板用到了 ${{{var}}} 但本块内没有定义它 —— "
            f"LLM 照抄即死（见本文件 docstring 的 A5 复发记录）")


# ======================= 自检：这些判据**真的会红**吗？ =======================

def test_guards_actually_discriminate():
    """负控 + 正控：每条谓词必须对**构造的坏样本**返回 True、对**好样本**返回 False。

    没有这组自检，一条写松的正则会在整个仓库上"全绿"，而它什么也没防住 ——
    正是本项目反复出现的"纸面具备"。注意第二段的**正控**同样重要：
    过宽的正则会命中纠正叙述（如"旧版本这里写的是 X —— 那句话是错的"），
    于是**真正的修复文本反而被拦**，防线会被迫关掉。
    """
    # (谓词, 坏样本, 好样本)
    cases = [
        (_pred_bare_tabix,
         'tabix -p vcf "${GVCF}"',
         'tabix -f -p vcf "${GVCF}"'),
        (_pred_bare_tabix,
         'tabix -p vcf "${VCF}"',
         '⚠ GATK 会自动产出索引，不要手工跑 tabix -p vcf'),
        (_pred_readgroup_auto_claim,
         '| Read Group | 必须手工 `-R` | fq2bam 自动生成（符合 `SM` 即可） |',
         '必须显式传 `--read-group-sm`；不传时自动生成的是**常量** `SM:sample`'),
        (_pred_readgroup_auto_claim,
         'Parabricks generates the Read Group automatically.',
         '旧版本此处写的是「Parabricks generates the Read Group automatically」—— 那句话是错的'),
        (_pred_hardcoded_conda_path,
         'BWA=/home/alice/anaconda3/envs/Agent/bin/bwa',
         '# 工具用 command -v 运行时探测（运行时解析）'),
        (_pred_hardcoded_conda_path,
         "Use conda absolute paths for all tools.",
         '⚠ 旧版本这里写的是「Use conda absolute paths for all tools」—— 与硬规则 #5.1 冲突'),
        # ---- 根因 A：不得推荐派生数据当判定来源（2026-10-28） ----
        # 坏样本 1：本次真实发生过的那条（wgs-expressive-pipeline/SKILL.md 第 15 行的旧文本）
        (_pred_idxstats_as_judgment_source,
         "必须再查 reads 数（`idxstats`）",
         "必须再查 reads 数；⚠️ 查法：`samtools view -c` 全量遍历 BAM 本体，"
         "不是 `idxstats`（读索引）"),
        # 坏样本 2：明说"用 idxstats 判定"
        (_pred_idxstats_as_judgment_source,
         "用 `samtools idxstats` 判定 reads 数与比对率",
         "用 `samtools view -c -F 4` 取 mapped（`idxstats` 只作旁证）"),
        # 好样本：mtime/快路径这类**与判定无关**的 idxstats 提及（实测存在，不得误伤）
        (_pred_idxstats_as_judgment_source,
         "必须再查 reads 数（`idxstats`）",
         "实测 `samtools idxstats` 对四种命名均走快路径、`gatk HaplotypeCaller` 均 rc=0"),
        # 好样本：把 idxstats 本身作为**被警告的对象**
        (_pred_idxstats_as_judgment_source,
         "用 `samtools idxstats` 判定 reads 数与比对率",
         "`samtools idxstats` 读的是索引（派生数据），不是 BAM 本体 —— 会撒谎"),
        # ---- 「怎么跑」配方自足性（2026-10-28） ----
        # 坏样本 1：本次真实发生过的那条（docs/test-matrix.md 第 1 步的旧文本）
        (_pred_pytest_recipe_missing_tool_env,
         "cd /home/chenyuxi/skill/agent && /home/chenyuxi/anaconda3/bin/python -m pytest tests/ -q",
         "export PATH=/home/chenyuxi/anaconda3/envs/Agent/bin:$PATH\n"
         "/home/chenyuxi/anaconda3/bin/python -m pytest tests/ -q"),
        # 坏样本 2：点名 WGS 证据文件、同样缺工具来源
        (_pred_pytest_recipe_missing_tool_env,
         "/home/chenyuxi/anaconda3/bin/python -m pytest tests/test_stage_verify.py -q",
         "PATH=/tmp/nobin:/usr/bin:/bin /home/chenyuxi/anaconda3/bin/python "
         "-m pytest tests/test_stage_verify.py -q"),
        # 好样本：显式声明"本次不跑 WGS 侧"（VERIFY-25 的正规出口），不得误伤。
        # 注意 `SKILL_AGENT_ALLOW_SKIP_WGS_TOOLS` 与 `PATH=` 是**两条**合法出口 ——
        # 只认 PATH= 的话，正规的跳过声明会被当成缺口拦下。
        (_pred_pytest_recipe_missing_tool_env,
         "/home/chenyuxi/anaconda3/bin/python -m pytest tests/ -q",
         "SKILL_AGENT_ALLOW_SKIP_WGS_TOOLS=1 /home/chenyuxi/anaconda3/bin/python "
         "-m pytest tests/ -q"),
        # 好样本：点名单个**非 WGS** 文件 —— 不加载那个文件，本就不需要工具环境。
        # 这一条刻意留着：过宽的正则会把它当成缺口，于是把一整类合法配方全拦下。
        (_pred_pytest_recipe_missing_tool_env,
         "cd /home/chenyuxi/skill/agent && /home/chenyuxi/anaconda3/bin/python -m pytest tests/ -q",
         "cd /home/chenyuxi/skill/agent && /home/chenyuxi/anaconda3/bin/python "
         "-m pytest tests/test_stops.py tests/test_budget_loop.py -q"),
        # 好样本：pytest 只出现在注释里，不是在跑（行尾与整行两种都覆盖）
        (_pred_pytest_recipe_missing_tool_env,
         "/home/chenyuxi/anaconda3/bin/python -m pytest tests/ -q   # 全量",
         "grep -rn 'pytest' docs/    # 看哪些配方提到 pytest"),
        # ---- F18 残余：不得断言"头部/索引能证明有序"（2026-10-28） ----
        # 坏样本 1：本次实测推翻的那条（docs/test-evidence.md §11.2 的旧文本）。
        # 它是「乱序…**不存在**」式**否定断言** —— 只认"…即/故…有序"的正则会漏掉它，
        # 而防线要防的恰恰就是这一条。
        (_pred_order_proven_by_header_or_index,
         "samtools 拒绝给乱序 BAM 建索引，故「乱序且带索引」在现实中不存在",
         "⚠ 实测已推翻：交换两个等长 BGZF 块并保留原 .bai 后，@HD 仍写 SO:coordinate"
         "而本体已乱序 —— 「乱序且带索引」是存在的"),
        # 坏样本 2：把 SO 标签当作排序的证明（这才是会生成错脚本的那种写法）
        (_pred_order_proven_by_header_or_index,
         "@HD 里有 SO:coordinate 即说明 BAM 已坐标排序",
         "`@HD SO` 只是**头部自称**，与本体无关；须拿本体重建索引复核（body_indexable）"),
        # 坏样本 3：由"索引存在"推断有效 —— 证据来自**索引**而非头部标签。
        # 早期版本第一个门槛只认 `SO:coordinate|@HD`，这一类会整片漏掉。
        (_pred_order_proven_by_header_or_index,
         "索引存在因此可判定该 BAM 的排序是正确的",
         "索引是派生数据，不参与判定（它记的是**建它那时**的偏移）"),
        # 坏样本 4：结论挂在"**不是**乱序"上（否定式正面论断）——
        # 若把「不是」也当豁免词就会漏掉它，故豁免集里只收「不能/并非/而非」这类
        # 明确的否定**态度**，不收「不是」。
        (_pred_order_proven_by_header_or_index,
         "SO:coordinate 标签存在即说明该 BAM 不是乱序的",
         "SO 标签**不能**说明该 BAM 不是乱序的（块交换后标签照旧、本体已乱）"),
        # 坏样本 5：证据 + 结论都全，但结论用"表明/正确"这类词
        (_pred_order_proven_by_header_or_index,
         "有 .bai 索引，因此该 BAM 已坐标排序",
         "有 .bai 索引**不代表**该 BAM 已坐标排序（索引可早于篡改而存在）"),
        # 好样本：表格行**明说"不验本体"**——是**告诫**，不是断言。
        # ⚠️ 这一条必须留在**好**样本位上：它同时点了 @HD 与 SO:coordinate，
        # 正则若只按"出现标签即算"来写就会把它拦下，而它是仓库里真实存在的
        # 正确写法 —— 那种过宽的防线会被迫关掉（本文件 docstring 记的老教训）。
        (_pred_order_proven_by_header_or_index,
         "排序判据：`@HD SO:coordinate` 标签即说明 BAM 已有序",
         "| `sorted` | hard | `@HD SO:coordinate` 标签 | 缺标签即拦（不验本体） |"),
        # 好样本：单纯描述头部内容/下游要求，不下"因此有序"的结论（都在仓库里真实存在）
        (_pred_order_proven_by_header_or_index,
         "`@HD SO:coordinate` 即证明排序完成",
         "下游 GATK 要求读入的 BAM 必须是 coordinate-sorted"),
        # 好样本：纠正叙述的典型写法 —— "应以本体判定，**而非**以 SO 标签"
        (_pred_order_proven_by_header_or_index,
         "以 SO 标签判定该 BAM 有序，而非重建索引",
         "应以本体重建索引判定该 BAM 有序，而非以 SO 标签"),
        # 好样本：报错信息的复述（samtools 自己拒绝乱序），不是断言"不存在"
        (_pred_order_proven_by_header_or_index,
         "samtools 因乱序而拒绝建索引，故该 BAM 是有序的",
         "samtools index 对乱序 BAM 报 [E::hts_idx_push] Unsorted positions"),
        # ---- F05 残余：pbrun 调用不得与 §4.1 契约相矛盾（2026-10-28） ----
        # （命令式谓词要多传一个推导出来的集合，单列在下面 cases_contract）
    ]
    # pbrun 契约谓词单列（要多传 §4.1 推导出来的 rejected/accepted）
    REJECTED, ACCEPTED = _pbrun_contract()
    cases_contract = [
        # 坏样本 1：v0.2.0 模板的原形（`--num-gpus 1` 加在 genotypegvcf 上）。
        # ⚠️ 该参数排在**最后**、中间隔着五六个参数与若干续行 —— 逐物理行看会整片漏掉，
        # 故判据必须先并逻辑行（`_pbrun_logical_lines`）。
        (_pred_rejected_tool_takes_flag,
         'PBRUN "${GPU_IDX}" genotypegvcf --ref "${REF}" --in-gvcf "${C}" '
         '--out-vcf "${J}" --num-threads "${N}" --num-gpus 1', True),
        # 坏样本 2：对照组 —— 同一个参数加在**接受**它的兄弟工具上是对的，不得误伤。
        # 这条证明"被拒集"真的在起作用，而不是"凡 --num-gpus 就拦"。
        (_pred_rejected_tool_takes_flag,
         'PBRUN "${GPU_IDX}" fq2bam --ref r --in-fq a b --out-bam o.bam --num-gpus 1', False),
        # 好样本：纠正叙述**必须**能把错写法逐字写出来（否则防线逼得人删证据）。
        # 这两条都是仓里真实存在的句子，逐字取自 joint-genotyping SKILL.md。
        (_pred_rejected_tool_takes_flag,
         "> 该子命令是唯一**不接受**这个参数的 —— `pbrun genotypegvcf --num-gpus 1 ...`",
         False),
        (_pred_rejected_tool_takes_flag,
         "> 当场 `rc=2`、`pbrun: error: unrecognized arguments: --num-gpus 1`、`out/` 零产物",
         False),
        # 好样本：修正后的模板（该参数已删）
        (_pred_rejected_tool_takes_flag,
         'PBRUN "${GPU_IDX}" genotypegvcf --ref "${REF}" --in-gvcf "${C}" '
         '--out-vcf "${J}" --num-threads "${N}"', False),
    ]
    # 段落级谓词单列（要多传两个推导出来的集合）
    REJ, ACC = {"genotypegvcf"}, {"fq2bam", "haplotypecaller", "markdup", "bqsr", "applybqsr"}
    cases_para = [
        # 坏样本 1：本次真实修好的那处（params-propose SKILL.md:144 的旧文本）
        ("1. `--gpus \"device=<宿主卡号>\"` 只暴露 1 张卡 → "
         "**容器内 pbrun 一律 `--num-gpus 1`**", True),
        # 坏样本 2：parabricks-setup §4 约定（旧文本）
        ("1. **`--gpus device=<宿主卡号>`** 只把那一张卡暴露进容器 → "
         "**容器内 pbrun 用 `--num-gpus 1`**", True),
        # 坏样本 3：server-detect 核心原则（旧文本）
        ("5. **GPU 存在且 Parabricks 可用时，优先走 GPU 路径**：样本并行由 GPU 卡数主导，"
         "每个 pbrun 任务绑 1 张卡（`--num-gpus 1`）", True),
        # 坏样本 4：**照抄面** —— 各阶段 skill 头部的样板注释（旧文本，逐字复制了三份）。
        # 这条是当初实际扩散的媒介：它跟着模板走，照抄进新生成的阶段脚本。
        ("# 每样本: PBRUN <宿主GPU索引> <pbrun 子命令> ... ；容器内只暴露 1 张卡"
         " → pbrun 用 --num-gpus 1", True),
        # 坏样本 5：英文形态（usage-guide.md 的旧文本）
        ("container sees one card, so pbrun uses `--num-gpus 1`:", True),
        # 坏样本 6：把"所有子命令"写死的强断言 —— 正是它后来被 genotypegvcf 证伪
        ("All pbrun subcommands take `--num-gpus`; container sees one card "
         "so pass `--num-gpus 1`.", True),
        # 坏样本 7：⚠️ **变异 2 当场抓到的洞** —— 主张行**下方紧跟**一句无关的
        # `--gpu` 告诫。按**整段**判时那句「⚠ 没有 --gpu 参数」把整片豁免掉，
        # 删掉 genotypegvcf 例外后 25 条参数化用例仍全绿。改成按**主张窗口**判才守住。
        # 这一条必须留着：它是这个谓词从"段级"退到"行窗"的唯一理由。
        ("1. `--gpus \"device=<宿主卡号>\"` 只暴露 1 张卡 → "
         "**容器内 pbrun 一律 `--num-gpus 1`**\n"
         "   （⚠ 4.3.x **没有** `--gpu` 参数：`--gpu 0` 会与前缀歧义报错）", True),
        # 好样本 1：修复后的写法 —— 泛化 + 点名例外
        ("1. `--gpus \"device=<宿主卡号>\"` 只暴露 1 张卡 → "
         "**容器内 pbrun 默认 `--num-gpus 1`**（⚠️ 例外只有一个：**`genotypegvcf` "
         "不接受该参数**）", False),
        # 好样本 2：修复后的**照抄面**样板注释（三份阶段 skill 已统一成这样）
        ("# 每样本: PBRUN <宿主GPU索引> <pbrun 子命令> ... ；容器内只暴露 1 张卡"
         " → pbrun 用 --num-gpus 1（⚠️ 唯一例外：genotypegvcf 不接受该参数，"
         "见 parabricks-setup.md §4.1）", False),
        # 好样本 3：把主语限定到**具体工具**（setup.md §4 的 fq2bam 关卡脚本）
        ("# 关卡 2：fq2bam（比对+排序+索引）—— 容器只暴露 1 卡 → pbrun 选卡用 "
         "--num-gpus 1（4.3.x 无 --gpu）", False),
        # 好样本 4：**实测记录**式的提及（§4.1 表下的命中数）—— 不是给读者的指令。
        # 若把它当缺陷，防线就会逼人删掉核对证据。
        ("`pbrun <tool> -h` 的 `--num-gpus` 命中数分别为 `1/1/1/1/1/0`）", False),
        # 好样本 5：工具并列行，讲的是**选卡**不是给 pbrun 传参
        ("# 每张 GPU 配套的 CPU 线程（fq2bam 用 --num-cpu / haplotypecaller 用 "
         "--num-htvc-threads / 选卡统一 --num-gpus）", False),
        # 好样本 6：点名单个工具的检查清单项（受本 skill 工具限定，非通例）
        ("13. ✅ GPU 路径：`--out-bam` 符合契约、每样本绑 1 卡（`--num-gpus`）、"
         "`pbrun fq2bam --help` 复核参数名", False),
        # 好样本 7：**跨段落**的续行形态 —— 例外写在紧邻的下一行，
        # 行窗必须把它收进来（否则修复后的真文档会被误伤）
        ("1. `--gpus \"device=<宿主卡号>\"` 只暴露 1 张卡 → "
         "**容器内 pbrun 默认 `--num-gpus 1`**\n"
         "   （⚠️ 例外只有一个：**`genotypegvcf` 不接受该参数**）", False),
    ]
    # 块级谓词单列（签名不同）
    cases_ok = [
        (_block_missing_samples_csv,
         ("python \"${VERIFIER}\" --samples \"${SAMPLES_CSV}\"", False), True),
        (_block_missing_samples_csv,
         ('SAMPLES_CSV="$(echo 1)"\npython "${VERIFIER}" --samples "${SAMPLES_CSV}"', False), False),
        (_block_missing_samples_csv,
         ("python \"${VERIFIER}\" --samples \"${SAMPLES_CSV}\"", True), False),
        (_block_missing_stale_failed_cleanup,
         ('date > "${DONE_FILE}"   # 写 .done 标记', ), True),
        (_block_missing_stale_failed_cleanup,
         ('date > "${DONE_FILE}"\nrm -f "${RUN_DIR}/.failed/01_x.failed"', ), False),
        # ---- _strip_comment：接线判据必须建在**可执行行**上（2026-10-28 变异①） ----
        # 坏样本：参数只在**注释**里出现过（真参数被删）—— 整块子串匹配会漏报这一条。
        # 这正是 align-sort 调用点的真实形状：警示注释在上、真参数在下。
        (_block_missing_prev_reads_flag,
         ("# ⚠️ 必须传 --prev-reads-file（指向 Stage 0.5 的报告）\n"
          'python "${VERIFIER}" --stage 01_bwa_sort --run-dir "${RUN_DIR}"; then',
          {"01_bwa_sort"}), True),
        # 好样本：同样的注释，但真参数**在**可执行行上
        (_block_missing_prev_reads_flag,
         ('# ⚠️ 必须传 --prev-reads-file\n'
          'python "${VERIFIER}" --stage 01_bwa_sort \\\n'
          '    --prev-reads-file "${PREV_QC}"; then', {"01_bwa_sort"}), False),
        # 好样本：`#` 在引号内不算注释（echo 的是文本，不是注释）
        (_block_missing_prev_reads_flag,
         ('echo "见 a # b"\npython "${VERIFIER}" --stage 01_bwa_sort; then',
          {"01_bwa_sort"}), True),
        (_block_missing_prev_reads_flag,
         ('echo "见 a # b"\npython "${VERIFIER}" --stage 01_bwa_sort '
          '--prev-reads-file x; then', {"01_bwa_sort"}), False),
        # 好样本：不消费该参数的 stage（05_filtering）—— 不该被拦
        (_block_missing_prev_reads_flag,
         ('python "${VERIFIER}" --stage 05_filtering --prefix p', {"01_bwa_sort"}), False),
    ]

    bad_missed = []
    for pred, bad, good in cases:
        if not pred(bad):
            bad_missed.append(f"**漏报**（坏样本没被抓到）: {pred.__name__} ← {bad[:70]!r}")
        if pred(good):
            bad_missed.append(f"**误伤**（好样本被拦）: {pred.__name__} ← {good[:70]!r}")
    for pred, args, expected in cases_ok:
        got = pred(*args)
        if got != expected:
            bad_missed.append(
                f"{'**漏报**' if expected else '**误伤**'}: {pred.__name__}({args!r}) "
                f"= {got}，期望 {expected}")
    for pred, sample, expected in cases_contract:
        got = pred(sample, REJECTED)
        if got != expected:
            bad_missed.append(
                f"{'**漏报**' if expected else '**误伤**'}: {pred.__name__}({sample[:60]!r}) "
                f"= {got}，期望 {expected}")
    for sample, expected in cases_para:
        got = _pred_blanket_flag_claim(sample.splitlines(), 0, REJECTED, ACCEPTED)
        if got != expected:
            bad_missed.append(
                f"{'**漏报**' if expected else '**误伤**'}: "
                f"_pred_blanket_flag_claim({sample[:60]!r}) = {got}，期望 {expected}")
    assert not bad_missed, "判据失去鉴别力（等于纸面具备）：\n  " + "\n  ".join(bad_missed)
