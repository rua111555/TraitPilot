---
name: wgs-express-markdup-bqsr
description: >
  Mark PCR and optical duplicates with GATK MarkDuplicates, then perform
  Base Quality Score Recalibration (BQSR) with BaseRecalibrator and ApplyBQSR.
  Per-sample processing. Called by the orchestration skill (wgs-express-pipeline)
  during Stage 2.
allowed-tools:
  - Read
  - Write
  - Bash
  - Grep
---

# MarkDuplicates + BQSR — Stage 2

标记 PCR/光学重复（MarkDuplicates）并进行碱基质量校正（BaseRecalibrator + ApplyBQSR）。

**由编排 skill 调用**，不直接暴露给用户。

## 输入

- `run.params.json` — 全部参数
- `{RUN_DIR}/out/{sample_name}.sorted.bam` — Stage 1 生成的 BAM（带有 `@RG` 标签）

## 输出

每个样本输出：
- `{RUN_DIR}/out/{sample_name}.sorted.markdup.bam` — 标记重复后的 BAM
- `{RUN_DIR}/out/{sample_name}.sorted.markdup.bam.bai` — 索引
- `{RUN_DIR}/out/{sample_name}.sorted.markdup.BQSR.bam` — BQSR 校正后的 BAM
- `{RUN_DIR}/out/{sample_name}.sorted.markdup.BQSR.bam.bai` — 索引
- `{RUN_DIR}/out/{sample_name}.recal.table` — BQSR 校正表

## 脚本编制要点

### SESSION-SAFE 样板（同 Stage 1）

包含完整的 `.done` / `.pid` / 日志配置。

### 注意：MarkDuplicates 和 BQSR 都是单线程

GATK 4.3 非 Spark 版中这两个工具**都是单线程的**。加速方式：
- **样本间并行**：多个样本同时跑
- **给足内存**：`-Xmx`（从 `run.params.json` 的 `java_xmx` 读取）
- **使用 SSD/tmpfs** 作为临时目录

### 工具全路径

```bash
GATK="/home/chenyuxi/anaconda3/envs/Agent/bin/gatk"
SAMTOOLS="/home/chenyuxi/anaconda3/envs/Agent/bin/samtools"
```

### MarkDuplicates 标准用法

```bash
# ✅ 正确 — 使用 --java-options（不是 -J）
gatk MarkDuplicates --java-options "-Xmx${JAVA_XMX}" \
  -I "${INPUT_BAM}" \
  -O "${MARKDUP_BAM}" \
  -M "${MARKDUP_METRICS}" \
  --TMP_DIR "${TMP_DIR}" \
  --REMOVE_DUPLICATES false

# 验证 @RG 存在
samtools view -H "${MARKDUP_BAM}" | grep '^@RG'
```

### BQSR 标准用法

```bash
# Step 1: BaseRecalibrator — 建立校正模型
gatk BaseRecalibrator --java-options "-Xmx${JAVA_XMX}" \
  -R "${REF_FASTA}" \
  -I "${MARKDUP_BAM}" \
  --known-sites "${KNOWN_SNPS}" \
  --known-sites "${KNOWN_INDELS}" \
  -O "${RECAL_TABLE}"

# Step 2: ApplyBQSR — 应用校正
gatk ApplyBQSR --java-options "-Xmx${JAVA_XMX}" \
  -R "${REF_FASTA}" \
  -I "${MARKDUP_BAM}" \
  --bqsr-recal-file "${RECAL_TABLE}" \
  -O "${BQSR_BAM}"
```

### 样本并行模式

```bash
MAX_CONCURRENT=3   # 从 parallelism.parallel_samples 读取
pids=()

for i in "${!samples[@]}"; do
    sample="${samples[$i]}"
    
    # resume 检查
    if [ -f "${BQSR_BAM}" ]; then
        log_info "BQSR BAM exists, skipping: ${BQSR_BAM}"
        continue
    fi
    
    (
        set -euo pipefail
        # MarkDuplicates
        # BaseRecalibrator
        # ApplyBQSR
    ) 2>&1 | stdbuf -oL tee -a "${LOG_FILE}" &
    pids+=($!)
    
    # 控制并发...
done
wait
```

## ⚠️ 错误预防清单

| # | 检查项 | 违反后果 |
|---|--------|---------|
| 1 | **GATK 参数用 `--java-options`** — 不是 `-J` | `-J is not a recognized option` |
| 2 | **GATK 参数名因工具而异** — `--TMP_DIR` / `--tmp-dir` | 参数不被识别 |
| 3 | **验证 BAM 包含 `@RG`** — 如果 Stage 1 未加 RG 则此处报错 | `Number of read groups must be >= 1` |
| 4 | **`known-sites` 参数指定** — BQSR 需要已知多态性位点 | BQSR 无法建立校正模型 |
| 5 | **`compute_backend=parabricks` 时禁用 GATK** — 用 `pbrun markdup/bqsr/applybqsr` | 参数不识别 / GPU 白开 |
| 6 | **pbrun 参数名以 `--help` 为准**（如 `--in-bam`/`--out-recal-file`/`--in-recal-file`） | 参数不被识别 |
| 7 | **Stage 1 已用 fq2bam（4.3.x 默认即 markdup）的样本勿重复 markdup** | 浪费算力；resume 存在性检查跳过 |

