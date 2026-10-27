# -*- coding: utf-8 -*-
"""WGS 阶段校验器（skills/wgs-expressive-pipeline/scripts/stage_verify.py）的判据测试。

用户诉求：在 WGS 管线每一步结束之后对结果值做校验，以免中途的错误传到后面步骤。

为什么必须配阴性对照
====================
本校验器要防的是一个**具体**的失效：脚本里 `wait "${pid}" || log_error ...` 吞掉了
子进程失败（`||` 使退出码变 0，`set -e` 不触发），脚本继续跑到末尾**无条件**写 `.done`，
编排层据此认为"完成"并推进下一阶段 —— 下游于是拿到空 BAM / 假 GVCF / 样本数不对的 VCF。

所以只测「正常产物能通过」是无效的：一个永远返回 pass 的校验器也能过那种测试，
而它对本失效毫无作用。每条硬性判据都要配一条**能触发的坏输入**，证明它真会拦。

测试用**真实 samtools/bcftools** 构造迷你 fixture（而非 mock）——这不是洁癖：
本校验器的两处真实 bug（GVCF 标记误判、Ti/Tv 取错列）都是靠真实工具输出才暴露的，
mock 会忠实复现我对输出格式的错误假设，从而把 bug 一路放过。

工具缺失时整体 skip（不伪报通过）。
"""
import json
import os
import re
import shutil
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SV_PATH = os.path.join(ROOT, "skills", "wgs-expressive-pipeline", "scripts")
sys.path.insert(0, SV_PATH)

import stage_verify as SV  # noqa: E402

SAMTOOLS = shutil.which("samtools")
BCFTOOLS = shutil.which("bcftools")

pytestmark = pytest.mark.skipif(
    not (SAMTOOLS and BCFTOOLS),
    reason="需要真实 samtools/bcftools 构造 fixture（缺工具时 skip，不伪报通过）")


# ============================ fixture 构造 ============================
REF = ("ACGT" * 2000)          # 8000bp 参考，重复但有 60bp 窗口内足够多样


def _sh(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, check=True, **kw)


def make_bam(path, *, reads=20, sample="sample1", sorted_=True,
             with_rg=True, header_only=False):
    """造一个迷你 BAM。header_only=True 时造**空 BAM**（只有头）。"""
    import random
    random.seed(hash(sample) % 1000)
    hdr = "@HD\tVN:1.6\tSO:coordinate\n@SQ\tSN:chr1\tLN:8000\n"
    if with_rg:
        hdr = (f"@HD\tVN:1.6\tSO:coordinate\n@SQ\tSN:chr1\tLN:8000\n"
               f"@RG\tID:{sample}\tSM:{sample}\tLB:{sample}\tPL:ILLUMINA\n")
    lines = []
    if not header_only:
        for i in range(reads):
            p = random.randint(0, 3000)
            lines.append(f"r{i}\t0\tchr1\t{p+1}\t60\t60M\t*\t0\t0\t{REF[p:p+60]}"
                         f"\t{'I'*60}\tRG:Z:{sample}")
            for l in lines:
                pass
    sam = path + ".sam"
    with open(sam, "w") as f:
        f.write(hdr + "\n".join(lines) + ("\n" if lines else ""))
    if sorted_:
        _sh([SAMTOOLS, "sort", "-o", path, sam])
    else:
        _sh([SAMTOOLS, "view", "-b", "-o", path, sam])
    _sh([SAMTOOLS, "index", path])
    os.remove(sam)
    return path


def make_vcf(path, *, variants=20, sample="S1", gvcf=False, all_filtered=False,
             ts_tv_ratio=None):
    """造一个 bgzip+tabix 的迷你 VCF。

    gvcf=True  → 头部含 ##ALT=<ID=NON_REF,...>（真 GVCF 的标志）
    all_filtered=True → 所有记录 FILTER=LowQual（模拟阈值过严）
    ts_tv_ratio=n → 控制 ts:tv 比例（None 表示交替）
    """
    import random
    random.seed(variants)
    ts = [("A", "G"), ("G", "A"), ("C", "T"), ("T", "C")]
    tv = [("A", "C"), ("A", "T"), ("C", "A"), ("C", "G"), ("G", "C"), ("G", "T"), ("T", "A"), ("T", "G")]
    out = ["##fileformat=VCFv4.2", "##contig=<ID=chr1,length=8000>",
           '##FILTER=<ID=LowQual,Description="low">',
           '##INFO=<ID=QD,Number=1,Type=Float,Description="q">',
           '##FORMAT=<ID=GT,Number=1,Type=String,Description="g">']
    if gvcf:
        out.insert(2, '##ALT=<ID=NON_REF,Description="Represents any possible alternative allele">')
    out.append(f"#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\t{sample}")
    for i in range(variants):
        if ts_tv_ratio is not None:
            n_ts = int(round(ts_tv_ratio / (1 + ts_tv_ratio) * variants))
            r, a = ts[i % len(ts)] if i < n_ts else tv[i % len(tv)]
        else:
            r, a = (ts + tv)[i % 12]
        alt = f"{a},<NON_REF>" if gvcf else a
        filt = "LowQual" if all_filtered else ("LowQual" if i % 10 == 0 else "PASS")
        out.append(f"chr1\t{100+i*7}\t.\t{r}\t{alt}\t50\t{filt}\tQD=10\tGT\t0/1")
    raw = path[:-3] if path.endswith(".gz") else path
    with open(raw, "w") as f:
        f.write("\n".join(out) + "\n")
    _sh(["bgzip", "-f", raw])
    gz = raw + ".gz"
    _sh(["tabix", "-p", "vcf", gz])
    if gz != path:
        os.replace(gz, path)
        os.replace(gz + ".tbi", path + ".tbi")
    return path


def run_verifier(run_dir, stage, **kw):
    """在进程内跑校验器，返回 (rc, Report-as-dict)。"""
    import io
    from contextlib import redirect_stdout
    argv = ["stage_verify.py", "--stage", stage, "--run-dir", str(run_dir)]
    for k, v in kw.items():
        if v is None:
            continue
        flag = "--" + k.replace("_", "-")
        if v is True:
            argv.append(flag)
        elif v is not False:
            argv += [flag, str(v)]
    old = sys.argv
    sys.argv = argv
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            rc = SV.main()
    finally:
        sys.argv = old
    import json
    vf = os.path.join(str(run_dir), ".verify", f"{stage}.json")
    rep = json.load(open(vf)) if os.path.isfile(vf) else None
    return rc, rep


def names(rep, level):
    return [c["name"] for c in rep["checks"]
            if c["level"] == level and not c["passed"] and not c["skipped"]]


# ============================ Stage 1：BAM 判据 ============================
def test_stage1_healthy_bam_passes(tmp_path):
    """阳性：正常 BAM + @RG 齐全 → 通过。"""
    out = tmp_path / "out"
    out.mkdir()
    for s in ("sample1", "sample2"):
        make_bam(str(out / f"{s}.sorted.bam"), sample=s, reads=20)
    rc, rep = run_verifier(tmp_path, "01_bwa_sort", samples="sample1,sample2")
    assert rc == 0, rep
    assert not rep["hard_failures"], rep


