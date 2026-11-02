# -*- coding: utf-8 -*-
"""GWAS 阶段脚本**收尾段**（定位校验器 → 校验门禁 → 写标记）的集成测试。

为什么必须在真 bash 里跑而不能只做文本断言
------------------------------------------
这一段是 shell，它的失效方式全是**控制流**：`set -e` 在哪一行退出、`$?` 有没有
传下去、`if` 有没有机会执行、`.failed` 到底写没写。文本断言（"文件里有 if 就通过"）
恰恰是"纸面具备"的来源 —— 写着的代码未必会执行。

WGS 侧已经栽过两次（见 `test_stage_gate_template.py` 的 docstring）：
  · `: "${VERIFIER:?}"` 在变量为空时立刻 exit 1，绕过写 `.failed` 的兜底
  · `VAR="$(cmd)"` 是赋值语句，`set -e` 下命令替换失败会当场退出

故这里的判据是**磁盘真值**（`.done`/`.failed` 在不在）+ 退出码 + stdout 是否走到
`TAIL_DONE`，不是源码里的字面量。
"""
import json
import os
import re
import shutil
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASH = "/bin/bash"
VERIFIER = os.path.join(REPO, "skills", "gwas-pipeline", "scripts", "gwas_verify.py")
MARKER = "# ─── 阶段收尾：先校验，再决定写哪个标记 ───"

# 端到端那条用例会真的跑 plink（模板里 `command -v plink`）。缺失时**收集期报错**
# 而不是静默 skip —— 静默跳过会让"模板能端到端跑通"这条证据无声消失。
# 显式跳过出口：SKILL_AGENT_ALLOW_SKIP_GWAS_TOOLS=1
PLINK = shutil.which("plink")
if PLINK is None:
    if os.environ.get("SKILL_AGENT_ALLOW_SKIP_GWAS_TOOLS") == "1":
        pytest.skip("plink 缺失（显式 SKILL_AGENT_ALLOW_SKIP_GWAS_TOOLS=1）",
                    allow_module_level=True)
    raise RuntimeError(
        "本文件的端到端用例需要真实 plink，但 PATH 里找不到它。\n"
        "  装上：conda run -n Agent plink --version\n"
        "  或显式跳过：SKILL_AGENT_ALLOW_SKIP_GWAS_TOOLS=1 pytest ...")


def _env():
    """让子进程能解析到 `python`（模板收尾段要调校验器）。

    测试可能在 PATH 很窄的环境里跑（例如 cron/CI）；把**当前解释器**的目录前插，
    与模板对 `python` 的预期一致，且不改变 plink 的解析结果。
    """
    env = dict(os.environ)
    env["PATH"] = os.path.dirname(sys.executable) + os.pathsep + env.get("PATH", "")
    return env

# 每个阶段 skill 的收尾段都带一份；stage = 脚本名去 .sh
TEMPLATE_FILES = {
    "skills/gwas-format-convert/SKILL.md": "00_format_convert",
    "skills/gwas-qc-filter/SKILL.md": "00.5_qc_filter",
    "skills/gwas-kinship-pca/SKILL.md": "01_kinship_pca",
    "skills/gwas-gemma/SKILL.md": "02_gemma",
    "skills/gwas-mvp/SKILL.md": "03_mvp",
    "skills/gwas-visualization/SKILL.md": "04_visualize",
    "skills/gwas-ld-block/SKILL.md": "05_ld_block",
}


def extract_tail(rel):
    """从 SKILL.md 抽出「阶段收尾」那段 shell（到本围栏结束）。"""
    text = open(os.path.join(REPO, rel), encoding="utf-8").read()
    i = text.find(MARKER)
    assert i >= 0, f"{rel} 里找不到收尾段标记（模板结构变了？）"
    j = text.find("\n```", i)
    assert j > i, f"{rel} 里找不到收尾段的围栏结束"
    return text[i:j]


