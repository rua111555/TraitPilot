# -*- coding: utf-8 -*-
"""GWAS 阶段结果校验器（`gwas_verify.py`）的判据测试。

为什么需要这一层
----------------
GWAS 各阶段的失败大多是**静默**的：`plink -maf` 没生效、位点被滤光、样本在 `--keep`
里丢了一半 —— 命令都返回 0，`.done` 照写，错误一路带到关联结果里。本文件测的就是
"这些形态会不会被拦下"，**全部用真实 plink 构造**（不 mock）：判据是对着真产物的
字节与列写的，mock 出来的形状说明不了任何事。

工具缺失时**收集期报错**而不是静默 skip：静默跳过会让整份证据链无声消失
（v0.3.0 已把 WGS 侧从 `skipif` 改成收集期报错，本文件沿用同一约定）。
显式跳过出口：`SKILL_AGENT_ALLOW_SKIP_GWAS_TOOLS=1`。
"""
import json
import os
import shutil
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VERIFIER = os.path.join(ROOT, "skills", "gwas-pipeline", "scripts", "gwas_verify.py")
PLINK = shutil.which("plink")

if PLINK is None:
    if os.environ.get("SKILL_AGENT_ALLOW_SKIP_GWAS_TOOLS") == "1":
        pytest.skip("plink 缺失（显式 SKILL_AGENT_ALLOW_SKIP_GWAS_TOOLS=1，允许整文件跳过）",
                    allow_module_level=True)
    raise RuntimeError(
        "本文件需要真实 plink 构造 fixture，但 PATH 里找不到它。\n"
        "  装上：conda run -n Agent plink --version\n"
        "  或显式跳过：SKILL_AGENT_ALLOW_SKIP_GWAS_TOOLS=1 pytest ...\n"
        "（不静默 skip：静默跳过会让 GWAS 门禁的整条证据链无声消失 —— 与 v0.3.0 修掉\n"
        "  WGS 侧同一形态的理由一致。）")

N_IND = 12
N_VAR = 10


# --------------------------------------------------------------------------- #
# fixture 构造：真 VCF → 真 plink 产物
# --------------------------------------------------------------------------- #

def make_vcf(path, *, n_ind=N_IND, n_var=N_VAR):
    """造一个小 VCF：1 个单态位点、1 个含缺失、其余 MAF 0.2~0.5。

    这些形态是刻意安排的 —— 它们分别对应 `-maf` / `-geno` 两条"参数生效性"判据：
      · 位点 1 全 0/0（MAF=0）→ `-maf 0.1` 必须把它滤掉
      · 位点 2 有一个缺失基因型（F_MISS=1/12≈0.083）→ `-geno 0.05` 必须把它滤掉
    """
    samples = [f"S{i + 1}" for i in range(n_ind)]
    lines = ["##fileformat=VCFv4.2", "##contig=<ID=1>",
             '##FORMAT=<ID=GT,Number=1,Type=String,Description="Genotype">',
             "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\t" + "\t".join(samples)]
    for j in range(n_var):
        if j == 0:                                   # 单态：全 0/0
            gts = ["0/0"] * n_ind
        elif j == 1:                                 # 含缺失
            gts = ["0/1", "./."] + ["0/0"] * (n_ind - 2)
        else:                                        # MAF 0.25~0.5，按 j 轮转
            n_het = 2 + (j % 5)
            gts = ["0/1"] * n_het + ["0/0"] * (n_ind - n_het)
        lines.append("\t".join(["1", str(1000 + j * 10), f"1:{1000 + j * 10}",
                                "A", "G", ".", "PASS", ".", "GT"] + gts))
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return path


def run_plink(*args, cwd=None):
    p = subprocess.run([PLINK] + list(args), capture_output=True, text=True,
                       cwd=cwd)
    assert p.returncode == 0, f"plink 失败 rc={p.returncode}: {args}\n{p.stderr[-800:]}"
    return p


def make_bfile(tmp_path, name="geno"):
    """VCF → bfile（S0 的健康产物）。返回 (vcf_path, prefix)。"""
    d = tmp_path / "s0"
    d.mkdir(parents=True, exist_ok=True)
    vcf = make_vcf(str(d / f"{name}.vcf"))
    prefix = str(d / name)
    run_plink("--vcf", vcf, "--make-bed", "--out", prefix, "--silent")
    return vcf, prefix


def run_verify(run_dir, *args):
    cmd = [sys.executable, VERIFIER, "--run-dir", str(run_dir)] + [str(a) for a in args]
    p = subprocess.run(cmd, capture_output=True, text=True)
    return p.returncode, p.stdout + p.stderr


def report_of(run_dir, stage):
    path = os.path.join(str(run_dir), ".verify", f"{stage}.json")
    assert os.path.isfile(path), f"校验报告没落盘：{path}"
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def hard(report):
    return set(report.get("hard_failures") or [])


# --------------------------------------------------------------------------- #
# S0：格式转换
# --------------------------------------------------------------------------- #

