# -*- coding: utf-8 -*-
"""GWAS 门禁的**真工具端到端**测试（真 plink / 真 GEMMA）。

为什么单独一个文件
------------------
其余 GWAS 用例用合成产物（快、可控）；这个文件只放"整条链真的能跑通"的证据 ——
构造 VCF → plink 转 bfile → GEMMA 算亲缘 → GEMMA 关联 → 门禁判定。它回答的是
另一个问题：**校验器对真实工具写出来的文件判得对不对**（合成 fixture 只能证明
"我按自己以为的格式解析"）。

本文件里的两个"S2 真相"都是实测得到的：
  · `-p` 是**单列值**文件；给成 `FID IID value` 三列时 GEMMA **rc=0、不报错**，
    但 vg=ve=0、beta=se=0、p_wald 全 nan —— 整份结果是废的。
  · 真实历史产物 `gwas_sub_GWAS.assoc.txt` 正是这个形态（12563 行全 nan、beta 全 0），
    当时没有任何环节发现。
"""
import os
import random
import shutil
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VERIFIER = os.path.join(ROOT, "skills", "gwas-pipeline", "scripts", "gwas_verify.py")
PLINK = shutil.which("plink")
GEMMA = shutil.which("gemma")

if PLINK is None or GEMMA is None:
    if os.environ.get("SKILL_AGENT_ALLOW_SKIP_GWAS_TOOLS") == "1":
        pytest.skip("plink/gemma 缺失（显式 SKILL_AGENT_ALLOW_SKIP_GWAS_TOOLS=1）",
                    allow_module_level=True)
    raise RuntimeError(
        "本文件需要真实 plink 与 gemma，但 PATH 里找不到：\n"
        f"  plink={PLINK!r}  gemma={GEMMA!r}\n"
        "  装上：conda run -n Agent plink --version / conda install -c bioconda gemma\n"
        "  或显式跳过：SKILL_AGENT_ALLOW_SKIP_GWAS_TOOLS=1 pytest ...")


def _run(cmd, cwd):
    p = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True)
    assert p.returncode == 0, f"{cmd[0]} 失败 rc={p.returncode}\n{p.stderr[-800:]}"
    return p


def make_dataset(d, *, n_ind=40, n_var=40, pheno_cols=1, seed=11):
    """造 VCF + 表型，跑 plink 转 bfile。

    `pheno_cols=1` → GEMMA 的正确格式（单列值）；`=3` → 常见的错误格式
    （`FID IID value`），用于复现"静默全 nan"。
    """
    rnd = random.Random(seed)
    samples = [f"S{i + 1}" for i in range(n_ind)]
    lines = ["##fileformat=VCFv4.2", "##contig=<ID=1>",
             '##FORMAT=<ID=GT,Number=1,Type=String,Description="Genotype">',
             "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\t" + "\t".join(samples)]
    causal = []
    for j in range(n_var):
        maf = rnd.uniform(0.1, 0.5)
        gts, dos = [], []
        for _ in range(n_ind):
            g = rnd.choices([0, 1, 2], weights=[(1 - maf) ** 2, 2 * maf * (1 - maf), maf ** 2])[0]
            dos.append(g)
            gts.append(["0/0", "0/1", "1/1"][g])
        if j == 0:
            causal = dos
        lines.append("\t".join(["1", str(1000 + j * 10), f"1:{1000 + j * 10}",
                                "A", "G", ".", "PASS", ".", "GT"] + gts))
    d.mkdir(parents=True, exist_ok=True)
    (d / "x.vcf").write_text("\n".join(lines) + "\n", encoding="utf-8")

    ys = [1.0 + 0.5 * causal[i] + rnd.gauss(0, 0.2) for i in range(n_ind)]
    if pheno_cols == 1:
        body = "".join(f"{y:.4f}\n" for y in ys)
    else:
        body = "".join(f"{s} {s} {y:.4f}\n" for s, y in zip(samples, ys))
    (d / "phe.txt").write_text(body, encoding="utf-8")

    _run([PLINK, "--vcf", "x.vcf", "--make-bed", "--out", "g", "--silent"], d)
    return str(d / "g"), str(d / "phe.txt")


def run_verifier(run_dir, *args):
    p = subprocess.run([sys.executable, VERIFIER, "--run-dir", str(run_dir)] + list(args),
                       capture_output=True, text=True)
    return p.returncode, p.stdout + p.stderr


