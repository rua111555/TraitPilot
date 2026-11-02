# -*- coding: utf-8 -*-
"""编排层硬门禁：`.done` 必须配一份**可采信**的 `.verify/<stage>.json`。

背景（这条防线为什么必须存在）
------------------------------
阶段脚本的收尾模板要求"先跑 `stage_verify.py`，通过才写 `.done`"。但阶段脚本是
**LLM 现场生成**的，模板只是模板 —— 以下四种形态都会留下一个孤立的 `.done`：

  · 漏抄收尾段的脚本（模型没照模板写，或写漏了一行）
  · **旧版本**生成的脚本（那一版还没有门禁）
  · 手工 `touch` / 复制运行目录带来的标记
  · 校验通过了，但报告在清理/传输中被删掉

只守脚本那一端，编排层就会把"没验证过"当成"验证过"。这正是本项目反复讲的
"纸面具备"换了个宿主：**承诺**写在模板里，**可执行性**没有任何运行时证据。
所以编排层必须自己再核一遍报告，四类一律不采信：

  missing        → 报告不存在，或不是合法 JSON
  stage_mismatch → 报告里的 `stage` 与本阶段不符（拿到了别的阶段的报告）
  blocked        → 报告 `pass=false`，却写了 `.done`（标记与报告互相矛盾）
  empty          → `checks` 为空（一条判据都没跑，等于没校验）

为什么必须在**真 bash** 里跑
----------------------------
这段是 shell 的控制流：`continue` 有没有生效、`set -e` 会不会在中途退出、
`${verdict}` 为空时走哪条分支 —— 文本断言（"文件里有 ⛔ 就通过"）全都看不出来。
本仓库已有两次栽在这上面（见 `test_stage_gate_template.py` 的 docstring："写着的
代码未必会执行"）。故这里把两份文档里的状态检查块**抽出来真跑**，判据是 stdout
与退出码，不是源码里的字面量。

三处都测的理由：用户从哪条路径查状态，取决于对话框当时开着还是关着、跑的是
WGS 还是 GWAS —— §2.5 自动推进（WGS SKILL.md）、场景 B 恢复检查（workflow.md）
与 GWAS 编排层的场景 B。只改一处等于没改：其余路径仍会把孤立的 `.done` 报成
「✅ 已完成」。
"""
import json
import os
import re
import subprocess

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASH = "/bin/bash"

# 每个站点挑一个**已接入门禁**的阶段名：
#   · WGS 六阶段全部门禁 → 01_bwa_sort
#   · GWAS 目前只接入 S0/S0.5 → 00_format_convert
#     （S1–S5 走"未接入"分支，见 test_ungated_gwas_stage_is_marked_not_verified）
STAGE_FOR = {
    "skills/wgs-expressive-pipeline/SKILL.md": "01_bwa_sort",
    "skills/wgs-expressive-pipeline/references/workflow.md": "01_bwa_sort",
    "skills/gwas-pipeline/SKILL.md": "00_format_convert",
}

# 三处状态检查块（都必须带硬门禁）
DOCS = {
    "skills/wgs-expressive-pipeline/SKILL.md": "§2.5 自动推进 / 场景 B 恢复检查",
    "skills/wgs-expressive-pipeline/references/workflow.md": "恢复连接检查",
    "skills/gwas-pipeline/SKILL.md": "GWAS 编排层场景 B 恢复检查",
}


def report_ok(stage):
    """一份合法报告（`stage_verify.py` / `gwas_verify.py` 的 as_dict 形状）。

    只给编排层门禁真正读的键：`stage` / `pass` / `checks`。
    """
    return {
        "stage": stage, "run_dir": "/tmp/run", "pass": True,
        "missing_tools": [], "hard_failures": [], "warnings": [],
        "checks": [{"name": "reads", "level": "hard", "passed": True,
                    "detail": "ok"}],
    }