def test_s0_healthy_is_passed(tmp_path):
    """阳性对照：健康产物必须过 —— 证明下面那些拦截不是"永远拦"。"""
    vcf, prefix = make_bfile(tmp_path)
    rc, out = run_verify(tmp_path, "--stage", "00_format_convert",
                         "--prefix", prefix, "--vcf", vcf)
    assert rc == 0, f"健康数据被拦下了：\n{out}"
    rep = report_of(tmp_path, "00_format_convert")
    assert rep["pass"] is True and rep["stage"] == "00_format_convert"
    assert rep["n_variants"] == N_VAR and rep["n_samples"] == N_IND
    names = {c["name"] for c in rep["checks"]}
    assert {"bed.magic", "bed.size", "samples_vs_vcf"} <= names


def test_s0_truncated_bed_is_blocked(tmp_path):
    """★核心：`.bed` 被截断（写了一半）—— 按格式等式必然对不上。

    这是"没有工具、没有索引、也没有日志"也能判的**结构不变量**：
    PLINK 1 SNP-major 的字节数 = 3 + n_var × ⌈n_ind/4⌉，与内容无关。
    """
    vcf, prefix = make_bfile(tmp_path)
    bed = prefix + ".bed"
    with open(bed, "r+b") as f:
        f.truncate(os.path.getsize(bed) - 7)
    rc, out = run_verify(tmp_path, "--stage", "00_format_convert",
                         "--prefix", prefix, "--vcf", vcf)
    assert rc == 1, "截断的 .bed 竟然通过了"
    assert "bed.size" in hard(report_of(tmp_path, "00_format_convert"))


def test_s0_bad_bed_magic_is_blocked(tmp_path):
    vcf, prefix = make_bfile(tmp_path)
    with open(prefix + ".bed", "r+b") as f:
        f.write(b"\x00\x00\x00")
    rc, _ = run_verify(tmp_path, "--stage", "00_format_convert",
                       "--prefix", prefix, "--vcf", vcf)
    assert rc == 1
    assert "bed.magic" in hard(report_of(tmp_path, "00_format_convert"))


def test_s0_lost_sample_is_blocked(tmp_path):
    """样本在转换里丢了 —— 下游所有关联都会少一个人，且没有任何环节会报错。"""
    vcf, prefix = make_bfile(tmp_path)
    fam = prefix + ".fam"
    lines = [ln for ln in open(fam, encoding="utf-8").read().splitlines() if ln.strip()]
    with open(fam, "w", encoding="utf-8") as f:
        f.write("\n".join(lines[:-1]) + "\n")          # 删掉最后一个样本
    rc, out = run_verify(tmp_path, "--stage", "00_format_convert",
                         "--prefix", prefix, "--vcf", vcf)
    assert rc == 1
    hf = hard(report_of(tmp_path, "00_format_convert"))
    assert "samples_vs_vcf" in hf, f"没抓到丢样本：{sorted(hf)}\n{out}"


def test_s0_empty_bim_is_blocked(tmp_path):
    """`.bim` 被清空 / 不存在 —— 下游无从读位点，必须拦。

    ⚠️ 判据落点是 `dataset.exists` 而不是 `bfile.variants`：`have()` 的定义是
    "存在**且非空**"，0 字节的 `.bim` 在语义上等于"没有这个文件"（这也正是
    PLINK 自己写不出 0 位点 bfile 的原因 —— 实测 `-maf` 滤光时它直接 rc=12 退出）。
    """
    vcf, prefix = make_bfile(tmp_path)
    open(prefix + ".bim", "w").close()
    rc, _ = run_verify(tmp_path, "--stage", "00_format_convert",
                       "--prefix", prefix, "--vcf", vcf)
    assert rc == 1
    hf = hard(report_of(tmp_path, "00_format_convert"))
    assert "dataset.exists" in hf or "bfile.variants" in hf, sorted(hf)


def test_s0_duplicate_ids_are_blocked(tmp_path):
    vcf, prefix = make_bfile(tmp_path)
    fam = prefix + ".fam"
    lines = [ln for ln in open(fam, encoding="utf-8").read().splitlines() if ln.strip()]
    with open(fam, "w", encoding="utf-8") as f:
        f.write("\n".join(lines + [lines[0]]) + "\n")   # 复制第一行
    rc, _ = run_verify(tmp_path, "--stage", "00_format_convert",
                       "--prefix", prefix, "--vcf", vcf)
    assert rc == 1
    assert "bfile.ids_unique" in hard(report_of(tmp_path, "00_format_convert"))


