#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""对抗式复核确认的 false-negative 判据 —— 用真实工具端到端验证。

每条都造出**真实产物**（真 bgzip/tabix/samtools），确认：
  · 修复前：坏产物 rc=0（放行）
  · 修复后：坏产物 rc=1（拦下），且好产物仍 rc=0（不误伤）

分段：A–J 为首批（样本集/未过滤/缺 BQSR/丢样本/串样本/截断 gz/空名单/索引命名/
判级/位点下限）；K–M 为第二批，对应三处**绕过门禁本身**的缺陷：
  K 陈旧索引（`idxstats` 读索引不读 BAM）—— 空 BAM 借旧索引全绿放行
  L pbrun 默认常量读组 `SM:sample` —— 默认后端下拦下每一个样本
  M 手工 `tabix -p vcf` 在 `set -e` 下退出码 1 —— 脚本死在门禁之前

用以防"修了代码但判据是装饰性的"—— 这是本项目"纸面具备"缺陷的同形风险。
"""
import json
import os
import subprocess
import sys
import tempfile

REPO = "/home/chenyuxi/skill/agent"
VERIFIER = os.path.join(REPO, "skills/wgs-expressive-pipeline/scripts/stage_verify.py")
PY = sys.executable
ENV = dict(os.environ)
ENV["PATH"] = "/home/chenyuxi/anaconda3/envs/Agent/bin:" + ENV.get("PATH", "")

results = []


def sh(cmd, **kw):
    return subprocess.run(cmd, shell=isinstance(cmd, str), capture_output=True,
                          text=True, env=ENV, **kw)


def verify(stage, run_dir, samples=None, prefix=None, extra=None):
    cmd = [PY, VERIFIER, "--stage", stage, "--run-dir", run_dir]
    if samples is not None:
        cmd += ["--samples", samples]
    if prefix is not None:
        cmd += ["--prefix", prefix]
    if extra:
        cmd += extra
    p = subprocess.run(cmd, capture_output=True, text=True, env=ENV)
    return p.returncode, p.stdout + p.stderr


def verdicts(stage, run_dir):
    """读回 `.verify/<stage>.json`，返回 {判据名: (级别, 是否通过)}。

    ⚠️ **不要在 stdout 上做 `"<name>" in out` 的子串断言** —— 那无法区分
    三种截然不同的情形：判据以 **hard 且 FAIL** 登记（真被拦下）、以 **warn** 登记
    （只告警、仍放行）、或只是**恰好出现在说明文字里**。本探针早期就栽在这上面：
    D 段把 `reads_conservation_lost` 写进 detail 字符串却不参与断言，于是把该判据的
    检测整段删掉，探针照样 45/45 全绿 —— 而它自称是"防止判据变装饰"的复核工具。
    改用结构化报告后，"某判据是否真的硬拦"成为可断言的事实。
    """
    p = os.path.join(run_dir, ".verify", f"{stage}.json")
    if not os.path.isfile(p):
        return {}
    with open(p, encoding="utf-8") as f:
        rep = json.load(f)
    return {c["name"]: (c["level"], c["passed"]) for c in rep["checks"]}


def hard_failed(stage, run_dir, name):
    """该判据是否以 **hard 且未通过** 登记（即：真的拦下了这一阶段）。"""
    lv = verdicts(stage, run_dir).get(name)
    return lv == ("hard", False)


def hard_failed_suffix(stage, run_dir, suffix):
    """按**后缀**匹配判据名（如 `readgroup` 命中 `bam[s1].readgroup`）。

    判据名在前面带标签前缀（`bam[s1].` / `snp.` / `joint.`），而前缀由各阶段的循环
    变量决定。探针若硬写完整名（`sample1.readgroup`），一旦标签约定变化就会**静默落空**：
    `.get()` 返回 None，断言变假但看不出是"判据不存在"还是"判据没生效"。
    故这里按后缀找，并且**要求恰好命中一个** —— 命中 0 个（判据被删/改名）与命中多个
    （歧义）都返回 False 并打印原因，让失败可诊断。
    """
    hits = [k for k, v in verdicts(stage, run_dir).items()
            if k.endswith(suffix) and v == ("hard", False)]
    return len(hits) == 1


def record(name, ok, detail):
    results.append((name, ok, detail))
    print(f"  [{'OK ' if ok else 'BAD'}] {name}: {detail}")


def mk_vcf(path, samples, n=100, filt="PASS", declare_gvcf=False, titv_ratio=2.0):
    """造一个真实的 bgzip+tabix VCF。filt 可为 'PASS' / '.' / 固定串。"""
    lines = ["##fileformat=VCFv4.2", "##contig=<ID=chr1,length=100000>"]
    if declare_gvcf:
        lines.append('##ALT=<ID=NON_REF,Description="any possible alt">')
    lines += ['##FILTER=<ID=QD2,Description="QD<2">',
              '##FORMAT=<ID=GT,Number=1,Type=String,Description="GT">',
              "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\t"
              + "\t".join(samples)]
    # 交替制造 ts (A>G) 与 tv (A>T) 以得到可控 Ti/Tv
    for i in range(n):
        ref, alt = ("A", "G") if (i % 2 == 0) else ("A", "T")
        lines.append(f"chr1\t{100+i*10}\t.\t{ref}\t{alt}\t50\t{filt}\t.\tGT\t"
                     + "\t".join("0/1" for _ in samples))
    raw = path[:-3] if path.endswith(".gz") else path
    with open(raw, "w") as f:
        f.write("\n".join(lines) + "\n")
    sh(["bgzip", "-f", raw])
    sh(["tabix", "-f", "-p", "vcf", path])


def mk_bam(path, nreads=200, sample="s1"):
    """造一个真实的坐标排序 BAM。"""
    sam = path + ".sam"
    with open(sam, "w") as f:
        f.write(f"@HD\tVN:1.6\tSO:coordinate\n@SQ\tSN:chr1\tLN:100000\n"
                f"@RG\tID:rg1\tSM:{sample}\tLB:lib1\tPL:ILLUMINA\n")
        for i in range(nreads):
            f.write(f"r{i}\t0\tchr1\t{100+i}\t60\t10M\t*\t0\t0\t"
                    f"ACGTACGTAC\tIIIIIIIIII\tRG:Z:rg1\n")
    sh(["samtools", "sort", "-o", path, sam])
    sh(["samtools", "index", path])
    os.unlink(sam)


print("=" * 74)
print("A. Stage 5：样本集不一致的过滤后 VCF（false-negative #1，critical）")
print("=" * 74)
d = tempfile.mkdtemp()
os.makedirs(os.path.join(d, "out"), exist_ok=True)
# 契约是 s1,s2，但 VCF 里装的是 WRONG
mk_vcf(os.path.join(d, "out", "co.snp.filter.vcf.gz"), ["WRONG"])
mk_vcf(os.path.join(d, "out", "co.indel.filter.vcf.gz"), ["WRONG"])
rc, out = verify("05_filtering", d, samples="s1,s2", prefix="co")
record("stage5 wrong samples -> blocked", rc == 1,
       f"rc={rc}（期望 1）；样本判据: "
       f"{'有' if 'samples' in out else '无'}")
# 对照：样本正确时应放行
d2 = tempfile.mkdtemp()
os.makedirs(os.path.join(d2, "out"), exist_ok=True)
mk_vcf(os.path.join(d2, "out", "co.snp.filter.vcf.gz"), ["s1", "s2"])
mk_vcf(os.path.join(d2, "out", "co.indel.filter.vcf.gz"), ["s1", "s2"])
rc2, _ = verify("05_filtering", d2, samples="s1,s2", prefix="co")
record("stage5 correct samples -> pass", rc2 == 0, f"rc={rc2}（期望 0，不误伤）")

print()
print("=" * 74)
print("B. Stage 5：FILTER 全为 '.'（未过滤，false-negative #11）")
print("=" * 74)
d3 = tempfile.mkdtemp()
os.makedirs(os.path.join(d3, "out"), exist_ok=True)
mk_vcf(os.path.join(d3, "out", "co.snp.filter.vcf.gz"), ["s1", "s2"], filt=".")
mk_vcf(os.path.join(d3, "out", "co.indel.filter.vcf.gz"), ["s1", "s2"], filt=".")
rc3, out3 = verify("05_filtering", d3, samples="s1,s2", prefix="co")
_v3 = {k: v for k, v in verdicts("05_filtering", d3).items()
       if k.endswith("filter_applied") or k.endswith("pass_rate")}
# ⚠️ 本段的期望值**已被加强**：早期只有一条 warn 级的 filter_applied（当时断言 rc3 == 0）。
#    后来 `pass_rate` 改为 hard（npass > 0），于是"全 '.'"这种**压根没过滤**的 VCF
#    不再只是告警 —— 直接拦截。这是更正确的行为：下游 GWAS 拿到未按 QD/FS/SOR/MQ
#    过滤的位点比"少一条告警"严重得多。探针的旧期望（rc3==0）因此过期，
#    现改为断言**两条判据都在、且真的拦下**（warn 说清原因 + hard 拦住）。
record("stage5 all-dot FILTER -> blocked（而非仅告警）",
       rc3 == 1 and len(_v3) == 4
       and all(v == ("warn", False) for k, v in _v3.items() if k.endswith("filter_applied"))
       and all(v == ("hard", False) for k, v in _v3.items() if k.endswith("pass_rate")),
       f"rc={rc3}（期望 1）；判据={_v3}（'.' 不再被算作 PASS；SNP/INDEL 各一条）")

print()
print("=" * 74)
print("C. Stage 2：只有 markdup、完全没有 BQSR（false-negative #2，critical）")
print("=" * 74)
d4 = tempfile.mkdtemp()
os.makedirs(os.path.join(d4, "out"), exist_ok=True)
mk_bam(os.path.join(d4, "out", "s1.sorted.markdup.bam"), sample="s1")
mk_bam(os.path.join(d4, "out", "s2.sorted.markdup.bam"), sample="s2")
rc4, out4 = verify("02_markdup_bqsr", d4, samples="s1,s2")
# ⚠️ 早期这里写的是 `rc == 1 or rc4 == 1`，而 `rc` 是 A 段 stage5 的旧值、**恒为 1** ——
#    `or` 左边永远成立，rc4 取什么值都不影响结论。实测：把 bqsr.coverage 的登记整段删掉
#    （该 critical 缺陷被真实回退），本段仍打印 [OK]，探针 45/45 全绿。故去掉陈旧变量，
#    并改判**结构化报告**（hard 且 FAIL），而不是 stdout 子串出现与否。
record("stage2 no BQSR at all -> blocked",
       rc4 == 1 and hard_failed("02_markdup_bqsr", d4, "bqsr.coverage"),
       f"rc={rc4}（期望 1）；bqsr.coverage 级别={verdicts('02_markdup_bqsr', d4).get('bqsr.coverage')}")
# 对照：BQSR BAM 齐 → 放行
d5 = tempfile.mkdtemp()
os.makedirs(os.path.join(d5, "out"), exist_ok=True)
for s in ("s1", "s2"):
    mk_bam(os.path.join(d5, "out", f"{s}.sorted.markdup.BQSR.bam"), sample=s)
rc5, out5 = verify("02_markdup_bqsr", d5, samples="s1,s2")
record("stage2 full BQSR (parabricks-style) -> pass", rc5 == 0,
       f"rc={rc5}（期望 0）")

print()
print("=" * 74)
print("D. Stage 2：队列里丢了一个样本（false-negative #3）")
print("=" * 74)
d6 = tempfile.mkdtemp()
os.makedirs(os.path.join(d6, "out"), exist_ok=True)
for s in ("s1", "s2"):
    mk_bam(os.path.join(d6, "out", f"{s}.sorted.markdup.BQSR.bam"), sample=s)
# s3 丢失；上一阶段报告称 s1/s2/s3 都有
prev = os.path.join(d6, ".verify", "01_bwa_sort.json")
os.makedirs(os.path.dirname(prev), exist_ok=True)
import json
json.dump({"read_totals": {"s1": 200, "s2": 200, "s3": 200}}, open(prev, "w"))
rc6, out6 = verify("02_markdup_bqsr", d6, samples="s1,s2,s3",
                   extra=["--prev-reads-file", prev])
# ⚠️ 早期只断言 rc6 == 1，而该场景下 rc 会被**另一条**判据（bqsr.coverage，构造里 s3
#    的 BQSR BAM 不存在）拉成 1 —— 把 reads_conservation_lost 的检测块改成 `if False:`
#    （"样本整份丢失"这条 hard 判据被真实回退），本段依旧 [OK]。故必须断言**该判据本身**。
record("stage2 one sample lost -> blocked",
       rc6 == 1 and hard_failed("02_markdup_bqsr", d6, "reads_conservation_lost"),
       f"rc={rc6}（期望 1）；reads_conservation_lost 级别="
       f"{verdicts('02_markdup_bqsr', d6).get('reads_conservation_lost')}")

print()
print("=" * 74)
print("E. Stage 3：GVCF 内部样本名与文件名不符（false-negative #4）")
print("=" * 74)
d7 = tempfile.mkdtemp()
os.makedirs(os.path.join(d7, "out"), exist_ok=True)
# 文件名是 s1/s2，内部却声明 WRONGSAMPLE
for s in ("s1", "s2"):
    mk_vcf(os.path.join(d7, "out", f"{s}.g.vcf.gz"), ["WRONGSAMPLE"],
           declare_gvcf=True)
rc7, out7 = verify("03_haplotypecaller", d7, samples="s1,s2")
record("stage3 sample swapped -> blocked",
       rc7 == 1 and any(hard_failed("03_haplotypecaller", d7, n)
                        for n in ("gvcf[s1].identity", "gvcf[s2].identity")),
       f"rc={rc7}（期望 1）；gvcf[s1].identity 级别="
       f"{verdicts('03_haplotypecaller', d7).get('gvcf[s1].identity')}")
# 对照：内部样本名正确
d8 = tempfile.mkdtemp()
os.makedirs(os.path.join(d8, "out"), exist_ok=True)
for s in ("s1", "s2"):
    mk_vcf(os.path.join(d8, "out", f"{s}.g.vcf.gz"), [s], declare_gvcf=True)
rc8, _ = verify("03_haplotypecaller", d8, samples="s1,s2")
record("stage3 correct GVCF -> pass", rc8 == 0, f"rc={rc8}（期望 0）")

print()
print("=" * 74)
print("F. Stage 0.5：截断的 clean FASTQ（false-negative #6）")
print("=" * 74)
d9 = tempfile.mkdtemp()
os.makedirs(os.path.join(d9, "out"), exist_ok=True)
# 造一个完好的 gz，再截断
raw = os.path.join(d9, "big.fq")
with open(raw, "w") as f:
    for i in range(20000):
        f.write(f"@r{i}\nACGTACGTAC\n+\nIIIIIIIIII\n")
sh(["gzip", "-f", raw])
full = raw + ".gz"
data = open(full, "rb").read()
for s in ("s1",):
    p = os.path.join(d9, "out", f"{s}.clean_1.fq.gz")
    open(p, "wb").write(data[:len(data) // 3])       # 截断
    p2 = os.path.join(d9, "out", f"{s}.clean_2.fq.gz")
    open(p2, "wb").write(data[:len(data) // 3])
    json.dump({"summary": {"after_filtering": {"total_reads": 900000},
                           "before_filtering": {"total_reads": 950000}}},
              open(os.path.join(d9, "out", f"{s}.fastp.json"), "w"))
rc9, out9 = verify("00_fastp_qc", d9, samples="s1")
record("stage0.5 truncated gz -> blocked", rc9 == 1,
       f"rc={rc9}（期望 1）；{'有' if 'gzip_ok' in out9 else '无'} gzip_ok 判据")
# 对照：完好 gz
d10 = tempfile.mkdtemp()
os.makedirs(os.path.join(d10, "out"), exist_ok=True)
for s in ("s1",):
    open(os.path.join(d10, "out", f"{s}.clean_1.fq.gz"), "wb").write(data)
    open(os.path.join(d10, "out", f"{s}.clean_2.fq.gz"), "wb").write(data)
    json.dump({"summary": {"after_filtering": {"total_reads": 20000},
                           "before_filtering": {"total_reads": 20000}}},
              open(os.path.join(d10, "out", f"{s}.fastp.json"), "w"))
rc10, _ = verify("00_fastp_qc", d10, samples="s1")
record("stage0.5 intact gz -> pass", rc10 == 0, f"rc={rc10}（期望 0）")

print()
print("=" * 74)
print("G. 空样本名单不再静默放行（integration #9）")
print("=" * 74)
d11 = tempfile.mkdtemp()          # 完全空的 RUN_DIR
for st, extra in (("00_fastp_qc", None), ("01_bwa_sort", None),
                  ("03_haplotypecaller", None)):
    rc11, out11 = verify(st, d11, samples="")
    record(f"{st} empty samples -> blocked", rc11 == 1,
           f"rc={rc11}（期望 1）；{'有' if 'samples_given' in out11 else '无'} "
           "samples_given")

print()
print("=" * 74)
print("H. 索引命名：GATK 写 foo.bai（不是 foo.bam.bai），四种合法命名都要认")
print("=" * 74)
# 背景：校验器早期只查 bam + ".bai"，而 GATK/Picard 的 --CREATE_INDEX true 写 foo.bai
# → 真机 GATK 产出的 BQSR BAM 稳定被判"缺索引"，健康数据被拦（实测复现）。
# samtools/GATK 实际认四种：foo.bam.bai / foo.bai / foo.bam.csi / foo.csi。
for naming in ("foo.bam.bai", "foo.bai", "foo.bam.csi", "foo.csi"):
    d12 = tempfile.mkdtemp()
    os.makedirs(os.path.join(d12, "out"))
    bam = os.path.join(d12, "out", "s1.sorted.markdup.BQSR.bam")
    mk_bam(bam, nreads=200, sample="s1")          # samtools index → .bam.bai
    os.remove(bam + ".bai")
    if naming.endswith(".csi"):
        sh(["samtools", "index", "-c", bam])
        produced = bam + ".csi"
    else:
        sh(["samtools", "index", bam])
        produced = bam + ".bai"
    if naming == "foo.bai":
        os.rename(produced, os.path.join(d12, "out", "s1.sorted.markdup.BQSR.bai"))
    elif naming == "foo.csi":
        os.rename(produced, os.path.join(d12, "out", "s1.sorted.markdup.BQSR.csi"))
    rc12, out12 = verify("02_markdup_bqsr", d12, samples="s1")
    _v12 = verdicts("02_markdup_bqsr", d12).get("bqsr[s1].index")
    # 判据必须**以 hard 且 PASS 登记**（而不是"stdout 里没出现'缺少索引'"这种弱判据：
    # 判据整段消失时，子串断言同样成立 —— 那正是本段要防的失效）。
    record(f"索引命名 {naming} 被识别", rc12 == 0 and _v12 == ("hard", True),
           f"rc={rc12}；bqsr[s1].index 级别={_v12}")

# 阴性对照：真删掉 BQSR 索引后必须仍被拦（否则上面可能是"永远放行"）
d13 = tempfile.mkdtemp()
os.makedirs(os.path.join(d13, "out"))
bam13 = os.path.join(d13, "out", "s1.sorted.markdup.BQSR.bam")
mk_bam(bam13, nreads=200, sample="s1")
os.remove(bam13 + ".bai")
rc13, out13 = verify("02_markdup_bqsr", d13, samples="s1")
record("BQSR 索引真缺失仍被拦",
       rc13 == 1 and hard_failed("02_markdup_bqsr", d13, "bqsr[s1].index"),
       f"rc={rc13}（期望 1）；bqsr[s1].index 级别="
       f"{verdicts('02_markdup_bqsr', d13).get('bqsr[s1].index')}")

print()
print("=" * 74)
print("I. markdup 索引只告警、BQSR 索引才硬拦（判级不能一刀切）")
print("=" * 74)
# MarkDuplicates 的 CREATE_INDEX 默认 false → 标准命令本就不产出该索引；
# 下游 BaseRecalibrator/ApplyBQSR 读它也不需要索引（实测 rc=0）→ 判 hard 会拦健康数据。
d14 = tempfile.mkdtemp()
os.makedirs(os.path.join(d14, "out"))
mk_bam(os.path.join(d14, "out", "s1.sorted.markdup.bam"), nreads=200, sample="s1")
os.remove(os.path.join(d14, "out", "s1.sorted.markdup.bam.bai"))
mk_bam(os.path.join(d14, "out", "s1.sorted.markdup.BQSR.bam"), nreads=200, sample="s1")
rc14, out14 = verify("02_markdup_bqsr", d14, samples="s1")
_v14 = verdicts("02_markdup_bqsr", d14).get("markdup[s1].index")
record("markdup 缺索引不拦截且以 warn 登记", rc14 == 0 and _v14 == ("warn", False),
       f"rc={rc14}（期望 0）；markdup[s1].index 级别={_v14}")

print()
print("=" * 74)
print("J. 位点数下限：显式声明则硬拦，未声明则告警明说未判定")
print("=" * 74)
# 2 样本全基因组队列真实应有数百万位点；早期实现只有 min_variants=1，
# 于是 2 条记录也"✅ 全部检查通过"——该阶段自称要抓的"变异数异常少"根本没落地。
d15 = tempfile.mkdtemp()
os.makedirs(os.path.join(d15, "out"))
# ⚠️ combined.g.vcf.gz 是 Stage 4 的契约产物，必须一起造（见 O 段）——
#    早期这里只造 joint VCF，是 fixture 不完整，A6 补上判据后才暴露。
mk_vcf(os.path.join(d15, "out", "co.combined.g.vcf.gz"), ["S1", "S2"], n=2,
       declare_gvcf=True)
mk_vcf(os.path.join(d15, "out", "co.vcf.gz"), ["S1", "S2"], n=2)
rc15, out15 = verify("04_joint_genotyping", d15, samples="S1,S2", prefix="co",
                     extra=["--min-variants", "1000"])
record("声明下限后变异数异常少被拦",
       rc15 == 1 and hard_failed("04_joint_genotyping", d15, "joint.variant_floor"),
       f"rc={rc15}（期望 1）；joint.variant_floor 级别="
       f"{verdicts('04_joint_genotyping', d15).get('joint.variant_floor')}")
rc16, out16 = verify("04_joint_genotyping", d15, samples="S1,S2", prefix="co")
# 同一次运行目录、不带 --min-variants，也没有 run.params.json 声明 → 应为 warn（未判定）
_v16 = verdicts("04_joint_genotyping", d15).get("joint.variant_floor")
record("未声明下限时告警而非静默通过", rc16 == 0 and _v16 == ("warn", False),
       f"rc={rc16}（期望 0）；joint.variant_floor 级别={_v16}；"
       f"{'已明说未判定' if '未做' in out16 else '静默通过'}")

print()
print("=" * 74)
print("K. 陈旧索引不得掩盖空 BAM（false-negative #12，critical）")
print("=" * 74)
# 背景：`samtools idxstats` 读的是**索引**，不是 BAM 本体。BAM 被重写而索引没重做时
# （重跑 / --resume 跳过 index / 覆写产物），idxstats 拿**上一轮的旧值**作答 ——
# 0 reads 的空 BAM 报出 200 reads，`read_count`/`mapped_ratio`/`mapped_rate` 三条
# 判据同时被骗过、门禁全绿放行。这是本模块自认头号失效（空 BAM）的**绕过路径**。
import time
d16 = tempfile.mkdtemp()
os.makedirs(os.path.join(d16, "out"))
bam16 = os.path.join(d16, "out", "s1.sorted.bam")
mk_bam(bam16, nreads=200, sample="s1")            # 正常产物：BAM + 索引
time.sleep(1.1)                                    # 保证 mtime 可分（秒级分辨率）
# 覆写成**只有头部**的空 BAM（模拟比对失败/重跑产出空文件），沿用旧索引
hdr = bam16 + ".hdr.sam"
with open(hdr, "w") as f:
    f.write("@HD\tVN:1.6\tSO:coordinate\n@SQ\tSN:chr1\tLN:100000\n"
            "@RG\tID:rg1\tSM:s1\tLB:lib1\tPL:ILLUMINA\n")
sh(["samtools", "sort", "-o", bam16, hdr])
os.unlink(hdr)
# 前提自检：必须真的构成"索引陈旧"这一场景，否则下面的结论无意义
n_real = sh(["samtools", "view", "-c", bam16]).stdout.strip()
p_idx = sh(["samtools", "idxstats", bam16])
n_idx = sum(int(l.split("\t")[2]) for l in p_idx.stdout.splitlines() if l.count("\t") >= 2)
record("K-前提 场景成立（真值 0 而 idxstats 说 200）",
       n_real == "0" and p_idx.returncode == 0 and n_idx == 200
       and "older than" in p_idx.stderr,
       f"view -c={n_real}；idxstats rc={p_idx.returncode} 报 mapped={n_idx}；"
       f"stderr 含 'older than'={'older than' in p_idx.stderr}")
rc17, out17 = verify("01_bwa_sort", d16, samples="s1")
record("K 陈旧索引下的空 BAM → 被拦",
       rc17 == 1 and hard_failed_suffix("01_bwa_sort", d16, ".index_stale"),
       f"rc={rc17}（期望 1）；索引判据="
       f"{ {k: v for k, v in verdicts('01_bwa_sort', d16).items() if 'index' in k} }；"
       f"旧实现此处报 mapped=200 且 rc=0")
# 修复动作必须真能恢复：重建索引后门禁应看到**真值 0**并据此拦截
sh(["samtools", "index", bam16])
rc18, out18 = verify("01_bwa_sort", d16, samples="s1")
_v18 = {k: v for k, v in verdicts("01_bwa_sort", d16).items() if "read" in k or "index" in k}
record("K 重建索引后仍拦（此刻按真值 0 判）",
       rc18 == 1 and any(v == ("hard", False) for v in _v18.values()),
       f"rc={rc18}（期望 1）；相关判据={_v18}")
# 对照：健康 BAM + 新鲜索引必须放行（不能因为加判据而误伤）
d17 = tempfile.mkdtemp()
os.makedirs(os.path.join(d17, "out"))
mk_bam(os.path.join(d17, "out", "s1.sorted.bam"), nreads=200, sample="s1")
rc19, _ = verify("01_bwa_sort", d17, samples="s1")
record("K 健康 BAM（索引新鲜）→ 放行", rc19 == 0, f"rc={rc19}（期望 0，不误伤）")

print()
print("=" * 74)
print("L. pbrun fq2bam 默认常量读组 SM:sample 必被拦（false-negative #13）")
print("=" * 74)
# 背景：`pbrun fq2bam` 不传 --read-group-sm 时自动生成 **写死常量** 读组
# `@RG ID:r0.1 LB:lib1 PL:bar SM:sample PU:r0.1`（真机跑出的原样）。所有样本同名 →
# Stage 1 的 readgroup(hard) 必拦；且该 SM 会继承进 GVCF 样本列，使 Stage 3 的
# samples/identity(hard) 连锁失败。故 skill 模板必须传 --read-group-sm "${sample}"。
def _mk_rg_bam(path, sm):
    sam = path + ".sam"
    with open(sam, "w") as f:
        f.write("@HD\tVN:1.6\tSO:coordinate\n@SQ\tSN:chr1\tLN:100000\n"
                f"@RG\tID:r0.1\tLB:lib1\tPL:bar\tSM:{sm}\tPU:r0.1\n")
        for i in range(200):
            f.write(f"r{i}\t0\tchr1\t{100+i}\t60\t10M\t*\t0\t0\t"
                    f"ACGTACGTAC\tIIIIIIIIII\tRG:Z:r0.1\n")
    sh(["samtools", "sort", "-o", path, sam])
    sh(["samtools", "index", path])
    os.unlink(sam)

d18 = tempfile.mkdtemp()
os.makedirs(os.path.join(d18, "out"))
_mk_rg_bam(os.path.join(d18, "out", "s1.sorted.bam"), "sample")   # pbrun 默认
rc20, out20 = verify("01_bwa_sort", d18, samples="s1")
record("L SM:sample（pbrun 默认）→ 被拦",
       rc20 == 1 and hard_failed_suffix("01_bwa_sort", d18, ".readgroup"),
       f"rc={rc20}（期望 1）；readgroup 判据="
       f"{ {k: v for k, v in verdicts('01_bwa_sort', d18).items() if 'readgroup' in k} }")
d19 = tempfile.mkdtemp()
os.makedirs(os.path.join(d19, "out"))
_mk_rg_bam(os.path.join(d19, "out", "s1.sorted.bam"), "s1")       # 传了 --read-group-sm
rc21, _ = verify("01_bwa_sort", d19, samples="s1")
record("L SM:s1（传了 --read-group-sm）→ 放行", rc21 == 0,
       f"rc={rc21}（期望 0）—— 证明拦截的是默认常量读组本身，不是 pbrun 产物")
# 文档侧：模板必须带上该参数，否则上面这条判据会在真机上拦下**每一个**样本
align_md = os.path.join(REPO, "skills/wgs-express-align-sort/SKILL.md")
t_align = open(align_md, encoding="utf-8").read() if os.path.isfile(align_md) else ""
record("L align-sort 模板含 --read-group-sm",
       "--read-group-sm" in t_align and "${sample_name}" in t_align,
       "fq2bam 模板必须传样本名，否则默认 SM:sample 恒定")

print()
print("=" * 74)
print("M. 手工 tabix 在 set -e 下杀死脚本、够不到门禁（false-negative #14）")
print("=" * 74)
# 背景：GATK 的 --create-output-variant-index 默认 true，HaplotypeCaller /
# VariantFiltration 都已写出 .tbi。skill 文档里若再补一条 `tabix -p vcf`，会因
# 索引已存在而 **rc=1**，配合阶段脚本头部的 `set -euo pipefail` 当场退出 ——
# 健康数据既不写 .done 也不写 .failed，编排层误判成"已中断"。
d20 = tempfile.mkdtemp()
mk_vcf(os.path.join(d20, "g.vcf.gz"), ["s1"], declare_gvcf=True)  # 真实 bgzip+tabix 产物
p_bare = sh(["tabix", "-p", "vcf", os.path.join(d20, "g.vcf.gz")])
record("M-前提 索引已存在时裸 tabix 退出码非 0",
       p_bare.returncode == 1 and "exists" in p_bare.stderr,
       f"rc={p_bare.returncode}（期望 1）；stderr={p_bare.stderr.strip()[:60]!r}")
p_f = sh(["tabix", "-f", "-p", "vcf", os.path.join(d20, "g.vcf.gz")])
record("M tabix -f 可重复执行（真要用就必须带 -f）", p_f.returncode == 0,
       f"rc={p_f.returncode}（期望 0）")
# 端到端：把裸 tabix 放进 set -e 脚本里，确认它**真的**会拦下后续语句
sh_script = os.path.join(d20, "stage.sh")
marker = os.path.join(d20, "gate_reached")
with open(sh_script, "w") as f:
    f.write("set -euo pipefail\n"
            f"tabix -p vcf '{os.path.join(d20, 'g.vcf.gz')}'\n"
            f"touch '{marker}'\n")
p_e2e = sh(["bash", sh_script])
record("M-端到端 裸 tabix 之后的门禁行不会被执行",
       p_e2e.returncode != 0 and not os.path.exists(marker),
       f"脚本 rc={p_e2e.returncode}（期望非 0）；门禁标记"
       f"{'存在（未复现）' if os.path.exists(marker) else '不存在（已复现）'}")
# 文档侧：三个阶段的 skill 不得再把裸 tabix 写成必做步骤。
# ⚠️ joint-genotyping 是审计发现的**第三处**（当时只修了另外两处）：它更危险 ——
#    `TABIX="$(resolve_tool tabix)"` 变量就摆在手边，LLM 逐条过清单时极易照抄。
for skill, why in (("wgs-express-haplotypecaller", "GVCF 索引"),
                   ("wgs-express-filtering", "SNP/INDEL 索引"),
                   ("wgs-express-joint-genotyping", "联合分型输出索引")):
    p_md = os.path.join(REPO, "skills", skill, "SKILL.md")
    t_md = open(p_md, encoding="utf-8").read() if os.path.isfile(p_md) else ""
    bad = [l for l in t_md.splitlines()
           if "tabix" in l and "-p vcf" in l and "-f" not in l
           and not l.lstrip().startswith("#") and "不要" not in l and "勿" not in l]
    record(f"M {skill} 无比做性质的裸 tabix（{why}）", not bad,
           f"可疑行={bad[:2]}" if bad else "已全部说明为「GATK 自动写索引，勿手工补」")

print()
print("=" * 74)
print("N. 重复率解析：真值 0 不得被报成 100%（false-negative #15）")
print("=" * 74)
# 背景：旧实现 `PERCENT_DUPLICATION[^\d]*([\d.]+)` 抓"紧跟其后的第一个数"。
# metrics 是**表结构**（表头行列名、下一行给值），而 `[^\d]*` 会跨行吃掉
# `ESTIMATED_LIBRARY_SIZE\n` 加库名前缀，抓到库名 `lib1` 里的 `1` ——
# 真值 0 被报成"重复率 100.0%"。报告里那行看起来完全正常（一个偏高的数值 +
# "异常偏高"的建议），没人会怀疑是解析错。
sys.path.insert(0, os.path.join(REPO, "skills/wgs-expressive-pipeline/scripts"))
import stage_verify as SV

_METRICS = (
    "## htsjdk.samtools.metrics.StringHeader\n"
    "# MarkDuplicates --INPUT in.bam --OUTPUT out.bam --METRICS_FILE m.txt\n"
    "\n## METRICS CLASS\tpicard.sam.DuplicationMetrics\n"
    "LIBRARY\tUNPAIRED_READS_EXAMINED\tREAD_PAIRS_EXAMINED\t"
    "SECONDARY_OR_SUPPLEMENTARY_RDS\tUNMAPPED_READS\tUNPAIRED_READ_DUPLICATES\t"
    "READ_PAIR_DUPLICATES\tREAD_PAIR_OPTICAL_DUPLICATES\tPERCENT_DUPLICATION\t"
    "ESTIMATED_LIBRARY_SIZE\n"
    "lib1\t400\t0\t0\t0\t0\t0\t0\t{}\t\n"
)
d0, why0 = SV.parse_percent_duplication(_METRICS.format("0"))
record("N 真值 0 解析为 0.0（不是库名里的 1）", d0 == 0.0,
       f"解析得 {d0}（{why0}）；旧正则会得 1.0 = 报成 100.0%")
# 前提自检：确认这份文件真的能骗过朴素正则（否则本段失去区分度）
import re as _re
sucker = _re.search(r"PERCENT_DUPLICATION[^\d]*([\d.]+)", _METRICS.format("0"))
record("N-前提 该文件确实能骗过朴素正则", sucker and sucker.group(1) == "1",
       f"朴素正则抓到 {sucker.group(1) if sucker else None!r} —— 区分度成立")
for raw, want in (("0.375", 0.375), ("0.05", 0.05)):
    d, _ = SV.parse_percent_duplication(_METRICS.format(raw))
    record(f"N 真值 {raw} 解析正确", d == want, f"解析得 {d}")
# 取不到值时必须明说，不能 except: pass 静默吞掉
d_none, why_none = SV.parse_percent_duplication("no metrics here\n")
record("N 非 metrics 文件 → 返回 None 且有原因（不静默）",
       d_none is None and "没有 PERCENT_DUPLICATION" in why_none, why_none)

print()
print("=" * 74)
print("O. Stage 4：combined.g.vcf.gz 缺失不得静默通过（false-negative #16）")
print("=" * 74)
# 背景：`verify_joint` 里只有 `if have(combined):` 没有 else —— combined 缺失时
# 报告里连一行都不提。实测：只放 co.vcf.gz，报告 4 PASS + 1 warn、rc=0，
# `grep -c combined` = 0。而它是本 skill 输出契约里列明的产物，错误预防清单
# 第 6 条还专门说 --in-vcf 必须是它。
d21 = tempfile.mkdtemp()
os.makedirs(os.path.join(d21, "out"))
mk_vcf(os.path.join(d21, "out", "co.vcf.gz"), ["S1"])           # 只有最终 VCF
rc22, out22 = verify("04_joint_genotyping", d21, samples="S1", prefix="co")
record("O 缺 combined → 被拦",
       rc22 == 1 and hard_failed("04_joint_genotyping", d21, "combined.exists"),
       f"rc={rc22}（期望 1）；combined.exists 级别="
       f"{verdicts('04_joint_genotyping', d21).get('combined.exists')}"
       f"（旧实现此处 rc=0 且报告里 0 次提到 combined）")
# 0 字节要能与"完全没生成"区分（前者是生成坏了，后者是没跑）
d22 = tempfile.mkdtemp()
os.makedirs(os.path.join(d22, "out"))
mk_vcf(os.path.join(d22, "out", "co.vcf.gz"), ["S1"])
open(os.path.join(d22, "out", "co.combined.g.vcf.gz"), "w").close()
rc23, out23 = verify("04_joint_genotyping", d22, samples="S1", prefix="co")
_v23 = verdicts("04_joint_genotyping", d22).get("combined.exists")
record("O 0 字节 combined → 拦且说'存在但为空'",
       rc23 == 1 and _v23 == ("hard", False) and "存在但为空" in out23,
       f"rc={rc23}（期望 1）；combined.exists 级别={_v23}")
# GenomicsDBImport 路径合法缺席 → 降级 warn，不得拦健康数据
d23 = tempfile.mkdtemp()
os.makedirs(os.path.join(d23, "out", "co.genomicsdb"))
mk_vcf(os.path.join(d23, "out", "co.vcf.gz"), ["S1"])
rc24, out24 = verify("04_joint_genotyping", d23, samples="S1", prefix="co")
_v24 = verdicts("04_joint_genotyping", d23).get("combined.exists")
record("O GenomicsDB 工作区存在 → 降级 warn 不拦（避免误伤）",
       rc24 == 0 and _v24 == ("warn", False), f"rc={rc24}（期望 0）；"
       f"combined.exists 级别={_v24}")
# 对照：两文件都在 → 放行
d24 = tempfile.mkdtemp()
os.makedirs(os.path.join(d24, "out"))
mk_vcf(os.path.join(d24, "out", "co.combined.g.vcf.gz"), ["S1"], declare_gvcf=True)
mk_vcf(os.path.join(d24, "out", "co.vcf.gz"), ["S1"])
rc25, _ = verify("04_joint_genotyping", d24, samples="S1", prefix="co")
record("O combined 与 joint 都在 → 放行", rc25 == 0, f"rc={rc25}（期望 0）")

print()
print("=" * 74)
print("P. 表型侧：两个 GWAS 导出的样本集必须一致（false-negative #17）")
print("=" * 74)
# 背景：PLINK 走 `dict(pred)` 去重 + `[iid for iid in fam_order if iid in pred]` 过滤，
# GAPIT 走 `df[["Taxa", pcol]].dropna()` 保留全部行。--fam 缺样本时两文件样本集
# 分不同（实测 PLINK 3 行 / GAPIT 4 行），却都作为权威产物写出 —— 下游用哪个
# 取决于跑 PLINK/GEMMA 还是 GAPIT，且没有任何机制会发现。
sys.path.insert(0, os.path.join(REPO, "skills/phenotype-extract/scripts"))
import phenotype_extract as PE                                    # noqa: E402

PCOL_P = "IMF_预测_%"
_rows = [{"Taxa": s, "状态": "ok", PCOL_P: float(v)} for s, v in
         (("s1", 1.0), ("s2", 2.0), ("s3", 3.0), ("s4", 4.0))]
d25 = tempfile.mkdtemp()
fam_p = os.path.join(d25, "cohort.fam")
open(fam_p, "w").write("F1 s1 0 0 0 -9\nF1 s2 0 0 0 -9\nF1 s3 0 0 0 -9\n")  # 缺 s4
pred, order, not_in_fam = PE.build_gwas_exports(_rows, PCOL_P, fam_p)
record("P fam 缺样本时两导出仍同集合", sorted(order) == sorted(pred),
       f"PLINK={sorted(order)} GAPIT={sorted(pred)}"
       f"（旧实现 PLINK 3 个 / GAPIT 4 个）")
record("P 有预测值的样本没被 fam 静默丢掉", set(order) == {"s1", "s2", "s3", "s4"},
       f"实际 {sorted(order)}；not_in_fam={not_in_fam}")
# fam 是**排序基准**不是过滤器：fam 序生效
open(fam_p, "w").write("F1 s3 0 0 0 -9\nF1 s1 0 0 0 -9\nF1 s2 0 0 0 -9\nF1 s4 0 0 0 -9\n")
pred, order, _ = PE.build_gwas_exports(_rows, PCOL_P, fam_p)
record("P fam 行序生效（不是字典序）", order == ["s3", "s1", "s2", "s4"],
       f"order={order}")

print()
print("=" * 74)
n_bad = [r for r in results if not r[1]]
print(f"总计 {len(results)} 项，通过 {len(results)-len(n_bad)}，失败 {len(n_bad)}")
for n, _, d in n_bad:
    print(f"  FAIL {n}: {d}")
sys.exit(1 if n_bad else 0)
