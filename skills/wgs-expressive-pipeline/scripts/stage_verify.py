#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""stage_verify.py —— WGS 管线各阶段的**结果值**校验器（阶段间门禁）

为什么需要它
============
编排层判定阶段完成的唯一依据是 `.done` 标记，而 `.done` 是脚本跑到末尾就**无条件**
写入的。于是存在一条静默的错误通道：

    BWA 对某个样本失败 → `wait "${pid}" || log_error ...` 吞掉退出码（`||` 使 rc=0，
    `set -e` 不触发）→ 脚本继续执行 → `.done` 照写 → 编排层认为"完成" → 自动推进
    下一阶段 → 下游拿到一个空 BAM / 缺失的 GVCF / 样本数不对的 VCF。

下游工具面对这类输入往往不会立刻报错（GATK 能读一个空 BAM），错误要到很久以后、
以"变异数异常少"这种面貌出现，此时已无法从数值上追溯到是哪一步坏掉的。

本脚本把"这一步的结果值是否可信"变成**可执行的判据**，供阶段脚本在写 `.done` 之前调用：
校验不通过 → 写 `.failed` 而不写 `.done` → 编排层据此停止推进并报错。

判据分级
--------
  hard  不过 → 该阶段判失败，**不写 .done**（编排层不得推进）
  warn  不过 → 仍然写 .done，但把告警写进 `.verify/<stage>.json` 与 stdout，
              供编排层在向用户汇报时带出（例如重复率偏高、Ti/Tv 偏移）

设计约束
--------
- **不依赖仓库相对路径**：生成的阶段脚本位于 `{RUN_DIR}/scripts/`，与仓库无固定相对
  关系。所有输入（--run-dir / --samples / --prefix）均由脚本从
  `run.params.json` 解析后显式传入，与本项目已有的 db_query.py 同一风格。
- **工具用 `command -v` 运行时探测**（仓库硬规则：禁止写死本机 conda 绝对路径）。
  缺失的工具判 **hard**（fail-closed）：工具不在就无从验证，放行等于把"没验证过"当成
  "验证过"——那正是本机制要消灭的静默错误换了个层级。缺失与"数据有问题"是两回事，
  文案会明确区分（见 `Report.need_tool`）：这是**环境问题**，装上后重跑本阶段即可。
- **读索引的检查优先**：`samtools idxstats` 走索引，秒级；避免在大 BAM 上做全量扫描。
- **位点数下限由调用方显式声明**（`--min-variants`）：上文的"变异数异常少"是本机制
  要防的头号失效，但**没有**普适的默认下限（取决于参考基因组大小、队列大小、深度、
  物种）。故未声明时登记一条 **warn** 明说"未做该判定"（不静默通过、也不误拦），
  声明后按 **hard** 判。详见 `variant_floor()`。

输出
----
stdout 人类可读报告 + `{RUN_DIR}/.verify/{stage}.json`（机器可读，供编排层解析）。
退出码：0=通过（含仅有 warn），1=有 hard 项未过。

用法（由阶段脚本在写 .done 前调用）
-----------------------------------
    conda run -n Agent python <repo>/skills/wgs-expressive-pipeline/scripts/stage_verify.py \
        --stage 01_bwa_sort --run-dir "${RUN_DIR}" \
        --samples "sample1,sample2" --json
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys

# ============================ 基础设施 ============================

# 阶段名 → 校验函数（在下方注册）
_STAGES: dict[str, callable] = {}


def stage(name):
    def deco(fn):
        _STAGES[name] = fn
        return fn
    return deco


class Check:
    """一条检查的结论。"""

    __slots__ = ("name", "level", "passed", "detail", "skipped")

    def __init__(self, name, level, passed, detail="", skipped=False):
        self.name = name
        self.level = level
        self.passed = bool(passed)
        self.detail = detail
        self.skipped = skipped

    def as_dict(self):
        return {"name": self.name, "level": self.level, "passed": self.passed,
                "detail": self.detail, "skipped": self.skipped}

    def render(self):
        if self.skipped:
            return f"  [SKIP] {self.name}: {self.detail}"
        mark = "PASS" if self.passed else ("FAIL" if self.level == "hard" else "WARN")
        return f"  [{mark}] {self.name}: {self.detail}"


class Report:
    def __init__(self, stage_name, run_dir):
        self.stage = stage_name
        self.run_dir = run_dir
        self.checks: list[Check] = []
        self.missing_tools: set[str] = set()
        # 本阶段观测到的每样本 reads 数，落进 .verify/*.json 供**下一阶段**做守恒比较。
        # 必须真的写出来：早期版本里 Stage 2 会读 prev["read_totals"]，但没有任何地方
        # 写过这个键 —— 那条守恒检查于是永远静默跳过（声称存在、从不生效）。
        self.read_totals: dict[str, int] = {}

    def add(self, name, level, passed, detail="", skipped=False):
        self.checks.append(Check(name, level, passed, detail, skipped))
        return self

    def skip(self, name, why, level="hard"):
        return self.add(name, level, True, why, skipped=True)

    def need_tool(self, name, purpose):
        """工具缺失时登记为**硬性失败**，而不是跳过。

        为什么不能"跳过 = 通过"：校验的全部价值在于「没验证过的不放行」。
        若工具缺失时判通过，那么任何一台没装 samtools/bcftools 的机器上，
        本门禁会**静默失效** —— 坏数据照常通过，而报告上一切正常。
        那正是这套机制要防的"静默错误"本身，只是换了个层级。

        缺失与"数据有问题"是两回事，所以错误文案明确区分：这里报的是**环境缺工具**，
        让人一眼看出该去装工具，而不是去查数据。
        """
        self.missing_tools.add(name)
        return self.add(
            f"tool[{name}]", "hard", False,
            f"未找到 `{name}` —— 无法完成「{purpose}」。"
            f"校验无法进行即不放行（fail-closed）；请先确保它在 PATH 中"
            f"（conda run -n Agent 下工具应在 PATH；直接跑脚本时可 source "
            f"skills/_shared/env.sh）")

    @property
    def hard_failures(self):
        return [c for c in self.checks if c.level == "hard" and not c.passed]

    @property
    def warnings(self):
        return [c for c in self.checks if c.level == "warn" and not c.passed]

    @property
    def blocked(self):
        return bool(self.hard_failures)

    def as_dict(self):
        return {
            "stage": self.stage,
            "run_dir": self.run_dir,
            "pass": not self.blocked,
            "missing_tools": sorted(self.missing_tools),
            "hard_failures": [c.name for c in self.hard_failures],
            "warnings": [c.name for c in self.warnings],
            # 供下一阶段用 --prev-reads-file 做 reads 守恒比较
            "read_totals": dict(self.read_totals),
            "checks": [c.as_dict() for c in self.checks],
        }

    def render(self):
        lines = [f"\n========== 阶段校验 {self.stage} =========="]
        lines += [c.render() for c in self.checks]
        if self.missing_tools:
            # 与环境相关的失败单独点出来：让人一眼看出"该装工具"而不是"该查数据"，
            # 免得在一个纯环境问题上翻日志找数据线索
            lines.append(f"🔧 环境缺工具：{', '.join(sorted(self.missing_tools))} —— "
                         f"这是**环境问题**（非数据问题）：装上后重跑本阶段即可，"
                         f"无需改动上游产出")
        if self.blocked:
            lines.append(f"❌ 未通过：{len(self.hard_failures)} 项硬性检查失败 "
                         f"→ 不写 .done（编排层不得推进）："
                         f"{', '.join(c.name for c in self.hard_failures)}")
        elif self.warnings:
            lines.append(f"⚠️  通过，但有 {len(self.warnings)} 项告警："
                         f"{', '.join(c.name for c in self.warnings)}")
        else:
            lines.append("✅ 全部检查通过")
        return "\n".join(lines)