def extract_loop(rel):
    """抽出文档里的「逐阶段查状态」循环（`for stage_script in ...; do ... done`）。

    抽不出来就 fail（结构变了，本测试必须跟着改），不静默跳过 —— 静默跳过会让
    这条防线在文档重构后变成空转，而那正是它要防的形态。
    """
    path = os.path.join(REPO, rel)
    lines = open(path, encoding="utf-8").read().splitlines()
    start = next((i for i, ln in enumerate(lines)
                  if ln.strip().startswith("for stage_script in")), None)
    assert start is not None, f"{rel} 里找不到状态检查循环（文档结构变了？）"
    end = next((i for i in range(start + 1, len(lines))
                if lines[i].strip() == "done"), None)
    assert end is not None, f"{rel} 里循环没有收尾 `done`"
    return "\n".join(lines[start:end + 1])


def build_script(rel, tmp, strict=False):
    """把抽出的循环包成可执行脚本；`strict=True` 用 `set -euo pipefail` 复现模板环境。"""
    script = os.path.join(str(tmp), "check.sh")
    header = "set -euo pipefail\n" if strict else ""
    with open(script, "w", encoding="utf-8") as f:
        f.write(header + 'RUN_DIR="$1"\n' + extract_loop(rel) + "\n")
    return script


def make_run(tmp, *, stage, done=True, report=None, failed=False, pid_alive=False):
    """构造一个最小 RUN_DIR：`scripts/` 里有一个阶段脚本就够了（循环按它枚举）。"""
    run = os.path.join(str(tmp), "run")
    for sub in ("scripts", ".done", ".verify", ".pid", ".failed", "logs"):
        os.makedirs(os.path.join(run, sub), exist_ok=True)
    with open(os.path.join(run, "scripts", stage + ".sh"), "w",
              encoding="utf-8") as f:
        f.write("#!/bin/bash\n")
    if done:
        with open(os.path.join(run, ".done", stage + ".done"), "w",
                  encoding="utf-8") as f:
            f.write("2026-10-30 12:00:00\n")
    if failed:
        with open(os.path.join(run, ".failed", stage + ".failed"), "w",
                  encoding="utf-8") as f:
            f.write("stage=%s\nreason=reads=0\n" % stage)
    if pid_alive:
        # 用**本进程**的 pid：测试进程一定活着，`kill -0` 必然成功
        with open(os.path.join(run, ".pid", stage + ".pid"), "w",
                  encoding="utf-8") as f:
            f.write(str(os.getpid()))
    if report is not None:
        body = report if isinstance(report, str) else json.dumps(
            report, ensure_ascii=False, indent=2)
        with open(os.path.join(run, ".verify", stage + ".json"), "w",
                  encoding="utf-8") as f:
            f.write(body)
    return run


def run_block(rel, tmp, *, strict=False, stage=None, **kw):
    stage = stage or STAGE_FOR[rel]
    run_dir = make_run(tmp, stage=stage, **kw)
    script = build_script(rel, tmp, strict=strict)
    p = subprocess.run([BASH, script, run_dir], capture_output=True, text=True)
    return p.returncode, p.stdout + p.stderr


# --------------------------------------------------------------------------- #
# 1) 四类"不可采信"的报告都不能推进
# --------------------------------------------------------------------------- #
UNTRUSTWORTHY = {
    # name:            (按阶段名生成报告的工厂, 期望出现在输出里的结论码)
    "no_report": (lambda st: None, "missing"),
    "unparsable": (lambda st: "{ broken", "missing"),
    "stage_mismatch": (lambda st: {**report_ok(st), "stage": st + "_other"},
                       "stage_mismatch"),
    "pass_false": (lambda st: {**report_ok(st), "pass": False,
                               "hard_failures": ["reads"]}, "blocked"),
    "empty_checks": (lambda st: {**report_ok(st), "checks": []}, "empty"),
}


