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
import glob
import os
import re

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKILLS = os.path.join(REPO, "skills")


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


DOCS = all_skill_docs()


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


def test_doc_discovery_is_not_empty():
    """前提自检：自动发现若扫不到文件，下面每条防线都会空转"全绿"。"""
    assert len(DOCS) >= 10, f"只发现 {len(DOCS)} 份文档，路径规则变了？{sorted(DOCS)[:5]}"


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


def _block_missing_samples_csv(blk, doc_has_assignment):
    """块里用了 `${SAMPLES_CSV}` 但全文档都没有它的赋值 ⇒ A5 复发。"""
    if "${SAMPLES_CSV}" not in blk:
        return False
    if re.search(r'^\s*SAMPLES_CSV="\$\(', blk, re.M):
        return False
    return not doc_has_assignment


def _block_missing_stale_failed_cleanup(blk):
    """块写 `DONE_FILE` 却没有 `rm -f .../.failed/...` ⇒ 陈旧失败标记会残留。"""
    if not re.search(r">\s*\"?\$\{?DONE_FILE", blk) and "DONE_FILE}" not in blk:
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


# ======================= 防线：扫全仓 =======================

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
    assert not bad_missed, "判据失去鉴别力（等于纸面具备）：\n  " + "\n  ".join(bad_missed)