def test_s0_pedmap_only_is_accepted_with_warning(tmp_path):
    """只产出 ped/map（vcftools 路径）也要能过，但必须如实说"没有 bfile"。"""
    d = tmp_path / "s0pm"
    d.mkdir()
    vcf = make_vcf(str(d / "x.vcf"))
    base = str(d / "x")
    run_plink("--vcf", vcf, "--make-bed", "--out", base, "--silent")
    run_plink("--bfile", base, "--recode", "--out", base, "--silent")
    os.remove(base + ".bed"), os.remove(base + ".bim"), os.remove(base + ".fam")
    rc, out = run_verify(tmp_path, "--stage", "00_format_convert",
                         "--prefix", base, "--vcf", vcf)
    assert rc == 0, f"ped/map 形态被拦下了：\n{out}"
    rep = report_of(tmp_path, "00_format_convert")
    assert rep["output_form"] == "pedmap"
    assert any(c["name"] == "pedmap.shape" and c["level"] == "warn"
               for c in rep["checks"])


def test_s0_accepts_fid_iid_vcf_naming(tmp_path):
    """VCF 样本名写成 `FID_IID` 时必须认得（真实 GWAS 产物就是这种形态）。

    实测教训：本项目 `Pig_geno/gwas_test` 的 `.fam` 是 `FID=IID=et10`，同目录 VCF
    表头是 `et10_et10` —— 只比 IID 会把 162 个样本全判成"丢了"，数据本身却是好的。
    """
    from importlib import util as _u

    spec = _u.spec_from_file_location("gwas_verify_mod", VERIFIER)
    mod = _u.module_from_spec(spec)
    spec.loader.exec_module(mod)

    vcf = tmp_path / "n.vcf"
    vcf.write_text("##fileformat=VCFv4.2\n"
                   "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\t"
                   "F1_S1\tF2_S2\n"
                   "1\t10\tv1\tA\tG\t.\tPASS\t.\tGT\t0/0\t0/1\n",
                   encoding="utf-8")
    rep = mod.GwasReport("00_format_convert", str(tmp_path))
    ds = {"samples": [("F1", "S1"), ("F2", "S2")]}
    mod.check_samples_match_vcf(rep, ds, str(vcf))
    assert rep.blocked is False, rep.render()
    assert rep.extras["sample_id_convention"] == "fid_iid"


# --------------------------------------------------------------------------- #
# S0.5：QC 过滤
# --------------------------------------------------------------------------- #

def make_qc_case(tmp_path, *, maf=0.1, geno=0.05, hwe=1e-6, filt_name="filt"):
    """S0 产物 → 过滤产物 + 过滤后 QC 报告。返回 (vcf, in_prefix, out_prefix, qc_prefix)。"""
    vcf, in_prefix = make_bfile(tmp_path)
    out_prefix = str(tmp_path / "s0" / filt_name)
    run_plink("--bfile", in_prefix, "-maf", str(maf), "-geno", str(geno),
              "-hwe", str(hwe), "--make-bed", "--out", out_prefix, "--silent")
    qc_prefix = out_prefix + ".qc"
    run_plink("--bfile", out_prefix, "--freq", "--missing", "--hardy",
              "--out", qc_prefix, "--silent")
    return vcf, in_prefix, out_prefix, qc_prefix


def s05_args(out_prefix, in_prefix, qc_prefix, **flt):
    args = ["--stage", "00.5_qc_filter", "--prefix", out_prefix,
            "--input-bfile", in_prefix, "--qc-prefix", qc_prefix]
    for k, v in flt.items():
        args += [f"--{k}", str(v)]
    return args


def test_s05_healthy_is_passed(tmp_path):
    _, in_p, out_p, qc = make_qc_case(tmp_path)
    rc, out = run_verify(tmp_path, *s05_args(out_p, in_p, qc, maf=0.1, geno=0.05))
    assert rc == 0, f"健康过滤产物被拦下了：\n{out}"
    rep = report_of(tmp_path, "00.5_qc_filter")
    # fixture 的位点分布（12 个体）：j0 单态(MAF 0)、j1 含缺失(MAF .045/缺失 .083)、
    # 其余 n_het=2..6 → MAF = n_het/24。`-maf 0.1` 后剩 j2,j3,j4,j6,j7,j8,j9 共 7 个。
    assert rep["n_variants"] == 7
    assert rep["n_samples"] == N_IND
    assert rep["retention"] == pytest.approx(0.7, abs=1e-3)


def test_s05_stale_frq_is_blocked(tmp_path):
    """`.frq` 是**过滤前**的（陈旧产物）—— 行数对不上 + min MAF 不达阈值。"""
    vcf, in_p = make_bfile(tmp_path)
    out_p = str(tmp_path / "s0" / "filt")
    run_plink("--bfile", in_p, "-maf", "0.1", "-geno", "0.05",
              "--make-bed", "--out", out_p, "--silent")
    stale = str(tmp_path / "s0" / "stale")
    run_plink("--bfile", in_p, "--freq", "--out", stale, "--silent")  # 过滤前的 .frq
    run_plink("--bfile", out_p, "--missing", "--hardy", "--out", stale, "--silent")
    rc, _ = run_verify(tmp_path, *s05_args(out_p, in_p, stale, maf=0.1, geno=0.05))
    assert rc == 1
    hf = hard(report_of(tmp_path, "00.5_qc_filter"))
    assert "qc.frq.rows" in hf and "qc.maf" in hf, f"陈旧 .frq 没被抓到：{sorted(hf)}"


