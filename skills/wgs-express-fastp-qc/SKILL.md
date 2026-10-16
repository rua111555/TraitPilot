---
name: wgs-express-fastp-qc
description: >
  Run all-in-one FASTQ preprocessing with fastp — adapter trimming via
  paired-end overlap analysis, quality/length filtering, 2-color poly-G
  removal, base correction, optional dedup/UMI/merge, and HTML/JSON reports.
  Called by the orchestration skill (wgs-express-pipeline) as Stage 0.5
  before alignment. Can also be used independently for FASTQ QC.
allowed-tools:
  - Read
  - Write
  - Bash
  - Grep
---

# fastp QC — Stage 0.5（可选）

使用 fastp 进行 FASTQ 质控：去接头、质量过滤、poly-G 去除，一条命令完成
原本需要 Cutadapt + Trimmomatic + FastQC 三个工具的流程。

**由编排 skill 调用**，也可独立使用。

**关键词**：`fastp`、`FASTQ 预处理`、`质控`、`去接头`、`adapter trimming`

## 输入

| 来源 | 说明 |
|------|------|
| `run.params.json` | 样本列表、输出目录等 |
| 数据库 `fastq_samples` 表 | 原始 FASTQ 文件路径（`read1_path` / `read2_path`） |

## 输出

每个样本在 `{RUN_DIR}/out/` 下生成：
| 文件 | 说明 |
|------|------|
| `{sample_name}.clean_1.fq.gz` | 清洗后 R1 |
| `{sample_name}.clean_2.fq.gz` | 清洗后 R2 |
| `{sample_name}.fastp.html` | HTML 可视化报告 |
| `{sample_name}.fastp.json` | JSON 报告（供 MultiQC 聚合） |

> 清洗后的 FASTQ 直接作为 Stage 1（BWA）的输入，不写回数据库。
> 数据库中 `fastq_samples` 表的路径始终指向原始 FASTQ 文件。

## 核心原理

1. **双端接头通过 OVERLAP ANALYSIS 自动检测** — R1 与 R2 反向互补比对，
   找到插入片段重叠区域，切除延伸超出部分。无需 adapter 序列即可切到单碱基级别。
   同一机制驱动 `--correction`：用高质量端修正低质量端的碱基。

2. **`--dedup` 是基于 SEQUENCE 身份的 FASTQ 级别去重** — 无法区分 PCR 重复与
   高表达转录本。**WGS 流程禁止使用**，应在比对后用 `MarkDuplicates` 做坐标去重。

3. **Poly-G 剪切对双色仪器（NextSeq/NovaSeq）自动开启** — G 是无信号碱基，
   建议保留开启。

## 工具定位

| 场景 | 使用 fastp? | 替代方案 |
|------|-----------|---------|
| 批量 PE WGS/WES 预处理 | ✅ 默认 | — |
| Small-RNA 3' adapter + 严格长度筛选 | ❌ | cutadapt |
| UMI 分子计数 / ctDNA 一致性序列 | 仅提取 UMI | umi_tools / fgbio |
| 比对后去重 | ❌ | GATK MarkDuplicates（Stage 2） |

## 核心命令

```bash
# ─── 标准 Illumina PE（WGS 默认） ───
fastp -i raw_R1.fq.gz -I raw_R2.fq.gz \
      -o clean_R1.fq.gz -O clean_R2.fq.gz \
      --detect_adapter_for_pe \
      --cut_right --cut_window_size 4 --cut_mean_quality 20 \
      -q 20 -l 36 -w 8 \
      -h sample.html -j sample.json

# ─── NovaSeq / NextSeq（2色，加 poly-G） ───
fastp -i raw_R1.fq.gz -I raw_R2.fq.gz \
      -o clean_R1.fq.gz -O clean_R2.fq.gz \
      --detect_adapter_for_pe --trim_poly_g \
      --cut_right --cut_window_size 4 --cut_mean_quality 20 \
      -q 20 -l 36 -w 8 \
      -h sample.html -j sample.json

# ─── 重叠碱基校正（PE only，可选） ───
fastp -i raw_R1.fq.gz -I raw_R2.fq.gz \
      -o clean_R1.fq.gz -O clean_R2.fq.gz --correction
```

## 脚本模板

### SESSION-SAFE 样板

```bash
#!/bin/bash
set -euo pipefail

# ============================================
# 配置
# ============================================
RUN_DIR="..."           # 从 run.params.json 读取
LOG_FILE="${RUN_DIR}/logs/00_fastp_qc.log"
DONE_FILE="${RUN_DIR}/.done/00_fastp_qc.done"
PID_FILE="${RUN_DIR}/.pid/00_fastp_qc.pid"

mkdir -p "${RUN_DIR}/logs" "${RUN_DIR}/.done" "${RUN_DIR}/.pid" "${RUN_DIR}/out"
echo $$ > "${PID_FILE}"
trap 'rm -f "${PID_FILE}"' EXIT

# ─── 日志函数 ───
log_info()  { echo "[$(date '+%Y-%m-%d %H:%M:%S')] [INFO]  $*"; }
log_error() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] [ERROR] $*"; }
log_done()  { echo "[$(date '+%Y-%m-%d %H:%M:%S')] [DONE]  $*"; }
```