def build_tail_script(rel, tmp, *, verifier_exit=0, write_report=True):
    """把收尾段包进最小骨架：`RUN_DIR` 由参数给，其余占位变量给哑值。

    ⚠️ 只跑**收尾段**，不跑围栏里前面的真实工作（vcftools/plink）—— 那些会去读
    真实输入文件。收尾段运行所需的 `RUN_DIR`/`PREFIX` 等由骨架供给。
    """
    script = os.path.join(str(tmp), "tail.sh")
    stage = TEMPLATE_FILES[rel]
    prelude = (
        "set -euo pipefail\n"
        'RUN_DIR="$1"\n'
        'PREFIX="$2"\n'
        'IN_VCF_GZ="$2.vcf"\n'
        'PED_NAME="$2"\n'
        'OUT_BFILE="$2.filt"\n'
        'QC_PREFIX="$2.filt.qc"\n'
        "MAF=0.01; GENO=0.2; HWE=0.000001\n"
        'BFILE="$2"\n'
        "TRAIT=IMF; METHODS=GLM,MLM,FarmCPU\n"
        'MAP_TXT=""; SOURCE=""; SIG_TXT=""; REGION=""; REGION_GT=""\n'
        'OUTPREFIX="$2.ld"; CHR=1; START=1000; END=2000\n'
        'GWAS_SOURCE=""; TOPSITE=""\n'
        'KINSHIP_R=""; PCA_R=""\n'
        f'DONE_FILE="${{RUN_DIR}}/.done/{stage}.done"\n'
        f'FAILED_FILE="${{RUN_DIR}}/.failed/{stage}.failed"\n'
    )
    with open(script, "w", encoding="utf-8") as f:
        f.write(prelude + extract_tail(rel) + '\necho "TAIL_DONE"\n')
    return script


def make_fake_verifier(tmp, exit_code=0, write_report=True):
    """假校验器：写一份（假的）报告，再按给定码退出。"""
    path = os.path.join(str(tmp), "fake_verifier.py")
    with open(path, "w", encoding="utf-8") as f:
        f.write("import json, os, sys\n"
                "a = sys.argv\n"
                "run_dir = a[a.index('--run-dir') + 1]\n"
                "stage = a[a.index('--stage') + 1]\n"
                f"write = {write_report!r}\n"
                "if write:\n"
                "    d = os.path.join(run_dir, '.verify')\n"
                "    os.makedirs(d, exist_ok=True)\n"
                "    json.dump({'stage': stage, 'pass': "
                f"{exit_code == 0!r}"
                ", 'checks': [{'name': 'x'}]},\n"
                "              open(os.path.join(d, stage + '.json'), 'w'))\n"
                f"sys.exit({exit_code})\n")
    return path


def run_tail(script, run_dir):
    # 真实模板在脚本开头就 `mkdir -p` 了 .done/.failed（模板顶部那几行）；
    # 本测试只抽收尾段，故这里替它建好目录。
    for sub in (".done", ".failed"):
        os.makedirs(os.path.join(str(run_dir), sub), exist_ok=True)
    p = subprocess.run([BASH, script, str(run_dir), str(run_dir) + "/out"],
                       capture_output=True, text=True, env=_env())
    return p.returncode, p.stdout + p.stderr


def write_params(run_dir, verifier=None):
    d = {"prefix": str(run_dir) + "/out"}
    if verifier is not None:
        d["verifier_path"] = verifier
    with open(os.path.join(str(run_dir), "run.params.json"), "w",
              encoding="utf-8") as f:
        json.dump(d, f)


MALFORMED = {
    "no_run_params": None,
    "bad_json": "{ broken",
    "no_verifier_key": '{"prefix": "/tmp/x"}',
    "empty_verifier": '{"verifier_path": ""}',
    "verifier_missing_file": '{"verifier_path": "/nonexistent/gwas_verify.py"}',
}