def tool(name):
    """运行时探测工具路径；找不到返回 None（仓库硬规则：不写死本机路径）。"""
    return shutil.which(name)


def run(cmd, timeout=120):
    """跑一条只读命令，返回 (rc, stdout, stderr)。绝不抛异常。"""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return 124, "", f"超时（>{timeout}s）"
    except FileNotFoundError as e:
        return 127, "", str(e)
    except Exception as e:                                    # noqa: BLE001
        return 1, "", str(e)


def have(path):
    return bool(path) and os.path.isfile(path) and os.path.getsize(path) > 0


def bam_index(bam):
    """返回该 BAM 的索引路径（不存在则 None）。

    ⚠️ 索引有三种**合法**命名，不能只认一种：
      · `foo.bam.bai` —— `samtools index foo.bam`（本仓库 Stage 1 的写法）
      · `foo.bai`     —— GATK/Picard `--CREATE_INDEX true`（把 .bam 换成 .bai）
      · `foo.bam.csi` —— `samtools index -c foo.bam`（CSI 格式，长 contig 场景）
    三种 samtools 与 GATK 都能自动识别（实测：`samtools idxstats` 与
    `gatk HaplotypeCaller` 对 `.bai` / `.csi` 均正常返回 rc=0）。
    早期版本只查 `bam + ".bai"`，于是**真机上用 GATK 产出的 BQSR BAM 一律被判"缺索引"**
    —— 而 ApplyBQSR 确实写了索引，只是叫 `foo.bai`。健康数据被拦下（实测复现）。
    """
    base, ext = os.path.splitext(bam)
    for cand in (bam + ".bai", base + ".bai", bam + ".csi", base + ".csi"):
        if have(cand):
            return cand
    return None


def parse_samples(s):
    """把 --samples 解析成 sample_name 列表。

    接受两种形态：`sample1,sample2` 或 run.params.json 的 `sample_names` 原样。
    """
    if not s:
        return []
    return [x.strip() for x in s.split(",") if x.strip()]


def parse_percent_duplication(text):
    """从 Picard/GATK 的 MarkDuplicates metrics 文本里取 PERCENT_DUPLICATION。

    返回 `(float 或 None, 说明)`。取不到时返回 `(None, 原因)`，**不抛异常**。

    ⚠️ 这里必须**按 metrics 表结构取列**，不能用 `PERCENT_DUPLICATION[^\\d]*([\\d.]+)`
    之类的正则去抓"紧跟其后的第一个数"。实测（真 GATK MarkDuplicates 产物）：
    该正则的 `[^\\d]*` 会跨行吃掉 `ESTIMATED_LIBRARY_SIZE\n` 和库名前缀，抓到库名
    `lib1` 里的那个 `1` —— 于是**真值 0.0 被报成"重复率 100.0%"**，而报告里那行
    看起来完全正常（一个偏高的数值 + "异常偏高，查文库/比对"的建议），
    没人会怀疑是解析错。同一个文件里 `PERCENT_DUPLICATION` 还出现在
    `## METRICS CLASS` 表头行；正则对该行的 `[^\\d]*` 同样会一路吃到别的字段。

    正确做法：metrics 文件形如
        ## METRICS CLASS  picard.sam.DuplicationMetrics
        LIBRARY <tab> UNPAIRED_READS_EXAMINED <tab> ... <tab> PERCENT_DUPLICATION <tab> ...
        lib1    <tab> 400 <tab> ... <tab> 0 <tab>
    即"表头行给列名、随后若干行给值"，按 `\\t` 切分后**用列名定位列下标**。
    库名恰好也是 `lib1` 这种形态，正是正则被误导的入口。
    """
    lines = [l for l in text.splitlines() if l.strip() and not l.lstrip().startswith("#")]
    for i, line in enumerate(lines):
        cols = line.rstrip("\n").split("\t")
        if "PERCENT_DUPLICATION" not in cols:
            continue
        j = cols.index("PERCENT_DUPLICATION")
        for row in lines[i + 1:]:
            vals = row.split("\t")
            # 只认**同宽度**的列，避免把另一个 metrics 段（如
            # `## METRICS CLASS picard.sam.GcBiasSummaryMetrics`）的行当成本表的。
            if len(vals) != len(cols) or not row.strip():
                break
            raw = vals[j].strip()
            if not raw:
                continue
            try:
                return float(raw), f"取自 {len(cols)} 列表的第 {j} 列"
            except ValueError:
                return None, f"PERCENT_DUPLICATION 列的值不可解析: {raw!r}"
        return None, "表头有 PERCENT_DUPLICATION 列，但后面没有同宽度的数据行"
    return None, "文件里没有 PERCENT_DUPLICATION 表头（不是 MarkDuplicates metrics？）"


# 必须知道样本名单才能判定"样本有没有丢"的阶段。缺名单 = 这条判据从未运行，
# 不能算通过（fail-open 会退化成"空名单 = 全 PASS"，见 main() 里的 fail-closed 检查）。
STAGES_NEEDING_SAMPLES = {"00_fastp_qc", "01_bwa_sort", "02_markdup_bqsr",
                          "03_haplotypecaller", "04_joint_genotyping",
                          "05_filtering"}

# 会做"位点数下限"判定的阶段（即实际调用 variant_floor 的那几个）。
# 一名畸形的 `min_variants` 只应在这几个阶段拦住 —— 若做成无条件拦截，Stage 1
# 这种与位点数毫无关系的阶段也会被卡死，那是把 fail-closed 用错了地方。
STAGES_NEEDING_VARIANT_FLOOR = {"03_haplotypecaller", "04_joint_genotyping",
                                "05_filtering"}


# ============================ BAM 检查 ============================