### 工具路径（运行时探测，勿写死本机路径）

```bash
FASTQ_DIR="${FASTQ_DIR:-$(sk_geno_dir)/fastq}"   # 优先用 run.params.json 的 fastq_dir；否则 PIG_GENO_DIR/fastq
OUT_DIR="${RUN_DIR}/out"

FASTP="$(command -v fastp || { echo "✗ 找不到 fastp —— 请激活 conda 环境 Agent" >&2; exit 1; })"
```

### 逐样本处理

```bash
# ─── 从数据库获取样本信息 ───
# samples 由编排层从 --samples 参数传入
# 实际运行时从 run.params.json 读取 sample_numbers 和 sample_names

THREADS=8   # 从 run.params.json 的 parallelism 读取

for i in "${!sample_numbers[@]}"; do
    sn="${sample_numbers[$i]}"
    name="${sample_names[$i]}"

    R1="${FASTQ_DIR}/${sn}/${sn}_1.fq.gz"
    R2="${FASTQ_DIR}/${sn}/${sn}_2.fq.gz"
    O1="${OUT_DIR}/${name}.clean_1.fq.gz"
    O2="${OUT_DIR}/${name}.clean_2.fq.gz"
    HTML="${OUT_DIR}/${name}.fastp.html"
    JSON="${OUT_DIR}/${name}.fastp.json"

    # resume 检查
    if [ -f "${O1}" ] && [ -f "${O2}" ]; then
        log_info "Clean FASTQ exists, skipping sample ${name}: ${O1}"
        continue
    fi

    log_info "Processing sample ${name} (${sn})..."

    ${FASTP} \
        -i "${R1}" -I "${R2}" \
        -o "${O1}" -O "${O2}" \
        --detect_adapter_for_pe \
        --cut_right --cut_window_size 4 --cut_mean_quality 20 \
        -q 20 -l 36 \
        -w "${THREADS}" \
        -h "${HTML}" -j "${JSON}"

    log_done "Sample ${name} done"
done

# ─── 完成标记 ───
date > "${DONE_FILE}"
log_done "Stage 0.5 (fastp QC) complete"
```

## JSON 报告摘要

```python
import json

with open('sample.fastp.json') as f:
    r = json.load(f)

after = r['summary']['after_filtering']
print(f"reads: {after['total_reads']}, Q30: {after['q30_rate']:.2%}")
print(f"dup rate: {r['duplication']['rate']:.2%}")  # 诊断用
```

## ⚠️ 错误预防清单

| # | 检查项 | 违反后果 |
|---|--------|---------|
| 1 | **WGS 流程禁止 `--dedup`** — fastp 序列去重无法区分生物/技术重复 | 变异检测假阴性 |
| 2 | **RNA-seq 禁止 `--dedup`** — 同上的理由 | 表达量严重偏低 |
| 3 | **单端数据必须提供 `--adapter_sequence`** — SE 无 overlap | adapter 残留 |
| 4 | **PE 双端文件顺序正确: `-i R1 -I R2`** | 接头检测和 read 命名错误 |
| 5 | **多色平台显式指定 `--trim_poly_g`** — 自动检测可能漏掉 | poly-G 残留 |
| 6 | **清洗后文件不入数据库** — DB 始终存原始 FASTQ 路径 | 原始数据引用丢失 |

## 脚本安全检查清单（生成脚本前逐条检查）

1. ✅ 不使用 `--dedup`（WGS 流程禁用）
2. ✅ PE 数据已加 `--detect_adapter_for_pe`
3. ✅ 根据平台决定是否加 `--trim_poly_g`
4. ✅ 线程数合理（不超服务器核数）
5. ✅ HTML/JSON 报告已开启（-h -j）
6. ✅ SESSION-SAFE 保护（`.done` + `.pid`）
7. ✅ 完成标记写入
8. ✅ resume 模式支持（输出文件存在性检查）
9. ✅ 清洗后输出命名与下游 Stage 1 期望的 BWA 输入一致

## 产出汇报

```
✅ fastp QC 完成
样本:   {SAMPLE_COUNT} 个处理
清洗率: {SAMPLE_A}: 180万→175万 (97.2%), Q30: 94.5%
        {SAMPLE_B}: 200万→192万 (96.0%), Q30: 93.8%
报告:   {RUN_DIR}/out/*.fastp.html
聚合:   multiqc {RUN_DIR}/out/ -o multiqc_report/
```