def _report(run_dir, stage):
    import json
    with open(os.path.join(str(run_dir), ".verify", f"{stage}.json"), encoding="utf-8") as f:
        return json.load(f)


def run_gemma(d, bfile, pheno):
    """真 GEMMA：先 -gk 2 算亲缘（需 -p），再 -lmm 1 关联。"""
    _run([GEMMA, "-bfile", os.path.basename(bfile), "-p", os.path.basename(pheno),
          "-gk", "2", "-o", "kin"], d)
    kin = d / "output" / "kin.sXX.txt"
    assert kin.is_file(), "GEMMA 没产出亲缘矩阵"
    _run([GEMMA, "-bfile", os.path.basename(bfile), "-p", os.path.basename(pheno),
          "-k", str(kin), "-lmm", "1", "-o", "assoc"], d)
    assoc = d / "output" / "assoc.assoc.txt"
    assert assoc.is_file(), "GEMMA 没产出 assoc"
    return str(assoc)


def test_s2_real_gemma_result_passes(tmp_path):
    """真 GEMMA（单列表型）+ 真门禁 → 通过。

    这是 S2 判据的**阳性对照**：证明 `assoc.p_valid` / `assoc.beta` 不会把
    健康结果拦下。
    """
    d = tmp_path / "s2"
    bfile, pheno = make_dataset(d)
    assoc = run_gemma(d, bfile, pheno)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    rc, out = run_verifier(run_dir, "--stage", "02_gemma",
                           "--bfile", bfile, "--assoc", assoc)
    assert rc == 0, f"健康 GEMMA 结果被拦：\n{out}"
    rep = _report(run_dir, "02_gemma")
    assert rep["pass"] is True
    assert rep.get("n_variants_in", 0) >= 1, "报告里没记录输入位点数（跨阶段对账会失效）"


def test_s2_real_gemma_multicolumn_pheno_is_blocked(tmp_path):
    """★实测复现：`-p` 给成三列 → GEMMA rc=0 但结果是全 nan / beta 全 0 → 门禁拦下。

    历史产物 `gwas_sub_GWAS.assoc.txt` 就是这一形态；当时没有任何环节发现，
    后续 MD5/作图都在拿一份没有信息的关联结果干活。
    """
    d = tmp_path / "s2bad"
    bfile, pheno = make_dataset(d, pheno_cols=3)
    assoc = run_gemma(d, bfile, pheno)
    # 先自证 fixture 的前提：GEMMA 确实"成功"了，但 p 全 nan（否则测的不是那一层）
    lines = open(assoc, encoding="utf-8").read().splitlines()
    import csv as _csv
    rows = list(_csv.DictReader(lines, delimiter="\t"))
    assert rows, "assoc 为空"
    assert all(r["p_wald"] in ("nan", "NA", "") for r in rows), (
        "fixture 前提变了：三列表型下 GEMMA 竟然给出了可解析的 p —— 本用例失去意义")

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    rc, out = run_verifier(run_dir, "--stage", "02_gemma",
                           "--bfile", bfile, "--assoc", assoc)
    assert rc == 1, "全 nan / beta 全 0 的结果竟然通过了"
    hf = set(_report(run_dir, "02_gemma")["hard_failures"])
    assert {"assoc.p_valid", "assoc.beta"} <= hf, f"没抓到那两条：{sorted(hf)}\n{out}"


def test_s1_plink_pca_fallback_passes(tmp_path):
    """真 plink `--pca`（无 gcta64 的回退路径）+ 真门禁 → 通过（GRM 只 warn）。"""
    d = tmp_path / "s1"
    bfile, _phe = make_dataset(d)
    _run([PLINK, "--bfile", os.path.basename(bfile), "--pca", "10",
          "--out", "pca"], d)
    ev = d / "pca.eigenvec"
    assert ev.is_file(), "plink --pca 没产出 eigenvec"
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    rc, out = run_verifier(run_dir, "--stage", "01_kinship_pca",
                           "--bfile", bfile,
                           "--pca-prefix", str(d / "pca"),
                           "--images-dir", str(tmp_path / "noimg"))
    assert rc == 0, f"plink --pca 回退路径被硬拦：\n{out}"
    rep = _report(run_dir, "01_kinship_pca")
    assert rep["pass"] is True