def check_bam(rep, bam, label, *, min_reads=1, expect_rg=None,
              expect_sorted=None, check_index=True, index_level="hard"):
    """对单个 BAM 做结构+内容检查。返回 (总 reads 数 或 None)。

    `index_level`：`"hard"` / `"warn"` / `None`（不判）。
      · 下游**需要**该索引才跑得动 → hard（如 BQSR BAM：HaplotypeCaller
        无索引直接 `A USER ERROR ... not indexed`，rc=2）
      · 只是中间产物、无人索引它 → warn（如 markdup BAM：BaseRecalibrator /
        ApplyBQSR 读它时**不需要**索引，判 hard 等于凭空加一道门槛）
    """
    if not have(bam):
        rep.add(f"{label}.exists", "hard", False, f"文件不存在或为空: {bam}")
        return None
    rep.add(f"{label}.exists", "hard", True, os.path.basename(bam))

    sam = tool("samtools")
    if not sam:
        # 缺 samtools → 无法验证 BAM，判失败而非跳过（fail-closed，见 Report.need_tool）
        rep.need_tool("samtools", f"验证 BAM {os.path.basename(bam)} 的结构、排序、@RG 与 reads 数")
        return None

    # 1) 结构完整性（注意：quickcheck 只验文件可读，**空 BAM 也会通过**）
    rc, _, err = run([sam, "quickcheck", bam])
    rep.add(f"{label}.quickcheck", "hard", rc == 0,
            "文件结构完整" if rc == 0 else f"samtools quickcheck 失败: {err.strip()[:200]}")

    # 2) 排序状态（下游 GATK 要求 coordinate-sorted）
    if expect_sorted:
        rc, out, _ = run([sam, "view", "-H", bam])
        m = re.search(r"^@HD.*SO:(\S+)", out, re.M)
        so = m.group(1) if m else "(无 SO 标签)"
        rep.add(f"{label}.sorted", "hard", so == "coordinate",
                f"@HD SO={so}" + ("" if so == "coordinate" else "，下游要求 coordinate"))

    # 3) @RG 存在且 SM 正确（Stage 1/2 的经典失败点）
    if expect_rg:
        rc, out, _ = run([sam, "view", "-H", bam])
        rg = [l for l in out.splitlines() if l.startswith("@RG")]
        if not rg:
            rep.add(f"{label}.readgroup", "hard", False,
                    "缺少 @RG —— GATK BaseRecalibrator/HaplotypeCaller 会报 "
                    "'Number of read groups must be >= 1'")
        else:
            sms = set()
            for l in rg:
                m = re.search(r"\tSM:(\S+)", l)
                if m:
                    sms.add(m.group(1))
            want = expect_rg
            ok = want in sms
            rep.add(f"{label}.readgroup", "hard", ok,
                    f"@RG SM={sorted(sms)}" +
                    ("" if ok else f"，期望包含 {want!r}（样本可能串了）"))

    # 4) reads 计数（**这条才是真正防空 BAM 的**）
    idx = bam_index(bam)
    total = None
    mapped = unmapped = None
    # ⚠️ `samtools idxstats` 读的是**索引**，不是 BAM 本体。BAM 被重写而索引没跟着
    #    重做时（重跑、`--resume` 跳过了 index 步骤、覆写产物），idxstats 会**照旧值
    #    作答**，于是空 BAM 报出上一轮的 reads 数 —— 正好放过了本判据要防的头号失效。
    #    实测：200 reads 的健康 BAM 覆写成 0 reads（沿用旧 .bai），`samtools view -c`
    #    说 0、idxstats 说 200，门禁 `[PASS] read_count: mapped=200` 并 rc=0 全绿。
    #    samtools 自己会往 stderr 打 `[W::hts_idx_load3] The index file is older than
    #    the data file` —— 这里直接采信该警告，不另造一套 mtime 比较。
    idx_stale = False
    if check_index and idx:
        rc, out, err = run([sam, "idxstats", bam])
        if "older than" in (err or ""):
            idx_stale = True
            rep.add(f"{label}.index_stale", "hard", False,
                    f"{os.path.basename(idx)} 比 {os.path.basename(bam)} 旧 —— "
                    f"索引与数据不匹配（重跑时索引步骤被跳过？）。**不可据此判定 "
                    f"reads 数**；已改用全量计数复核。重新 `samtools index` 即可修复")
        if rc == 0 and not idx_stale:
            mapped = sum(int(l.split("\t")[2]) for l in out.splitlines()
                         if l.count("\t") >= 2)
            unmapped = sum(int(l.split("\t")[3]) for l in out.splitlines()
                           if l.count("\t") >= 3)
            total = mapped + unmapped
            rep.add(f"{label}.read_count", "hard", total >= min_reads,
                    f"mapped={mapped} unmapped={unmapped} total={total}"
                    + ("" if total >= min_reads else "；**空 BAM**（比对失败但脚本照跑）"))
    if total is None:
        # 无索引（或索引陈旧不可信）→ 回退全量计数（慢，故只作为兜底）
        rc, out, _ = run([sam, "view", "-c", bam], timeout=600)
        if rc == 0 and out.strip().isdigit():
            total = int(out.strip())
            rep.add(f"{label}.read_count", "hard", total >= min_reads,
                    f"total={total}（" + ("索引陈旧，全量计数" if idx_stale
                                          else "无索引，全量计数") + "）")
        else:
            rep.add(f"{label}.read_count", "warn", False,
                    "无 .bai 且无法计数（samtools view -c 失败）")
    if check_index and index_level:
        rep.add(f"{label}.index", index_level, bool(idx),
                os.path.basename(idx) if idx else f"缺少索引: {bam}.bai 或 "
                f"{os.path.splitext(bam)[0]}.bai")

    # 5) 比对率与"全 unmapped"判定（idxstats 已经算出两个数，这里是白送的）
    #
    # 为什么单列这条：**全部 reads 都没比上**是比空 BAM 更隐蔽的失败 ——
    # 文件非空、reads 数正常、quickcheck 通过、@RG 也有，只是 mapped=0。
    # 常见成因：参考基因组用的是另一个物种/版本，BWA 于是把所有 read 都标成 unmapped
    # 却没报错。下游 HaplotypeCaller 会产出「几乎没有变异」的 GVCF，而这看起来
    # 和一个真的低变异样本没有区别。
    if mapped is not None and total:
        if total >= 100:      # 样本量太小时比对率无统计意义，不判
            rate = mapped / total
            rep.add(f"{label}.mapped_ratio", "hard", mapped > 0,
                    f"mapped={mapped}/{total}（{rate:.1%}）"
                    + ("" if mapped > 0 else
                       "；**没有任何 read 比对上** —— 查参考基因组是否选错"
                       "（物种/版本/contig 命名），而非查数据质量"))
            rep.add(f"{label}.mapped_rate", "warn", rate >= 0.90,
                    f"比对率 {rate:.1%}（WGS 通常 ≥95%，告警线 90%）"
                    + ("" if rate >= 0.90 else
                       "；偏低，查污染、接头残留或参考不匹配"))
    elif idx_stale and check_index:
        # 索引陈旧 → mapped/unmapped 无从取得，上面那两条判据**整条不登记**。
        # 不能就此静默：`read_count` 只能证明"有 reads"，证明不了"比上了"。
        # （索引重建后重跑本校验即可恢复完整判定。）
        rep.add(f"{label}.mapped_ratio", "warn", False,
                "索引陈旧，无法用 idxstats 拆分 mapped/unmapped —— "
                "**未做「全部 read 都没比对上」判定**。重建索引后重跑本校验")
    return total


def check_bam_cohort(rep, samples, bam_of, *, min_reads=1, expect_rg=True,
                     label="bam"):
    """对整队列的 BAM 做检查，并做**样本集合守恒**判定。

    bam_of(sample) -> 期望路径。返回 {sample: total_reads}。
    """
    totals = {}
    missing = []
    for s in samples:
        b = bam_of(s)
        t = check_bam(rep, b, f"{label}[{s}]", min_reads=min_reads,
                      expect_rg=(s if expect_rg else None),
                      expect_sorted=True)
        if t is None:
            missing.append(s)
        else:
            totals[s] = t
    if samples:
        rep.add("cohort_sample_set", "hard", not missing,
                f"{len(samples) - len(missing)}/{len(samples)} 个样本产出完整"
                + (f"；缺失/损坏: {missing[:8]}" if missing else ""))
    return totals


# ============================ VCF 检查 ============================

