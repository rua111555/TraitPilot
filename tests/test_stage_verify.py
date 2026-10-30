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

# 构造 fixture 需要的四个真实工具。**四个都算**，不能只判前两个：
# bgzip/tabix 也曾是 6 条用例各自的 skipif（同样静默），而它们覆盖的正是
# combined GVCF / GVCF 样本身份 / INDEL 位点下限 —— 一样是证据链的一部分。
# 现已合并到本模块级守卫，逐用例的 skipif 全部删除（否则缺口换个地方又回来）。
SAMTOOLS = shutil.which("samtools")
BCFTOOLS = shutil.which("bcftools")
BGZIP = shutil.which("bgzip")
TABIX = shutil.which("tabix")

# 工具缺失时**整体 fail 而不是静默 skip**（第二轮审计 finding #11，实测确认）。
#
# 实测过的形态：某台机器上 PATH 里没有 samtools/bcftools 时，
#     pytest tests/test_stage_verify.py -q  →  **71 skipped**，退出码 **0**。
# 于是"WGS 门禁的整套判据从未运行"这件事，在 CI/批处理里看起来与"全部通过"
# 完全一样 —— 正是本套测试要防的"静默失效"，只是换到了测试层。
# `skipif` 的语义（"环境不具备，这条与本环境无关"）在这里是**错的**：
# 这些用例是门禁的唯一证据链，缺工具意味着证据不存在，而不是无关。
#
# ⚠️ 为什么用**收集期报错**而不是"改断言让它失败"：
#    · 断言型失败需要每条用例各自判环境，71 条会各报一次同样的错，噪音淹没真信号；
#    · 收集期报错只报一次、消息里给出**确切的修法**，且不会被 `-k` 过滤掉
#      （`-k` 挑走的用例连断言都不会跑到，那样缺口又回来了）。
#    · 关键：**默认必须是失败**。加一个"缺工具就跳过"的开关会让缺口重新变成
#      默认静默 —— 那是把本缺陷原样搬回来。故这里的开关方向相反：
#      要跳过必须**显式**声明 `SKILL_AGENT_ALLOW_SKIP_WGS_TOOLS=1`，
#      并且跳过时会在 pytest 的 warnings summary 里留一条告警（不会无声无息）。
_HAVE_TOOLS = all((SAMTOOLS, BCFTOOLS, BGZIP, TABIX))
_ALLOW_SKIP = os.environ.get("SKILL_AGENT_ALLOW_SKIP_WGS_TOOLS") == "1"

if not _HAVE_TOOLS and not _ALLOW_SKIP:
    raise RuntimeError(
        "本文件是 WGS 阶段门禁的**唯一**证据链，而它需要真实 "
        "samtools 与 bcftools：\n"
        f"    samtools = {SAMTOOLS!r}\n"
        f"    bcftools = {BCFTOOLS!r}\n"
        f"    bgzip    = {BGZIP!r}\n"
        f"    tabix    = {TABIX!r}\n"
        f"当前 PATH = {os.environ.get('PATH', '')!r}\n"
        "缺工具时**不是 skip、是失败** —— 跳过会让“整套门禁判据从未运行”"
        "在退出码上与“全部通过”无法区分（实测：71 skipped / rc=0）。\n"
        "修法（任选）：\n"
        "  1) 激活工具环境（本机）：\n"
        "       PATH=/home/chenyuxi/anaconda3/envs/Agent/bin:$PATH \\\n"
        "         /home/chenyuxi/anaconda3/bin/python -m pytest tests/ -q\n"
        "  2) 确知本次不跑 WGS 侧（如只测 UI）时，**显式**声明跳过：\n"
        "       SKILL_AGENT_ALLOW_SKIP_WGS_TOOLS=1 python -m pytest tests/ -q\n"
        "     （会在 warnings summary 里留一条告警，不会无声跳过）")

if not _HAVE_TOOLS and _ALLOW_SKIP:
    # 显式声明了要跳过：仍然**出声**。静默的 skip 正是本条要防的东西。
    # ⚠️ 用 `warnings.warn` 而不是 `print(..., file=sys.stderr)`：实测 pytest
    #    默认会**捕获**收集期的 stderr（不带 `-s` 就看不到），而 warnings summary
    #    在默认输出里**必定**打印。实测确认过这一点（`-q` 下 print 消失、
    #    warnings.warn 仍出现在 summary 里）。
    import warnings
    warnings.warn(
        "SKILL_AGENT_ALLOW_SKIP_WGS_TOOLS=1 —— 本文件全部 WGS 门禁用例被跳过，"
        "整套门禁判据**本次未经验证**。仅在你确知本次无需验证它时使用。",
        stacklevel=1)
    pytestmark = pytest.mark.skip(
        reason="显式设置 SKILL_AGENT_ALLOW_SKIP_WGS_TOOLS=1（缺 samtools/bcftools）")



# ============================ fixture 构造 ============================
REF = ("ACGT" * 2000)          # 8000bp 参考，重复但有 60bp 窗口内足够多样


def _sh(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, check=True, **kw)