def test_s05_missing_post_filter_reports_are_blocked(tmp_path):
    """阶段脚本没留过滤后 QC 报告 —— fail-closed（不能证明过滤生效就不放行）。"""
    _, in_p, out_p, _ = make_qc_case(tmp_path)
    rc, out = run_verify(tmp_path, *s05_args(out_p, in_p, str(tmp_path / "nope"),
                                             maf=0.1, geno=0.05))
    assert rc == 1, "没有 QC 报告竟然通过了"
    hf = hard(report_of(tmp_path, "00.5_qc_filter"))
    assert {"qc.frq", "qc.lmiss", "qc.hwe"} <= hf, f"缺报告没被拦：{sorted(hf)}\n{out}"


def test_s05_all_variants_removed_is_blocked(tmp_path):
    """过滤到 0 个位点：PLINK 自己就 rc=12（写不出 bfile），门禁必须判"产物不完整"。

    实测：`plink -maf 0.6` 直接 `Error: All variants removed due to minor allele
    threshold(s)` 并退出，`.bed/.bim/.fam` **一个都不写**。这正是"没有产物"的形态 ——
    若阶段脚本忽略了这个退出码、照写 `.done`，门禁就是最后一道防线。
    """
    vcf, in_p = make_bfile(tmp_path)
    out_p = str(tmp_path / "s0" / "zero")
    p = subprocess.run([PLINK, "--bfile", in_p, "-maf", "0.6",
                        "--make-bed", "--out", out_p, "--silent"],
                       capture_output=True, text=True)
    assert p.returncode != 0, "plink 竟然允许 0 位点产出 bfile —— 本测试的前提变了"
    assert not os.path.isfile(out_p + ".bed"), "0 位点时不该有 .bed"
    rc, out = run_verify(tmp_path, *s05_args(out_p, in_p, out_p + ".qc", maf=0.6))
    assert rc == 1, f"没有产物竟然通过了：\n{out}"
    assert "dataset.exists" in hard(report_of(tmp_path, "00.5_qc_filter"))


def test_s05_sample_loss_is_blocked(tmp_path):
    """输出少了样本、且参数里没有 keep/remove 声明 —— 静默丢样本必须拦。"""
    _, in_p, out_p, qc = make_qc_case(tmp_path)
    fam = out_p + ".fam"
    lines = [ln for ln in open(fam, encoding="utf-8").read().splitlines() if ln.strip()]
    with open(fam, "w", encoding="utf-8") as f:
        f.write("\n".join(lines[:-1]) + "\n")
    rc, _ = run_verify(tmp_path, *s05_args(out_p, in_p, qc, maf=0.1, geno=0.05))
    assert rc == 1
    assert "samples_conserved" in hard(report_of(tmp_path, "00.5_qc_filter"))


def test_s05_declared_remove_turns_sample_loss_into_warning(tmp_path, monkeypatch):
    """参数里**显式声明**了 remove 时，样本减少是预期行为 —— 降为 warn 而非 hard。"""
    _, in_p, out_p, qc = make_qc_case(tmp_path)
    fam = out_p + ".fam"
    lines = [ln for ln in open(fam, encoding="utf-8").read().splitlines() if ln.strip()]
    with open(fam, "w", encoding="utf-8") as f:
        f.write("\n".join(lines[:-1]) + "\n")
    # 用 run.params.json 声明 remove（CLI 没有这个参数，走参数文件通道）
    params = {"prefix": out_p, "input_bfile": in_p, "qc_prefix": qc,
              "filter": {"maf": 0.1, "geno": 0.05}, "remove": "S12"}
    with open(tmp_path / "run.params.json", "w", encoding="utf-8") as f:
        json.dump(params, f)
    rc, out = run_verify(tmp_path, "--stage", "00.5_qc_filter")
    assert rc == 0, f"显式 remove 仍被 hard 拦下：\n{out}"
    rep = report_of(tmp_path, "00.5_qc_filter")
    assert any(c["name"] == "samples_conserved" and c["level"] == "warn"
               for c in rep["checks"])


def test_s05_retention_warning_does_not_block(tmp_path):
    """重过滤（保留率低）只该 warn —— 阈值严不严是用户的判断，不是门禁的判断。"""
    _, in_p, out_p, qc = make_qc_case(tmp_path, maf=0.2, filt_name="tight")
    rc, out = run_verify(tmp_path, *s05_args(out_p, in_p, qc, maf=0.2, geno=0.05))
    assert rc == 0, f"保留率低被硬拦：\n{out}"
    rep = report_of(tmp_path, "00.5_qc_filter")
    assert rep["retention"] < 0.5, rep.get("retention")
    assert "retention" in (rep.get("warnings") or [])


# --------------------------------------------------------------------------- #
# 参数通道 / 报告 / 未知阶段
# --------------------------------------------------------------------------- #