def check_vcf(rep, vcf, label, *, expect_index=True, expect_gvcf=False,
              min_variants=1, expect_samples=None, expect_chroms=None):
    """对单个 VCF/VCF.gz 做检查。返回 (位点数, 样本名列表 或 None)。"""
    if not have(vcf):
        rep.add(f"{label}.exists", "hard", False, f"文件不存在或为空: {vcf}")
        return None, None
    rep.add(f"{label}.exists", "hard", True, os.path.basename(vcf))

    bcf = tool("bcftools")
    tbx = tool("tabix")

    # 索引
    if expect_index:
        idx = vcf + (".csi" if os.path.isfile(vcf + ".csi") else ".tbi")
        ok = have(vcf + ".tbi") or have(vcf + ".csi")
        rep.add(f"{label}.index", "hard", ok,
                os.path.basename(idx) if ok else f"缺少 .tbi/.csi 索引（下游无法按区读取）")

    if not bcf:
        # 缺 bcftools → 无法读 VCF 内容，判失败而非跳过（fail-closed，见 Report.need_tool）。
        # 跳过会把「内容是乱码的 VCF」判成通过 —— 实测确认过这个漏洞。
        rep.need_tool("bcftools", f"读取 {os.path.basename(vcf)} 的记录与样本")
        return None, None

    # 位点数（-H 是流式统计；对 GVCF 可能较慢，给足超时）
    rc, out, err = run([bcf, "view", "-H", vcf], timeout=900)
    if rc != 0:
        rep.add(f"{label}.readable", "hard", False,
                f"bcftools 无法读取: {err.strip()[:200]}")
        return None, None
    n_var = sum(1 for l in out.splitlines() if l.strip())
    rep.add(f"{label}.variants", "hard", n_var >= min_variants,
            f"{n_var} 条记录" +
            ("" if n_var >= min_variants else "；**空 VCF**（上游失败但脚本照跑）"))

    # 样本名（bcf -l 更快；退化为解析 #CHROM 行）
    smps = None
    if expect_samples is not None:
        rc, out2, _ = run([bcf, "query", "-l", vcf])
        if rc == 0:
            smps = [x for x in out2.strip().split("\n") if x]
        else:
            for l in out.splitlines():
                if l.startswith("#CHROM"):
                    smps = l.split("\t")[9:]
                    break
        if smps is None:
            rep.add(f"{label}.samples", "warn", False, "无法取得样本列表")
        else:
            want = set(expect_samples)
            got = set(smps)
            miss, extra = sorted(want - got), sorted(got - want)
            ok = not miss and not extra
            rep.add(f"{label}.samples", "hard", ok,
                    f"{len(smps)} 个样本" +
                    ("" if ok else f"；缺 {miss[:6]} 多 {extra[:6]}"))

    # GVCF 特征：头部必须有 `##ALT=<ID=NON_REF,...>` 声明。
    # ⚠️ 判据要看**头部声明**而非字面 `<NON_REF>`：VCF 头部写的是
    # `##ALT=<ID=NON_REF,Description="...">`（ID 值不带尖括号），而 `<NON_REF>`
    # 那种带括号的形态只出现在**记录行**的 ALT 列。若在头部里找带括号的字面量，
    # 真 GVCF 会被误判为假 GVCF —— 本校验器早期版本正是这么错的，靠真实
    # bcftools 输出才发现（纯 mock 测不出来）。
    if expect_gvcf:
        rc, out3, _ = run([bcf, "view", "-h", vcf])
        has_decl = bool(re.search(r"^##ALT=<ID=NON_REF", out3, re.M))
        rep.add(f"{label}.gvcf_marker", "hard", has_decl,
                "头部声明 ##ALT=<ID=NON_REF>（确为 GVCF）" if has_decl else
                "**头部缺少 ##ALT=<ID=NON_REF> 声明** —— 不是 GVCF，"
                "Stage 4 CombineGVCFs 会失败"
                "（HaplotypeCaller 漏加 -ERC GVCF，或 pbrun 漏加 --gvcf）")

    return n_var, smps


def _resolve_min_variants(args):
    """确定位点数下限，返回 ``(值, 来源说明, 错误)``。

    `--min-variants` 的设计是"由调用方显式声明"（见 variant_floor 的说明），
    但**光有一个 CLI 开关并不构成可用的声明路径**：阶段脚本由 LLM 现场生成，
    没有任何一处会把该参数写进去，于是这条判据恒停在 warn —— 又是一个"声称可声明、
    实际无从声明"的纸面具备。故这里补上真正的通路：**Stage 0 在 run.params.json
    里写 `min_variants`**，本函数在 CLI 未给时回落到它（与 `verifier_path` 同一范式：
    参数由 Stage 0 解析落盘，阶段侧读回显式使用）。

    取值优先级：CLI `--min-variants` > `run.params.json` 的 `min_variants` > 未声明。

    ⚠️ `run.params.json` 里**写了但读不出来**（非正整数/类型错）时判**错误**而不是
    当作"未声明"：那是调用方**明确想开**这条判据、却没生效 —— 静默降级成 warn 会让
    报告全绿而保护缺席，正是本项目反复出现的失效形态（fail-closed）。
    """
    if args.min_variants > 0:
        return args.min_variants, "--min-variants", None

    p = os.path.join(args.run_dir, "run.params.json")
    try:
        with open(p, encoding="utf-8") as f:
            params = json.load(f)
    except FileNotFoundError:
        return 0, None, None          # 无文件 = 真未声明（VERIFIER 段已单独处理该情形）
    except Exception as e:                                    # noqa: BLE001
        return 0, None, (f"{p} 无法解析（{e!r}）—— 无法确认位点数下限，"
                         f"不按'未声明'静默降级")
    if not isinstance(params, dict) or "min_variants" not in params:
        return 0, None, None          # 键不存在 = 真未声明

    raw = params["min_variants"]
    if isinstance(raw, bool) or not isinstance(raw, int) or raw <= 0:
        return 0, None, (f"{p} 的 min_variants={raw!r} 不是正整数 —— "
                         f"写这个键即表示要开'变异数异常少'判定，却无法生效。"
                         f"请改成正整数（按参考基因组大小/队列规模推算），"
                         f"或删掉该键以明确表示不做此判定")
    return raw, f"{p} 的 min_variants", None


def variant_floor(rep, n_var, label, min_variants, source=None):
    """位点数下限判定。

    **为什么要单独一条**：本模块 docstring 把"变异数异常少"列为要防的头号失效
    （下游不报错，很久以后才以"变异数异常少"的面貌出现）。但早期实现里**没有任何
    带宽判据** —— `min_variants=1` 只拦"完全为空"，于是实测一个 2 样本的全基因组
    队列 VCF 只有 2 条记录时，门禁照样 `✅ 全部检查通过`、rc=0。
    该阶段 skill 自称的用途（抓"变异数异常少"）实际未落地。

    **为什么不给一个绝对默认下限**：合理位点数上下差几个数量级 —— 取决于参考基因组
    大小（人类 3Gb vs 本仓库探针用的 200kb 迷你 ref）、队列大小、测序深度、物种。
    拍一个硬编码数字必然要么拦健康数据（本仓库自己的 smoke 测试用的是迷你 ref），
    要么松到没用。故按本项目既有的 `nir_agreement` 范式：
      · 调用方**显式**声明（`--min-variants N` 或 run.params.json 的 `min_variants`）
        → 这是它声明的验收标准，未达到即 hard
      · 未声明 → **不静默通过**，登记一条 warn 明说"未做位点数下限判定"
        （避免又一处"纸面具备"：报告里不能让人以为已经卡过变异数）
    """
    if min_variants <= 0:
        rep.add(f"{label}.variant_floor", "warn", False,
                f"未声明位点数下限，**未做'变异数异常少'判定**；"
                f"实测 {n_var} 条记录。如需此保护，在 run.params.json 里加 "
                f'"min_variants": N（或调用方传 --min-variants N）—— '
                f"N 按参考基因组大小/队列大小推算（人类全基因组队列通常 ≥ 数百万位点）")
        return
    ok = n_var >= min_variants
    rep.add(f"{label}.variant_floor", "hard", ok,
            f"{n_var} 条记录（下限 {min_variants}"
            + (f"，来自 {source}）" if source else "）") +
            ("" if ok else "；**变异数异常少** —— 上游 GVCF 有问题、或参考基因组选错"
                           "（物种/版本/contig 命名）；下游不报错，只会得到一份"
                           "几乎无变异的 VCF"))