@pytest.mark.parametrize("rel", sorted(DOCS))
@pytest.mark.parametrize("name", sorted(UNTRUSTWORTHY))
def test_done_without_trustworthy_report_does_not_advance(tmp_path, rel, name):
    """★核心：`.done` 在、但报告不可采信 → **不算完成、不推进**。

    三条断言缺一不可：
      · 必须出现 ⛔（明确判成"未校验"）；
      · 必须**不**出现 ✅（否则用户会照旧推进下一阶段）；
      · 必须写明是哪一类（missing/stage_mismatch/blocked/empty）——
        否则用户不知道该重跑门禁，还是该整个阶段重跑。
    """
    make, code = UNTRUSTWORTHY[name]
    rc, out = run_block(rel, tmp_path, report=make(STAGE_FOR[rel]))

    assert rc == 0, f"{rel} [{name}]: 状态检查自身异常退出（rc={rc}）—— 报告不可采信不该让它崩。{out}"
    assert "⛔" in out, f"{rel} [{name}]: 没判成「未校验」。输出：{out}"
    assert "✅" not in out, (
        f"{rel} [{name}]: 竟然报「✅ 已完成」—— 孤立的 `.done` 被当成了完成，"
        f"编排层会据此推进下一阶段。输出：{out}")
    assert code in out, (
        f"{rel} [{name}]: 没写明不可采信的原因（期望含 {code!r}），"
        f"用户无从知道该重跑门禁还是重跑阶段。输出：{out}")


# --------------------------------------------------------------------------- #
# 2) 阳性对照：报告可采信时必须判「已完成」（证明上面不是"永远 ⛔"）
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("rel", sorted(DOCS))
def test_trustworthy_report_is_judged_done(tmp_path, rel):
    rc, out = run_block(rel, tmp_path, report=report_ok(STAGE_FOR[rel]))
    assert rc == 0
    assert "✅" in out, f"{rel}: 报告齐全却判成未完成（把好数据拦下）。输出：{out}"
    assert "⛔" not in out, f"{rel}: 报告齐全却被判成未校验。输出：{out}"


def test_ungated_gwas_stage_is_marked_not_verified(tmp_path):
    """**尚未接入校验器**的阶段 → 必须如实报「🟡 已完成但未接入校验」。

    ⚠️ 这是 GWAS 编排层的**安全阀**：`GATED_STAGES` 之外的阶段（将来新增、或校验器
    还没写好的）若被套上硬门禁，会永久判成「未校验」而**卡死**；若一律放行成 ✅，
    又等于假装它们被校验过。两者都不行 —— 所以用独立状态「🟡 完成但未接入」，
    既推进也**明确不声称已校验**。
    （S0–S5 现在已全部接入，故这里用一个不在注册表里的未来阶段名。）
    """
    rc, out = run_block("skills/gwas-pipeline/SKILL.md", tmp_path,
                        stage="09_future_stage", report=None)
    assert rc == 0, f"状态检查异常退出：{out}"
    assert "🟡" in out and "尚未接入结果校验" in out, f"没如实报「未接入」：{out}"
    assert "✅" not in out, f"把未接入校验的阶段报成了「已完成」：{out}"
    assert "⛔" not in out, f"把未接入校验的阶段误报成「校验未通过」：{out}"


def test_auto_advance_site_carries_warnings(tmp_path):
    """warn 项必须被带进汇报（§2.5 的既有约定）——但不影响"已完成"的判定。

    只测 SKILL.md：它是**汇报**路径；workflow.md 是恢复查询，只回状态，不承担汇报。
    这个不对称是刻意的，写在这里免得后来者把它当成漏改。
    """
    report = {**report_ok("01_bwa_sort"), "warnings": ["bam.mapped_rate 0.71 < 0.90"]}
    rc, out = run_block("skills/wgs-expressive-pipeline/SKILL.md", tmp_path,
                        report=report)
    assert rc == 0
    assert "✅" in out and "⛔" not in out
    assert "bam.mapped_rate 0.71 < 0.90" in out, (
        f"软告警没带进汇报 —— 报告全绿会让人以为已把关。输出：{out}")


