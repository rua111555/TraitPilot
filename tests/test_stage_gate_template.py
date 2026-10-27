# -*- coding: utf-8 -*-
"""阶段脚本收尾段（`VERIFIER` 定位 → 校验门禁 → 写标记）的集成测试。

为什么这些必须在**真 bash** 里跑而不能只做文本断言：
这一段是 shell，它的失效方式全是**控制流**问题 —— `set -e` 在哪一行退出、
`$?` 传没传下去、`if` 有没有机会执行。文本断言（"文件里有 if 就通过"）
恰恰是"纸面具备"的来源：写着的代码未必会执行。本项目已实测踩到两次：

  · `: "${VERIFIER:?}"` 在变量为空时**立刻 exit 1**，绕过下面写 `.failed` 的兜底
  · `VAR="$(cmd)"` 是**赋值**语句，cmd 失败时 `set -e` 当场退出，
    连 `if [ -z "${VAR}" ]` 都到不了

两者都表现为「磁盘上既无 .done 也无 .failed」→ 编排层四态判定归为"已中断"，
而真实原因是配置读不到（环境问题），用户被引去查进程。
"""
import os
import re
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKILLS = os.path.join(REPO, "skills")
BASH = "/bin/bash"

# 六个阶段 skill + 编排层：都带一份收尾段模板
TEMPLATE_FILES = {
    "skills/wgs-expressive-pipeline/SKILL.md": "01_bwa_sort",
    "skills/wgs-express-align-sort/SKILL.md": "01_bwa_sort",
    "skills/wgs-express-fastp-qc/SKILL.md": "00_fastp_qc",
    "skills/wgs-express-markdup-bqsr/SKILL.md": "02_markdup_bqsr",
    "skills/wgs-express-haplotypecaller/SKILL.md": "03_haplotypecaller",
    "skills/wgs-express-joint-genotyping/SKILL.md": "04_joint_genotyping",
    "skills/wgs-express-filtering/SKILL.md": "05_filtering",
}


def extract_verifier_block(path):
    """从 SKILL.md 里抽出「定位校验器 → 写 .failed 兜底」那段 shell。

    起于 `VERIFIER="$(python`，止于紧随其后的 `fi`。抽不出来就 fail ——
    那说明模板结构变了，本测试必须跟着改（而不是静默跳过、假装还在测）。

    ⚠️ 本函数**只取门禁那一段**，不含同围栏里更靠前的代码（align-sort 的同围栏
    前缀是注释，但 fastp-qc 的同围栏前缀是**真的 fastp 循环** —— 整段跑起来会去调
    fastp）。门禁段运行所需的 `log_*` 由 `extract_log_definitions()` 单独供给。
    """
    text = open(path, encoding="utf-8").read()
    i = text.find('VERIFIER="$(python')
    assert i >= 0, f"{path} 里找不到 VERIFIER= 定位段（模板结构变了？）"
    j = text.find("\nfi\n", i)
    assert j > i, f"{path} 里找不到该段的收尾 fi"
    return text[i:j + 4]


def extract_log_definitions(path):
    """抽出该文档里**自己定义的** `log_*` shell 函数行。

    ⚠️ 为什么需要它（第二轮审计 major）：`build_stage_script` 早期**硬编码注入**了一个
    假的 `log_error() { echo ...; }`。骨架里既然已经有了定义，"模板自己有没有定义
    log_error"这个问题就**永远不会被问到** —— 而六份子 skill 里当时只有 2 份定义过
    它，其余都在失败分支里裸调 `log_error`（首次调用即 `command not found` rc=127，
    崩在写 .failed 之前 → 磁盘上两个标记都没有 → 编排层误判"已中断"）。
    本测试的假函数正是把这个漏洞盖住的那层纸。

    修法：不注入假的，改用**文档自己的**定义。某份文档若没定义，骨架里就没有 ——
    测试当场以 rc=127 失败，漏洞变成可观测的。定义在文档哪个围栏里不重要
    （align-sort/fastp-qc 在配置段、其余四份在门禁段），只要**本文档**里有即可。
    """
    text = open(path, encoding="utf-8").read()
    return [ln for ln in text.splitlines()
            if re.match(r"^log_[a-z_]+\(\)\s*\{.*\}\s*$", ln)]