# ============================ 各阶段判据 ============================

@stage("00_fastp_qc")
def verify_fastp(args, rep):
    """Stage 0.5：fastp 清洗后的 FASTQ 与 JSON 报告。"""
    for s in args.samples:
        for suffix in (".clean_1.fq.gz", ".clean_2.fq.gz"):
            p = os.path.join(args.run_dir, "out", f"{s}{suffix}")
            ok = have(p)
            rep.add(f"clean[{s}]{suffix}", "hard", ok,
                    os.path.basename(p) if ok else f"缺失或为空: {p}")
            # 存在且非空 ≠ 完整。fastp 写到一半被 SIGKILL / 磁盘满 / NFS 抖动，
            # 会留下一个能通过 have() 的**截断 gzip**：BWA 读到中途才报错。
            # fastp 的 JSON 报告对此不构成交叉验证 —— 那是 fastp"打算"写多少条，
            # 写盘失败时它照样完整（实测：截断的 .fq.gz + 完好的 JSON 声称 900000 reads，
            # 本校验器判 PASS，而 `gzip -t` 返回 rc=1 `unexpected end of file`）。
            # 仓库自己的错误预防清单 #7 就写着"启动前需批量 gzip -t 校验"。
            if ok:
                gz = tool("gzip")
                if not gz:
                    rep.need_tool("gzip", f"校验 {os.path.basename(p)} 的 gzip 完整性")
                else:
                    rc, _, err = run([gz, "-t", p], timeout=900)
                    rep.add(f"clean[{s}]{suffix}.gzip_ok", "hard", rc == 0,
                            "gzip 流完整" if rc == 0 else
                            f"**gzip 流损坏/被截断**（{err.strip()[:120]}）—— "
                            f"fastp 写盘中断，须重跑本阶段；BWA 会在读到中途才失败")
        j = os.path.join(args.run_dir, "out", f"{s}.fastp.json")
        if not have(j):
            rep.add(f"report[{s}]", "warn", False, f"缺 fastp JSON 报告: {j}")
            continue
        try:
            r = json.load(open(j))
        except Exception as e:                                # noqa: BLE001
            rep.add(f"report[{s}]", "hard", False, f"fastp JSON 无法解析: {e}")
            continue
        af = (r.get("summary") or {}).get("after_filtering") or {}
        reads = af.get("total_reads")
        bf = (r.get("summary") or {}).get("before_filtering") or {}
        reads_before = bf.get("total_reads")
        if not reads:
            rep.add(f"report[{s}].reads", "hard", False,
                    f"after_filtering.total_reads={reads}（清洗后无 read）")
            continue
        rep.add(f"report[{s}].reads", "hard", True, f"清洗后 {reads} reads")
        if reads_before:
            keep = reads / reads_before
            rep.add(f"report[{s}].keep_rate", "warn", keep >= 0.8,
                    f"保留率 {keep:.1%}（阈值 ≥80%）" +
                    ("" if keep >= 0.8 else "；过滤过猛，可能丢真实信号"))


@stage("01_bwa_sort")
def verify_align(args, rep):
    """Stage 1：BWA 比对 + 排序。核心判据是「BAM 非空且样本齐」。"""
    totals = check_bam_cohort(
        rep, args.samples,
        lambda s: os.path.join(args.run_dir, "out", f"{s}.sorted.bam"),
        min_reads=1, expect_rg=True, label="bam")
    # 记进报告 → 供 Stage 2 的 --prev-reads-file 做守恒比较
    rep.read_totals.update(totals)
    if totals:
        rep.add("read_totals_recorded", "hard", True,
                f"已记录 {len(totals)} 个样本的 reads 数（供下一阶段守恒比较）")