@pytest.mark.parametrize("shape,content", sorted(MALFORMED.items()))
@pytest.mark.parametrize("rel", sorted(TEMPLATE_FILES))
def test_malformed_config_writes_failed(tmp_path, rel, shape, content):
    """★核心：定位不到校验器（环境问题）也必须留下 `.failed`。

    只 `exit 1` 不写 `.failed` 时，编排层四态判定会把"环境问题"归成「已中断」，
    引用户去查一个不存在的进程 —— 而真实原因是 run.params.json 不可用。
    """
    stage = TEMPLATE_FILES[rel]
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    if content is not None:
        (run_dir / "run.params.json").write_text(content)

    script = build_tail_script(rel, tmp_path)
    rc, out = run_tail(script, run_dir)

    failed = run_dir / ".failed" / f"{stage}.failed"
    assert rc != 0, f"{rel} [{shape}]: 配置不可用竟然成功退出（rc=0）"
    assert failed.is_file(), (
        f"{rel} [{shape}]: 没写 .failed —— 编排层会判成「已中断」。输出：{out[-400:]}")
    assert "reason=" in failed.read_text(encoding="utf-8"), \
        f"{rel} [{shape}]: .failed 里没有 reason，排查时无从下手"
    assert not (run_dir / ".done" / f"{stage}.done").exists(), \
        f"{rel} [{shape}]: 配置不可用却写了 .done"
    assert "TAIL_DONE" not in out, f"{rel} [{shape}]: 没在兜底处停住"


@pytest.mark.parametrize("rel", sorted(TEMPLATE_FILES))
def test_healthy_config_writes_done(tmp_path, rel):
    """阳性对照：校验器通过 → 写 `.done`、清 `.failed`、不写失败标记。"""
    stage = TEMPLATE_FILES[rel]
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / ".failed").mkdir()
    (run_dir / ".failed" / f"{stage}.failed").write_text("stale")  # 上一轮的陈旧失败
    write_params(run_dir, make_fake_verifier(tmp_path, exit_code=0))

    script = build_tail_script(rel, tmp_path)
    rc, out = run_tail(script, run_dir)

    assert rc == 0, f"{rel}: 健康配置非零退出（rc={rc}）\n{out[-500:]}"
    assert (run_dir / ".done" / f"{stage}.done").is_file(), f"{rel}: 没写 .done"
    assert not (run_dir / ".failed" / f"{stage}.failed").exists(), (
        f"{rel}: 成功后没清陈旧 .failed —— 编排层四态判定优先读 .failed，"
        f"会把已修好的阶段误报成「校验未通过」")
    assert "TAIL_DONE" in out


@pytest.mark.parametrize("rel", sorted(TEMPLATE_FILES))
def test_verifier_failure_blocks_and_carries_report(tmp_path, rel):
    """校验器判失败 → 不写 `.done`、写 `.failed`，并把本轮报告正文带进去。"""
    stage = TEMPLATE_FILES[rel]
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    write_params(run_dir, make_fake_verifier(tmp_path, exit_code=1))

    script = build_tail_script(rel, tmp_path)
    rc, out = run_tail(script, run_dir)

    failed = run_dir / ".failed" / f"{stage}.failed"
    assert rc != 0, f"{rel}: 校验失败却成功退出"
    assert not (run_dir / ".done" / f"{stage}.done").exists(), f"{rel}: 校验失败却写了 .done"
    assert failed.is_file(), f"{rel}: 校验失败没写 .failed"
    body = failed.read_text(encoding="utf-8")
    assert "--- 校验报告（本轮）---" in body, (
        f"{rel}: .failed 里没有本轮报告正文 —— 用户只能看到一句「失败」，无从修复\n{body}")
    assert "TAIL_DONE" not in out


@pytest.mark.parametrize("rel", sorted(TEMPLATE_FILES))
def test_gate_is_blocking_not_just_printing(rel):
    """收尾段必须是**拦截式**：有 `exit 1`、写 `.failed`，不是只 echo 一句警告。"""
    tail = extract_tail(rel)
    assert "exit 1" in tail, f"{rel}: 收尾段没有 exit 1 —— 失败也挡不住推进"
    assert ".failed" in tail, f"{rel}: 收尾段没有写 .failed"
    assert "--stage" in tail, f"{rel}: 收尾段没把 --stage 传给校验器"