### GATK 参数名差异注意

| 参数含义 | MarkDuplicates 写法 | BaseRecalibrator 写法 |
|---------|-------------------|---------------------|
| 临时目录 | `--TMP_DIR` | `--tmp-dir` |
| 参考基因组 | `-R`（通用） | `-R`（通用） |

使用 `gatk <ToolName> --help` 确认具体参数名。

## Parabricks 模式（compute_backend=parabricks）

**`compute_backend == "parabricks"` 时启用**，用 `pbrun markdup` + `pbrun bqsr` + `pbrun applybqsr`
替代 GATK MarkDuplicates / BaseRecalibrator / ApplyBQSR。逐样本绑 1 卡。

> ⚠ **4.3.x 的 `pbrun fq2bam` 默认就已做 MarkDuplicates + BQSR**（见 `parabricks-setup.md` §4.1）。
> 若上游 align-sort 阶段用 fq2bam 且未单独关闭，本阶段 **markdup 应跳过**，仅对增量情况做
> BQSR 校准；否则会重复标记。参数名速查见 `parabricks-setup.md` §4.1。

### 工具路径

```bash
# 统一 PBRUN() docker wrapper（由 wgs-express-params-propose 在脚本头部展开，见其「PBRUN wrapper」）
# 每样本: PBRUN <宿主GPU索引> <pbrun 子命令> ... ；容器内只暴露 1 张卡 → pbrun 用 --num-gpus 1
```

### markdup + BQSR 标准用法

```bash
GPU_IDX=$(( i % GPU_COUNT ))

# 1) 标记重复（对应 GATK MarkDuplicates）
PBRUN "${GPU_IDX}" markdup \
    --in-bam "${INPUT_BAM}" \
    --out-bam "${MARKDUP_BAM}" \
    --out-duplicate-metrics "${MARKDUP_METRICS}" \
    --num-worker-threads "${per_gpu_cpus}" \
    --num-gpus 1 \
    --tmp-dir "${TMP_DIR}"

# 2) 建立校正模型（对应 BaseRecalibrator）
PBRUN "${GPU_IDX}" bqsr \
    --ref "${REF_FASTA}" \
    --in-bam "${MARKDUP_BAM}" \
    --knownSites "${KNOWN_SNPS}" \
    --knownSites "${KNOWN_INDELS}" \
    --out-recal-file "${RECAL_TABLE}" \
    --num-gpus 1

# 3) 应用校正（对应 ApplyBQSR）
PBRUN "${GPU_IDX}" applybqsr \
    --ref "${REF_FASTA}" \
    --in-bam "${MARKDUP_BAM}" \
    --in-recal-file "${RECAL_TABLE}" \
    --out-bam "${BQSR_BAM}" \
    --num-gpus 1
```

> `--knownSites` 是**驼峰**拼写（非 GATK 的 `--known-sites`，实测 `pbrun bqsr -h`）。
> markdup 的 CPU 线程参数是 `--num-worker-threads`（非 `--num-cpu`）；bqsr/applybqsr
> 的线程参数以 `pbrun <tool> --help` 为准（本机 4.3.x 未显式公开，未设亦可正常跑）。

- 输出命名与 CPU 路径**契约一致**（`.sorted.markdup.bam` / `.BQSR.bam` / `.recal.table`），
  Stage 3 直接消费
- **Stage 1 用 fq2bam 时 4.3.x 已默认做 MarkDuplicates**：本阶段跳过 markdup，仅跑 bqsr+applybqsr
  （resume 存在性检查已涵盖）
- `pbrun bqsr` 支持 `--out-bam` 时，可 bqsr+apply 一步完成（省一次 I/O）；参数名用
  `pbrun <tool> --help` 确认
- 均单 GPU 任务，并行度 `parallel_samples = min(N, gpus_to_use)`

## 数据库更新

- 启动时：`INSERT INTO sample_steps (step_name='markdup_bqsr', status='running')`
- 完成后：`UPDATE sample_steps SET status='completed' WHERE ...`

## 脚本安全检查清单

1. ✅ `--java-options` 正确使用
2. ✅ 已知 sites 路径存在且正确
3. ✅ RemoveDuplicates 设为 `false`（标准做法：标记而非删除）
4. ✅ 临时目录使用 SSD 或 tmpfs
5. ✅ SESSION-SAFE 保护
6. ✅ 完成标记写入
7. ✅ resume 模式支持（输出文件存在性检查）
8. ✅ 工具使用 conda 绝对路径
9. ✅ 按 `compute_backend` 选择：parabricks→pbrun markdup/bqsr/applybqsr；否则→GATK
10. ✅ GPU 路径：每样本绑 1 卡、名称契约一致、`pbrun <tool> --help` 复核参数