def build_stage_script(path, tmp):
    """把抽出的段包进最小骨架，变成可直接 bash 执行的脚本。

    ⚠️ `log_*` 用**该文档自己定义的**，不再硬编码注入假的（见
    `extract_log_definitions` 的说明）：注入假函数会让"模板是否自足"永远测不到。
    """
    script = os.path.join(tmp, "stage.sh")
    with open(script, "w", encoding="utf-8") as f:
        f.write("set -euo pipefail\n"
                + "".join(ln + "\n" for ln in extract_log_definitions(path))
                + 'RUN_DIR="$1"\n'
                + extract_verifier_block(path)
                + '\necho "REACHED_VERIFIER_CHECK"\n')
    return script


def run_stage(script, run_dir):
    p = subprocess.run([BASH, script, run_dir], capture_output=True, text=True)
    return p.returncode, p.stdout + p.stderr


MALFORMED = {
    "no_run_params": None,                                        # 文件不存在
    "bad_json": "{ broken",                                       # 非法 JSON
    "no_verifier_key": '{"samples": ["s1"]}',                     # 缺 verifier_path
    "empty_verifier": '{"verifier_path": ""}',                    # 空串
    "verifier_missing_file": '{"verifier_path": "/nonexistent/x.py"}',  # 指向不存在的文件
}


@pytest.mark.parametrize("shape,content", sorted(MALFORMED.items()))
@pytest.mark.parametrize("rel", sorted(TEMPLATE_FILES))
def test_every_malformed_config_writes_failed(tmp_path, rel, shape, content):
    """★核心：任何**定位不到校验器**的配置都必须留下 `.failed`。

    判据是磁盘真值（`.failed` 文件在不在），不是退出码 —— 退出码非 0 是对的，
    但只 exit 1 不写 `.failed` 时编排层会把"环境问题"误判成"已中断"。
    """
    stage = TEMPLATE_FILES[rel]
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    if content is not None:
        (run_dir / "run.params.json").write_text(content)

    script = build_stage_script(os.path.join(REPO, rel), str(tmp_path))
    rc, out = run_stage(script, str(run_dir))

    failed = run_dir / ".failed" / f"{stage}.failed"
    assert rc != 0, f"{rel} [{shape}]: 竟然成功退出（rc=0），配置问题被当成正常完成"
    assert failed.is_file(), (
        f"{rel} [{shape}]: 退出码 {rc} 但**没写 .failed** —— 编排层会判成"
        f"「已中断」并引用户去查进程。输出：{out[-400:]}")
    body = failed.read_text()
    assert "reason=" in body, f"{rel} [{shape}]: .failed 里没有 reason，排查时无从下手"
    assert "REACHED_VERIFIER_CHECK" not in out, \
        f"{rel} [{shape}]: 配置不可用却走到了校验调用"


@pytest.mark.parametrize("rel", sorted(TEMPLATE_FILES))
def test_healthy_config_reaches_verifier(tmp_path, rel):
    """阳性对照：配置正常时必须**走到**校验器调用（证明上面不是"永远写 .failed"）。"""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    real = tmp_path / "real_verifier.py"
    real.write_text("import sys\n"
                    "print('VERIFIER_WAS_CALLED')\n"
                    "sys.exit(0)\n")
    (run_dir / "run.params.json").write_text(
        '{"verifier_path": "%s"}' % str(real).replace("\\", "\\\\"))

    script = build_stage_script(os.path.join(REPO, rel), str(tmp_path))
    rc, out = run_stage(script, str(run_dir))

    assert rc == 0, f"{rel}: 健康配置非零退出（rc={rc}）—— 门禁模板本身会把好数据拦下。{out[-400:]}"
    assert "REACHED_VERIFIER_CHECK" in out, f"{rel}: 没走到校验段"
    assert not (run_dir / ".failed").exists(), f"{rel}: 健康配置却写了 .failed"