def test_every_template_passes_its_own_stage_name():
    """每个模板传给校验器的 `--stage` 必须与它自己的阶段名一致（防复制粘贴串台）。"""
    for rel, stage in TEMPLATE_FILES.items():
        tail = extract_tail(rel)
        assert f"--stage {stage}" in tail, (
            f"{rel}: 收尾段里的 --stage 不是 {stage}（复制粘贴时改漏了？）")


def test_gated_stage_list_matches_verifier_registry():
    """编排层的 `GATED_STAGES` 必须与 `gwas_verify.py` 的 `_STAGES` 注册表一致。

    为什么必须有这条：GWAS 只有部分阶段接入了校验器（Phase 1 = S0/S0.5）。编排层
    用 `GATED_STAGES` 决定"哪些阶段要核 `.verify` 报告"。若将来给 S1 写了校验器
    却**忘了**把它加进列表，该阶段的 `.done` 会走"未接入"分支被放行 ——
    校验器写了、判据跑了、但编排层根本不看它。这正是"纸面具备"的经典形态，
    而且它**不会**让任何现有测试变红（新校验器自己的单测全绿）。故两端必须钉死。
    """
    from importlib import util as _u

    spec = _u.spec_from_file_location("gwas_verify_mod", VERIFIER)
    mod = _u.module_from_spec(spec)
    spec.loader.exec_module(mod)
    registered = set(mod._STAGES)

    doc = open(os.path.join(REPO, "skills/gwas-pipeline/SKILL.md"),
               encoding="utf-8").read()
    m = re.search(r'GATED_STAGES="([^"]*)"', doc)
    assert m, "编排层里找不到 GATED_STAGES（结构变了？本防线会空转）"
    listed = set(m.group(1).split())

    assert listed == registered, (
        f"GATED_STAGES 与校验器注册表不一致：\n"
        f"  仅在文档（会被核报告，但校验器不认识）: {sorted(listed - registered)}\n"
        f"  仅在校验器（写了校验器，但编排层不看它）: {sorted(registered - listed)}")
    assert registered, "校验器一个阶段都没注册 —— 本防线在空转"


def test_templates_share_the_same_guard_shape():
    """两个模板的门禁形态必须一致（不一致 = 某一份漏改，且只改了一份）。"""
    shapes = {}
    for rel in TEMPLATE_FILES:
        tail = extract_tail(rel)
        shapes[rel] = (
            'VERIFIER="$(python' in tail,
            "[ -z \"${VERIFIER}\" ]" in tail,
            "[ ! -f \"${VERIFIER}\" ]" in tail,
            "|| true)" in tail,
        )
    bad = {k: v for k, v in shapes.items() if v != (True, True, True, True)}
    assert not bad, f"这些模板的守卫形态与约定不一致：{bad}"


# --------------------------------------------------------------------------- #
# 端到端：把**真实模板**渲染成脚本跑一遍（S0 → S0.5 → 门禁 → .done）
# --------------------------------------------------------------------------- #

def _fences(text):
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


def render_stage_script(rel, **subs):
    """抽出模板里的**完整阶段脚本**围栏（以 `#!/bin/bash` 开头），替换占位符。"""
    text = open(os.path.join(REPO, rel), encoding="utf-8").read()
    for blk in _fences(text):
        if blk.lstrip().startswith("#!/bin/bash"):
            break
    else:
        raise AssertionError(f"{rel}: 找不到以 #!/bin/bash 开头的阶段脚本围栏")
    for key, val in subs.items():
        placeholder = f'{key}="..."'
        assert placeholder in blk, f"{rel}: 模板里没有占位符 {placeholder}"
        blk = blk.replace(placeholder, f'{key}="{val}"')
    return blk