@stage("02_markdup_bqsr")
def verify_markdup(args, rep):
    """Stage 2：MarkDuplicates + BQSR。判据含「BQSR 后 reads 与 Stage 1 守恒」。"""
    cohorts = {
        "markdup": "{s}.sorted.markdup.bam",
        "bqsr": "{s}.sorted.markdup.BQSR.bam",
    }
    # ── 为何 BQSR BAM 是**硬性**判据，而 markdup BAM 只告警 ──
    # Stage 3 的输入契约写死了 `{sample}.sorted.markdup.BQSR.bam`（见
    # wgs-express-haplotypecaller/SKILL.md 的输入清单），所以 BQSR BAM 缺一个都不行。
    # 而 markdup BAM 在 parabricks 路径下可能不单独产出（fq2bam 一步完成
    # markdup+BQSR），属**预期**缺失 → 只告警。
    #
    # ⚠️ 早期版本对两组用同一个 `if not got: continue`，于是在"每个样本都有 markdup
    #    BAM、但一个 BQSR BAM 都没有"时：bqsr 轮次 `got` 为空 → 直接 continue，
    #    而队列级的兜底 `if args.samples and not totals` 又因为 totals 已被 markdup
    #    轮次填满而跳过 —— **整个 stage 2 只做了 markdup 也算通过**（实测 rc=0）。
    #    为 parabricks 放宽的那一处，顺带把"这一步根本没做 BQSR"也吞掉了。
    totals = {}
    bqsr_got = {}
    for tag, pat in cohorts.items():
        got = {}
        for s in args.samples:
            p = os.path.join(args.run_dir, "out", pat.format(s=s))
            if have(p):
                got[s] = p
        if tag == "bqsr":
            bqsr_got = got
        # 逐样本点名缺了谁 —— 不能只在 `got` 内部遍历：没有产出的样本根本不会
        # 进入 got，整份丢失（而非数值异常）就永远不会被发现。Stage 1 的
        # check_bam_cohort 有 cohort_sample_set 兜底，Stage 2 此前没有对应判据。
        missing = [s for s in args.samples if s not in got]
        if tag == "bqsr" and missing:
            rep.add("bqsr.coverage", "hard", False,
                    f"仅 {len(got)}/{len(args.samples)} 个样本有 BQSR BAM；"
                    f"缺: {missing[:8]} —— Stage 3 的输入契约要求**每个样本**都有 "
                    f"{{sample}}.sorted.markdup.BQSR.bam"
                    + ("（parabricks 下亦然：fq2bam 的产出契约是 .sorted.bam，"
                       "BQSR BAM 仍由本阶段产出）"
                       if len(missing) == len(args.samples) else ""))
        if not got:
            continue
        cohorts_present = len(got)
        if tag == "markdup" and cohorts_present < len(args.samples):
            rep.add("markdup.coverage", "warn", False,
                    f"仅 {cohorts_present}/{len(args.samples)} 个样本有 markdup BAM"
                    "（parabricks fq2bam 路径可能已跳过，属预期）")
        for s, p in got.items():
            # 索引分级不同，理由见 check_bam 的 index_level 说明：
            #   · BQSR BAM → Stage 3 的 HaplotypeCaller 无索引直接 rc=2（实测），必须 hard
            #   · markdup BAM → 只是一份中间产物，BaseRecalibrator/ApplyBQSR 读它不需要
            #     索引；且 `gatk MarkDuplicates` 的 CREATE_INDEX **默认 false**，文档里的
            #     标准命令本就不产出索引 —— 判 hard 会稳定拦下健康数据（实测复现）
            t = check_bam(rep, p, f"{tag}[{s}]", min_reads=1,
                          expect_rg=None, expect_sorted=True,
                          index_level="hard" if tag == "bqsr" else "warn")
            if t is not None:
                totals[s] = t
    if args.samples and not totals:
        rep.add("bqsr_output", "hard", False,
                "没有任何 BQSR/markdup BAM 产出 —— Stage 2 实际未完成")
    rep.read_totals.update(totals)
    # 与 Stage 1 守恒（reads 数不应因 markdup/BQSR 显著变化）
    #
    # ⚠️ 三种"没做成"的情形必须各自说清，不能一律静默跳过 —— 否则这条检查会
    #    看起来存在、实际从不生效（本项目称之为"纸面具备"）。
    if not args.prev_reads_file:
        rep.add("reads_conservation", "warn", False,
                "未提供 --prev-reads-file，跳过与 Stage 1 的 reads 守恒检查"
                "（阶段脚本应传 ${RUN_DIR}/.verify/01_bwa_sort.json）")
    elif not os.path.isfile(args.prev_reads_file):
        rep.add("reads_conservation", "warn", False,
                f"前一阶段报告不存在: {args.prev_reads_file} —— 守恒检查无从比较"
                "（Stage 1 是否跑过校验？）")
    else:
        try:
            prev = json.load(open(args.prev_reads_file))
            prev_totals = prev.get("read_totals") or {}
            if not prev_totals:
                rep.add("reads_conservation", "warn", False,
                        f"{os.path.basename(args.prev_reads_file)} 里没有 read_totals 记录"
                        "（该报告由旧版校验器产出？）—— 守恒检查无从比较")
            else:
                bad = []
                # ⚠️ 守恒检查必须**双向**看：
                #   ① totals 里的样本 reads 数变了（数值异常）
                #   ② 上一阶段有、本阶段没了（整份丢失）
                # 早期版本只做 ①，因为循环写的是 `for s, t in totals.items()` ——
                # 而 totals 只含**磁盘上尚存**的样本，丢掉的样本根本不进这个集合，
                # 于是"样本在中间被丢掉"（本次改动最重的防守对象）在 Stage 2 完全放行。
                gone = [s for s in prev_totals if s not in totals]
                for s, t in totals.items():
                    pt = prev_totals.get(s)
                    if pt and not (0.9 * pt <= t <= 1.1 * pt):
                        bad.append(f"{s}({t} vs {pt})")
                if gone:
                    rep.add("reads_conservation_lost", "hard", False,
                            f"{len(gone)} 个样本在 Stage 1 有产出、Stage 2 没有了："
                            f"{gone[:8]} —— 样本整份丢失（非数值异常）")
                rep.add("reads_conservation", "warn", not bad,
                        f"{len(totals)} 个样本与上一阶段 reads 数相符（±10%）" +
                        (f"；偏差: {bad[:5]}" if bad else ""))
        except Exception as e:                                # noqa: BLE001
            rep.add("reads_conservation", "warn", False, f"前阶段记录无法解析: {e}")

    # BQSR 校正表是否存在：它不直接进入下游，但**缺失说明 BQSR 没真跑**
    # （ApplyBQSR 会因缺 --bqsr-recal-file 而失败，或脚本静默跳过了 BQSR 步骤）
    if totals:
        no_table = [s for s in totals
                    if not have(os.path.join(args.run_dir, "out", f"{s}.recal.table"))]
        if no_table and len(no_table) == len(totals):
            # 全部缺失 → 该阶段很可能没做 BQSR（parabricks 路径可能一步完成，故为 warn）
            rep.add("recal_table", "warn", False,
                    f"没有 {len(no_table)} 个样本的 .recal.table —— BQSR 可能未执行"
                    "（parabricks bqsr 一步完成时不产出此文件，属预期；"
                    "GATK 路径下缺失说明 BQSR 被跳过）")

    # 重复率（仅当指标文件存在；该文件名 skill 未固定，故为可选）
    for s in args.samples:
        for cand in (f"{s}.markdup.metrics", f"{s}.metrics.txt",
                     f"{s}.sorted.markdup.metrics"):
            p = os.path.join(args.run_dir, "out", cand)
            if have(p):
                try:
                    d, why = parse_percent_duplication(open(p).read())
                    if d is None:
                        # 解析不出来就**明说**，不能静默 —— 早期版本此处 except: pass，
                        # 于是"指标文件在、重复率却没被检查"这件事在报告里看不出来。
                        rep.add(f"dup_rate[{s}]", "warn", False,
                                f"{os.path.basename(p)} 里没取到 PERCENT_DUPLICATION"
                                f"（{why}）—— **未做重复率判定**")
                    else:
                        rep.add(f"dup_rate[{s}]", "warn", d <= 0.40,
                                f"重复率 {d:.1%}"
                                + ("" if d <= 0.40 else "；异常偏高，查文库/比对"))
                except Exception as e:                        # noqa: BLE001
                    rep.add(f"dup_rate[{s}]", "warn", False,
                            f"重复率无法解析: {e}")
                break


@stage("03_haplotypecaller")
def verify_hc(args, rep):
    """Stage 3：HaplotypeCaller GVCF。核心判据是「是 GVCF 而非普通 VCF」。"""
    n_ok = 0
    for s in args.samples:
        g = os.path.join(args.run_dir, "out", f"{s}.g.vcf.gz")
        # ⚠️ 必须把文件名里的样本名与该 GVCF **内部** #CHROM 行的样本列对照。
        #    只按文件名找文件、只数记录条数，会放行"文件叫 s1.g.vcf.gz、里面装的却是
        #    别的样本"这种情形（resume 存在性检查跳过了上一个 run 的残留 GVCF、
        #    或拷贝/改名步骤错配）。Stage 4 会把这些错的基因型按对的标签联合起来，
        #    得到一份样本标签全对、基因型全错的 VCF —— 下游任何检查都发现不了。
        #    实测：两份 GVCF 头部都声明 WRONGSAMPLE，`[PASS] gvcf_coverage: 2/2`、rc=0。
        n, smps = check_vcf(rep, g, f"gvcf[{s}]", expect_index=True,
                            expect_gvcf=True, min_variants=1,
                            expect_samples=[s] if args.samples else None)
        # 位点数下限：这里是最早能发现"变异数异常少"的地方（还没联合分型）。
        # 单个样本的 GVCF 位点数天然低于联合 VCF（联合会把队列里的位点并起来），
        # 故同一 --min-variants 用在这里偏保守 —— 这是刻意的：宁可在此处告警，
        # 也别等到最后一棒的联合 VCF 才发现整批样本都没出变异。
        if n is not None:
            variant_floor(rep, n, f"gvcf[{s}]", args.min_variants,
                          args.min_variants_source)
        if smps is not None and s not in smps:
            # check_vcf 的 {label}.samples 已判 hard 失败，这条给出**可读的归因**：
            # 文件名与内容不一致 = 样本串了，而不是"少了一个样本"
            rep.add(f"gvcf[{s}].identity", "hard", False,
                    f"{os.path.basename(g)} 内部声明的样本是 {smps[:4]}，"
                    f"与文件名 {s!r} 不符 —— **样本串了**。"
                    f"查 resume 的存在性检查是否跳过了上一个 run 的残留 GVCF、"
                    f"或拷贝/改名步骤错配")
        if n:
            n_ok += 1
    if args.samples:
        rep.add("gvcf_coverage", "hard", n_ok == len(args.samples),
                f"{n_ok}/{len(args.samples)} 个样本产出可用 GVCF"
                + ("" if n_ok == len(args.samples) else "；Stage 4 需要全部样本的 GVCF"))