@pytest.mark.parametrize("rel", sorted(TEMPLATE_FILES))
def test_no_bare_colon_question_guard(rel):
    """回归防线：`${VERIFIER:?}` 早失败写法不得回来。

    它本身"看起来更安全"（早失败），代价是绕过 .failed 兜底 ——
    这正是被替换掉的原因，容易被后来者以"简化"为名改回去。
    """
    text = open(os.path.join(REPO, rel), encoding="utf-8").read()
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("#"):
            continue
        assert ': "${VERIFIER:?' not in line, (
            f"{rel}: 又出现了 `: \"${{VERIFIER:?...}}\"` —— 它会在变量为空时"
            f"立刻退出、绕过写 .failed 的兜底（见本文件 docstring）")


@pytest.mark.parametrize("rel", sorted(TEMPLATE_FILES))
def test_assignments_are_set_e_safe(rel):
    """回归防线：`VERIFIER="$(...)"` / `SAMPLES_CSV="$(...)"` 必须带 `|| true`。

    赋值语句在 `set -e` 下同样会因命令替换失败而退出整个脚本 —— 那时
    下面的 `if [ -z ... ]` 兜底根本执行不到。实测：无 run.params.json 与
    非法 JSON 两种形态正是这样漏掉的（前 4 种形态中它们占了 2 种）。
    """
    text = open(os.path.join(REPO, rel), encoding="utf-8").read()
    for var in ("VERIFIER", "SAMPLES_CSV"):
        for m in re.finditer(r'^%s="\$\(python.*$' % var, text, re.M):
            assert "|| true" in m.group(0), (
                f"{rel}: `{var}=\"$(...)\"` 缺少 `|| true` —— "
                f"set -e 下命令替换失败会直接退出，兜底分支到不了：\n  {m.group(0)}")


def test_all_templates_use_the_same_guard_shape():
    """七份模板的守卫形态应一致（不一致意味着某份漏改）。"""
    shapes = {}
    for rel in TEMPLATE_FILES:
        text = open(os.path.join(REPO, rel), encoding="utf-8").read()
        i = text.find('VERIFIER="$(python')
        j = text.find("\nfi\n", i)
        seg = text[i:j]
        has_empty_check = "[ -z \"${VERIFIER}\" ]" in seg
        has_isfile = "[ ! -f \"${VERIFIER}\" ]" in seg
        shapes[rel] = (has_empty_check, has_isfile)
    bad = {k: v for k, v in shapes.items() if v != (True, True)}
    assert not bad, f"这些模板的守卫不是「判空 + 判存在」两段式：{bad}"


@pytest.mark.parametrize("rel", sorted(TEMPLATE_FILES))
def test_template_defines_every_log_function_it_calls(rel):
    """★本轮新增防线：模板调用的每个 `log_*` 必须在**本文档内**有定义。

    为什么这条重要（第二轮审计 major，实测确认）：门禁段的 `log_error` 调用
    **全在失败分支里**。某份文档只调用不定义时，LLM 照抄生成的脚本在"本该写
    `.failed` 并打印原因"的路径上反而崩在写标记之前（`command not found`，
    rc=127）—— 磁盘上 `.done`/`.failed` 都不存在，编排层四态判定归为"已中断"，
    引用户去查一个不存在的进程。**失败报告路径本身的失败**是最难自查的一类：
    它只在已经出错时才出现，而"没有标记"又被归因到别处。

    实测：本测试原先在骨架里硬编码注入了一个假的 `log_error()`，于是这个问题
    永远问不出口。改为用文档自己的定义后，本条即是那条缺失的判据。
    """
    text = open(os.path.join(REPO, rel), encoding="utf-8").read()
    i = text.find('VERIFIER="$(python')
    j = text.find("\nfi\n", i)
    seg = text[i:j + 4]
    called = set(re.findall(r"\b(log_[a-z_]+)\b", seg))
    if not called:
        pytest.skip(f"{rel}: 门禁段未调用任何 log_* 函数")
    defined = set(re.findall(r"^(log_[a-z_]+)\(\)\s*\{", text, re.M))
    missing = sorted(called - defined)
    assert not missing, (
        f"{rel}: 门禁段调用了 {missing}，但**本文档内没有定义** —— "
        f"照抄生成的脚本执行到该行会 `command not found`（rc=127）。"
        f"而这些调用全在失败分支里：本该写 .failed 的路径反而崩在写标记之前，"
        f"磁盘上两个标记都没有，编排层会误判成「已中断」。"
        f"本文档已定义：{sorted(defined)}")