def test_cli_threshold_is_not_dropped_when_params_filter_present(tmp_path):
    """回归：CLI 的 `--maf` 不能被 run.params.json 的 filter **静默覆盖**。

    实测踩到的缺陷：早期 main() 把合并结果写回 args.filter，导致
    `--maf 0.01` 传了却报"未声明 --maf 阈值，未核对" —— 门禁自己犯了它要防的
    "参数传了没生效"。
    """
    vcf, in_p = make_bfile(tmp_path)                     # 未过滤：min MAF = 0
    qc = str(tmp_path / "s0" / "raw")
    run_plink("--bfile", in_p, "--freq", "--missing", "--hardy", "--out", qc, "--silent")
    with open(tmp_path / "run.params.json", "w", encoding="utf-8") as f:
        json.dump({"filter": {"maf": 0.0}}, f)           # 参数文件里只有 maf=0
    rc, _ = run_verify(tmp_path, "--stage", "00.5_qc_filter",
                       "--prefix", in_p, "--input-bfile", in_p,
                       "--qc-prefix", qc, "--maf", "0.01")
    assert rc == 1, "CLI 的 --maf 0.01 被参数文件覆盖了（未过滤数据竟然通过）"
    assert "qc.maf" in hard(report_of(tmp_path, "00.5_qc_filter"))


def test_thresholds_can_come_from_run_params_json(tmp_path):
    """阈值也可只写在 run.params.json（与 verifier_path 同一范式）。"""
    _, in_p, out_p, qc = make_qc_case(tmp_path)
    with open(tmp_path / "run.params.json", "w", encoding="utf-8") as f:
        json.dump({"prefix": out_p, "input_bfile": in_p, "qc_prefix": qc,
                   "filter": "genos=0.05 maf=0.1 hwe=1e-6",
                   "min_snps": 1}, f)
    rc, out = run_verify(tmp_path, "--stage", "00.5_qc_filter")
    assert rc == 0, f"参数文件通道没生效：\n{out}"
    rep = report_of(tmp_path, "00.5_qc_filter")
    names = [c["name"] for c in rep["checks"]]
    assert "qc.maf" in names and "qc.geno" in names and "qc.hwe" in names


def test_min_snps_floor_blocks_when_declared(tmp_path):
    _, in_p, out_p, qc = make_qc_case(tmp_path)
    rc, out = run_verify(tmp_path, *s05_args(out_p, in_p, qc, maf=0.1, geno=0.05),
                         "--min-snps", "1000")
    assert rc == 1
    assert "min_snps" in hard(report_of(tmp_path, "00.5_qc_filter"))


def test_unknown_stage_is_blocked(tmp_path):
    rc, _ = run_verify(tmp_path, "--stage", "99_nope")
    assert rc == 1
    assert "stage_known" in hard(report_of(tmp_path, "99_nope"))


# --------------------------------------------------------------------------- #
# Phase 2/3：S1–S5（synthetic artifacts；S2 的真 gemma 端到端另见 E2E 文件）
# --------------------------------------------------------------------------- #

def _fam_ids(prefix):
    return [ln.split()[1] for ln in open(prefix + ".fam", encoding="utf-8").read().splitlines() if ln.strip()]


def _write(path, text):
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


def _pdf(path, size=2048):
    with open(path, "wb") as f:
        f.write(b"%PDF-1.4\n" + b"0" * size)
    return path


def _png(path, size=2048):
    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n" + b"0" * size)
    return path


def make_s1_case(tmp_path, *, eigenvec_rows=None, eigenval=None, ids=None):
    vcf, bf = make_bfile(tmp_path)
    ids = ids or _fam_ids(bf)
    n = len(ids)
    grm = str(tmp_path / "grm")
    _write(grm + ".grm.id", "".join(f"{i} {i}\n" for i in ids))
    import gzip as _gz
    with _gz.open(grm + ".grm.gz", "wt", encoding="utf-8") as f:
        f.write("1 1 1 0.9\n" * max(n, 1))
    kin = str(tmp_path / "kin.txt")
    rows = ["\t".join(ids)]
    for i, who in enumerate(ids):
        rows.append("\t".join(f"{1.0 if i == j else 0.1:.4f}" for j in range(n)))
    _write(kin, "\n".join(rows) + "\n")
    pca = str(tmp_path / "pca")
    ev_rows = eigenvec_rows if eigenvec_rows is not None else n
    _write(pca + ".eigenvec",
           "".join(f"{i} {i} 0.5 -0.2\n" for i in ids[:ev_rows]))
    _write(pca + ".eigenval", eigenval or "3.2\n1.1\n")
    img = tmp_path / "plots"
    img.mkdir(exist_ok=True)
    _pdf(str(img / "pca.pdf"))
    return bf, grm, kin, pca, str(img)


def s1_args(bf, grm, kin, pca, img):
    return ["--stage", "01_kinship_pca", "--bfile", bf, "--grm-prefix", grm,
            "--kinship", kin, "--pca-prefix", pca, "--images-dir", img]