@stage("04_joint_genotyping")
def verify_joint(args, rep):
    """Stage 4：联合分型。核心判据是「VCF 样本数 == 输入 GVCF 数」。"""
    if not args.prefix:
        rep.add("prefix", "hard", False, "未提供 --prefix，无法定位输出 VCF")
        return
    combined = os.path.join(args.run_dir, "out", f"{args.prefix}.combined.g.vcf.gz")
    if have(combined):
        nc, _ = check_vcf(rep, combined, "combined", expect_index=True,
                          expect_gvcf=True, min_variants=1)
        if nc is not None:
            variant_floor(rep, nc, "combined", args.min_variants,
                          args.min_variants_source)
    else:
        # ⚠️ 早期版本这里**只有 if 没有 else**：`combined.g.vcf.gz` 缺失时静默通过，
        #    报告里连一行都不提。实测（只放一个 co.vcf.gz、不造 combined）：
        #    报告 4 条 PASS + 1 条 warn、`grep -c combined` = 0、rc=0。
        #    为什么这不能算"文件可选"：本 skill 的输出契约里**列明**该文件是产物之一，
        #    错误预防清单第 6 条还专门说 `--in-vcf` 必须是它；resume 的存在性检查也是
        #    `if [ -f "${COMBINED_GVCF}" ]; then skip` —— 文件不在就会**重跑一次合并**，
        #    而本阶段已经写了 .done 决定不再重跑，于是下一阶段拿不到输入。
        #
        #    ⚠️ 唯一一条合法缺席的路径是 `GenomicsDBImport` 取代 `CombineGVCFs`
        #    （本 skill 对 >200 样本提到过两次，但**没有**给工作区命名、没有模板、
        #    resume 逻辑也没实现）。若将来真实现它，产物是工作区目录而非单个 GVCF，
        #    此处必须能识别出来，否则这道 hard 会拦下健康数据（那是更难察觉的方向）。
        #    现在先按"见到工作区就降级为 warn"处理，并在 warning 里点明exemption 位置。
        import glob as _glob
        db_ws = sorted(_glob.glob(os.path.join(args.run_dir, "out",
                                               f"{args.prefix}*genomicsdb*")))
        db_ws = [d for d in db_ws if os.path.isdir(d)]
        exists = os.path.exists(combined)
        if db_ws:
            rep.add("combined.exists", "warn", False,
                    f"没有 {os.path.basename(combined)}，但发现 GenomicsDB 工作区 "
                    f"{[os.path.basename(d) for d in db_ws[:3]]} —— 该路径合法地用 "
                    f"GenomicsDBImport 取代了 CombineGVCFs，故**未做**「合并 GVCF 存在且"
                    f"是真 GVCF」判定（工作区形态不同，此处没有对应判据）")
        else:
            rep.add("combined.exists", "hard", False,
                    (f"{os.path.basename(combined)} 存在但为空（0 字节）—— "
                     f"CombineGVCFs 中途失败？" if exists else
                     f"缺少 {os.path.basename(combined)} —— CombineGVCFs 没跑或产物被删。"
                     f"resume 的存在性检查会因它不在而重跑合并，但本阶段已写 .done、"
                     f"不会重跑；下游排查请从 Stage 4 的第一个命令看起")
                    + f"（路径: {combined}）")
    vcf = os.path.join(args.run_dir, "out", f"{args.prefix}.vcf.gz")
    n, smps = check_vcf(rep, vcf, "joint", expect_index=True,
                        expect_gvcf=False, min_variants=1,
                        expect_samples=args.samples if args.samples else None)
    # ⚠️ 这里**不再**重复登记 `joint.nonempty`：它与 check_vcf 已经登记的
    #    `joint.variants` 判的是同一个 `n`、同一个阈值、同一套补救措施，
    #    属于纯冗余 —— 报告里多一行会让读报告的人以为有"两道独立的位点守卫"。
    #    真正缺的是**位点数下限**（"变异数异常少"），改由 variant_floor 承担。
    if n is not None:
        variant_floor(rep, n, "joint", args.min_variants,
                      args.min_variants_source)


@stage("05_filtering")
def verify_filtering(args, rep):
    """Stage 5：硬过滤。核心判据是「SNP/INDEL 都非空 + Ti/Tv 合理 + 保留率不过低」。"""
    if not args.prefix:
        rep.add("prefix", "hard", False, "未提供 --prefix，无法定位输出 VCF")
        return
    bcf = tool("bcftools")
    for tag in ("snp", "indel"):
        v = os.path.join(args.run_dir, "out", f"{args.prefix}.{tag}.filter.vcf.gz")
        # ⚠️ 必须传 expect_samples：Stage 5 是**最后一棒**，下游没有任何阶段会再核样本集。
        #    早期版本漏传，样本列被换掉/丢一半的 VCF 照样 rc=0 —— 而本 skill 的文档
        #    当时已写着"产出样本数与上游一致"（纸面具备）。实测：VCF 里只有
        #    TOTALLY_OTHER_SAMPLE、传 --samples S1,S2，仍旧"✅ 全部检查通过"。
        n, _ = check_vcf(rep, v, tag, expect_index=True,
                         expect_gvcf=False, min_variants=1,
                         expect_samples=args.samples if args.samples else None)
        # 位点数下限（"变异数异常少"）—— Stage 5 是最后一棒，此处不判就没机会了。
        # SNP 与 INDEL 各自的期望量级差一个数量级（人类 ~4-5M vs ~0.5-1M），
        # 故下限只按调用方声明的单一值判，不在这里臆造两套阈值。
        if n is not None:
            variant_floor(rep, n, tag, args.min_variants, args.min_variants_source)
        if not n or not bcf:
            continue

        # PASS 率 —— 对 SNP 与 INDEL **都要**检查。
        # 只看 SNP 会漏掉「INDEL 阈值过严导致全被滤掉」这种情况（真实且常见：
        # INDEL 的 FS/MQRankSum 阈值比 SNP 严得多）。
        rc, out, _ = run([bcf, "query", "-f", "%FILTER\n", v], timeout=900)
        if rc == 0:
            fl = [x for x in out.split("\n") if x]
            if fl:
                # ⚠️ `FILTER="."` 是 VCF 的**缺失值**记号，意思是"没有记录过滤判定"——
                #    正是 VariantFiltration 没跑或表达式没生效的典型症状。把它算作
                #    PASS 会让整批未过滤的变异看起来"保留率 100%"。实测：把每个位点
                #    的 FILTER 改写成 "."，报告 `[PASS] pass_rate: PASS 3/3（100.0%）`
                #    且 rc=0，而下游 GWAS 拿到的是从未按 QD/FS/SOR/MQ 过滤过的位点。
                npass = sum(1 for x in fl if x == "PASS")
                n_missing = sum(1 for x in fl if x == ".")
                rep.add(f"{tag}.pass_rate", "hard", npass > 0,
                        f"PASS {npass}/{len(fl)}（{npass/len(fl):.1%}）" +
                        (f"；另有 {n_missing} 条 FILTER 为 '.'（未记录判定）"
                         if n_missing else "") +
                        ("" if npass > 0 else
                         "；**全部位点被过滤掉** —— 阈值过严或上游质量异常"))
                if n_missing:
                    # 单独一条 warn 级：诚实地区分"过滤后都留下了"与"压根没过滤"
                    rep.add(f"{tag}.filter_applied", "warn", False,
                            f"{n_missing}/{len(fl)} 条位点的 FILTER 是 '.'（缺失值）——"
                            f"VariantFiltration 可能没跑，或表达式未生效；"
                            f"这些位点未经过 QD/FS/SOR/MQ 过滤")
                rep.add(f"{tag}.pass_ratio", "warn", npass / len(fl) >= 0.5,
                        f"保留率 {npass/len(fl):.1%}（告警阈值 ≥50%）" +
                        ("" if npass / len(fl) >= 0.5 else
                         "；保留率偏低，先看过滤前的指标分布再定阈值"))

        if tag == "snp":
            # Ti/Tv：WGS SNP 应约 2.0-2.1；偏离说明过度过滤或混入假阳性。
            # ⚠️ bcftools stats 的 TSTV 行格式是：
            #     TSTV <id> <ts_count> <tv_count> <ts/tv_ratio> <ts_alt> <tv_alt> <ratio_alt>
            #     第 5 个字段才是比值。早期版本误取第 4 个（=tv 计数，如 20 → 报成 20.00），
            #     靠真实 bcftools 输出才发现。
            rc, out, _ = run([bcf, "stats", v], timeout=600)
            m = re.search(r"^TSTV\t\S+\t(\d+)\t(\d+)\t([\d.]+)", out, re.M)
            if m:
                ts, tv, titv = int(m.group(1)), int(m.group(2)), float(m.group(3))
                rep.add("snp.titv", "warn", 1.8 <= titv <= 2.3,
                        f"Ti/Tv={titv:.2f}（ts={ts} tv={tv}；WGS 预期 ~2.0-2.1，"
                        f"接受 1.8-2.3）" +
                        ("" if 1.8 <= titv <= 2.3 else
                         "；偏离提示过度过滤或质量异常"))
            else:
                rep.add("snp.titv", "warn", False, "bcftools stats 未给出 TSTV 行")