def test_stage1_empty_bam_is_caught(tmp_path):
    """★核心阴性对照：**空 BAM** 必须被拦。

    这正是本校验器存在的理由 —— 且它**能通过** samtools quickcheck
    （quickcheck 只验文件结构完整，不看有无数据），所以只有 reads 计数能抓住它。
    """
    out = tmp_path / "out"
    out.mkdir()
    make_bam(str(out / "sample1.sorted.bam"), sample="sample1", reads=20)
    make_bam(str(out / "sample2.sorted.bam"), sample="sample2", header_only=True)
    rc, rep = run_verifier(tmp_path, "01_bwa_sort", samples="sample1,sample2")
    assert rc == 1, "空 BAM 未被拦截"
    assert any("read_count" in n for n in names(rep, "hard")), rep
    # 同时确认 quickcheck 对它**是**放行的 —— 否则这条对照没有区分度
    rc2, out2, _ = SV.run([SAMTOOLS, "quickcheck", str(out / "sample2.sorted.bam")])
    assert rc2 == 0, "前提不成立：空 BAM 本应能过 quickcheck，才需要 reads 计数兜底"


def test_stage1_missing_index_is_caught(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    make_bam(str(out / "sample1.sorted.bam"), sample="sample1")
    os.remove(str(out / "sample1.sorted.bam.bai"))
    rc, rep = run_verifier(tmp_path, "01_bwa_sort", samples="sample1")
    assert rc == 1
    assert any("index" in n for n in names(rep, "hard")), rep


def test_stage1_stale_index_does_not_mask_empty_bam(tmp_path):
    """★核心阴性对照：**索引比 BAM 旧**时，空 BAM 必须仍被拦。

    这条防的是一个比"没索引"更隐蔽的形态：`samtools idxstats` 读的是**索引**而不是
    BAM 本体。重跑 / `--resume` 跳过 index 步骤 / 直接覆写产物时，索引停在上一轮，
    于是 idxstats 拿旧值作答 —— 0 reads 的空 BAM 报出上一轮的 reads 数，
    `read_count`、`mapped_ratio`、`mapped_rate` 三条判据**同时被骗过**，
    门禁 `✅ 全部检查通过` 放行。

    这正是本校验器 docstring 列的头号防守对象（空 BAM 传导到下游、要到
    "变异数异常少"才暴露），所以必须有对照钉住它。

    构造成本说明：需要两个 BAM + 1 秒 mtime 间隔（samtools 按 mtime 判新旧）。
    """
    import time
    out = tmp_path / "out"
    out.mkdir()
    bam = str(out / "sample1.sorted.bam")

    # 1) 先造健康 BAM 并建索引（模拟上一轮的成功产物）
    make_bam(bam, sample="sample1", reads=20)
    assert SV.run([SAMTOOLS, "view", "-c", bam])[1].strip() == "20"

    # 2) 用空 BAM **覆写**数据文件，索引保持不动（关键：不重建索引）
    time.sleep(1.1)                      # samtools 按整秒 mtime 比较，必须跨秒
    make_bam(bam, sample="sample1", header_only=True)
    os.remove(bam + ".bai")              # make_bam 会建索引，这里移掉它
    # 手工把旧索引放回来（内容无所谓，只要 mtime 比 BAM 旧）
    make_bam(str(out / "old.bam"), sample="sample1", reads=20)
    os.replace(str(out / "old.bam.bai"), bam + ".bai")
    os.utime(bam + ".bai", (os.path.getmtime(bam) - 60,) * 2)

    # 前提自检：BAM 真的是空的，而 samtools 真的会报索引陈旧（否则这条对照没有区分度）
    assert SV.run([SAMTOOLS, "view", "-c", bam])[1].strip() == "0"
    rc_idx, _out_idx, err_idx = SV.run([SAMTOOLS, "idxstats", bam])
    assert rc_idx == 0, "前提不成立：idxstats 本应能跑（它正是骗过门禁的那一步）"
    assert "older than" in err_idx, (
        f"前提不成立：samtools 未报索引陈旧，err={err_idx!r}")

    rc, rep = run_verifier(tmp_path, "01_bwa_sort", samples="sample1")
    assert rc == 1, "陈旧索引把空 BAM 骗过了门禁"
    hard = names(rep, "hard")
    assert any("index_stale" in n for n in hard), rep
    assert any("read_count" in n for n in hard), rep
    # read_count 必须报**真实**的 0，而不是索引里的旧值
    rc_entry = [c for c in rep["checks"] if c["name"].endswith("read_count")][0]
    assert "total=0" in rc_entry["detail"], rc_entry
    # 索引陈旧时无法拆分 mapped/unmapped，必须明说"未做该判定"而非静默
    assert any("mapped_ratio" in n for n in names(rep, "warn")), rep


def test_stage1_stale_index_fixed_by_reindexing_passes(tmp_path):
    """对照：把索引重建后，同一份数据必须恢复放行（修法可执行，不是死锁）。"""
    import time
    out = tmp_path / "out"
    out.mkdir()
    bam = str(out / "sample1.sorted.bam")
    make_bam(bam, sample="sample1", reads=20)
    time.sleep(1.1)
    os.utime(bam, None)                  # 让 BAM 比索引新
    rc, _ = run_verifier(tmp_path, "01_bwa_sort", samples="sample1")
    assert rc == 1, "前提不成立：BAM 比索引新时本应判陈旧"
    _sh([SAMTOOLS, "index", bam])        # 重建索引
    rc2, rep2 = run_verifier(tmp_path, "01_bwa_sort", samples="sample1")
    assert rc2 == 0, rep2
    assert not any("index_stale" in n for n in names(rep2, "hard")), rep2


def test_stage1_missing_readgroup_is_caught(tmp_path):
    """缺 @RG 必须拦 —— GATK 后续工具会报 'Number of read groups must be >= 1'。"""
    out = tmp_path / "out"
    out.mkdir()
    make_bam(str(out / "sample1.sorted.bam"), sample="sample1", with_rg=False)
    rc, rep = run_verifier(tmp_path, "01_bwa_sort", samples="sample1")
    assert rc == 1
    assert any("readgroup" in n for n in names(rep, "hard")), rep


def test_stage1_missing_sample_is_caught(tmp_path):
    """样本缺失必须拦（并报出是哪个）。"""
    out = tmp_path / "out"
    out.mkdir()
    make_bam(str(out / "sample1.sorted.bam"), sample="sample1")
    rc, rep = run_verifier(tmp_path, "01_bwa_sort", samples="sample1,sample2")
    assert rc == 1
    det = " ".join(c["detail"] for c in rep["checks"] if "cohort_sample_set" in c["name"])
    assert "sample2" in det, rep


def test_stage1_coordinate_header_is_checked(tmp_path):
    """`SO:coordinate` 头标签缺失时必须拦。

    说明为什么不用「真乱序 BAM」做这条对照：samtools **拒绝**给乱序 BAM 建索引
    （`[E::hts_idx_push] Unsorted positions`），所以「乱序且带索引」这个状态在
    现实中不存在 —— 真乱序的 BAM 会先被 `.index` 判据拦下。
    这里改为直接构造**缺少 SO 标签**的头，精确验证排序判据本身。
    """
    out = tmp_path / "out"
    out.mkdir()
    # 头里不带 @HD 行（故无 SO 标签）——用 samtools 的 -u（未压缩）不重排
    hdr = "@SQ\tSN:chr1\tLN:8000\n@RG\tID:s1\tSM:s1\tLB:s1\tPL:ILLUMINA\n"
    lines = [f"r{i}\t0\tchr1\t{200+i*10}\t60\t60M\t*\t0\t0\t{REF[200+i*10:260+i*10]}\t{'I'*60}\tRG:Z:s1"
             for i in range(8)]
    sam = str(tmp_path / "nohd.sam")
    with open(sam, "w") as f:
        f.write(hdr + "\n".join(lines) + "\n")
    _sh([SAMTOOLS, "view", "-b", "-o", str(out / "s1.sorted.bam"), sam])
    _sh([SAMTOOLS, "index", str(out / "s1.sorted.bam")])
    rc, rep = run_verifier(tmp_path, "01_bwa_sort", samples="s1")
    assert rc == 1, "缺 SO 标签未被拦截"
    assert any("sorted" in n for n in names(rep, "hard")), rep
    det = " ".join(c["detail"] for c in rep["checks"] if c["name"].endswith(".sorted"))
    assert "无 SO 标签" in det, det


# ============================ Stage 3：GVCF 判据 ============================
def test_stage3_real_gvcf_passes(tmp_path):
    """阳性：头部有 ##ALT=<ID=NON_REF> 的真 GVCF → 通过。"""
    out = tmp_path / "out"
    out.mkdir()
    make_vcf(str(out / "S1.g.vcf.gz"), sample="S1", gvcf=True, variants=15)
    rc, rep = run_verifier(tmp_path, "03_haplotypecaller", samples="S1")
    assert rc == 0, rep


def test_stage3_fake_gvcf_is_caught(tmp_path):
    """★核心阴性对照：漏加 -ERC GVCF 产出的**普通 VCF** 必须被拦。

    下游 Stage 4 CombineGVCFs 面对它必失败；这条判据把错误挡在第 3 步。
    """
    out = tmp_path / "out"
    out.mkdir()
    make_vcf(str(out / "S1.g.vcf.gz"), sample="S1", gvcf=False, variants=15)
    rc, rep = run_verifier(tmp_path, "03_haplotypecaller", samples="S1")
    assert rc == 1, "假 GVCF（缺 NON_REF 声明）未被拦截"
    assert any("gvcf_marker" in n for n in names(rep, "hard")), rep


def test_gvcf_marker_checks_header_declaration_not_literal():
    """判据本身的区分度：必须认 `##ALT=<ID=NON_REF` 头部声明。

    回归防护：早期版本在头部里找字面量 ``<NON_REF>``（带尖括号），而 VCF 头部写的是
    ``##ALT=<ID=NON_REF,Description=...>``（ID 值不带尖括号）—— 于是真 GVCF 被判成假。
    这里直接钉住正则的语义。
    """
    real_header = '##ALT=<ID=NON_REF,Description="Represents any possible alternative allele">\n'
    plain_header = '##ALT=<ID=DEL,Description="deletion">\n'
    assert re.search(r"^##ALT=<ID=NON_REF", real_header, re.M)
    assert not re.search(r"^##ALT=<ID=NON_REF", plain_header, re.M)


def test_stage3_cohort_coverage_is_checked(tmp_path):
    """队列里有样本没产出 GVCF → 必须拦（Stage 4 需要全部样本）。"""
    out = tmp_path / "out"
    out.mkdir()
    make_vcf(str(out / "S1.g.vcf.gz"), sample="S1", gvcf=True, variants=15)
    rc, rep = run_verifier(tmp_path, "03_haplotypecaller", samples="S1,S2")
    assert rc == 1
    assert "gvcf_coverage" in names(rep, "hard"), rep


# ============================ Stage 5：过滤判据 ============================
def test_stage5_healthy_filters_pass(tmp_path):
    """阳性：SNP/INDEL 都有 PASS 位点、Ti/Tv 合理 → 通过。"""
    out = tmp_path / "out"
    out.mkdir()
    make_vcf(str(out / "co.snp.filter.vcf.gz"), sample="S1", variants=40, ts_tv_ratio=2.0)
    make_vcf(str(out / "co.indel.filter.vcf.gz"), sample="S1", variants=10)
    rc, rep = run_verifier(tmp_path, "05_filtering", prefix="co", samples="S1")
    assert rc == 0, rep


def test_stage5_all_filtered_indel_is_caught(tmp_path):
    """★阴性对照：INDEL 全被滤掉必须硬拦。

    这条专门防「只检查 SNP」的疏漏 —— INDEL 的 FS/MQRankSum 阈值比 SNP 严得多，
    INDEL 被滤光是真实且常见的失败，而 SNP 侧看起来一切正常。
    """
    out = tmp_path / "out"
    out.mkdir()
    make_vcf(str(out / "co.snp.filter.vcf.gz"), sample="S1", variants=40, ts_tv_ratio=2.0)
    make_vcf(str(out / "co.indel.filter.vcf.gz"), sample="S1", variants=10, all_filtered=True)
    rc, rep = run_verifier(tmp_path, "05_filtering", prefix="co", samples="S1")
    assert rc == 1, "INDEL 全被过滤未被拦截"
    assert any("indel.pass_rate" in n for n in names(rep, "hard")), rep


def test_stage5_empty_snp_is_caught(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    make_vcf(str(out / "co.snp.filter.vcf.gz"), sample="S1", variants=40)
    make_vcf(str(out / "co.indel.filter.vcf.gz"), sample="S1", variants=10)
    # 把 SNP 文件替换成只有头的空 VCF
    p = str(out / "co.snp.filter.vcf.gz")
    os.remove(p); os.remove(p + ".tbi")
    make_vcf(p, sample="S1", variants=0)
    rc, rep = run_verifier(tmp_path, "05_filtering", prefix="co", samples="S1")
    assert rc == 1
    assert any("snp.variants" in n or "snp.exists" in n for n in names(rep, "hard")), rep


def test_titv_ratio_parsing_matches_bcftools_format(tmp_path):
    """回归防护：Ti/Tv 必须取 TSTV 行的**比值列**（第 5 个字段），不是 tv 计数。

    bcftools stats 的行格式：``TSTV <id> <ts> <tv> <ratio> ...``
    早期版本误取第 4 个字段（tv 计数）→ 把 tv=20 报成 Ti/Tv=20.00。
    """
    out = tmp_path / "out"
    out.mkdir()
    make_vcf(str(out / "co.snp.filter.vcf.gz"), sample="S1",
             variants=60, ts_tv_ratio=2.0)
    make_vcf(str(out / "co.indel.filter.vcf.gz"), sample="S1", variants=10)
    rc, rep = run_verifier(tmp_path, "05_filtering", prefix="co", samples="S1")
    chk = next(c for c in rep["checks"] if c["name"] == "snp.titv")
    m = re.search(r"Ti/Tv=([\d.]+)", chk["detail"])
    assert m, chk
    val = float(m.group(1))
    assert 1.5 <= val <= 2.5, f"Ti/Tv 解析错误：{val}（应为 ~2.0，取错列会得到 20.00）"


# ============================ Stage 4：样本数守恒 ============================
def test_stage4_sample_count_mismatch_is_caught(tmp_path):
    """★阴性对照：联合分型 VCF 的样本数与输入 GVCF 数不符 → 必须拦。

    这是「样本在中间某步被丢掉」的典型表现，且下游关联分析会静默地用错样本集。
    """
    out = tmp_path / "out"
    out.mkdir()
    make_vcf(str(out / "co.vcf.gz"), sample="SAMPLE_A", variants=25)
    rc, rep = run_verifier(tmp_path, "04_joint_genotyping", prefix="co",
                           samples="SAMPLE_A,SAMPLE_B")
    assert rc == 1, "样本数不符未被拦截"
    assert any("samples" in n for n in names(rep, "hard")), rep


def test_stage4_matching_samples_pass(tmp_path):
    """阳性：样本集合一致 → 通过。"""
    out = tmp_path / "out"
    out.mkdir()
    make_vcf(str(out / "co.vcf.gz"), sample="SAMPLE_A", variants=25)
    rc, rep = run_verifier(tmp_path, "04_joint_genotyping", prefix="co", samples="SAMPLE_A")
    assert not any("samples" in n for n in rep["hard_failures"]), rep


# ==================== 位点数下限（"变异数异常少"）====================
def test_tiny_variant_count_is_caught_when_floor_declared(tmp_path):
    """★阴性对照：显式声明下限后，位点数异常少必须被拦。

    本模块 docstring 把「变异数异常少」列为要防的头号失效（下游不报错，很久以后才
    以"变异数异常少"的面貌出现）。但早期实现里**只有 `min_variants=1`**，即只拦
    "完全为空" —— 实测一个 2 样本队列的 VCF 只有 2 条记录时仍 `✅ 全部检查通过`、
    rc=0。该阶段 skill 自称的用途实际未落地。
    """
    out = tmp_path / "out"
    out.mkdir()
    # 2 样本、仅 2 个位点 —— 全基因组队列真实应有数百万
    make_vcf(str(out / "co.vcf.gz"), sample="SAMPLE_A", variants=2)
    rc, rep = run_verifier(tmp_path, "04_joint_genotyping", prefix="co",
                           samples="SAMPLE_A", min_variants=1000)
    assert rc == 1, "位点数远低于声明下限却未被拦截"
    assert "joint.variant_floor" in names(rep, "hard"), rep


def test_variant_floor_undeclared_warns_not_silent(tmp_path):
    """未声明下限时**不能静默通过**，要明说"没做这个判定"。

    否则又是一处"纸面具备"：报告全绿会让人以为变异数已被把关，而实际什么都没断言。
    同时确认它**不拦截**（合理位点数取决于参考基因组大小/队列大小/物种，
    拍一个硬编码默认值必然要么拦健康数据，要么松到没用）。
    """
    out = tmp_path / "out"
    out.mkdir()
    # ⚠️ combined.g.vcf.gz 是 Stage 4 的**契约产物**（见 SKILL.md 输出表与
    #    错误预防清单第 6 条），必须一起造 —— 早期这两条测试只造 joint VCF，
    #    是 fixture 不完整；A6 补上 combined 判据后它们才暴露出来。
    #    不能为了迁就而放宽判据：真实产物就是两个文件都在。
    make_vcf(str(out / "co.combined.g.vcf.gz"), sample="SAMPLE_A", variants=2, gvcf=True)
    make_vcf(str(out / "co.vcf.gz"), sample="SAMPLE_A", variants=2)
    rc, rep = run_verifier(tmp_path, "04_joint_genotyping", prefix="co", samples="SAMPLE_A")
    fl = next((c for c in rep["checks"] if c["name"] == "joint.variant_floor"), None)
    assert fl is not None, "未声明下限时连一条说明都没有 —— 静默通过"
    assert fl["level"] == "warn", fl
    assert "未做" in fl["detail"], fl
    assert rc == 0, "未声明下限不应拦截（默认值无法普适）"


def test_variant_floor_passes_when_above_threshold(tmp_path):
    """阳性对照：位点数高于声明下限时不拦（否则上一条可能被"永远拦"蒙混）。"""
    out = tmp_path / "out"
    out.mkdir()
    make_vcf(str(out / "co.combined.g.vcf.gz"), sample="SAMPLE_A", variants=150, gvcf=True)
    make_vcf(str(out / "co.vcf.gz"), sample="SAMPLE_A", variants=150)
    rc, rep = run_verifier(tmp_path, "04_joint_genotyping", prefix="co",
                           samples="SAMPLE_A", min_variants=100)
    fl = next(c for c in rep["checks"] if c["name"] == "joint.variant_floor")
    assert fl["passed"] and fl["level"] == "hard", fl
    assert rc == 0, rep


def _write_params(run_dir, **kv):
    with open(os.path.join(str(run_dir), "run.params.json"), "w",
              encoding="utf-8") as f:
        json.dump(kv, f, ensure_ascii=False)


def _tiny_joint_fixture(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    make_vcf(str(out / "co.combined.g.vcf.gz"), sample="SAMPLE_A", variants=2, gvcf=True)
    make_vcf(str(out / "co.vcf.gz"), sample="SAMPLE_A", variants=2)


def test_min_variants_declarable_from_run_params_json(tmp_path):
    """★声明通路必须真的存在：Stage 0 在 run.params.json 里写 `min_variants` 即可生效。

    只提供一个 `--min-variants` CLI 开关**不构成可用的声明路径** —— 阶段脚本由 LLM
    现场生成，没有任何一处会把它写进命令行，于是这条判据恒停在 warn。这正是本项目
    反复出现的"纸面具备"：文档说"由调用方显式声明"，而调用方**无从声明**。
    故本测试钉住真正的通路（与 `verifier_path` 同范式：Stage 0 解析落盘，阶段侧读回）。
    """
    _tiny_joint_fixture(tmp_path)
    _write_params(tmp_path, sample_names=["SAMPLE_A"], min_variants=1000)
    rc, rep = run_verifier(tmp_path, "04_joint_genotyping", prefix="co", samples="SAMPLE_A")
    assert rc == 1, "run.params.json 里声明了下限却未被拦截 —— 声明通路没接通"
    fl = next(c for c in rep["checks"] if c["name"] == "joint.variant_floor")
    assert fl["level"] == "hard" and not fl["passed"], fl
    assert "run.params.json" in fl["detail"], f"报告应写明下限来自哪里: {fl}"


def test_min_variants_cli_overrides_run_params_json(tmp_path):
    """CLI 显式给了就以 CLI 为准（run.params.json 是回落，不是覆盖）。"""
    _tiny_joint_fixture(tmp_path)
    _write_params(tmp_path, sample_names=["SAMPLE_A"], min_variants=1)  # 松
    rc, rep = run_verifier(tmp_path, "04_joint_genotyping", prefix="co",
                           samples="SAMPLE_A", min_variants=1000)       # 紧，应生效
    assert rc == 1, "CLI 的紧下限没生效（被 run.params.json 的松值盖住了？）"
    fl = next(c for c in rep["checks"] if c["name"] == "joint.variant_floor")
    assert "1000" in fl["detail"], fl


def test_min_variants_from_run_params_json_absent_is_still_warn(tmp_path):
    """键不存在 = 真未声明 → warn 明说未判定（保持原设计，不变成默认拦截）。"""
    _tiny_joint_fixture(tmp_path)
    _write_params(tmp_path, sample_names=["SAMPLE_A"])       # 没有 min_variants 键
    rc, rep = run_verifier(tmp_path, "04_joint_genotyping", prefix="co", samples="SAMPLE_A")
    fl = next(c for c in rep["checks"] if c["name"] == "joint.variant_floor")
    assert fl["level"] == "warn" and not fl["passed"], fl
    assert "未做" in fl["detail"], fl
    assert rc == 0, "未声明不应拦截"


@pytest.mark.parametrize("bad", ["1M", 0, -5, 3.5, True, None])
def test_min_variants_malformed_in_run_params_json_blocks(tmp_path, bad):
    """★写在 run.params.json 里但**无法生效**时必须拦住，不能静默降级成 warn。

    写这个键 = 调用方**明确想开**"变异数异常少"保护。若类型不对（`"1M"`/`0`/`-5`/
    `3.5`/`true`/`null`）而我们当作"未声明"处理，报告会全绿（只有一条 warn），
    用户以为已把关 —— 保护缺席而无人知晓。这与 `verifier_path` 段同一约定：
    **想开却没生效 = 不放行**（fail-closed）。
    """
    _tiny_joint_fixture(tmp_path)
    _write_params(tmp_path, sample_names=["SAMPLE_A"], min_variants=bad)
    rc, rep = run_verifier(tmp_path, "04_joint_genotyping", prefix="co", samples="SAMPLE_A")
    assert rc == 1, f"min_variants={bad!r} 无法生效却被放行（应判 fail-closed）"
    assert "min_variants_declared" in names(rep, "hard"), rep


def test_min_variants_malformed_does_not_break_other_stages(tmp_path):
    """畸形的 min_variants 不应把**所有**阶段都拦掉（只在需要它的阶段生效）。

    本判据属于变异数语义（Stage 3/4/5）。若它变成无条件拦截，Stage 1 这种与位点数
    无关的阶段也会被卡住 —— 那是把 fail-closed 用错了地方。
    """
    out = tmp_path / "out"
    out.mkdir()
    make_bam(str(out / "sample1.sorted.bam"), reads=20, sample="sample1")  # 自带 .bai
    _write_params(tmp_path, sample_names=["sample1"], min_variants="oops")
    rc, rep = run_verifier(tmp_path, "01_bwa_sort", samples="sample1")
    # Stage 1 的结论不受 min_variants 影响：不该出现该判据
    assert "min_variants_declared" not in [c["name"] for c in rep["checks"]], rep


# ============================ Stage 2：reads 守恒链 ============================
def test_stage1_records_read_totals_for_next_stage(tmp_path):
    """★Stage 1 必须把 reads 数**真的写进**报告，供 Stage 2 比较。

    实测过的缺陷：Stage 2 会读 `prev["read_totals"]`，但没有任何地方写过这个键 ——
    那条守恒检查于是永远静默跳过（声称存在、从不生效，即本项目说的"纸面具备"）。
    这条钉住写入侧。
    """
    out = tmp_path / "out"
    out.mkdir()
    make_bam(str(out / "s1.sorted.bam"), sample="s1", reads=30)
    rc, rep = run_verifier(tmp_path, "01_bwa_sort", samples="s1")
    assert rc == 0, rep
    assert rep.get("read_totals"), "Stage 1 报告里没有 read_totals —— 守恒链断开"
    assert rep["read_totals"].get("s1") == 30, rep["read_totals"]


def test_stage2_reads_conservation_actually_fires(tmp_path):
    """★阴性对照：Stage 2 的 reads 数若与 Stage 1 严重不符，必须告警。

    证明这条路**真会触发**，而不只是代码里写着。造一份前阶段报告说 s1 有 300 reads，
    实际 BQSR BAM 只有 30 → 偏差远超 ±10% → 应告警。
    """
    out = tmp_path / "out"
    out.mkdir()
    make_bam(str(out / "s1.sorted.markdup.BQSR.bam"), sample="s1", reads=30)
    prev = tmp_path / "prev.json"
    prev.write_text(json.dumps({"stage": "01_bwa_sort",
                                "read_totals": {"s1": 300}}))
    rc, rep = run_verifier(tmp_path, "02_markdup_bqsr", samples="s1",
                           prev_reads_file=str(prev))
    con = next((c for c in rep["checks"] if c["name"] == "reads_conservation"), None)
    assert con is not None, "守恒检查根本没跑"
    assert not con["passed"], f"reads 从 300 变成 30 却未告警：{con}"
    assert "s1" in con["detail"], con


def test_stage2_reads_conservation_passes_when_consistent(tmp_path):
    """阳性：reads 数一致时守恒检查通过（不能永远告警）。"""
    out = tmp_path / "out"
    out.mkdir()
    make_bam(str(out / "s1.sorted.markdup.BQSR.bam"), sample="s1", reads=50)
    prev = tmp_path / "prev.json"
    prev.write_text(json.dumps({"read_totals": {"s1": 50}}))
    rc, rep = run_verifier(tmp_path, "02_markdup_bqsr", samples="s1",
                           prev_reads_file=str(prev))
    con = next(c for c in rep["checks"] if c["name"] == "reads_conservation")
    assert con["passed"], con
    assert rc == 0, rep


def test_stage2_missing_prev_file_warns_not_silent(tmp_path):
    """前阶段报告缺失时要**说出来**，不能静默跳过。

    静默跳过会让"守恒检查"看起来存在、实际从不生效 —— 正是本项目要防的形态。
    """
    out = tmp_path / "out"
    out.mkdir()
    make_bam(str(out / "s1.sorted.markdup.BQSR.bam"), sample="s1", reads=20)
    rc, rep = run_verifier(tmp_path, "02_markdup_bqsr", samples="s1",
                           prev_reads_file=str(tmp_path / "nope.json"))
    con = next(c for c in rep["checks"] if c["name"] == "reads_conservation")
    assert not con["passed"], "前阶段报告缺失却当成通过 —— 静默失效"
    assert "不存在" in con["detail"] or "无从比较" in con["detail"], con


def test_stage2_no_prev_file_arg_warns_not_silent(tmp_path):
    """根本没传 --prev-reads-file 时也要告警（而不是当作检查通过）。"""
    out = tmp_path / "out"
    out.mkdir()
    make_bam(str(out / "s1.sorted.markdup.BQSR.bam"), sample="s1", reads=20)
    rc, rep = run_verifier(tmp_path, "02_markdup_bqsr", samples="s1")
    con = next(c for c in rep["checks"] if c["name"] == "reads_conservation")
    assert not con["passed"], "未传前阶段报告却当成通过"
    assert "未提供" in con["detail"] or "跳过" in con["detail"], con


# ============ 索引命名：GATK 写 `foo.bai`，不是 `foo.bam.bai` ============
def test_gatk_style_bai_is_recognized(tmp_path):
    """`foo.bai` 是**合法**索引名，不能判缺失。

    `samtools index foo.bam` 写 `foo.bam.bai`，而 GATK/Picard 的
    `--CREATE_INDEX true` 把索引写成 `foo.bai`（`.bam` 整个换成 `.bai`）。
    两者 samtools 都能自动识别。早期实现只查 `bam + ".bai"`，于是**真机上
    GATK 产出的 BQSR BAM 稳定被判"缺索引"** —— 健康数据被拦下（实测复现：
    ApplyBQSR 产出 `S1.sorted.markdup.BQSR.bai`，校验器报缺
    `S1.sorted.markdup.BQSR.bam.bai`，rc=1）。
    """
    out = tmp_path / "out"
    out.mkdir()
    p = make_bam(str(out / "s1.sorted.markdup.BQSR.bam"), sample="s1", reads=30)
    # 从 samtools 写法改成 GATK 写法
    os.rename(p + ".bai", str(out / "s1.sorted.markdup.BQSR.bai"))
    rc, rep = run_verifier(tmp_path, "02_markdup_bqsr", samples="s1")
    idx = next(c for c in rep["checks"] if c["name"] == "bqsr[s1].index")
    assert idx["passed"], f"GATK 风格的 .bai 未被识别: {idx}"
    assert rc == 0, rep
    # 顺带确认 idxstats 走的是索引路径（快），而非回退全量计数
    cnt = next(c for c in rep["checks"] if c["name"] == "bqsr[s1].read_count")
    assert "无索引" not in cnt["detail"], cnt


def test_bqsr_index_is_hard_but_markdup_index_is_warn(tmp_path):
    """两个索引的判级必须不同 —— 照抄成一档会两边都错。

    · BQSR BAM：Stage 3 的 HaplotypeCaller 无索引直接 rc=2
      （实测 `A USER ERROR ... some input files are not indexed`）→ **hard**
    · markdup BAM：`gatk MarkDuplicates` 的 CREATE_INDEX **默认 false**，标准命令
      本就不产出；下游读它也不需要索引 → 若判 hard 会稳定拦下健康数据 → **warn**
    """
    out = tmp_path / "out"
    out.mkdir()
    # 只有 markdup BAM（无索引，复刻 MarkDuplicates 默认行为）
    make_bam(str(out / "s1.sorted.markdup.bam"), sample="s1", reads=30)
    os.remove(str(out / "s1.sorted.markdup.bam.bai"))
    # BQSR BAM 带索引
    make_bam(str(out / "s1.sorted.markdup.BQSR.bam"), sample="s1", reads=30)

    rc, rep = run_verifier(tmp_path, "02_markdup_bqsr", samples="s1")
    md = next(c for c in rep["checks"] if c["name"] == "markdup[s1].index")
    bq = next(c for c in rep["checks"] if c["name"] == "bqsr[s1].index")
    assert md["level"] == "warn", f"markdup 索引应为 warn（MarkDuplicates 默认不产出）: {md}"
    assert not md["passed"]
    assert bq["level"] == "hard" and bq["passed"], bq
    assert rc == 0, "markdup 缺索引不应拦截（健康数据的常见形态）"


def test_bqsr_missing_index_still_blocks(tmp_path):
    """阴性对照：BQSR BAM 真缺索引时必须拦（否则上一条可能被"永远 warn"蒙混）。

    这条判据是有实测依据的 —— 无索引的 BAM 喂给 HaplotypeCaller 会直接失败，
    而不是"慢一点"。
    """
    out = tmp_path / "out"
    out.mkdir()
    make_bam(str(out / "s1.sorted.markdup.BQSR.bam"), sample="s1", reads=30)
    os.remove(str(out / "s1.sorted.markdup.BQSR.bam.bai"))
    rc, rep = run_verifier(tmp_path, "02_markdup_bqsr", samples="s1")
    assert rc == 1, "BQSR 索引缺失未被拦截"
    assert "bqsr[s1].index" in names(rep, "hard"), rep


def test_all_four_index_namings_are_accepted(tmp_path):
    """四种索引命名都必须被认（实测 samtools 四种全认，见下）。

    samtools/GATK 会依次找 `<bam>.bai`、`<bam 去扩展>.bai`、`<bam>.csi`、
    `<bam 去扩展>.csi`。只认其中一种，就会把别的工具产出的合法索引判成"缺失"。
    本用例逐个命名跑一遍，确保四种都不被误判。
    """
    import subprocess
    for naming in ("foo.bam.bai", "foo.bai", "foo.bam.csi", "foo.csi"):
        d = tmp_path / naming
        d.mkdir()
        out = d / "out"
        out.mkdir()
        bam = str(out / "s1.sorted.markdup.BQSR.bam")
        make_bam(bam, sample="s1", reads=30)
        os.remove(bam + ".bai")          # make_bam 用 samtools index → .bam.bai
        if naming.endswith(".csi"):
            subprocess.run([SAMTOOLS, "index", "-c", bam], check=True)
            produced = bam + ".csi"
        else:
            subprocess.run([SAMTOOLS, "index", bam], check=True)
            produced = bam + ".bai"
        if naming == "foo.bai":
            os.rename(produced, str(out / "s1.sorted.markdup.BQSR.bai"))
        elif naming == "foo.csi":
            os.rename(produced, str(out / "s1.sorted.markdup.BQSR.csi"))

        rc, rep = run_verifier(d, "02_markdup_bqsr", samples="s1")
        idx = next(c for c in rep["checks"] if c["name"] == "bqsr[s1].index")
        assert idx["passed"], f"{naming} 未被识别为合法索引: {idx}"


# ============================ Stage 1：比对率 ============================
def test_stage1_all_unmapped_is_caught(tmp_path):
    """★阴性对照：**所有 reads 都未比对上**必须拦。

    这比空 BAM 更隐蔽：文件非空、reads 数正常、quickcheck 通过、@RG 也有，
    只是 mapped=0。典型成因是参考基因组选错（物种/版本），BWA 不报错。
    下游 HC 会产出"几乎没有变异"的 GVCF，与真的低变异样本无法区分。
    """
    out = tmp_path / "out"
    out.mkdir()
    hdr = ("@HD\tVN:1.6\tSO:coordinate\n@SQ\tSN:chr1\tLN:8000\n"
           "@RG\tID:s1\tSM:s1\tLB:s1\tPL:ILLUMINA\n")
    # 150 条 unmapped read（flag 4）—— 数量足够触发比对率判据（阈值 total>=100）
    lines = [f"r{i}\t4\t*\t0\t0\t*\t*\t0\t0\t{'N'*60}\t{'I'*60}\tRG:Z:s1"
             for i in range(150)]
    sam = str(tmp_path / "u.sam")
    with open(sam, "w") as f:
        f.write(hdr + "\n".join(lines) + "\n")
    _sh([SAMTOOLS, "view", "-b", "-o", str(out / "s1.sorted.bam"), sam])
    _sh([SAMTOOLS, "index", str(out / "s1.sorted.bam")])
    rc, rep = run_verifier(tmp_path, "01_bwa_sort", samples="s1")
    assert rc == 1, "全部 unmapped 未被拦截"
    assert any("mapped_ratio" in n for n in names(rep, "hard")), rep


def test_stage1_good_mapping_rate_passes(tmp_path):
    """阳性：正常比对率不告警。"""
    out = tmp_path / "out"
    out.mkdir()
    make_bam(str(out / "s1.sorted.bam"), sample="s1", reads=150)
    rc, rep = run_verifier(tmp_path, "01_bwa_sort", samples="s1")
    assert rc == 0, rep
    con = next(c for c in rep["checks"] if c["name"].endswith(".mapped_rate"))
    assert con["passed"], con


# ============================ 判据框架自身 ============================
def test_hard_failure_blocks_and_writes_json(tmp_path):
    """hard 失败 → 退出码 1 + `.verify/<stage>.json` 落盘（供编排层解析）。"""
    out = tmp_path / "out"
    out.mkdir()
    make_bam(str(out / "s1.sorted.bam"), sample="s1", header_only=True)
    rc, rep = run_verifier(tmp_path, "01_bwa_sort", samples="s1")
    assert rc == 1
    assert rep["pass"] is False and rep["hard_failures"]
    assert os.path.isfile(tmp_path / ".verify" / "01_bwa_sort.json")


def test_unknown_stage_fails_closed(tmp_path):
    """未知阶段名 → 判失败（fail-closed），不许假装通过。"""
    (tmp_path / "out").mkdir()
    rc, rep = run_verifier(tmp_path, "99_nonexistent")
    assert rc == 1
    assert "stage_known" in names(rep, "hard")


def test_missing_tool_fails_closed_not_silently_passed(tmp_path, monkeypatch):
    """★核心阴性对照：**工具缺失必须判失败**，不能"跳过 = 通过"。

    实测过的漏洞：bcftools 不在 PATH 时，一个内容是乱码的 VCF 会判**通过**
    （检查被标成 SKIP 而 SKIP 不阻止放行）。这等于说：任何没装工具的机器上，
    整个门禁静默失效，而报告上一切正常 —— 那正是本机制要防的"静默错误"本身。

    无法验证 ≠ 验证通过。缺工具是**环境问题**，要显式暴露（报告里单列
    missing_tools），让人去装工具，而不是伪装成数据没问题。
    """
    out = tmp_path / "out"
    out.mkdir()
    # 造一个真 VCF，但让 bcftools 探测返回 None
    make_vcf(str(out / "co.vcf.gz"), sample="S1", variants=10)
    monkeypatch.setattr(SV, "tool", lambda n: None if n in ("bcftools", "samtools") else shutil.which(n))
    rc, rep = run_verifier(tmp_path, "04_joint_genotyping", prefix="co", samples="S1")
    assert rc == 1, "工具缺失时未拦截（fail-open —— 门禁会静默失效）"
    assert rep["missing_tools"], rep
    assert any("tool[" in n for n in names(rep, "hard")), rep


def test_missing_tool_is_reported_distinctly_from_data_problem(tmp_path, monkeypatch):
    """缺工具的错误文案要指向**环境**，别让人去查数据。"""
    out = tmp_path / "out"
    out.mkdir()
    make_vcf(str(out / "co.vcf.gz"), sample="S1", variants=10)
    monkeypatch.setattr(SV, "tool", lambda n: None if n == "bcftools" else shutil.which(n))
    rc, rep = run_verifier(tmp_path, "04_joint_genotyping", prefix="co", samples="S1")
    det = " ".join(c["detail"] for c in rep["checks"] if c["name"].startswith("tool["))
    assert "未找到" in det and "PATH" in det, det
    assert "bcftools" in rep["missing_tools"]


def test_verifier_internal_error_fails_closed(tmp_path, monkeypatch):
    """校验器内部异常 → 必须判失败并说明「该阶段未经校验」，不许静默放行。"""
    (tmp_path / "out").mkdir()

    def boom(args, rep):
        raise RuntimeError("模拟内部错误")

    monkeypatch.setitem(SV._STAGES, "01_bwa_sort", boom)
    rc, rep = run_verifier(tmp_path, "01_bwa_sort", samples="s1")
    assert rc == 1, "校验器内部异常被吞成通过了"
    assert "verifier_internal" in names(rep, "hard")


def test_report_has_discrimination(tmp_path):
    """整体区分度：同一判据下好输入过、坏输入拦。

    单测「好输入能过」是无效的 —— 永远返回 pass 的实现也能过。
    """
    good = tmp_path / "good" / "out"
    good.mkdir(parents=True)
    make_bam(str(good / "s1.sorted.bam"), sample="s1", reads=20)
    rc_good, rep_good = run_verifier(tmp_path / "good", "01_bwa_sort", samples="s1")

    bad = tmp_path / "bad" / "out"
    bad.mkdir(parents=True)
    make_bam(str(bad / "s1.sorted.bam"), sample="s1", header_only=True)
    rc_bad, rep_bad = run_verifier(tmp_path / "bad", "01_bwa_sort", samples="s1")

    assert rc_good == 0 and rc_bad == 1, \
        "校验器缺少区分度：好输入与坏输入得到相同结论"


def test_tool_probe_does_not_hardcode_paths():
    """仓库硬规则：工具用 command -v 运行时探测，禁止写死本机 conda 绝对路径。"""
    src = open(os.path.join(SV_PATH, "stage_verify.py"), encoding="utf-8").read()
    assert "/home/" not in src, "校验器里出现了写死的本机路径"
    assert "anaconda3" not in src, "校验器里出现了写死的 conda 路径"
    assert "shutil.which" in src


# ==================== A3：PERCENT_DUPLICATION 解析（假阳性解析）====================
# 背景：旧实现用 `PERCENT_DUPLICATION[^\d]*([\d.]+)` 抓"紧跟其后的第一个数"。
# 真 GATK MarkDuplicates 的 metrics 是**表结构**：表头行列名、下一行给值，
# 而 `[^\d]*` 会跨行吃掉 `ESTIMATED_LIBRARY_SIZE\n` 加库名前缀，抓到库名 `lib1`
# 里的 `1` —— 真值 0.0 被报成"重复率 100.0%"。报告里那行看起来完全正常
# （一个偏高的数值 + "异常偏高"的建议），没人会怀疑是解析错。

_REAL_METRICS = (
    "## htsjdk.samtools.metrics.StringHeader\n"
    "# MarkDuplicates --INPUT in.bam --OUTPUT out.bam --METRICS_FILE m.txt "
    "--MAX_SEQUENCES_FOR_DISK_READ_ENDS_MAP 50000 --CREATE_INDEX false\n"
    "## htsjdk.samtools.metrics.StringHeader\n"
    "# Started on: Tue Oct 27 14:17:22 CST 2026\n"
    "\n"
    "## METRICS CLASS\tpicard.sam.DuplicationMetrics\n"
    "LIBRARY\tUNPAIRED_READS_EXAMINED\tREAD_PAIRS_EXAMINED\t"
    "SECONDARY_OR_SUPPLEMENTARY_RDS\tUNMAPPED_READS\tUNPAIRED_READ_DUPLICATES\t"
    "READ_PAIR_DUPLICATES\tREAD_PAIR_OPTICAL_DUPLICATES\tPERCENT_DUPLICATION\t"
    "ESTIMATED_LIBRARY_SIZE\n"
    "lib1\t400\t0\t0\t0\t0\t0\t0\t{d}\t\n"
)


def test_dup_rate_parses_real_gatk_metrics_value():
    """★核心阴性对照：真 GATK 产物（真值 0）不得被报成 100%。"""
    d, why = SV.parse_percent_duplication(_REAL_METRICS.format(d="0"))
    assert d == 0.0, f"真值 0 被解析成 {d}（旧正则的典型症状：抓到库名 lib1 的 1）"
    # 反向确认前提：这个文件里确实存在一个能骗过朴素正则的数字
    assert re.search(r"PERCENT_DUPLICATION[^\d]*([\d.]+)", _REAL_METRICS.format(d="0")) \
        .group(1) == "1", "前提失效：该文件不再能骗过朴素正则，本测试失去区分度"


def test_dup_rate_parses_high_value_correctly():
    """真值偏高时也要取对（不能因为修 0 而把非 0 取错）。"""
    for raw, want in (("0.375", 0.375), ("0.05", 0.05), ("1", 1.0)):
        d, _ = SV.parse_percent_duplication(_REAL_METRICS.format(d=raw))
        assert d == want, f"{raw} 解析成了 {d}"


def test_dup_rate_library_name_with_digits_does_not_mislead():
    """库名恰好是 lib2/lib9 时同样不受影响（旧正则的误报源就是库名里的数字）。"""
    d, _ = SV.parse_percent_duplication(_REAL_METRICS.format(d="0").replace("lib1\t", "lib99\t"))
    assert d == 0.0


def test_dup_rate_unparseable_is_reported_not_silently_skipped():
    """取不到值时必须**登记一条告警明说未判定**，不能 except: pass 静默吞掉。"""
    for bad, why_part in (
        ("nothing here\n", "没有 PERCENT_DUPLICATION"),
        ("LIBRARY\tPERCENT_DUPLICATION\n", "没有同宽度的数据行"),
        ("LIBRARY\tPERCENT_DUPLICATION\nlib1\tn/a\n", "不可解析"),
    ):
        d, why = SV.parse_percent_duplication(bad)
        assert d is None and why_part in why, f"{bad!r} → {d}, {why}"


def test_stage2_dup_rate_wired_into_report(tmp_path):
    """端到端：dup_rate 判据真的进报告，且用的是文件里的真值。"""
    out = tmp_path / "out"
    out.mkdir()
    make_bam(str(out / "s1.sorted.markdup.BQSR.bam"), sample="s1", reads=20)
    (out / "s1.markdup.metrics").write_text(_REAL_METRICS.format(d="0"))
    rc, rep = run_verifier(tmp_path, "02_markdup_bqsr", samples="s1")
    dup = [c for c in rep["checks"] if c["name"] == "dup_rate[s1]"]
    assert dup, f"dup_rate[s1] 没进报告：{[c['name'] for c in rep['checks']]}"
    assert "0.0%" in dup[0]["detail"], \
        f"报告里的重复率不是真值 0.0%：{dup[0]['detail']!r}"
    assert dup[0]["passed"], "0% 重复率被判为异常偏高"


def test_stage2_dup_rate_high_value_warns(tmp_path):
    """真值偏高时登记 warn（判据本身仍有区分度）。"""
    out = tmp_path / "out"
    out.mkdir()
    make_bam(str(out / "s1.sorted.markdup.BQSR.bam"), sample="s1", reads=20)
    (out / "s1.markdup.metrics").write_text(_REAL_METRICS.format(d="0.85"))
    rc, rep = run_verifier(tmp_path, "02_markdup_bqsr", samples="s1")
    dup = [c for c in rep["checks"] if c["name"] == "dup_rate[s1]"][0]
    assert "85.0%" in dup["detail"] and not dup["passed"], dup
    assert rc == 0, "重复率是 warn 级，不应拦截"


# ==================== A6：combined.g.vcf.gz 缺失不得静默通过 ====================
def _mk_vcf(path, samples, n=20, gvcf=True):
    """造真实 bgzip+tabix VCF（GVCF 形态）。"""
    raw = str(path)[:-3]
    lines = ["##fileformat=VCFv4.2", "##contig=<ID=chr1,length=100000>"]
    if gvcf:
        lines.append('##ALT=<ID=NON_REF,Description="any possible alt">')
    lines += ['##FORMAT=<ID=GT,Number=1,Type=String,Description="GT">',
              "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\t"
              + "\t".join(samples)]
    for i in range(n):
        lines.append(f"chr1\t{100+i*10}\t.\tA\tG\t50\tPASS\t.\tGT\t"
                     + "\t".join("0/1" for _ in samples))
    with open(raw, "w") as f:
        f.write("\n".join(lines) + "\n")
    assert _sh(["bgzip", "-f", raw]).returncode == 0
    assert _sh(["tabix", "-f", "-p", "vcf", str(path)]).returncode == 0
    return str(path)


@pytest.mark.skipif(not shutil.which("bgzip"), reason="需要 bgzip/tabix")
def test_stage4_missing_combined_gvcf_is_caught(tmp_path):
    """★核心阴性对照：只有最终 VCF、没有 combined.g.vcf.gz → 必须拦。"""
    out = tmp_path / "out"
    out.mkdir()
    _mk_vcf(out / "co.vcf.gz", ["S1"], gvcf=False)
    rc, rep = run_verifier(tmp_path, "04_joint_genotyping", samples="S1", prefix="co")
    assert rc == 1, f"缺 combined.g.vcf.gz 却放行了：{[(c['name'], c['passed']) for c in rep['checks']]}"
    assert "combined.exists" in names(rep, "hard"), names(rep, "hard")


@pytest.mark.skipif(not shutil.which("bgzip"), reason="需要 bgzip/tabix")
def test_stage4_empty_combined_gvcf_distinguished_from_missing(tmp_path):
    """0 字节的 combined 与"完全没生成"要说清楚是两回事。"""
    out = tmp_path / "out"
    out.mkdir()
    _mk_vcf(out / "co.vcf.gz", ["S1"], gvcf=False)
    (out / "co.combined.g.vcf.gz").write_bytes(b"")
    rc, rep = run_verifier(tmp_path, "04_joint_genotyping", samples="S1", prefix="co")
    assert rc == 1
    det = [c["detail"] for c in rep["checks"] if c["name"] == "combined.exists"][0]
    assert "存在但为空" in det, f"没区分「为空」与「缺失」：{det!r}"


@pytest.mark.skipif(not shutil.which("bgzip"), reason="需要 bgzip/tabix")
def test_stage4_genomicsdb_workspace_is_exempt_not_blocked(tmp_path):
    """GenomicsDBImport 路径下 combined 合法缺席 → 降级 warn，不得拦健康数据。"""
    out = tmp_path / "out"
    out.mkdir()
    _mk_vcf(out / "co.vcf.gz", ["S1"], gvcf=False)
    (out / "co.genomicsdb").mkdir()
    rc, rep = run_verifier(tmp_path, "04_joint_genotyping", samples="S1", prefix="co")
    assert rc == 0, f"GenomicsDB 路径被误拦：{names(rep, 'hard')}"
    cs = [c for c in rep["checks"] if c["name"] == "combined.exists"][0]
    assert cs["level"] == "warn" and "未做" in cs["detail"], cs


@pytest.mark.skipif(not shutil.which("bgzip"), reason="需要 bgzip/tabix")
def test_stage4_combined_present_healthy_passes(tmp_path):
    """阳性：combined 与 joint VCF 都在 → 通过（证明上面不是"永远拦"）。"""
    out = tmp_path / "out"
    out.mkdir()
    _mk_vcf(out / "co.combined.g.vcf.gz", ["S1"], gvcf=True)
    _mk_vcf(out / "co.vcf.gz", ["S1"], gvcf=False)
    rc, rep = run_verifier(tmp_path, "04_joint_genotyping", samples="S1", prefix="co")
    assert rc == 0, f"健康数据被拦：{names(rep, 'hard')}"