def test_s1_healthy_passes(tmp_path):
    bf, grm, kin, pca, img = make_s1_case(tmp_path)
    rc, out = run_verify(tmp_path, *s1_args(bf, grm, kin, pca, img))
    assert rc == 0, out
    names = {c["name"] for c in report_of(tmp_path, "01_kinship_pca")["checks"]}
    assert {"grm.id.rows", "kinship.symmetric", "pca.ids", "pca.eigenval.order"} <= names


def test_s1_pca_row_loss_is_blocked(tmp_path):
    bf, grm, kin, pca, img = make_s1_case(tmp_path, eigenvec_rows=N_IND - 1)
    rc, _ = run_verify(tmp_path, *s1_args(bf, grm, kin, pca, img))
    assert rc == 1
    assert "pca.rows" in hard(report_of(tmp_path, "01_kinship_pca"))


def test_s1_eigenval_not_descending_is_blocked(tmp_path):
    bf, grm, kin, pca, img = make_s1_case(tmp_path, eigenval="1.1\n3.2\n")
    rc, _ = run_verify(tmp_path, *s1_args(bf, grm, kin, pca, img))
    assert rc == 1
    assert "pca.eigenval.order" in hard(report_of(tmp_path, "01_kinship_pca"))


def test_s1_fallback_without_grm_only_warns(tmp_path):
    """没有 gcta64 的回退路径：GRM/亲缘矩阵/热图本就不产出 → 只登记"未核对"。"""
    bf, grm, kin, pca, img = make_s1_case(tmp_path)
    args = ["--stage", "01_kinship_pca", "--bfile", bf,
            "--grm-prefix", str(tmp_path / "nogrm"),
            "--kinship", str(tmp_path / "nokin.txt"),
            "--pca-prefix", pca, "--images-dir", str(tmp_path / "noimg")]
    rc, out = run_verify(tmp_path, *args)
    assert rc == 0, f"回退路径被硬拦：\n{out}"
    rep = report_of(tmp_path, "01_kinship_pca")
    assert {"grm.id", "kinship"} & set(rep["warnings"])


def make_assoc(tmp_path, *, all_nan=False, n=10):
    rows = ["chr\trs\tps\tn_miss\tallele1\tallele0\taf\tbeta\tse\tlogl_H1\tl_remle\tp_wald\tp_lrt\tp_score"]
    for i in range(n):
        if all_nan:
            rows.append(f"1\tv{i}\t{1000+i}\t0\tA\tG\t0.2\t0.000000e+00\t0.000000e+00\t1\t1\tnan\t1\t1")
        else:
            rows.append(f"1\tv{i}\t{1000+i}\t0\tA\tG\t0.2\t0.1\t0.05\t1\t1\t{0.01 * (i + 1):.4g}\t0.2\t0.3")
    p = str(tmp_path / "g.assoc.txt")
    _write(p, "\n".join(rows) + "\n")
    return p


def test_s2_healthy_passes(tmp_path):
    _, bf = make_bfile(tmp_path)
    assoc = make_assoc(tmp_path)
    rc, out = run_verify(tmp_path, "--stage", "02_gemma", "--bfile", bf, "--assoc", assoc)
    assert rc == 0, out


def test_s2_all_nan_beta_zero_is_blocked(tmp_path):
    """实测过的真实形态：beta/se 全 0、p_wald 全 nan —— 模型根本没拟合出来。"""
    _, bf = make_bfile(tmp_path)
    assoc = make_assoc(tmp_path, all_nan=True)
    rc, out = run_verify(tmp_path, "--stage", "02_gemma", "--bfile", bf, "--assoc", assoc)
    assert rc == 1, "全 nan / beta 全 0 的结果竟然通过了"
    hf = hard(report_of(tmp_path, "02_gemma"))
    assert {"assoc.p_valid", "assoc.beta"} <= hf, sorted(hf)


def test_s2_missing_meta_mismatch_is_blocked(tmp_path):
    _, bf = make_bfile(tmp_path)
    assoc = make_assoc(tmp_path)
    src = open(assoc, encoding="utf-8").read()
    _write(assoc, "## number of analyzed SNPs = 999\n" + src)
    rc, _ = run_verify(tmp_path, "--stage", "02_gemma", "--bfile", bf, "--assoc", assoc)
    assert rc == 1
    assert "meta.snps" in hard(report_of(tmp_path, "02_gemma"))


def make_mvp_case(tmp_path, trait="IMF"):
    d = tmp_path / "out"
    d.mkdir(exist_ok=True)
    for m in ("GLM", "MLM", "FarmCPU"):
        rows = ['"SNP","CHROM","POS","A1","A2","MAF","Effect","SE","%s.%s"' % (trait, m)]
        for i in range(10):
            p = 0.01 * (i + 1)
            rows.append(f'"1:{1000+i}",1,{1000+i},"A","G",0.2,0.1,0.05,{p}')
        _write(str(d / f"{trait}.{m}.csv"), "\n".join(rows) + "\n")
    mp = str(tmp_path / "snp.map")
    _write(mp, "SNP\tCHROM\tPOS\n" + "".join(f"1:{1000+i}\t1\t{1000+i}\n" for i in range(10)))
    return trait, str(d), mp