# --------------------------------------------------------------------------- #
# 3) 既有判序不能被这次改动打乱（回归）
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("rel", sorted(DOCS))
def test_failed_marker_still_wins(tmp_path, rel):
    """`.failed` 最优先：即使 `.done` 也在、报告也没有，也该报「校验未通过」。"""
    rc, out = run_block(rel, tmp_path, failed=True, report=None)
    assert rc == 0
    assert "❌" in out, f"{rel}: .failed 没优先。输出：{out}"
    assert "⛔" not in out and "✅" not in out, f"{rel}: 判定串了。输出：{out}"


@pytest.mark.parametrize("rel", sorted(DOCS))
def test_alive_pid_still_wins(tmp_path, rel):
    """pid 存活优先于 `.done`：陈旧 `.done` + 存活进程必须报「运行中」。"""
    rc, out = run_block(rel, tmp_path, pid_alive=True, report=None)
    assert rc == 0
    assert "🕐" in out, f"{rel}: 存活进程没被优先判成运行中。输出：{out}"
    assert "⛔" not in out and "✅" not in out, f"{rel}: 判定串了。输出：{out}"


# --------------------------------------------------------------------------- #
# 4) `set -euo pipefail` 下不能因为报告缺失而中途崩掉
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("rel", sorted(DOCS))
def test_block_survives_set_e(tmp_path, rel):
    """模板环境是 `set -euo pipefail`：报告缺失不得让**状态检查**自己退出。

    这一条是给新写的 `verdict=$(python -c ...)` 兜底的：命令替换失败在 `set -e` 下
    会让整块当场退出 —— 那时用户看到的是"没有任何状态输出"，比误判更难查。
    本仓库已有两次同形事故（`VERIFIER="$(...)"` 与 `: "${VERIFIER:?}"`）。
    """
    rc, out = run_block(rel, tmp_path, report=None, strict=True)
    assert rc == 0, f"{rel}: `set -e` 下报告缺失让状态检查崩了（rc={rc}）。输出：{out}"
    assert "⛔" in out, f"{rel}: 崩掉后没有给出可读结论。输出：{out}"


# --------------------------------------------------------------------------- #
# 5) 两处必须用同一套结论码（防"改了一处"）
# --------------------------------------------------------------------------- #
def test_both_sites_use_the_same_verdict_vocabulary():
    vocab = {}
    for rel in DOCS:
        block = extract_loop(rel)
        vocab[rel] = set(re.findall(r'print\("([a-z_]+)"\)', block))
    expected = {"missing", "stage_mismatch", "blocked", "empty", "ok"}
    for rel, codes in vocab.items():
        assert codes == expected, (
            f"{rel}: 结论码与约定不一致（拿到 {sorted(codes)}）—— 两处必须同一套，"
            f"否则用户在两条查询路径上会看到不同的判定")


def test_extracted_block_is_the_orchestrator_loop():
    """反空转：确认抽到的确实是带门禁的那段，而不是别的循环。

    ⚠️ 没有这条时，若有人把 `for stage_script in` 改名，extract 会失败（已断言）；
    但若文档里另加了一个同名循环，本测试会先在这里红，而不是让上面那组执行用例
    在错误的块上"全绿"。
    """
    for rel in DOCS:
        block = extract_loop(rel)
        assert '.verify/${stage_name}.json' in block, (
            f"{rel}: 抽出的循环里没有读 `.verify/<stage>.json` —— "
            f"要么门禁被删了，要么抽错了块")
        assert 'd.get("pass") is not True' in block and 'd.get("checks")' in block, (
            f"{rel}: 门禁的四类判据不全（pass / checks 至少各一处）")