# ============================ 主流程 ============================

def main():
    ap = argparse.ArgumentParser(
        description="WGS 管线阶段结果校验器（写 .done 前的门禁）")
    ap.add_argument("--stage", required=True,
                    help="阶段名（= 脚本名去掉 .sh），如 01_bwa_sort")
    ap.add_argument("--run-dir", required=True, help="RUN_DIR（含 out/ 目录）")
    ap.add_argument("--samples", default="",
                    help="逗号分隔的 sample_name 列表（来自 run.params.json）")
    ap.add_argument("--prefix", default="", help="cohort 级阶段（4/5）的输出前缀")
    ap.add_argument("--prev-reads-file", default="",
                    help="上一阶段的 .verify/<stage>.json（用于 reads 守恒比较，"
                         "如 ${RUN_DIR}/.verify/01_bwa_sort.json）")
    ap.add_argument("--min-variants", type=int, default=0,
                    help="位点数下限（0 = 未声明；可改由 run.params.json 的 min_variants "
                         "声明）。本模块 docstring 把「变异数异常少」列为要防的头号失效，"
                         "但**没有**绝对下限可套：合理位点数取决于参考基因组大小、队列大小、"
                         "测序深度、物种。故此处只在调用方显式声明时判 hard；未声明时登记"
                         "一条 warn 说明未做此判定")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出到 stdout")
    ap.add_argument("--quiet", action="store_true", help="只在失败时输出")
    args = ap.parse_args()

    args.samples = parse_samples(args.samples)
    rep = Report(args.stage, args.run_dir)

    # 空样本名单 = 整套样本守恒判据从未运行。这是 fail-open 的旁路，必须堵死：
    # 各阶段的循环都写成 `for s in args.samples`，名单为空时循环零次、且
    # `if args.samples:` 的队列级判据也全部跳过 —— 于是报告是"✅ 全部检查通过"，
    # 而实际**一条判据都没跑**。空 RUN_DIR 也能拿到 rc=0（实测过）。
    # 触发路径不需要操作失误：bash 4.2 下 `"${sample_names[*]}"` 在 `set -u` 且数组
    # 为空时报 unbound variable，最"稳妥"的绕法就是传空串。
    # 同文件里 need_tool / 报告写盘都选了 fail-closed，此处对齐同一约定。
    if args.stage in STAGES_NEEDING_SAMPLES and not args.samples:
        rep.add("samples_given", "hard", False,
                "未收到样本名单（--samples 为空）—— 样本守恒判据从未运行，"
                "不能判为通过。阶段脚本应传 "
                '--samples "$(IFS=,; echo "${sample_names[*]}")"；'
                "若样本数组确为空，那是 run.params.json 的 sample_names 有问题，"
                "应先修它再跑管线")

    # 位点数下限：CLI > run.params.json > 未声明（见 _resolve_min_variants）
    args.min_variants, args.min_variants_source, _mv_err = _resolve_min_variants(args)
    if _mv_err and args.stage in STAGES_NEEDING_VARIANT_FLOOR:
        # 调用方**明确想开**这条判据却没生效 —— 判失败而非静默降级（fail-closed）。
        # 只在本阶段真的会用到该下限时才拦（见 STAGES_NEEDING_VARIANT_FLOOR）。
        rep.add("min_variants_declared", "hard", False,
                f"{_mv_err}；本条判据未能生效，不放行")

    fn = _STAGES.get(args.stage)
    if fn is None:
        rep.add("stage_known", "hard", False,
                f"未知阶段 {args.stage!r}；已注册: {sorted(_STAGES)}")
    else:
        try:
            fn(args, rep)
        except Exception as e:                                # noqa: BLE001
            # 校验器自身出错 → 判失败而不是假装通过（fail-closed，
            # 与"校验没跑"区分开：这种失败会明确写出原因）
            rep.add("verifier_internal", "hard", False,
                    f"校验器内部异常（该阶段结果**未经校验**，不放行）: {e!r}")

    # 落盘机器可读结果。
    # ⚠️ 写盘失败要**判失败**，不能只告警：`.verify/<stage>.json` 不只是给人看的报告，
    # 它还是**下一阶段做 reads 守恒比较的输入**（`--prev-reads-file`）。报告缺失时
    # 下游的守恒检查会静默失效（"声称存在、从不生效"）。缺了机制的一环即不放行。
    vdir = os.path.join(args.run_dir, ".verify")
    try:
        os.makedirs(vdir, exist_ok=True)
        with open(os.path.join(vdir, f"{args.stage}.json"), "w",
                  encoding="utf-8") as f:
            json.dump(rep.as_dict(), f, ensure_ascii=False, indent=2)
    except Exception as e:                                    # noqa: BLE001
        rep.add("report_persisted", "hard", False,
                f"校验报告无法写入 {vdir}: {e} —— 下一阶段的 reads 守恒检查"
                f"依赖它，缺了就不该放行（检查 RUN_DIR 的写权限）")

    if args.json:
        print(json.dumps(rep.as_dict(), ensure_ascii=False, indent=2))
    elif not (args.quiet and not rep.blocked):
        print(rep.render())

    return 1 if rep.blocked else 0


if __name__ == "__main__":
    sys.exit(main())