def test_s3_healthy_passes(tmp_path):
    trait, d, mp = make_mvp_case(tmp_path)
    rc, out = run_verify(tmp_path, "--stage", "03_mvp", "--trait", trait,
                         "--csv-dir", d, "--map", mp)
    assert rc == 0, out


def test_s3_missing_method_csv_is_blocked(tmp_path):
    trait, d, mp = make_mvp_case(tmp_path)
    os.remove(os.path.join(d, f"{trait}.FarmCPU.csv"))
    rc, _ = run_verify(tmp_path, "--stage", "03_mvp", "--trait", trait,
                       "--csv-dir", d, "--map", mp)
    assert rc == 1
    assert "FarmCPU.csv.exists" in hard(report_of(tmp_path, "03_mvp"))


def test_s3_p_column_missing_is_blocked(tmp_path):
    trait, d, mp = make_mvp_case(tmp_path)
    _write(os.path.join(d, f"{trait}.GLM.csv"),
           '"SNP","CHROM","POS","A1","A2","MAF","Effect","SE","X"\n"1:1000",1,1000,"A","G",0.2,0.1,0.05,0.01\n')
    rc, _ = run_verify(tmp_path, "--stage", "03_mvp", "--trait", trait,
                       "--csv-dir", d, "--map", mp)
    assert rc == 1
    assert "GLM.csv.pcol" in hard(report_of(tmp_path, "03_mvp"))


def test_s3_method_snp_sets_differ_is_blocked(tmp_path):
    trait, d, mp = make_mvp_case(tmp_path)
    p = os.path.join(d, f"{trait}.MLM.csv")
    rows = open(p, encoding="utf-8").read().splitlines()
    _write(p, "\n".join(rows[:-1]) + "\n")          # 少一个位点
    rc, _ = run_verify(tmp_path, "--stage", "03_mvp", "--trait", trait,
                       "--csv-dir", d, "--map", mp)
    assert rc == 1
    assert "methods.snp_set" in hard(report_of(tmp_path, "03_mvp"))


def make_s4_case(tmp_path, *, stale=False):
    _, bf = make_bfile(tmp_path)
    src = make_assoc(tmp_path, n=10)                 # p = 0.01..0.10
    img = tmp_path / "out"
    img.mkdir(exist_ok=True)
    _pdf(str(img / "manhattan.pdf"))
    _png(str(img / "qq.png"))
    sig = str(tmp_path / "sig.txt")
    ids = [f"v{i}" for i in range(10)]
    # 阈值 0.05 → v0..v3（p=0.01..0.04）
    got = ids[:4] + (["v9"] if stale else [])
    _write(sig, "".join(f'"{s}"\n' for s in got))
    gt = str(tmp_path / "region.genotype")
    _write(gt, "".join(f"1:{1000 + i * 10}\t1\t{1000 + i * 10}\tn1\tn2\n" for i in range(10)))
    return bf, src, sig, str(img), gt


def test_s4_healthy_passes(tmp_path):
    bf, src, sig, img, gt = make_s4_case(tmp_path)
    rc, out = run_verify(tmp_path, "--stage", "04_visualize", "--source", src,
                         "--sig-file", sig, "--sig-thresh", "0.05",
                         "--images-dir", img, "--bfile", bf,
                         "--region", "1:1000-1090", "--region-gt", gt)
    assert rc == 0, out


def test_s4_stale_sig_list_is_blocked(tmp_path):
    """实测过的真实形态：清单里有上游结果里达不到阈值的位点（陈旧清单）。"""
    bf, src, sig, img, gt = make_s4_case(tmp_path, stale=True)
    rc, out = run_verify(tmp_path, "--stage", "04_visualize", "--source", src,
                         "--sig-file", sig, "--sig-thresh", "0.05",
                         "--images-dir", img)
    assert rc == 1, "陈旧显著位点清单竟然通过了"
    assert "sig.matches" in hard(report_of(tmp_path, "04_visualize"))


def test_s4_bad_image_and_missing_image_are_blocked(tmp_path):
    bf, src, sig, img, gt = make_s4_case(tmp_path)
    _write(os.path.join(img, "qq.png"), "not an image")   # 坏文件头
    rc, _ = run_verify(tmp_path, "--stage", "04_visualize", "--source", src,
                       "--sig-file", sig, "--sig-thresh", "0.05", "--images-dir", img)
    assert rc == 1
    assert "plots.magic" in hard(report_of(tmp_path, "04_visualize"))