def _make_vcf(path):
    """与 test_gwas_verify 同形的小 VCF：1 个单态 + 1 个含缺失 + 其余常见 MAF。"""
    n_ind, n_var = 12, 10
    samples = [f"S{i + 1}" for i in range(n_ind)]
    lines = ["##fileformat=VCFv4.2", "##contig=<ID=1>",
             '##FORMAT=<ID=GT,Number=1,Type=String,Description="Genotype">',
             "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\t" + "\t".join(samples)]
    for j in range(n_var):
        if j == 0:
            gts = ["0/0"] * n_ind
        elif j == 1:
            gts = ["0/1", "./."] + ["0/0"] * (n_ind - 2)
        else:
            n_het = 2 + (j % 5)
            gts = ["0/1"] * n_het + ["0/0"] * (n_ind - n_het)
        lines.append("\t".join(["1", str(1000 + j * 10), f"1:{1000 + j * 10}",
                                "A", "G", ".", "PASS", ".", "GT"] + gts))
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def test_stage_scripts_run_end_to_end(tmp_path):
    """★端到端：S0 脚本 → S0.5 脚本 → 两阶段都留下 pass=true 的报告与 `.done`。

    这条的价值是**模板可执行性**：文本断言看不出 `VCFTOOLS` 为空时那一行会不会
    以 rc=127 炸掉、也看不出 `python "${VERIFIER}"` 能不能找到校验器。实测踩到过：
    模板里 `${VCFTOOLS} --gzvcf ...` 在没装 vcftools 的机器上直接失败，而错误发生在
    写任何标记之前 —— 编排层只会说"已中断"。
    """
    run = tmp_path / "run"
    run.mkdir()
    vcf = str(run / "in.vcf")
    _make_vcf(vcf)
    prefix = str(run / "cohort")
    filt = prefix + ".wy1.snp"

    with open(run / "run.params.json", "w", encoding="utf-8") as f:
        json.dump({"verifier_path": VERIFIER, "prefix": prefix, "vcf": vcf,
                   "input_bfile": prefix, "qc_prefix": filt + ".qc",
                   "filter": {"geno": 0.2, "maf": 0.01, "hwe": 1e-6}}, f)

    # --- S0 ---
    s0 = tmp_path / "00_format_convert.sh"
    s0.write_text(render_stage_script(
        "skills/gwas-format-convert/SKILL.md",
        RUN_DIR=str(run), IN_VCF_GZ=vcf, PREFIX=prefix), encoding="utf-8")
    p = subprocess.run([BASH, str(s0)], capture_output=True, text=True, env=_env())
    assert p.returncode == 0, f"S0 模板跑不起来：\n{p.stdout[-800:]}\n{p.stderr[-800:]}"
    assert (run / ".done" / "00_format_convert.done").is_file(), "S0 没写 .done"
    assert not (run / ".failed" / "00_format_convert.failed").exists()
    rep0 = json.loads((run / ".verify" / "00_format_convert.json").read_text())
    assert rep0["pass"] is True and rep0["stage"] == "00_format_convert"

    # --- S0.5 ---
    s05 = tmp_path / "00.5_qc_filter.sh"
    s05.write_text(render_stage_script(
        "skills/gwas-qc-filter/SKILL.md", RUN_DIR=str(run), PREFIX=prefix),
        encoding="utf-8")
    p = subprocess.run([BASH, str(s05)], capture_output=True, text=True, env=_env())
    assert p.returncode == 0, f"S0.5 模板跑不起来：\n{p.stdout[-800:]}\n{p.stderr[-800:]}"
    assert (run / ".done" / "00.5_qc_filter.done").is_file(), "S0.5 没写 .done"
    rep5 = json.loads((run / ".verify" / "00.5_qc_filter.json").read_text())
    assert rep5["pass"] is True
    assert rep5["n_variants"] < rep5["n_variants_in"], "过滤没有生效（位点数没减少）"
    # 过滤后 QC 三件套必须留（门禁的实测值来源）
    for ext in (".frq", ".lmiss", ".hwe"):
        assert os.path.isfile(filt + ".qc" + ext), f"S0.5 没留 {filt}.qc{ext}"