def _sh_rc(cmd):
    """只取退出码，不因非 0 抛异常（造"应当失败"的 fixture 时要用）。"""
    return subprocess.run(cmd, capture_output=True).returncode


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
             ts_tv_ratio=None, pos_offset=0):
    """造一个 bgzip+tabix 的迷你 VCF。

    gvcf=True  → 头部含 ##ALT=<ID=NON_REF,...>（真 GVCF 的标志）
    all_filtered=True → 所有记录 FILTER=LowQual（模拟阈值过严）
    ts_tv_ratio=n → 控制 ts:tv 比例（None 表示交替）
    pos_offset=n → 记录坐标整体右移 n（造"同条数但内容完全不同"的另一份 VCF 用）
    """
    import random
    random.seed(variants)
    ts = [("A", "G"), ("G", "A"), ("C", "T"), ("T", "C")]
    tv = [("A", "C"), ("A", "T"), ("C", "A"), ("C", "G"), ("G", "C"), ("G", "T"), ("T", "A"), ("T", "G")]
    out = ["##fileformat=VCFv4.2",
           f"##contig=<ID=chr1,length={max(8000, pos_offset + 100 + variants * 7 + 8000)}>",
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
        out.append(f"chr1\t{pos_offset + 100 + i*7}\t.\t{r}\t{alt}\t50\t{filt}"
                   f"\tQD=10\tGT\t0/1")
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


def _make_bam_with_split(path, *, mapped, unmapped, sample="s1"):
    """造 BAM：前 `mapped` 条 flag=0（已比上）、后 `unmapped` 条 flag=4（未比上）。

    ⚠️ 顺序不能交错：coordinate-sort 要求 unmapped 段在末尾，否则
    `samtools index` 报 `[E::hts_idx_push] NO_COOR reads not in a single block
    at the end`。（本仓实测踩过。）
    """
    hdr = ("@HD\tVN:1.6\tSO:coordinate\n@SQ\tSN:chr1\tLN:1000000\n"
           f"@RG\tID:{sample}\tSM:{sample}\tLB:{sample}\tPL:ILLUMINA\n")
    lines = []
    for i in range(mapped):
        p = (i % 999000) + 1
        lines.append(f"m{i}\t0\tchr1\t{p}\t60\t100M\t*\t0\t0\t{'A'*100}\t{'I'*100}"
                     f"\tRG:Z:{sample}")
    for i in range(unmapped):
        lines.append(f"u{i}\t4\t*\t0\t0\t*\t*\t0\t0\t{'A'*100}\t{'I'*100}"
                     f"\tRG:Z:{sample}")
    sam = path + ".sam"
    with open(sam, "w") as f:
        f.write(hdr + "\n".join(lines) + "\n")
    _sh([SAMTOOLS, "view", "-b", "-o", path, sam])
    _sh([SAMTOOLS, "index", path])
    os.remove(sam)
    return path


def _swap_index(donor_split, victim_bam, tmp):
    """把"另一个 BAM"的索引装到 victim_bam 上（外来索引形态）。

    两个 BAM 的**总条数必须相同** —— 这正是早期守卫（只比总条数
    `idx_says == total`）看不见的形态。
    """
    donor = _make_bam_with_split(str(tmp / "donor.bam"),
                                 mapped=donor_split[0], unmapped=donor_split[1])
    os.replace(donor + ".bai", victim_bam + ".bai")


# ─────────────── BGZF 块级构造（F18 残余：块交换 / 块边界截断）───────────────
#
# ⚠️ 为什么必须按**块**构造，而不是像 test_quickcheck_pass_is_not_enough_body_
#    must_be_readable 那样抹中段字节：那是"损坏"，读它就报错，有现成的工具能抓。
#    本节要造的是**完全合法的篡改** —— 文件仍是逐块合法的 BGZF，只是块的内容/顺序
#    变了或尾部块丢了，于是"读这个文件本身"的一切判据都通过。这两种形态此前
#    **无任何阴性对照**（见 docs/test-evidence.md 那条已作废的边界登记）。

def _bgzf_blocks(path):
    """返回该 BGZF 文件的**块边界**列表（每个元素是一块的结束偏移）。

    BGZF 块布局（本仓实测校准过的偏移，早期手写过一版算错 BSIZE 偏移的解析器，
    症状是所有块级构造都"解析失步"，故这里把布局写在案上）：
        0-9   固定 gzip 头（ID1 ID2 CM FLG MTIME XFL OS）
        10-11 XLEN（=6）
        12-17 额外字段：SI1='B' SI2='C' SLEN(=2) **BSIZE(=2, 小端)** ← 关键在 16-17
        18..  CDATA
        末 8 字节 CRC32 + ISIZE
    块总长 = **BSIZE + 1**（不是 BSIZE，实测）。
    """
    import struct
    data = open(path, "rb").read()
    bounds, i = [], 0
    while i + 18 <= len(data):
        assert data[i:i + 2] == b"\x1f\x8b", \
            f"第 {i} 字节处不是 gzip 魔数 —— 解析失步（块布局假设错了）"
        assert (data[i + 12], data[i + 13]) == (ord("B"), ord("C")), \
            f"第 {i} 字节处不是 BC 子字段 —— 不是 BGZF"
        bsize = struct.unpack("<H", data[i + 16:i + 18])[0]
        i += bsize + 1
        bounds.append(i)
    return bounds


def _make_sorted_bam(path, *, reads=40000, sample="s1"):
    """造一个坐标严格有序、块数足够多的 BAM（供块级篡改用）。

    条数取大不是为了慢，而是**块数**要多：块交换要在等长的相邻块里挑一对，
    块边界截断要截在"中途"而不是头部。
    """
    import random
    random.seed(9)                     # 固定种子：fixture 要可复现
    ln = 1_000_000
    hdr = (f"@HD\tVN:1.6\tSO:coordinate\n@SQ\tSN:chr1\tLN:{ln}\n"
           f"@RG\tID:{sample}\tSM:{sample}\tLB:{sample}\tPL:ILLUMINA\n")
    rows = sorted((random.randint(1, ln - 1), i) for i in range(reads))
    sam = path + ".sam"
    with open(sam, "w") as f:
        f.write(hdr)
        for p, i in rows:
            f.write(f"r{i}\t0\tchr1\t{p}\t60\t100M\t*\t0\t0\t{'A'*100}\t{'I'*100}"
                    f"\tRG:Z:{sample}\n")
    _sh([SAMTOOLS, "view", "-b", "-o", path, sam])
    os.remove(sam)
    return path


def _swap_two_blocks(src, dst, *, frac=40):
    """**交换两个相邻的 BGZF 数据块**，文件**总字节数不变**。

    这是"字节数不变的就地篡改"的代表形态：长度、头、末尾 EOF 块全都没动，
    变的是数据块之间的顺序 —— 于是本体坐标不再单调。

    ⚠️ 不需要挑**等长**的块：重排片段时总长恒等于各块长度之和，与顺序无关
    （`out` 的四个切片拼起来就是原文件的全部分字节）。早期探针特意挑等长块是
    多余的谨慎，写进 fixture 反而让它对块长分布敏感、依赖运气。
    ⚠️ 避开首块（含 BAM 头，移走它 samtools 就找不到头了）与末块（BGZF EOF，
    28 字节）—— 只在中段动。
    ⚠️ 保留**原有索引**不动 —— 这正是该形态能骗过既有判据的原因：samtools 只在
    **你试图建/重建索引**时才检查顺序，而那时原来的 .bai 已经在了。
    """
    data = open(src, "rb").read()
    b = _bgzf_blocks(src)
    # 中段取一对相邻块；b[i] 是第 i 块的**结束**偏移，故块 i 的字节是 [b[i-1], b[i])
    j = max(2, min(int(len(b) * frac / 100), len(b) - 3))
    s1, e1, s2, e2 = b[j - 1], b[j], b[j], b[j + 1]
    assert 0 < s1 < e1 <= s2 < e2 <= len(data), "块边界不合法"
    out = data[:s1] + data[s2:e2] + data[s1:e1] + data[e2:]
    assert len(out) == len(data), "交换后字节数变了（重排不该改变总长）"
    assert out != data, "交换后内容没变 —— 挑到的两块相同"
    open(dst, "wb").write(out)
    return s1, e1


def _truncate_at_block_boundary(src, dst, *, frac=50, rebuild_index=True):
    """在**块边界**截断，再把 28 字节 BGZF EOF 块贴回去。

    结果是一份**完全合法但更短**的 BAM —— 这是本仓最难的一种坏形态：
      · 每个保留的块都完整 ⇒ 不含"损坏"，读它**不报错**；
      · 贴回 EOF 块 ⇒ `quickcheck` 满意；
      · 按**截断后**的本体重建索引 ⇒ 索引与本体自洽、坐标仍严格有序；
      ⇒ quickcheck、`view -c`、idxstats、头部 SO、本体重建索引 **全部通过**。
    BGZF/gzip **不自描述块数**，故没有任何"读该文件"的判据能发现它，
    只有**文件之外**的基准（上游 FASTQ 的条数）能。这正是 reads_anchor 的存在理由。

    `rebuild_index=True` 造"自洽型"（本判据的主防守对象）；
    反之造"索引仍指着已删数据"的形态（那是 index_drift 的戏份）。
    """
    data = open(src, "rb").read()
    b = _bgzf_blocks(src)
    at = b[int(len(b) * frac / 100)]
    out = data[:at] + data[-28:]          # 末 28 字节 = BGZF EOF 块
    open(dst, "wb").write(out)
    if rebuild_index:
        _sh([SAMTOOLS, "index", dst])
    return at


def test_stage1_same_total_split_swap_does_not_block_healthy_bam(tmp_path):
    """★阴性对照（假阳性方向）：总条数相同、拆分颠倒的外来索引**不得**误拦健康 BAM。

    形态：健康 BAM（200 条**全部** mapped）+ 一个总条数同为 200 但拆分为
    mapped=0/unmapped=200 的外来索引。早期实现的守卫只比**总条数**
    （`idx_says == total` → 200 == 200 通过），于是索引的旧拆分被当成事实照用：

        改动前实测：[FAIL] bam[s1].mapped_ratio: mapped=0/200；**没有任何 read
        比对上** —— 查参考基因组是否选错…   → 硬拦健康数据，
        还叫用户去查参考基因组（**误导性诊断**，比不拦更坏）。

    根因：判据读派生数据（索引）而非本体。修法：mapped/unmapped 从 BAM 本体数出
    （`view -c -F 4` / `-f 4` 两遍），索引降级为旁证。
    """
    out = tmp_path / "out"
    out.mkdir()
    bam = str(out / "s1.sorted.bam")
    _make_bam_with_split(bam, mapped=200, unmapped=0, sample="s1")
    _swap_index((0, 200), bam, tmp_path)         # 总条数同为 200，拆分颠倒

    # 前提自检：索引真的在骗人（否则这条对照没有区分度）
    rc_i, out_i, _ = SV.run([SAMTOOLS, "idxstats", bam])
    assert rc_i == 0
    idx_m = sum(int(l.split("\t")[2]) for l in out_i.splitlines() if l.count("\t") >= 2)
    assert idx_m == 0, f"前提不成立：外来索引本应报 mapped=0，实际 {idx_m}"
    assert SV.run([SAMTOOLS, "view", "-c", "-F", "4", bam])[1].strip() == "200", \
        "前提不成立：BAM 本体本应是 200 条全 mapped"

    rc, rep = run_verifier(tmp_path, "01_bwa_sort", samples="s1")
    # mapped_ratio 不得因索引的谎话而失败
    assert not any("mapped_ratio" in n for n in names(rep, "hard")), \
        f"外来索引的旧拆分硬拦了健康数据: {rep}"
    con = next(c for c in rep["checks"] if c["name"] == "bam[s1].mapped_ratio")
    assert con["passed"] and "200/200" in con["detail"], con
    # 分歧必须报出来（否则"索引已脱节"这件事被静默）
    assert any("split_drift" in n for n in names(rep, "warn")), rep
    assert rc == 0, rep


def test_stage1_same_total_split_swap_cannot_hide_all_unmapped(tmp_path):
    """★核心阴性对照（假阴性方向）：总条数相同、拆分成"全 mapped"的外来索引
    **不得**放行一个全 unmapped 的 BAM。

    这是本判据的**头号防守对象**（参考选错 → 全部 reads 标成 unmapped →
    下游产出"几乎没有变异"的 GVCF，与真的低变异样本无法区分）。
    早期实现下这条索引恰好把 mapped 报成 200/200：

        改动前实测：[PASS] bam[s1].mapped_ratio: mapped=200/200（100.0%）
                     → 全 unmapped 放行。

    与上一条是**互补**的两条：同一条缝隙的两个方向（一个误拦好的、一个放行坏的）。
    只测一个方向的话，"把守卫整个删掉"或"永远判 True/False"都能骗过回归。
    """
    out = tmp_path / "out"
    out.mkdir()
    bam = str(out / "s1.sorted.bam")
    _make_bam_with_split(bam, mapped=0, unmapped=200, sample="s1")
    _swap_index((200, 0), bam, tmp_path)         # 总条数同为 200，谎报全 mapped

    # 前提自检：索引报"全比上"，本体是"全没比上"
    assert SV.run([SAMTOOLS, "view", "-c", "-F", "4", bam])[1].strip() == "0"
    assert SV.run([SAMTOOLS, "view", "-c", "-f", "4", bam])[1].strip() == "200"
    rc_i, out_i, _ = SV.run([SAMTOOLS, "idxstats", bam])
    idx_m = sum(int(l.split("\t")[2]) for l in out_i.splitlines() if l.count("\t") >= 2)
    assert idx_m == 200, f"前提不成立：外来索引本应谎报 mapped=200，实际 {idx_m}"

    rc, rep = run_verifier(tmp_path, "01_bwa_sort", samples="s1")
    assert rc == 1, "全 unmapped 的 BAM 被外来索引放行了"
    assert any("mapped_ratio" in n for n in names(rep, "hard")), rep
    con = next(c for c in rep["checks"] if c["name"] == "bam[s1].mapped_ratio")
    assert "mapped=0/200" in con["detail"], con


def test_stage1_empty_bam_does_not_get_reference_misdiagnosis(tmp_path):
    """★误诊防线：**空 BAM**（total=0）不得被报成"查参考基因组是否选错"。

    空 BAM 也满足 `mapped == 0`（0/0），若不加 `total > 0` 守卫就会落进
    "没有任何 read 比对上 → 查参考基因组"那一支 —— 成因明明是"比对压根没产出"。
    只判错方向还不够，**指错方向**会让人去查一个没问题的东西。
    （本仓实测：改写该分支时漏掉了 HEAD 版本的 `and total` 守卫，回归当场抓到。）
    """
    out = tmp_path / "out"
    out.mkdir()
    make_bam(str(out / "s1.sorted.bam"), sample="s1", header_only=True)
    rc, rep = run_verifier(tmp_path, "01_bwa_sort", samples="s1")
    assert rc == 1, "空 BAM 未被拦截"
    assert any("read_count" in n for n in names(rep, "hard")), rep
    con = next(c for c in rep["checks"] if c["name"] == "bam[s1].mapped_ratio")
    assert "参考基因组" not in con["detail"], \
        f"空 BAM 被误诊为「参考基因组选错」: {con['detail']}"


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

    ⚠️ 本条只验**头标签**（`@HD SO`）这一层，不验本体是否真有序 —— 后者由
    `body_indexable` 负责（见 `test_stage1_block_swap_...`）。

    早期这里写的理由是"真乱序 BAM 会被 samtools 拒绝建索引，故'乱序且带索引'
    在现实中不存在"。**该理由已被实测推翻**（F18 残余）：samtools 只在**你试图
    建索引**时才检查顺序 —— 对一个**已有** `.bai` 的 BAM 做"字节数不变的就地篡改"
    （交换两个等长 BGZF 数据块），索引还在、乱序也在，且头标签仍自称
    `SO:coordinate`。故本条的定位不变（验头标签），但"那状态不存在"的说法作废。
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


# ══════════ F18 残余：块交换（body_indexable）与块边界截断（reads_anchor）══════════
#
# 本节补的是一类此前**无任何阴性对照**的坏产物：**完全合法的篡改**。
# 此前两条测试只覆盖"损坏"（抹中段字节 → 读它报错；删 EOF 块 → quickcheck 报错），
# 而这两条新形态里**每一个读该文件的判据都会通过** —— 必须有文件之外的基准或
# 重算一次索引才能发现。文件顶部"每条硬性判据都要配一条能触发的坏输入"的约定，
# 在这一类上此前是空着的。

def test_stage1_block_swap_is_caught_by_body_indexable(tmp_path):
    """★★核心阴性对照：交换两个等长 BGZF 数据块 → 必须被 `body_indexable` 拦下。

    篡改前实测（当前门禁对这份产物）：
        字节数 353848 == 完整文件；quickcheck rc=0；`view -c`=40000（与完整相同）；
        idxstats=40000；@HD SO=coordinate —— 判据全绿、`✅ 全部检查通过` rc=0。
    而本体里 chr1 的坐标从 999993 跳回 988415（0.3% 的记录错位）——
    下游 GATK 走索引按区读会**静默取到错的数据**（不是报错）。

    唯一能发现它的是"拿本体重建一次索引"：`samtools index` 拒绝该文件
    （`[E::hts_idx_push] Unsorted positions`，实测三次重复逐字相同）。

    本条同时是那个"已登记边界"的反例：`docs/test-evidence.md` 写「samtools 拒绝给
    乱序 BAM 建索引，故"乱序且带索引"在现实中不存在」—— 前半句正是本条利用的性质，
    后半句被本 fixture 直接证伪。
    """
    out = tmp_path / "out"
    out.mkdir()
    full = str(tmp_path / "full.bam")
    _make_sorted_bam(full, reads=40000, sample="s1")
    _sh([SAMTOOLS, "index", full])
    victim = str(out / "s1.sorted.bam")
    _swap_two_blocks(full, victim)
    shutil.copy(full + ".bai", victim + ".bai")      # 保留**原有**索引

    # ── 先把"篡改不可见"这一前提钉死：否则本条测不到该测的东西 ──
    def _count(*a):
        return subprocess.run([SAMTOOLS, *a], capture_output=True,
                              text=True).stdout.strip()
    full_total = _count("view", "-c", full)
    assert _count("view", "-c", victim) == full_total, \
        "前提不成立：交换块后条数变了（本应一模一样）"
    assert os.path.getsize(victim) == os.path.getsize(full), \
        "前提不成立：交换块后文件字节数变了（本条要的是字节数不变）"
    assert subprocess.run([SAMTOOLS, "quickcheck", victim]).returncode == 0, \
        "前提不成立：quickcheck 已经报错了（那不是本形态）"
    hdr = _count("view", "-H", victim)
    assert "SO:coordinate" in hdr, "前提不成立：头部标签不是 SO:coordinate"

    rc, rep = run_verifier(tmp_path, "01_bwa_sort", samples="s1")
    bi = [c for c in rep["checks"] if c["name"].endswith(".body_indexable")]
    assert bi, "本体乱序，却没有登记 body_indexable —— 覆盖缺口静默"
    assert bi[0]["level"] == "hard" and not bi[0]["passed"], bi[0]
    assert rc == 1, (
        "块交换（字节数不变、头尾不动、索引仍在）被放行 —— 这正是 F18 残余："
        f"{[c['name'] for c in rep['checks'] if c['level'] == 'hard' and not c['passed']]}")


def test_stage1_healthy_bam_is_not_blocked_by_body_indexable(tmp_path):
    """★假阳性方向：健康 BAM 的 `body_indexable` 必须通过（不得凭空加门槛）。

    重建索引这条路**每一次运行都要做**，任何假阳性都会稳定拦下健康数据。
    实测过的健康形态（本节覆盖前两种，其余在同批探针里验过）：
      · 普通坐标排序 BAM（本条）
      · 全 unmapped、空 BAM、5000 条并列同坐标、真实 bwa mem 输出（含 supplementary）、
        多 contig 含**零记录 contig**（GRCh38 常态）、头部无 SO 但有序、
        真实 `gatk MarkDuplicates` 输出 —— 重建索引一律 rc=0
    """
    out = tmp_path / "out"
    out.mkdir()
    _make_sorted_bam(str(out / "s1.sorted.bam"), reads=5000, sample="s1")
    _sh([SAMTOOLS, "index", str(out / "s1.sorted.bam")])
    rc, rep = run_verifier(tmp_path, "01_bwa_sort", samples="s1")
    bi = [c for c in rep["checks"] if c["name"].endswith(".body_indexable")]
    assert bi, "健康 BAM 没有登记 body_indexable（判据没跑？）"
    assert bi[0]["passed"], f"健康 BAM 被 body_indexable 误拦：{bi[0]}"
    assert rc == 0, f"健康 BAM 被判失败：{[c['name'] for c in rep['checks'] if c['level'] == 'hard' and not c['passed']]}"


def test_stage1_long_contig_bam_is_not_blocked_by_body_indexable(tmp_path):
    """★假阳性方向：长 contig（坐标超出 `.bai` 上限）的健康 BAM **不得**被拦下。

    这是 `body_indexable` 最危险的一条假阳性路径，实测确认：
    contig 长 600Mbp、记录放在 550Mbp 之后时，**默认（.bai）**重建索引 rc=1
    （`cannot be stored in a bai index. Try using a csi index` /
    `Numerical result out of range`），而 `-c`（CSI）rc=0。
    只试 .bai 的写法会把这类**健康** BAM 稳定拦下 —— 而这不是臆想的边角：
    本仓 `bam_index()` 认 `.csi` 的理由就是"长 contig 场景"，即**已知并支持**。
    故实现里必须"先 .bai、失败且是 bai 上限签名时再试 -c"。

    ⚠️ 乱序**不会**被这次重试放过：实测块交换文件两级都失败（都是
    `Unsorted positions`），只有 bai 上限签名才降级重试。
    """
    out = tmp_path / "out"
    out.mkdir()
    ln = 600_000_000
    sam = str(tmp_path / "long.sam")
    with open(sam, "w") as f:
        f.write(f"@HD\tVN:1.6\tSO:coordinate\n@SQ\tSN:chrLong\tLN:{ln}\n"
                f"@RG\tID:s1\tSM:s1\tLB:s1\tPL:ILLUMINA\n")
        for i in range(300):
            f.write(f"r{i}\t0\tchrLong\t{550_000_000 + i*10}\t60\t100M\t*\t0\t0"
                    f"\t{'A'*100}\t{'I'*100}\tRG:Z:s1\n")
    bam = str(out / "s1.sorted.bam")
    _sh([SAMTOOLS, "view", "-b", "-o", bam, sam])
    # 前提：默认 .bai 确实装不下（否则本条测的不是这条路径）
    assert _sh_rc([SAMTOOLS, "index", "-o", os.devnull, bam]) != 0, \
        "前提不成立：默认 .bai 竟然能建（那条假阳性路径不存在？）"
    assert _sh_rc([SAMTOOLS, "index", "-c", "-o", os.devnull, bam]) == 0, \
        "前提不成立：CSI 也建不了（该参考不是本条的形态）"
    _sh([SAMTOOLS, "index", "-c", bam])          # 真造一个 .csi（健康路径的产物）
    rc, rep = run_verifier(tmp_path, "01_bwa_sort", samples="s1")
    bi = [c for c in rep["checks"] if c["name"].endswith(".body_indexable")]
    assert bi and bi[0]["passed"], (
        f"长 contig 的健康 BAM 被 body_indexable 误拦（.bai 上限被当成了乱序）：{bi}")
    assert rc == 0, (
        f"长 contig 的健康 BAM 被判失败："
        f"{[c['name'] for c in rep['checks'] if c['level'] == 'hard' and not c['passed']]}")


def test_stage1_block_truncation_is_caught_by_reads_anchor(tmp_path):
    """★★核心阴性对照：块边界截断 + 贴回 EOF 块 + 按本体重建索引 →
    **所有 BAM 内部判据都通过**，必须由 `reads_anchor`（外部锚）拦下。

    这是本仓最难的一种坏形态，实测确认（40000→11585/19949/27989 三种都试过）：
        quickcheck rc=0、`view -c` rc=0、索引自洽、idxstats 与本体一致、
        @HD SO=coordinate、坐标严格有序（`samtools index` 也 rc=0）
        —— 门禁 `✅ 全部检查通过` rc=0，**放行了一份少了 30%-70% read 的 BAM**。

    为什么没有任何"读该文件"的判据能发现它：BGZF/gzip **不自描述块数**。
    截断后的文件仍逐块合法，只是**少了尾巴** —— 它不是"损坏"，是"更短"。
    必须有**文件之外**的基准：Stage 0.5 数出的 clean FASTQ 条数。

    不变式（实测，bwa mem 0.7.19-r1273）：BWA 对**每一条输入的 read 段**恰好产出一条
    primary alignment —— 6000 段输入 → 6000 primary（另有 300 条 supplementary，
    共 6300 条记录）。故 `primary == FASTQ 条数` 是结构性等式，不是阈值。
    """
    out = tmp_path / "out"
    out.mkdir()
    full = str(tmp_path / "full.bam")
    _make_sorted_bam(full, reads=40000, sample="s1")
    victim = str(out / "s1.sorted.bam")
    _truncate_at_block_boundary(full, victim, frac=50, rebuild_index=True)

    def _count(*a):
        return subprocess.run([SAMTOOLS, *a], capture_output=True,
                              text=True).stdout.strip()
    n_full, n_cut = int(_count("view", "-c", full)), int(_count("view", "-c", victim))
    assert n_cut < n_full, "前提不成立：截断后条数没变少"
    assert subprocess.run([SAMTOOLS, "quickcheck", victim]).returncode == 0, \
        "前提不成立：quickcheck 报错了（那不是本形态 —— 本形态恰恰是 quickcheck 通过）"
    assert "SO:coordinate" in _count("view", "-H", victim)

    # 造 Stage 0.5 的报告：基准 = **完整**文件的条数（模拟"FASTQ 有 40000 段，
    # 而 BAM 只剩一半"）。primary == 输入段数，故基准就取完整文件的 primary 数。
    prev = tmp_path / ".verify"
    prev.mkdir(exist_ok=True)
    ref_primary = int(_count("view", "-c", "-F", "0x900", full))
    (prev / "00_fastp_qc.json").write_text(json.dumps(
        {"stage": "00_fastp_qc", "primary_totals": {"s1": ref_primary}}))

    rc, rep = run_verifier(tmp_path, "01_bwa_sort", samples="s1",
                           prev_reads_file=str(prev / "00_fastp_qc.json"))
    # ── 先钉死"这不是被别的判据抓到的"：body_indexable 必须是通过的 ──
    bi = [c for c in rep["checks"] if c["name"].endswith(".body_indexable")]
    assert bi and bi[0]["passed"], (
        f"本形态里 body_indexable 本应通过（截断后仍有序）—— 若它失败，"
        f"本条就没测到 reads_anchor：{bi}")
    an = [c for c in rep["checks"] if c["name"].endswith("reads_anchor")
          or c["name"] == "reads_anchor"]
    assert an, "BAM 比上游 FASTQ 少了近一半 read，却没有登记 reads_anchor —— 覆盖缺口静默"
    assert an[0]["level"] == "hard" and not an[0]["passed"], an[0]
    assert str(n_cut) in an[0]["detail"] and str(ref_primary) in an[0]["detail"], \
        f"锚判据的文案没报出两个数（BAM {n_cut} / FASTQ {ref_primary}）：{an[0]['detail']}"
    # ── 文案还须指对**下一处该查的地方**（G06 第三处，2026-10-29）──
    # 这条判据最常见的真实起因是「Stage 1 消费的不是 Stage 0.5 的那份 FASTQ」。
    # 修复前这里写的是"本管线 Stage 1 的输入契约是两份 clean FASTQ" —— 断言的
    # 恰是**前提**，等于把用户从真正的原因上引开（跳过 Stage 0.5 时输入就是原始的）。
    # 钉住"说了由 fastq_dir 决定"，而不是钉某一句逐字文案（写法可以再改，规则不能丢）。
    assert "fastq_dir" in an[0]["detail"], (
        f"锚判据的失败文案没说 Stage 1 的输入**由什么决定** —— 用户会按一句写死的契约"
        f"去查，而本次运行可能走的是另一半（如未跑 Stage 0.5 的原始 FASTQ）。"
        f"文案：{an[0]['detail']}")
    assert rc == 1, "块边界截断（合法但更短的 BAM）被放行 —— 这正是 F18 残余的头号形态"


def test_stage1_anchor_missing_baseline_says_not_judged(tmp_path):
    """锚的基准取不到时**必须留痕**，不能静默消失（本项目"纸面具备"的约定）。

    三种"没做成"各自登记、各说各的方向：
      · 未传 `--prev-reads-file`（未跑 Stage 0.5 属预期）
      · 文件不存在
      · 文件在但没有 `primary_totals`（旧版校验器产出的报告）
    否则这条判据会在"没跑 fastp"的运行里看起来存在、从不生效。
    """
    out = tmp_path / "out"
    out.mkdir()
    _make_sorted_bam(str(out / "s1.sorted.bam"), reads=500, sample="s1")
    _sh([SAMTOOLS, "index", str(out / "s1.sorted.bam")])

    # ① 未传
    rc, rep = run_verifier(tmp_path, "01_bwa_sort", samples="s1")
    an = [c for c in rep["checks"] if c["name"] == "reads_anchor"]
    assert an and not an[0]["passed"] and an[0]["level"] == "warn", an
    assert "未提供 --prev-reads-file" in an[0]["detail"], an[0]["detail"]
    assert "未做" in an[0]["detail"], "没说明「未做该判定」"

    # ② 传了但文件不存在
    rc, rep = run_verifier(tmp_path, "01_bwa_sort", samples="s1",
                           prev_reads_file=str(tmp_path / "nope.json"))
    an = [c for c in rep["checks"] if c["name"] == "reads_anchor"]
    assert an and "不存在" in an[0]["detail"], an

    # ③ 文件在，但没有 primary_totals（旧版报告形态）
    p = tmp_path / "old.json"
    p.write_text(json.dumps({"stage": "00_fastp_qc", "read_totals": {"s1": 500}}))
    rc, rep = run_verifier(tmp_path, "01_bwa_sort", samples="s1",
                           prev_reads_file=str(p))
    an = [c for c in rep["checks"] if c["name"] == "reads_anchor"]
    assert an and "没有 primary_totals" in an[0]["detail"], an
    assert rc == 0, "基准取不到时不该判失败（那是「未做」，不是「没通过」）"


def test_stage1_anchor_equal_counts_passes(tmp_path):
    """★假阳性方向：BAM 的 primary 数与基准**相等**时必须通过。

    ⚠️ 基准要用 **primary** 数，不是**记录**数：实测 6000 段输入 → 6300 条记录
    （+300 supplementary）。若拿记录数当基准，健康数据会因 supplementary
    而系统性"多出"一截被误拦 —— 这正是本仓 `read_totals` / `primary_totals`
    必须分开的理由。本条顺带把这个区别钉在测试里。
    """
    out = tmp_path / "out"
    out.mkdir()
    bam = str(out / "s1.sorted.bam")
    _make_sorted_bam(bam, reads=800, sample="s1")
    _sh([SAMTOOLS, "index", bam])
    primary = int(subprocess.run([SAMTOOLS, "view", "-c", "-F", "0x900", bam],
                                 capture_output=True, text=True).stdout.strip())
    p = tmp_path / "prev.json"
    p.write_text(json.dumps({"stage": "00_fastp_qc",
                             "primary_totals": {"s1": primary}}))
    rc, rep = run_verifier(tmp_path, "01_bwa_sort", samples="s1",
                           prev_reads_file=str(p))
    an = [c for c in rep["checks"] if c["name"] == "reads_anchor"]
    assert an and an[0]["passed"], f"primary 与基准相等却未通过：{an}"
    assert rc == 0, f"健康数据被拦：{[c['name'] for c in rep['checks'] if c['level'] == 'hard' and not c['passed']]}"


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


def test_stage3_partially_covering_stale_index_is_caught(tmp_path):
    """★F02 阴性对照：**索引只覆盖文件前段**（建索引后又把文件写长）必须被拦。

    这是"判据看了一个角落就当全部"的又一形态。早期可用性探测**只落在首条记录的
    窗口**（`chr1:100-1100`），于是：
        先在 60 条时建 .tbi → 再覆写成 3000 条而不重建
        → 首窗口照样返回 11 行、rc=0 → 旧判据判「索引可用」
        → 而**尾部**窗口静默返回 **0 行、rc=0**（实测）
    下游 GATK -L 取后半段染色体时正是这样**不报错**、只拿到空结果。

    修法（两层，互补）：
      · `index_coverage` —— 拿 `bcftools index -n`（索引自称覆盖多少条）与全量
        扫描条数对账。实测七种形态（健康 .tbi/.csi、部分覆盖、无索引、外来索引、
        空 VCF、坏 .tbi）下只有不合格形态不一致。
      · 首窗口探测照旧保留 —— 它抓的是「条数恰好相同但内容完全对不上」的外来索引
        （`index -n` 对这种**看不出来**，见下一条测试），两层各堵一个方向。
    """
    out = tmp_path / "out"
    out.mkdir()
    dst = str(out / "S1.g.vcf.gz")
    make_vcf(dst, sample="S1", gvcf=True, variants=60)
    stale = dst + ".tbi"
    make_vcf(str(tmp_path / "big.vcf.gz"), sample="S1", gvcf=True, variants=3000)
    shutil.copy(stale, tmp_path / "keep.tbi")
    os.replace(str(tmp_path / "big.vcf.gz"), dst)
    shutil.copy(str(tmp_path / "keep.tbi"), stale)      # 换成 60 条时的那份

    # 前提自检（本条的区分度全靠这两条）：
    scan = subprocess.run([BCFTOOLS, "view", "-H", dst], capture_output=True,
                          text=True).stdout
    assert sum(1 for l in scan.splitlines() if l.strip()) == 3000
    n_idx = subprocess.run([BCFTOOLS, "index", "-n", dst], capture_output=True,
                           text=True).stdout.strip()
    assert n_idx == "60", f"前提不成立：索引应自称 60 条，实际 {n_idx!r}"
    # 首窗口**能**读（这正是旧判据被骗的原因），尾窗口读不到
    first = next(l for l in scan.splitlines() if l.strip()).split("\t")
    head_win = f"{first[0]}:{first[1]}-{int(first[1]) + 1000}"
    n_head = _sh_rc([BCFTOOLS, "view", "-H", "-r", head_win, dst])
    assert n_head == 0, "前提不成立：首窗口本应读得到（旧判据就是这样被骗过的）"

    rc, rep = run_verifier(tmp_path, "03_haplotypecaller", samples="S1")
    assert rc == 1, "索引只覆盖前 60/3000 条，却被判「可用」放行"
    assert "gvcf[S1].index_coverage" in _hard_names(rep), _hard_names(rep)
    det = next(c for c in rep["checks"]
               if c["name"] == "gvcf[S1].index_coverage")["detail"]
    assert "60" in det and "3000" in det, det


def test_stage3_foreign_index_with_same_count_is_caught_by_window_probe(tmp_path):
    """★互补对照：**条数完全相同**的外来索引 —— `index -n` 看不出来，靠首窗口探测抓。

    与上一条配对，合起来说明两层防线**各堵一个方向**：
        部分覆盖（条数 60≠3000）  → index_coverage 抓
        外来索引（条数 3000=3000）→ 首窗口探测抓（按区读 0 行）
    少了本条，把 `index_coverage` 之外的探测整个删掉，回归仍然全绿 ——
    而"条数对得上、内容对不上"的外来索引会重新变成盲区。
    """
    out = tmp_path / "out"
    out.mkdir()
    dst = str(out / "S1.g.vcf.gz")
    make_vcf(dst, sample="S1", gvcf=True, variants=3000)
    # 另造一份**同为 3000 条**但坐标完全不同的 VCF，把它的索引装过来
    # （`pos_offset` 必须给足，否则两份坐标重叠 —— 那样索引实际上是对的，
    #  本条就退化成"测夹具"了）
    other = str(tmp_path / "other.vcf.gz")
    make_vcf(other, sample="S1", gvcf=True, variants=3000, pos_offset=5_000_000)
    os.replace(other + ".tbi", dst + ".tbi")

    scan = subprocess.run([BCFTOOLS, "view", "-H", dst], capture_output=True,
                          text=True).stdout
    assert sum(1 for l in scan.splitlines() if l.strip()) == 3000
    n_idx = subprocess.run([BCFTOOLS, "index", "-n", dst], capture_output=True,
                           text=True).stdout.strip()
    assert n_idx == "3000", f"前提不成立：外来索引的条数本应也是 3000，实际 {n_idx!r}"

    rc, rep = run_verifier(tmp_path, "03_haplotypecaller", samples="S1")
    assert rc == 1, "条数相同但内容对不上的外来索引被放行"
    bad = _hard_names(rep)
    assert "gvcf[S1].index" in bad, bad
    assert "gvcf[S1].index_coverage" not in bad, (
        f"这一条的条数是对的，index_coverage 不该失败 —— 否则说明 fixture 没造出"
        f"「条数相同」的形态，本条就测不到首窗口探测那一层: {bad}")


def test_stage3_healthy_index_still_passes(tmp_path):
    """阴性对照的阳性配对：健康索引下 `index_coverage` 判过（不是永远判失败）。"""
    out = tmp_path / "out"
    out.mkdir()
    make_vcf(str(out / "S1.g.vcf.gz"), sample="S1", gvcf=True, variants=200)
    rc, rep = run_verifier(tmp_path, "03_haplotypecaller", samples="S1")
    assert rc == 0, f"健康索引被拦: {_hard_names(rep)}"
    cov = [c for c in rep["checks"] if c["name"] == "gvcf[S1].index_coverage"]
    assert not cov or cov[0]["passed"], cov


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


@pytest.mark.parametrize("bad", ["1M", -5, 3.5, True, None])
def test_min_variants_malformed_in_run_params_json_blocks(tmp_path, bad):
    """★写在 run.params.json 里但**无法生效**时必须拦住，不能静默降级成 warn。

    写这个键 = 调用方**明确想开**"变异数异常少"保护。若类型不对（`"1M"`/`-5`/
    `3.5`/`true`/`null`）而我们当作"未声明"处理，报告会全绿（只有一条 warn），
    用户以为已把关 —— 保护缺席而无人知晓。这与 `verifier_path` 段同一约定：
    **想开却没生效 = 不放行**（fail-closed）。

    ⚠️ `0` **不在**本清单里（曾在此，2026-10-29 移出）：它是**文档明文定义的
    「显式不做此判定」**，与"类型写错"不同类。params-propose/SKILL.md 的 schema
    字面量默认值就是 `0`，并逐字说明"用户明确不做此判定时写 0（= 未声明，校验器
    登记一条 warn 明说'未做该判定'）"。旧实现把 `raw <= 0` 一并判 hard，于是
    **照文档生成的默认 run.params.json** 让 Stage 3/4/5 在健康数据上永远写不出
    .done —— 文档教 A、代码判 B。`0` 的行为由下面
    `test_min_variants_zero_means_explicitly_not_doing_it` 钉住。
    """
    _tiny_joint_fixture(tmp_path)
    _write_params(tmp_path, sample_names=["SAMPLE_A"], min_variants=bad)
    rc, rep = run_verifier(tmp_path, "04_joint_genotyping", prefix="co", samples="SAMPLE_A")
    assert rc == 1, f"min_variants={bad!r} 无法生效却被放行（应判 fail-closed）"
    assert "min_variants_declared" in names(rep, "hard"), rep


@pytest.mark.parametrize("bad_zero", [False, 0.0])
def test_min_variants_false_is_malformed_not_exempt(tmp_path, bad_zero):
    """★`false` / `0.0` 必须仍判畸形 —— `False == 0` 在 Python 里为真，是**类型写错**。

    这条是 `0` 豁免的**边界**：豁免的是**整数** `0`（文档定义的"显式不做"），
    不是任何"等于 0"的值。`"min_variants": false`（bool）与 `0.0`（float）
    都是类型写错 → fail-closed。若不加 `isinstance` 守卫，`false` 会被静默放行。
    """
    _tiny_joint_fixture(tmp_path)
    _write_params(tmp_path, sample_names=["SAMPLE_A"], min_variants=bad_zero)
    rc, rep = run_verifier(tmp_path, "04_joint_genotyping", prefix="co", samples="SAMPLE_A")
    assert rc == 1, f"min_variants={bad_zero!r} 是类型写错，必须 fail-closed（不能当豁免）"
    assert "min_variants_declared" in names(rep, "hard"), rep


def test_min_variants_zero_means_explicitly_not_doing_it(tmp_path):
    """★`0` = 文档定义的「显式不做此判定」→ 行为与"键不存在"一致（warn、不拦）。

    这是 G04 的回归防线：文档（params-propose SKILL.md）与实现必须在**同一
    语义**上 —— 文档教的写值不得被校验器判 hard。行为上与"未声明"完全一致：
    报告里有一条 warn 明说"未做该判定"（不是静默通过，也不是拦截）。
    """
    _tiny_joint_fixture(tmp_path)
    _write_params(tmp_path, sample_names=["SAMPLE_A"], min_variants=0)
    rc, rep = run_verifier(tmp_path, "04_joint_genotyping", prefix="co", samples="SAMPLE_A")
    assert rc == 0, (
        "min_variants=0 被判拦截 —— 而 params-propose 的 schema 默认值就是 0，"
        "照文档生成的 run.params.json 会让健康数据永远写不出 .done")
    assert "min_variants_declared" not in names(rep, "hard"), rep
    fl = next(c for c in rep["checks"] if c["name"] == "joint.variant_floor")
    assert fl["level"] == "warn" and not fl["passed"], fl
    assert "未做" in fl["detail"], fl


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


# ============================ Stage 0.5：fastp 产物 ============================
# 为什么这一组必须存在（第二轮审计 major，全部对真实 fastp 1.1.0 实测）：
# 早期实现**只从 fastp.json 读一个数**、从不打开 clean FASTQ，于是这些全放行：
#   · 10 条 read 的文件配声称 90 万条的 JSON（rc=0）
#   · R1/R2 严重不配对（rc=0）
#   · 0 条 read 的合法 gzip 空流（`gzip -t` 对它也返回 0，rc=0）
# 而 BWA 消费的是 FASTQ，JSON 只是 fastp **打算**写多少条。
import gzip as _gzip


def _mk_fastp(tmp_path, n1, n2, jr1, jr2, trust=None, *, json_present=True,
              merged=None, tag="s1", summary_after=None, summary_before=None):
    """造一套 fastp 产物：两个 clean FASTQ + 一份 JSON（形态同真 fastp）。

    真 fastp 1.1.0 在 PE 模式下**总是**写顶层 `read1/read2_after_filtering`
    （实测四种配置皆然），故夹具也写上 —— 否则测的是夹具而不是真实产物。
    `trust` 传 "raw" 可强制写成旧式 JSON（缺 per-mate 字段）以测 fail-closed 分支。

    `summary_after` / `summary_before` 用来**只篡改 `summary` 那一处**，让
    per-mate 键与 FASTQ 保持真值 —— 这是 F01 的攻击形态（JSON 自证。
    默认 None 时三者自洽，同真 fastp）。
    """
    out = tmp_path / "out"
    out.mkdir(exist_ok=True)
    for name, n in (("clean_1", n1), ("clean_2", n2)):
        with _gzip.open(str(out / f"{tag}.{name}.fq.gz"), "wt") as f:
            for i in range(n):
                f.write(f"@r{i}\nACGTACGTAC\n+\nIIIIIIIIII\n")
    if not json_present:
        return
    r = {"summary": {"after_filtering": {"total_reads":
                                         jr1 + jr2 if summary_after is None else summary_after},
                     "before_filtering": {"total_reads":
                                          jr1 + jr2 if summary_before is None else summary_before}}}
    if merged is not None:
        r["merged_and_filtered"] = {"total_reads": merged}
    else:
        r["read1_after_filtering"] = {"total_reads": jr1}
        r["read2_after_filtering"] = {"total_reads": jr2}
    (out / f"{tag}.fastp.json").write_text(json.dumps(r))


def test_stage05_healthy_fastp_products_pass(tmp_path):
    """阳性对照：JSON 与 FASTQ 一致 → 通过（证明下面不是"永远拦"）。"""
    _mk_fastp(tmp_path, 1000, 1000, 1000, 1000)
    rc, rep = run_verifier(tmp_path, "00_fastp_qc", samples="s1")
    assert rc == 0, f"健康 fastp 产物被拦：{names(rep, 'hard')}"


def test_stage05_read_counts_come_from_fastq_not_json(tmp_path):
    """★核心阴性对照：JSON 声称的读数与 FASTQ 实际不符 → 必须拦。

    这是审计的原始攻击形态：10 条 read 的文件 + 声称 90 万条的 JSON。
    旧实现读 JSON 就放行（rc=0），而 Stage 1 会拿这 10 条去比对 ——
    下游"变异数异常少"，届时已无法追溯。
    """
    _mk_fastp(tmp_path, 10, 10, 450000, 450000)
    rc, rep = run_verifier(tmp_path, "00_fastp_qc", samples="s1")
    assert rc == 1, "JSON 说 45 万、实际只有 10 条，却放行了"
    bad = names(rep, "hard")
    assert "report[s1].matches_clean_1" in bad, bad


def test_stage05_summary_lying_while_per_mate_honest_is_caught(tmp_path):
    """★F01 阴性对照：**只篡改 `summary`**、per-mate 键与 FASTQ 全真 → 必须拦。

    这是"判据读派生数据不读本体"在 fastp 侧的原样重现：`report[s].reads` 与
    `keep_rate` 早期**只读 `summary`**，而 `summary` 是同一份 JSON 的另一处 ——
    拿文件跟它自己对齐。实测（把 after 改成 900000、before 改成 4000，
    两份 clean FASTQ 与 per-mate 键全部保持真值 1000/1000）：

        改动前： [PASS] report[s1].reads: 清洗后 900000 reads
                 [PASS] report[s1].keep_rate: 保留率 22500.0%（阈值 ≥80%）
                 ✅ 全部检查通过   rc=0

    注意 `matches_clean_*`（拿 per-mate 键与文件对账）**照样通过** —— 那条判据
    对得上，因为它比的是 per-mate 键，而攻击只动 summary。于是整套判据无人能拦。
    """
    _mk_fastp(tmp_path, 1000, 1000, 1000, 1000,
              summary_after=900000, summary_before=4000)
    rc, rep = run_verifier(tmp_path, "00_fastp_qc", samples="s1")
    assert rc == 1, "summary 自证 90 万条、实际 2000 条，却放行了"
    bad = names(rep, "hard")
    assert "report[s1].reads" in bad, bad


def test_stage05_impossible_keep_rate_is_caught(tmp_path):
    """★`keep_rate > 100%` 必须判 hard：fastp 只会丢弃/裁短 read，不会凭空增加。

    实测 fastp 1.1.0 九种配置（默认/-A/-l/-D/-5 -3/--unpaired1,2/-c/-Y/-g）
    下 after ≤ before 恒成立（比值 1.000 或 0.666）。故 >1 说明分子分母不是同一轮。
    早期实现算出 22500% 还报 `[PASS] 保留率 22500.0%（阈值 ≥80%）` ——
    一个算术上不可能的值被当成"保留率很高，真好"。
    """
    _mk_fastp(tmp_path, 1000, 1000, 1000, 1000,
              summary_after=900000, summary_before=4000)
    rc, rep = run_verifier(tmp_path, "00_fastp_qc", samples="s1")
    assert rc == 1
    assert "report[s1].keep_rate" in names(rep, "hard"), names(rep, "hard")
    con = next(c for c in rep["checks"] if c["name"] == "report[s1].keep_rate")
    assert "不可能" in con["detail"], con


def test_stage05_keep_rate_under_80pct_is_only_warn(tmp_path):
    """对照：真实可发生的低保留率（30%）只该告警，不该拦 —— 过滤过猛是可调参的问题。

    与上一条配对，确保"算术不可能"那一支**没有**把正常的低保留率也当成 hard。
    """
    _mk_fastp(tmp_path, 300, 300, 300, 300,
              summary_after=600, summary_before=2000)
    rc, rep = run_verifier(tmp_path, "00_fastp_qc", samples="s1")
    assert rc == 0, f"低保留率不该拦: {names(rep, 'hard')}"
    assert "report[s1].keep_rate" in names(rep, "warn"), rep


def test_stage05_missing_before_reads_says_not_judged(tmp_path):
    """分母取不到时必须**登记"未做该判定"**，不能像早期那样整条静默消失。"""
    _mk_fastp(tmp_path, 1000, 1000, 1000, 1000, summary_before=0)
    rc, rep = run_verifier(tmp_path, "00_fastp_qc", samples="s1")
    ks = [c for c in rep["checks"] if c["name"] == "report[s1].keep_rate"]
    assert ks, "分母取了 0 时 keep_rate 整条消失 —— 读者会以为判过了"
    assert not ks[0]["passed"] and "未做" in ks[0]["detail"], ks[0]


def test_stage05_unpaired_mates_are_caught(tmp_path):
    """R1/R2 条数不等 → 拦。两份产物不属于同一次运行的同一对。"""
    _mk_fastp(tmp_path, 1000, 500, 1000, 500)
    rc, rep = run_verifier(tmp_path, "00_fastp_qc", samples="s1")
    assert rc == 1
    assert "clean[s1].paired" in names(rep, "hard"), names(rep, "hard")


def test_stage05_zero_read_valid_gzip_is_caught(tmp_path):
    """★合法 gzip 空流（0 条 read）→ 拦。`gzip -t` 对它返回 0，完整性判据抓不到。

    ⚠️ JSON 这里刻意写成**非零**（50000）—— 否则 `report[s1].reads`（另一条 hard 判据）
    会把本场景拦下，测试就会"因为别的原因通过"，从而测不出 FASTQ 计数判据是否还在。
    实测踩到过：把本判据降级成 warn，测试依然全绿（被 JSON 侧那条掩盖）。
    故断言必须**指名** clean[..].reads 这一条。
    """
    _mk_fastp(tmp_path, 0, 0, 50000, 50000)
    rc, rep = run_verifier(tmp_path, "00_fastp_qc", samples="s1")
    assert rc == 1
    bad = names(rep, "hard")
    assert "clean[s1].clean_1.fq.gz.reads" in bad, (
        f"0 条 read 未被**计数判据**拦下（只有 JSON 侧那条在起作用）：{bad}")


def test_stage05_stale_json_is_caught(tmp_path):
    """陈旧 JSON（旧轮次的报告配新 FASTQ）→ 拦。检查两个方向都能发现。"""
    _mk_fastp(tmp_path, 1000, 1000, 500, 500)
    rc, rep = run_verifier(tmp_path, "00_fastp_qc", samples="s1")
    assert rc == 1
    assert "report[s1].matches_clean_2" in names(rep, "hard")


def test_stage05_missing_json_is_hard_not_warn(tmp_path):
    """缺 JSON 必须是 hard：它是唯一能与 FASTQ 读数对账的独立来源。

    旧实现只登记 warn、rc=0 放行 —— 等于"截断/混轮次"的产物在无人对账时通过。
    """
    _mk_fastp(tmp_path, 100, 100, 0, 0, json_present=False)
    rc, rep = run_verifier(tmp_path, "00_fastp_qc", samples="s1")
    assert rc == 1, "缺 JSON 却放行 —— 没有东西能对账 FASTQ 读数"
    assert "report[s1]" in names(rep, "hard")


def test_stage05_unparseable_json_is_hard(tmp_path):
    """**F24 残留**：fastp JSON 损坏到无法解析 → hard，且**不得**把它的读数当 0。

    为什么单列（同一判据名下有三个登记点，此前只有"缺文件"那一个被测过）：
    `report[{s}]` 在源码里有三处 hard 分支 —— 缺文件（:1267）、无法解析（:1274）、
    顶层不是对象（:1277）。第二轮审计用"把 hard,False 改成 hard,True"的**恒真变异**
    （即判据永远判过、完全不看数据）逐点验证，实测后两处**中和后整套 577 条测试
    仍然全绿**，真 CLI 行为却已被改写 —— 损坏的 JSON 被放行 rc=0。那就是"判据在、
    从不生效"：登记点存在，但没有任何东西守着它。
    （同一批变异对前一个登记点是**会变红**的，故本条缺口是后两处特有。）

    形态按真实的 truncate 来造：fastp 被中断时留下的半截 JSON，最常见的正是
    截断 —— 解析必失败。这里同时钉住"不得静默当成 0 条"。
    """
    _mk_fastp(tmp_path, 100, 100, 100, 100)
    (tmp_path / "out" / "s1.fastp.json").write_text('{"summary": {"after_fil')  # 半截
    rc, rep = run_verifier(tmp_path, "00_fastp_qc", samples="s1")
    assert rc == 1, f"损坏的 fastp JSON 被放行: {names(rep, 'hard')}"
    assert "report[s1]" in names(rep, "hard"), names(rep, "hard")
    det = next(c["detail"] for c in rep["checks"] if c["name"] == "report[s1]")
    assert "无法解析" in det, det


def test_stage05_non_object_json_is_hard(tmp_path):
    """**F24 残留**：fastp JSON 顶层是数组/标量（不是对象）→ hard。

    这是上面那条的姊妹登记点（源码 :1277），同样从未被测到。真实的成因不是
    "fastp 写了数组"，而是**张冠李戴**：`-j` 指到了别的产物（如某个工具写的
    `[1,2,3]`、或聚合脚本的输出）。若不拦，后续 `r.get("summary")` 会在
    **非 dict** 上抛 AttributeError —— 门禁自己崩掉，比拦下更坏。
    故本条既钉住"拦"，也钉住"不崩"。
    """
    _mk_fastp(tmp_path, 100, 100, 100, 100)
    (tmp_path / "out" / "s1.fastp.json").write_text("[1, 2, 3]")
    rc, rep = run_verifier(tmp_path, "00_fastp_qc", samples="s1")
    assert rc == 1, f"顶层非对象的 JSON 被放行: {names(rep, 'hard')}"
    assert "report[s1]" in names(rep, "hard"), names(rep, "hard")
    det = next(c["detail"] for c in rep["checks"] if c["name"] == "report[s1]")
    assert "顶层不是对象" in det, det



def test_stage05_merge_mode_is_caught(tmp_path):
    """`--merge` 的产物形态（per-mate 字段消失、多出 merged_and_filtered）→ 拦。

    本管线 Stage 1 的输入契约是 PE 的两份 clean FASTQ，merged 文件没有任何环节消费：
    部分合并时这些 read 在 Stage 1 静默丢失。此形态由**真 fastp --merge** 实测确认。
    """
    _mk_fastp(tmp_path, 500, 500, 0, 0, merged=500)
    rc, rep = run_verifier(tmp_path, "00_fastp_qc", samples="s1")
    assert rc == 1
    assert "report[s1].merge_mode" in names(rep, "hard"), names(rep, "hard")


def test_stage05_unknown_json_shape_fails_closed(tmp_path):
    """JSON 没有可对账字段（非预期形态）→ fail-closed，不静默放行。

    按本模块原则「无法验证 ≠ 验证通过」：只告警会让这条判据在任何不写 per-mate
    字段的 fastp 上从未生效，而报告上一切正常 —— 即"纸面具备"。
    文案还须点明**重跑不会改变它**，否则用户会陷入空转。
    """
    _mk_fastp(tmp_path, 100, 100, 0, 0, trust="raw")
    out = tmp_path / "out"
    (out / "s1.fastp.json").write_text(json.dumps(
        {"summary": {"after_filtering": {"total_reads": 200},
                     "before_filtering": {"total_reads": 200}}}))
    rc, rep = run_verifier(tmp_path, "00_fastp_qc", samples="s1")
    assert rc == 1
    det = [c["detail"] for c in rep["checks"]
           if c["name"] == "report[s1].per_mate_fields"]
    assert det, f"没有登记 per_mate_fields 判据：{names(rep, 'hard')}"
    assert "重跑" in det[0], f"文案没说清重跑无用（用户会空转）：{det[0]!r}"


def test_stage05_truncated_gzip_still_caught(tmp_path):
    """回归防线：合并成"解压即计数"后，gzip 完整性判据不得退化。"""
    out = tmp_path / "out"
    out.mkdir()
    raw = tmp_path / "big.fq"
    with open(raw, "w") as f:
        for i in range(20000):
            f.write(f"@r{i}\nACGTACGTAC\n+\nIIIIIIIIII\n")
    _sh(["gzip", "-f", str(raw)])
    data = open(str(raw) + ".gz", "rb").read()
    for name in ("clean_1", "clean_2"):
        (out / f"s1.{name}.fq.gz").write_bytes(data[:len(data) // 3])
    (out / "s1.fastp.json").write_text(json.dumps(
        {"summary": {"after_filtering": {"total_reads": 40000},
                     "before_filtering": {"total_reads": 40000}},
         "read1_after_filtering": {"total_reads": 20000},
         "read2_after_filtering": {"total_reads": 20000}}))
    rc, rep = run_verifier(tmp_path, "00_fastp_qc", samples="s1")
    assert rc == 1
    assert any("gzip_ok" in n for n in names(rep, "hard")), names(rep, "hard")


def test_stage05_fastq_without_trailing_newline_is_not_blocked(tmp_path):
    """★假阳性防线：末行无换行的合法 FASTQ 不得被当成"截断"拦下。

    行数是数换行符得的，故这类文件会少数一行（4n 行 → 4n-1 个换行）。
    若照 `行数 % 4` 判就会误拦健康数据 —— 必须用「末行是否为不完整记录」区分。
    """
    out = tmp_path / "out"
    out.mkdir()
    rec = "@r{i}\nACGTACGTAC\n+\nIIIIIIIIII\n"
    for name in ("clean_1", "clean_2"):
        with _gzip.open(str(out / f"s1.{name}.fq.gz"), "wt") as f:
            f.write("".join(rec.format(i=i) for i in range(100)).rstrip("\n"))
    (out / "s1.fastp.json").write_text(json.dumps(
        {"summary": {"after_filtering": {"total_reads": 200},
                     "before_filtering": {"total_reads": 200}},
         "read1_after_filtering": {"total_reads": 100},
         "read2_after_filtering": {"total_reads": 100}}))
    rc, rep = run_verifier(tmp_path, "00_fastp_qc", samples="s1")
    assert rc == 0, f"末行无换行的合法 FASTQ 被误拦：{names(rep, 'hard')}"


def test_stage05_truncated_record_is_caught(tmp_path):
    """与上一条配对：真被截断的记录（行数不是 4 的倍数）→ 必须拦。"""
    out = tmp_path / "out"
    out.mkdir()
    recs = "".join(f"@r{i}\nACGTACGTAC\n+\nIIIIIIIIII\n" for i in range(100))
    for name in ("clean_1", "clean_2"):
        with _gzip.open(str(out / f"s1.{name}.fq.gz"), "wt") as f:
            f.write(recs + "@rX\nACGT")          # 半截记录（末行不完整）
    (out / "s1.fastp.json").write_text(json.dumps(
        {"summary": {"after_filtering": {"total_reads": 200},
                     "before_filtering": {"total_reads": 200}},
         "read1_after_filtering": {"total_reads": 100},
         "read2_after_filtering": {"total_reads": 100}}))
    rc, rep = run_verifier(tmp_path, "00_fastp_qc", samples="s1")
    assert rc == 1
    assert any("fastq_shape" in n for n in names(rep, "hard")), names(rep, "hard")


def test_stage05_missing_gzip_is_environment_not_data(monkeypatch, tmp_path):
    """缺 gzip → 报**环境**问题（need_tool），不是"数据损坏"。"""
    _mk_fastp(tmp_path, 100, 100, 100, 100)
    monkeypatch.setattr(SV, "tool",
                        lambda n: None if n == "gzip" else shutil.which(n))
    rc, rep = run_verifier(tmp_path, "00_fastp_qc", samples="s1")
    assert rc == 1
    assert "gzip" in rep["missing_tools"], rep.get("missing_tools")


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


def test_stage4_missing_combined_gvcf_is_caught(tmp_path):
    """★核心阴性对照：只有最终 VCF、没有 combined.g.vcf.gz → 必须拦。"""
    out = tmp_path / "out"
    out.mkdir()
    _mk_vcf(out / "co.vcf.gz", ["S1"], gvcf=False)
    rc, rep = run_verifier(tmp_path, "04_joint_genotyping", samples="S1", prefix="co")
    assert rc == 1, f"缺 combined.g.vcf.gz 却放行了：{[(c['name'], c['passed']) for c in rep['checks']]}"
    assert "combined.exists" in names(rep, "hard"), names(rep, "hard")


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


def test_stage4_combined_present_healthy_passes(tmp_path):
    """阳性：combined 与 joint VCF 都在 → 通过（证明上面不是"永远拦"）。"""
    out = tmp_path / "out"
    out.mkdir()
    _mk_vcf(out / "co.combined.g.vcf.gz", ["S1"], gvcf=True)
    _mk_vcf(out / "co.vcf.gz", ["S1"], gvcf=False)
    rc, rep = run_verifier(tmp_path, "04_joint_genotyping", samples="S1", prefix="co")
    assert rc == 0, f"健康数据被拦：{names(rep, 'hard')}"


# ========== 第二轮审计 finding #12：11 条 hard 判据此前无任何测试触达 ==========
# 怎么发现这 11 条的：给 `Report.add` 打桩，跑**整个** tests/ 目录，记录每轮真正
# 登记过的判据名，再与源码里声明的 hard 判据对照（不是 grep 猜的）。
# 结果：源码声明 44 条 hard，测试只触达 33 条。
#
# 为什么这些缺口危险：本项目反复出现的形态是**判据存在、但从不生效**（"纸面具备"）。
# 一条没有任何用例触达的 hard 判据，删掉它、或者把它降级成 warn，**没有任何测试会红**
# —— 它和"从未实现"在证据链上是同一件事。
#
# 每条都配了**阳性对照**（健康数据必须放行），否则"永远拦"的实现也能过。
# 而且断言必须**指名**判据：这些场景大多会同时触发别的判据（实测每条都打印了
# 全部 hard 未过项才敢这么写）——只断言 `rc == 1` 的测试会"因为错误的原因通过"。
def _hard_names(rep):
    return [c["name"] for c in rep["checks"]
            if c["level"] == "hard" and not c["passed"] and not c.get("skipped")]


def test_uncovered_readable_garbage_vcf_is_caught(tmp_path):
    """`{label}.readable`：VCF 存在且非空，但**根本不是 VCF**。

    与 `{label}.exists`（判文件在不在）是两条不同的判据：这个文件在、也有字节，
    只是 bcftools 读不了（实测 rc=255 `unknown file type`）。缺了这条，一个被
    截断/覆写成垃圾的 VCF 会以"文件存在"通过存在性检查。
    """
    out = tmp_path / "out"
    out.mkdir()
    (out / "co.vcf.gz").write_text("not a vcf at all\n")
    rc, rep = run_verifier(tmp_path, "04_joint_genotyping", samples="S1", prefix="co")
    assert rc == 1
    assert "joint.readable" in _hard_names(rep), _hard_names(rep)


def test_uncovered_prefix_missing_is_caught(tmp_path):
    """`prefix`：cohort 级阶段（4/5）不给 `--prefix` → 无法定位任何输出。

    必须拦而不是"跳过"：定位不到输出时循环零次，报告会变成"✅ 全部检查通过"，
    而**一条判据都没跑**（同 `samples_given` 的空转形态）。
    """
    (tmp_path / "out").mkdir()
    for stage in ("04_joint_genotyping", "05_filtering"):
        rc, rep = run_verifier(tmp_path, stage, samples="S1")
        assert rc == 1, f"{stage}: 不给 --prefix 却放行"
        assert "prefix" in _hard_names(rep), (stage, _hard_names(rep))


def test_uncovered_empty_sample_list_is_caught(tmp_path):
    """`samples_given`：`--samples` 为空 → 样本守恒判据从未运行，不能判通过。

    触发路径很现实：bash 4.2 + `set -u` 下 `"${sample_names[*]}"` 对空数组报
    unbound variable，最"稳妥"的绕法就是传空串。而空 RUN_DIR 也能拿 rc=0。
    """
    (tmp_path / "out").mkdir()
    rc, rep = run_verifier(tmp_path, "01_bwa_sort", samples="")
    assert rc == 1
    assert "samples_given" in _hard_names(rep), _hard_names(rep)


def test_uncovered_stage2_no_output_at_all_is_caught(tmp_path):
    """`bqsr_output`：Stage 2 一个 BAM 都没产出 → 该阶段实际没完成。

    与 `bqsr.coverage` 的分工：coverage 管"部分样本缺"，本条管"全军覆没"。
    早期实现对 markdup/bqsr 两组共用一个 `if not got: continue`，导致
    "只有 markdup BAM、没有 BQSR BAM"时整个 stage 2 也算通过。
    """
    (tmp_path / "out").mkdir()
    rc, rep = run_verifier(tmp_path, "02_markdup_bqsr", samples="s1")
    assert rc == 1
    assert "bqsr_output" in _hard_names(rep), _hard_names(rep)


def test_uncovered_bqsr_coverage_partial_is_caught(tmp_path):
    """`bqsr.coverage`：2 个样本只有 1 个有 BQSR BAM —— Stage 3 输入契约要求全有。

    阳性侧由既有用例覆盖（Stage 2 全套产物 → rc=0），这里专测"丢了一部分"。
    本条**不能**只在 `got` 内部遍历：没产出的样本根本不进 got，整份丢失永远发现不了。
    """
    out = tmp_path / "out"
    out.mkdir()
    make_bam(str(out / "s1.sorted.markdup.BQSR.bam"), sample="s1")
    rc, rep = run_verifier(tmp_path, "02_markdup_bqsr", samples="s1,s2")
    assert rc == 1
    assert "bqsr.coverage" in _hard_names(rep), _hard_names(rep)


def test_uncovered_reads_conservation_lost_is_caught(tmp_path):
    """★`reads_conservation_lost`：样本在 Stage 1 有产出、Stage 2 整份没了。

    守恒检查必须**双向**：数值异常（`reads_conservation`，warn）与整份丢失
    （本条，**hard**）是两件事。早期只做前者，因为循环写的是
    `for s, t in totals.items()` —— 丢掉的样本不进 totals，于是"样本在中间被丢掉"
    （本次改动最重的防守对象）在 Stage 2 完全放行。

    ⚠️ 断言必须评到 `reads_conservation_lost` **本条**：该场景下
    `reads_conservation`（warn）**不会**未过（存活样本的数值是相符的），
    所以只断言 rc==1 会漏掉"hard 那条被删、靠别的判据拉成 1"的变异。
    """
    out = tmp_path / "out"
    out.mkdir()
    make_bam(str(out / "s1.sorted.markdup.BQSR.bam"), sample="s1")
    prev = tmp_path / "prev.json"
    prev.write_text(json.dumps({"read_totals": {"s1": 20, "s2": 20}}))
    rc, rep = run_verifier(tmp_path, "02_markdup_bqsr", samples="s1",
                           prev_reads_file=str(prev))
    assert rc == 1
    assert "reads_conservation_lost" in _hard_names(rep), _hard_names(rep)


def test_uncovered_gvcf_identity_mismatch_is_caught(tmp_path):
    """`gvcf[{s}].identity`：文件名是 s1、内部 #CHROM 声明的却是别的样本。

    与 `{label}.samples` 是两条：samples 判"队列名单对不对"，identity 判
    "文件名与内容是不是同一个样本"。后者给的是**可读归因** —— "样本串了"而不是
    "少了一个样本"，排查方向完全不同（查 resume 是否跳过了上一轮残留的 GVCF，
    或拷贝/改名错配）。
    """
    out = tmp_path / "out"
    out.mkdir()
    _mk_vcf(out / "s1.g.vcf.gz", ["WRONGSAMPLE"], gvcf=True)
    rc, rep = run_verifier(tmp_path, "03_haplotypecaller", samples="s1")
    assert rc == 1
    got = _hard_names(rep)
    assert "gvcf[s1].identity" in got, got
    # 归因文案要真的指向"串样本"，而不是复述文件缺失
    det = " ".join(c["detail"] for c in rep["checks"]
                   if c["name"] == "gvcf[s1].identity")
    assert "样本串了" in det, det


def test_uncovered_report_persisted_failure_is_caught(tmp_path):
    """`report_persisted`：校验报告写不出去 → 必须拦。

    报告不只是给人看的：`.verify/<stage>.json` 是**下一阶段 reads 守恒比较的输入**
    （`--prev-reads-file`）。写不出去时下游的守恒检查会静默失效 —— 正是本机制的
    头号失效形态。故缺了机制的一环即不放行。

    构造：把 `.verify/` 置为不可写（chmod 500）。⚠️ 此时报告本身也读不回来，
    所以断言走 stdout 的渲染文本，而不是 `.verify/*.json`。
    """
    import io
    from contextlib import redirect_stdout

    out = tmp_path / "out"
    out.mkdir()
    vdir = tmp_path / ".verify"
    vdir.mkdir()
    make_bam(str(out / "s1.sorted.bam"), sample="s1")
    os.chmod(vdir, 0o500)
    argv, old = ["stage_verify.py", "--stage", "01_bwa_sort",
                 "--run-dir", str(tmp_path), "--samples", "s1"], sys.argv
    sys.argv = argv
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            rc = SV.main()
    finally:
        sys.argv = old
        os.chmod(vdir, 0o700)          # 让 pytest 的 tmp 清理能删掉它
    assert rc == 1, "报告写不出去却放行 —— 下一阶段的守恒检查会静默失效"
    assert "report_persisted" in buf.getvalue(), buf.getvalue()[-800:]


def test_uncovered_single_end_fastp_shape_is_caught(tmp_path):
    """`report[{s}].single_end`：fastp 以 **SE 模式**跑出来的 JSON 形态。

    本管线 Stage 1 的输入契约是 PE 的两份 clean FASTQ。SE 形态（只有
    `read1_after_filtering`）意味着 fastp 只产出了一端的清洗结果，下游 BWA
    的 `-I`（或配对读取）会直接失败或只比对一半。
    真 fastp 的 SE 形态实测：只有顶层 `read1_after_filtering`，无 read2 键。
    """
    out = tmp_path / "out"
    out.mkdir()
    with _gzip.open(str(out / "s1.clean_1.fq.gz"), "wt") as f:
        for i in range(100):
            f.write("@r\nACGT\n+\nIIII\n")
    (out / "s1.fastp.json").write_text(json.dumps(
        {"summary": {"after_filtering": {"total_reads": 100}},
         "read1_after_filtering": {"total_reads": 100}}))
    rc, rep = run_verifier(tmp_path, "00_fastp_qc", samples="s1")
    assert rc == 1
    assert "report[s1].single_end" in _hard_names(rep), _hard_names(rep)


def test_uncovered_malformed_indel_floor_blocks(tmp_path):
    """`indel.min_variants_declared`：`min_variants_indel` 写了但不是正整数。

    fail-closed 原则的 INDEL 侧站点：写这个键即表示**要开**该判定，却无法生效
    → 判失败而非静默降级。与 SNP 侧的 `min_variants_declared` 同一条原则。
    """
    out = tmp_path / "out"
    out.mkdir()
    _mk_vcf(out / "co.snp.filter.vcf.gz", ["S1"], gvcf=False)
    _mk_vcf(out / "co.indel.filter.vcf.gz", ["S1"], gvcf=False)
    (tmp_path / "run.params.json").write_text(json.dumps({"min_variants_indel": "1M"}))
    rc, rep = run_verifier(tmp_path, "05_filtering", samples="S1", prefix="co")
    assert rc == 1
    assert "indel.min_variants_declared" in _hard_names(rep), _hard_names(rep)


def test_uncovered_missing_samtools_blocks_not_skips(tmp_path, monkeypatch):
    """`tool[samtools]`：samtools 探测不到 → fail-closed 拦截（不是"跳过=通过"）。

    与既有的 `test_missing_tool_fails_closed_not_silently_passed` 是**不同的工具**：
    那条屏蔽的是 bcftools（VCF 侧），本条屏蔽 samtools（BAM 侧）。
    两侧各自 fail-closed 才算完整 —— 只堵一边的话，没装 samtools 的机器上
    BAM 的 reads 数、@RG、排序、索引**全部**判据静默失效而报告全绿。
    """
    out = tmp_path / "out"
    out.mkdir()
    make_bam(str(out / "s1.sorted.bam"), sample="s1")
    _orig = SV.tool
    monkeypatch.setattr(SV, "tool", lambda n: None if n == "samtools" else _orig(n))
    rc, rep = run_verifier(tmp_path, "01_bwa_sort", samples="s1")
    assert rc == 1, "samtools 缺失时未拦截（门禁在没装工具的机器上静默失效）"
    assert "tool[samtools]" in _hard_names(rep), _hard_names(rep)
    assert "samtools" in rep["missing_tools"], rep["missing_tools"]


# ============ 覆盖面审计：**只跑过 PASS、从没见过 FAIL** 的判据 ============
#
# 为什么单列这一段（方法论，值得记）：上面那批 11 条是拿"源码里有哪些判据"
# 对比"测试里提到过哪些名字"找出来的，抓的是**整条判据从未被触及**。
# 但那个办法看不见第二种形态 —— **判据跑了，却只跑过 PASS 分支**。
# 一条 hard 判据若永远只在健康数据上被调用过，它接错参数、判反条件、
# 把真值写成常量 True，整套回归**一个红灯都不会有**。
#
# 实测办法：给 `Report.add` 打桩，记录三元组 (源码行号, level, passed)，
# 跑完整套测试后统计。结果是下面这些判据**从未登记过 passed=False**：
#     hard: body_readable / genotypes / quickcheck / read_totals_recorded
#     warn: index_drift / genotype_completeness / filter_applied / markdup.coverage
# 其中 quickcheck 是审计 finding [12] 点名的那条（"连 stdout/JSON 都用不上，
# 强制它永远判 FAIL 后 pytest 全绿、探针全绿"）。下面逐条补上阴性对照。
# 变异验证：把上面四条的登记整块删掉，修复前 566 passed（无人察觉）。


def _mk_genotype_vcf(path, samples, *, missing, n=30):
    """造一个**基因型可控**的 VCF：前 n-missing 条 `0/1`，其余 `./.`。

    与 `_mk_vcf` 的区别：那个所有位点都是 `0/1`，因此 `genotypes` 判据
    **只会走 PASS 分支** —— 正是本节要补的缺口。
    """
    raw = str(path)[:-3]
    lines = ["##fileformat=VCFv4.2", "##contig=<ID=chr1,length=100000>",
             '##FILTER=<ID=PASS,Description="ok">',
             '##FORMAT=<ID=GT,Number=1,Type=String,Description="GT">',
             "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\t"
             + "\t".join(samples)]
    for i in range(n):
        gt = "./." if i >= n - missing else "0/1"
        lines.append(f"chr1\t{100+i*10}\t.\tA\tG\t50\tPASS\t.\tGT\t"
                     + "\t".join(gt for _ in samples))
    with open(raw, "w") as f:
        f.write("\n".join(lines) + "\n")
    assert _sh(["bgzip", "-f", raw]).returncode == 0
    assert _sh(["tabix", "-f", "-p", "vcf", str(path)]).returncode == 0
    return str(path)


def _mk_vcf_no_gt_header(path, samples, n=30):
    """造一个**头部没有 ##FORMAT=<ID=GT>** 的 VCF（记录行仍是 GT）。

    这正是 `genotypes` 判据第一条分支描述的形态：GenotypeGVCFs 没跑、
    或被 `bcftools annotate -x` 把 FORMAT/GT 定义删掉。下游
    `bcftools query -f '[%GT\\n]'` 会直接报 no such tag defined。
    """
    raw = str(path)[:-3]
    lines = ["##fileformat=VCFv4.2", "##contig=<ID=chr1,length=100000>",
             '##FILTER=<ID=PASS,Description="ok">',
             # 故意不加 ##FORMAT=<ID=GT,...>
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


def test_quickcheck_pass_is_not_enough_body_must_be_readable(tmp_path):
    """★核心阴性对照：**中段损坏**的 BAM 必须拦（`quickcheck` 抓不到它）。

    这是 finding [17] 的第二半，也是本判据存在的**唯一理由**：
    `quickcheck` 只看文件头与末尾 28 字节 BGZF EOF，**不读数据块**；
    `idxstats` 只看索引。于是"位翻转/局部覆写但长度、头、EOF、索引都没变"
    的 BAM 一路全绿。实测（本节 fixture 自证）：
        healthy   quickcheck rc=0
        corrupted quickcheck rc=0   <- 结构检查**通过**
        corrupted view -c   rc=1    <- 本体读不出来
    故断言必须是"quickcheck 判过、body_readable 判失败、且 rc=1"三者同时成立：
    只断言 rc==1 的话，换成别的判据拦住也会通过（"因错误的原因通过"）。
    """
    out = tmp_path / "out"
    out.mkdir()
    bam = str(out / "s1.sorted.bam")
    make_bam(bam, sample="s1", reads=200)
    # 前提自检：健康时两样都过（否则下面的拦不一定是"中段损坏"引起的）
    assert _sh([SAMTOOLS, "quickcheck", bam]).returncode == 0
    # 中段抹掉 64 字节（保持文件长度、头部与 EOF 不变）
    data = open(bam, "rb").read()
    mid = len(data) // 2
    open(bam, "wb").write(data[:mid] + bytes(64) + data[mid + 64:])
    assert _sh([SAMTOOLS, "quickcheck", bam]).returncode == 0, \
        "前提不成立：本 fixture 没能造出「quickcheck 看不出来」的损坏"
    assert subprocess.run([SAMTOOLS, "view", "-c", bam],
                          capture_output=True).returncode != 0, \
        "前提不成立：损坏还不够深，samtools 本体读得出来"

    rc, rep = run_verifier(tmp_path, "01_bwa_sort", samples="s1")
    assert rc == 1, "中段损坏的 BAM 被放行（quickcheck 通过了）"
    bad = _hard_names(rep)
    assert "bam[s1].body_readable" in bad, bad
    assert "bam[s1].quickcheck" not in bad, (
        "quickcheck 本应对此**通过** —— 若它失败了，说明 fixture 变成了"
        "另一种损坏（头/EOF 被破坏），本条就测不到「结构检查挡不住」这一层了")


def test_truncated_bgzf_eof_is_caught_by_quickcheck(tmp_path):
    """★阴性对照：**截断掉末尾 BGZF EOF 块**的 BAM 必须被 `quickcheck` 拦下。

    与上面 `test_quickcheck_pass_is_not_enough_body_must_be_readable` 是**互补**的
    两条，合起来才说明 `quickcheck` + `body_readable` **两条判据各司其职**：
        中段损坏   quickcheck rc=0（看不见） / view -c rc=1  → body_readable 抓
        EOF 截断   quickcheck rc=16（专抓这个） / view -c rc=0 → quickcheck 抓
    实测确认（见两条测试的前置断言）。少了这一条，`quickcheck` 的失败分支
    从未被执行过 —— 它的登记即使被整块删掉、或永远判 True，回归也全绿
    （审计 finding [12] 正是这么证明它"对整套回归没有任何贡献"的）。
    `view -c` 在本形态下**返回 0 且读数正常**，所以只靠 body_readable 抓不到它。
    """
    out = tmp_path / "out"
    out.mkdir()
    bam = str(out / "s1.sorted.bam")
    make_bam(bam, sample="s1", reads=200)
    data = open(bam, "rb").read()
    open(bam, "wb").write(data[:-28])            # 去掉 28 字节 BGZF EOF 块
    assert _sh_rc([SAMTOOLS, "quickcheck", bam]) != 0, \
        "前提不成立：截断 EOF 后 quickcheck 仍然通过"
    assert _sh_rc([SAMTOOLS, "view", "-c", bam]) == 0, \
        "本 fixture 同时破坏了数据体 —— 那测的就不是 quickcheck 这一条了"

    rc, rep = run_verifier(tmp_path, "01_bwa_sort", samples="s1")
    assert rc == 1, "缺 BGZF EOF 块的 BAM 被放行"
    bad = _hard_names(rep)
    assert "bam[s1].quickcheck" in bad, bad
    assert "bam[s1].body_readable" not in bad, (
        "本体可读，这条不该失败 —— 否则说明 fixture 破坏过头，"
        "本条就测不到「quickcheck 独立抓 EOF 截断」这一层")


def test_index_drift_is_reported_as_warn_not_silent(tmp_path):
    """`index_drift`：索引漏计（Parabricks applybqsr 常见）→ 必须**报出来**。

    finding [23] 的产物：`pbrun applybqsr` 写的 .bai 在中小输入上系统性漏计
    （实测 10/200/300 reads → 索引报 0）。修法是"本体计数为准"，但**分歧本身
    必须可见** —— 否则一个坏索引会静静躺在磁盘上，直到某天下游按区读取拿到空结果。
    判据定为 warn 而非 hard：BAM 完好、下游可用，只需重建索引（拦下会误伤健康数据）。
    故本条同时断言 rc==0（不误伤）与 index_drift 存在且为 warn（不静默）。
    """
    out = tmp_path / "out"
    out.mkdir()
    bam = str(out / "s1.sorted.markdup.BQSR.bam")
    make_bam(bam, sample="s1", reads=200)
    # 造"漏计"的索引：拿另一个 10-reads BAM 的 .bai 顶替（长度足够，内容指向太少）
    decoy = str(tmp_path / "decoy.bam")
    make_bam(decoy, sample="s1", reads=10)
    shutil.copy(decoy + ".bai", bam + ".bai")
    idx_says = subprocess.run([SAMTOOLS, "idxstats", bam], capture_output=True,
                              text=True).stdout
    body = subprocess.run([SAMTOOLS, "view", "-c", bam], capture_output=True,
                          text=True).stdout.strip()
    assert int(body) == 200, body
    assert "200" not in idx_says, (
        f"前提不成立：索引没被换成漏计的版本，idxstats={idx_says!r}")

    rc, rep = run_verifier(tmp_path, "02_markdup_bqsr", samples="s1")
    drift = [c for c in rep["checks"] if c["name"].endswith(".index_drift")]
    assert drift, "索引与本体分歧，却没有登记 index_drift（缺口静默）"
    assert drift[0]["level"] == "warn" and not drift[0]["passed"], drift[0]
    assert rc == 0, (
        "漏计索引被判 hard 拦下了健康 BAM —— 这正是 finding [23] 的原始症状"
        f"（拦下好数据）：{_hard_names(rep)}")


def test_all_missing_genotypes_are_caught(tmp_path):
    """★核心阴性对照：记录行/样本列/FILTER 全正常，但**所有基因型都是 `./.`**。

    finding 另一条：Stage 4/5 早期没有任何判据读基因型。这种产物的所有"结构性"
    判据（存在、索引、位点数、样本列、GVCF 标记）**全部通过**，而交付物里
    一个可用基因型都没有 —— 下游关联分析拿到的是空表。
    """
    out = tmp_path / "out"
    out.mkdir()
    _mk_vcf(out / "co.combined.g.vcf.gz", ["S1"], gvcf=True)
    _mk_genotype_vcf(out / "co.vcf.gz", ["S1"], missing=30)   # 全部 ./.
    rc, rep = run_verifier(tmp_path, "04_joint_genotyping", samples="S1", prefix="co")
    assert rc == 1, "全部基因型缺失却放行"
    assert "joint.genotypes" in _hard_names(rep), _hard_names(rep)


def test_missing_gt_header_is_caught(tmp_path):
    """`genotypes` 的另一条分支：**头部没有 FORMAT/GT 定义**（记录行仍是 GT）。

    与"全 `./.`"是不同的失效：这条是定义被删（`annotate -x`）或
    GenotypeGVCFs 没真正跑。下游 `bcftools query -f '[%GT\\n]'` 直接报错，
    而门禁若只看 `bcftools view -H` 的行数是看不出区别的。
    """
    out = tmp_path / "out"
    out.mkdir()
    _mk_vcf(out / "co.combined.g.vcf.gz", ["S1"], gvcf=True)
    _mk_vcf_no_gt_header(out / "co.vcf.gz", ["S1"])
    rc, rep = run_verifier(tmp_path, "04_joint_genotyping", samples="S1", prefix="co")
    assert rc == 1, "头部缺 FORMAT/GT 定义却放行"
    bad = _hard_names(rep)
    assert "joint.genotypes" in bad, bad
    det = " ".join(c["detail"] for c in rep["checks"]
                   if c["name"] == "joint.genotypes")
    assert "FORMAT/GT" in det, det


def test_sparse_genotypes_warn_on_completeness(tmp_path):
    """`genotype_completeness`：非缺失基因型 < 50% → warn（放行但告警）。

    为什么是 warn 不是 hard：某些真实项目确实靠低深度样本，位点缺失过半可能
    是**预期**的（如低覆盖测序）。但它必须被说出来 —— 关联分析的有效样本量
    会因此大幅缩水，而报告若全绿没人会去数。
    故断言 rc==0（不误伤）+ 该 warn 存在（不静默）。
    """
    out = tmp_path / "out"
    out.mkdir()
    _mk_vcf(out / "co.combined.g.vcf.gz", ["S1"], gvcf=True)
    _mk_genotype_vcf(out / "co.vcf.gz", ["S1"], missing=24, n=30)   # 只有 6/30 非缺失
    rc, rep = run_verifier(tmp_path, "04_joint_genotyping", samples="S1", prefix="co")
    gc = [c for c in rep["checks"] if c["name"].endswith(".genotype_completeness")]
    assert gc, "非缺失基因型仅 20%，却没有登记 genotype_completeness"
    assert gc[0]["level"] == "warn" and not gc[0]["passed"], gc[0]
    assert rc == 0, f"低完整度被判 hard 拦下（应只告警）：{_hard_names(rep)}"


def test_filter_dot_is_distinguished_from_all_passing(tmp_path):
    """`filter_applied`：FILTER 为 `.` 的位点必须与"过滤后都留下"区分开。

    这是本项目反复出现的"假 PASS"形态（V18/fail_rate 同族）：全部位点的
    FILTER 都是 `.`（未记录判定）时，`pass_rate` 只统计 `== "PASS"` 的条数，
    于是 npass=0 → 那条判据反而会拦（正确）；但若**一部分**是 PASS、另一部分
    是 `.`，`pass_rate` 依然绿，读报告的人会以为"这些位点通过了 QD/FS/SOR/MQ"，
    实际上它们**根本没被过滤过**（VariantFiltration 没跑）。
    故本条造"一半 PASS 一半 `.`"：断言 rc==0（pass_rate 不该拦）但
    `filter_applied` 必须登记为 warn。
    """
    out = tmp_path / "out"
    out.mkdir()
    raw = str(out / "co.snp.filter.vcf.gz")
    lines = ["##fileformat=VCFv4.2", "##contig=<ID=chr1,length=100000>",
             '##FILTER=<ID=PASS,Description="ok">',
             '##FORMAT=<ID=GT,Number=1,Type=String,Description="GT">',
             "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tS1"]
    ts = [("A", "G"), ("G", "A"), ("C", "T"), ("T", "C")]
    for i in range(40):
        r, a = ts[i % 4]
        filt = "PASS" if i < 20 else "."          # 后 20 条未记录判定
        lines.append(f"chr1\t{100+i*7}\t.\t{r}\t{a}\t50\t{filt}\t.\tGT\t0/1")
    rawp = raw[:-3]
    open(rawp, "w").write("\n".join(lines) + "\n")
    assert _sh(["bgzip", "-f", rawp]).returncode == 0
    assert _sh(["tabix", "-f", "-p", "vcf", raw]).returncode == 0
    _mk_vcf(out / "co.indel.filter.vcf.gz", ["S1"], gvcf=False)

    rc, rep = run_verifier(tmp_path, "05_filtering", samples="S1", prefix="co")
    fa = [c for c in rep["checks"] if c["name"].endswith(".filter_applied")]
    assert fa, "有 20 条 FILTER='.' 却没有任何登记（与'都过滤过了'无法区分）"
    assert fa[0]["level"] == "warn" and not fa[0]["passed"], fa[0]
    assert "snp.filter_applied" not in _hard_names(rep), _hard_names(rep)


def test_markdup_coverage_partial_warns(tmp_path):
    """`markdup.coverage`：部分样本缺 markdup BAM → warn（不是静默通过）。

    为什么是 warn：走 Parabricks fq2bam 后端时 markdup 已在该步完成，
    本阶段不产出 markdup BAM **属预期**。但"少了几个"必须看得见 ——
    否则 markdup 步骤真被跳过时，报告里连一行都没有。
    与 `bqsr.coverage`（hard）的分工：BQSR BAM 是 Stage 3 的输入契约，
    缺了必然失败；markdup BAM 只是中间产物。
    """
    out = tmp_path / "out"
    out.mkdir()
    make_bam(str(out / "s1.sorted.markdup.BQSR.bam"), sample="s1")
    make_bam(str(out / "s1.sorted.markdup.bam"), sample="s1")
    make_bam(str(out / "s2.sorted.markdup.BQSR.bam"), sample="s2")
    # s2 缺 markdup BAM（但 BQSR BAM 在）
    rc, rep = run_verifier(tmp_path, "02_markdup_bqsr", samples="s1,s2")
    mc = [c for c in rep["checks"] if c["name"] == "markdup.coverage"]
    assert mc, "s2 缺 markdup BAM 却没有登记 markdup.coverage"
    assert mc[0]["level"] == "warn" and not mc[0]["passed"], mc[0]
    assert "markdup.coverage" not in _hard_names(rep), _hard_names(rep)


def test_read_totals_recorded_is_asserted_not_assumed(tmp_path):
    """`read_totals_recorded`：Stage 1 必须真的把 reads 数写进报告。

    finding [17]/V4 的同族形态：Stage 2 的 reads 守恒判据读 `prev["read_totals"]`，
    而早期**没有任何地方写过该键** —— 守恒检查因此永远静默跳过（"声称存在、
    从不生效"）。本判据是"上游真的写了"的唯一证据，故它不能只被当成
    "反正总会过"的一条。这里断言三件事：该判据存在、**判过**、且
    `rep["read_totals"]` 里确实有该样本的读数。
    """
    out = tmp_path / "out"
    out.mkdir()
    make_bam(str(out / "s1.sorted.bam"), sample="s1", reads=120)
    rc, rep = run_verifier(tmp_path, "01_bwa_sort", samples="s1")
    assert rc == 0, rep
    rt = [c for c in rep["checks"] if c["name"] == "read_totals_recorded"]
    assert rt, "Stage 1 没有登记 read_totals_recorded"
    assert rt[0]["passed"], rt[0]
    assert rep.get("read_totals"), (
        "报告里 read_totals 是空的 —— Stage 2 的守恒判据会因此静默跳过")
    assert rep["read_totals"].get("s1") == 120, rep["read_totals"]