def make_s5_case(tmp_path):
    _, bf = make_bfile(tmp_path)
    import gzip as _gz
    out = str(tmp_path / "ld")
    _write(out + ".svg", "<svg xmlns='http://www.w3.org/2000/svg'>" + "x" * 2048 + "</svg>")
    _png(out + ".png")
    with _gz.open(out + ".site.gz", "wt", encoding="utf-8") as f:
        f.write("1\t1000\n1\t1001\n1\t1002\n")
    with _gz.open(out + ".blocks.gz", "wt", encoding="utf-8") as f:
        f.write("CHR\tBP1\tBP2\tKB\tNSNPS\tSNPS\n1\t1000\t1002\t0.2\t3\tx\n")
    src = make_assoc(tmp_path, n=10)
    return bf, out, src


def test_s5_healthy_passes(tmp_path):
    bf, out, src = make_s5_case(tmp_path)
    rc, o = run_verify(tmp_path, "--stage", "05_ld_block", "--out-prefix", out,
                       "--bfile", bf, "--region", "1:1000-1090",
                       "--source", src, "--top-site", "v0")
    assert rc == 0, o


def test_s5_missing_svg_is_blocked(tmp_path):
    bf, out, src = make_s5_case(tmp_path)
    os.remove(out + ".svg")
    rc, _ = run_verify(tmp_path, "--stage", "05_ld_block", "--out-prefix", out,
                       "--bfile", bf, "--region", "1:1000-1009")
    assert rc == 1
    assert "svg.exists" in hard(report_of(tmp_path, "05_ld_block"))


def test_s5_too_small_region_is_blocked(tmp_path):
    """区域位点 < 10 时 LDBlockShow 会除零 —— 必须在调用前拦住。"""
    bf, out, src = make_s5_case(tmp_path)
    rc, _ = run_verify(tmp_path, "--stage", "05_ld_block", "--out-prefix", out,
                       "--bfile", bf, "--region", "1:1000-1001")
    assert rc == 1
    assert "region.size" in hard(report_of(tmp_path, "05_ld_block"))


# --------------------------------------------------------------------------- #
# S0.2 → S0.5 的交接：keep.txt 决定"预期分析集"
# --------------------------------------------------------------------------- #

def _write_keep(path, pairs):
    with open(path, "w", encoding="utf-8") as f:
        for fid, iid in pairs:
            f.write(f"{fid}\t{iid}\n")
    return str(path)


def _fam_pairs(prefix):
    return [tuple(ln.split()[:2]) for ln in
            open(prefix + ".fam", encoding="utf-8").read().splitlines() if ln.strip()]


def test_s05_keep_file_defines_expected_sample_set(tmp_path):
    """S0.2 的 keep.txt 交给 S0.5：输出样本集必须**正好等于**它。

    这是"表型对齐 → 质控"的交接契约：对齐决定谁有表型，S0.5 用 --keep 固定分析集，
    门禁再核一遍"输出 == keep"，防止 keep 传了却没生效。
    """
    _, in_p = make_bfile(tmp_path)
    pairs = _fam_pairs(in_p)
    keep = _write_keep(tmp_path / "keep.txt", pairs[:10])
    out_p = str(tmp_path / "s0" / "filt_keep")
    run_plink("--bfile", in_p, "--keep", keep, "-maf", "0.1", "-geno", "0.05",
              "--make-bed", "--out", out_p, "--silent")
    qc = out_p + ".qc"
    run_plink("--bfile", out_p, "--freq", "--missing", "--hardy", "--out", qc, "--silent")

    rc, out = run_verify(tmp_path, *s05_args(out_p, in_p, qc, maf=0.1, geno=0.05),
                         "--keep-file", keep)
    assert rc == 0, f"按 keep 取子集被误拦：\n{out}"
    rep = report_of(tmp_path, "00.5_qc_filter")
    assert rep["n_samples"] == 10
    assert "out.expected_samples" in {c["name"] for c in rep["checks"]}
    assert "samples_conserved" not in (rep.get("hard_failures") or [])


def test_s05_keep_file_mismatch_is_blocked(tmp_path):
    """keep.txt 里多了输出中没有的样本 → S0.5 没按分析集过滤 → hard。"""
    _, in_p = make_bfile(tmp_path)
    pairs = _fam_pairs(in_p)
    keep = _write_keep(tmp_path / "keep.txt", pairs[:10])
    bad_keep = _write_keep(tmp_path / "bad_keep.txt", pairs[:10] + pairs[10:11])
    out_p = str(tmp_path / "s0" / "filt_keep")
    run_plink("--bfile", in_p, "--keep", keep, "-maf", "0.1", "-geno", "0.05",
              "--make-bed", "--out", out_p, "--silent")
    qc = out_p + ".qc"
    run_plink("--bfile", out_p, "--freq", "--missing", "--hardy", "--out", qc, "--silent")

    rc, out = run_verify(tmp_path, *s05_args(out_p, in_p, qc, maf=0.1, geno=0.05),
                         "--keep-file", bad_keep)
    assert rc == 1, f"keep 与输出不一致竟然通过：\n{out}"
    assert "out.expected_samples" in hard(report_of(tmp_path, "00.5_qc_filter"))
