---
name: wgs-express-haplotypecaller
description: >
  Call germline SNPs and indels with GATK HaplotypeCaller in GVCF mode
  (-ERC GVCF) per sample. Uses local reassembly and PairHMM for accurate
  variant detection. Called by the orchestration skill (wgs-express-pipeline)
  during Stage 3.
allowed-tools:
  - Read
  - Write
  - Bash
  - Grep
---

# HaplotypeCaller GVCF — Stage 3

使用 GATK HaplotypeCaller 的 GVCF 模式为每个样本生成包含所有位点信息的 GVCF 文件。

**由编排 skill 调用**，不直接暴露给用户。

## 输入

- `run.params.json` — 全部参数
- `{RUN_DIR}/out/{sample_name}.sorted.markdup.BQSR.bam` — BQSR 校正后的 BAM

## 输出

每个样本输出：
- `{RUN_DIR}/out/{sample_name}.g.vcf.gz` — GVCF 文件
- `{RUN_DIR}/out/{sample_name}.g.vcf.gz.tbi` — TBI 索引

## 脚本编制要点

### SESSION-SAFE 样板

完整的 `.done` / `.pid` / 日志配置（同前）。

### 工具全路径

```bash
GATK="/home/chenyuxi/anaconda3/envs/Agent/bin/gatk"
```

### HaplotypeCaller 标准用法

```bash
gatk HaplotypeCaller --java-options "-Xmx${JAVA_XMX}" \
  -R "${REF_FASTA}" \
  -I "${INPUT_BAM}" \
  -O "${GVCF}" \
  -ERC GVCF \
  --native-pair-hmm-threads "${HMM_THREADS}"
```

### 参数说明

| 参数 | 值 | 说明 |
|------|-----|------|
| `-ERC GVCF` | GVCF 模式 | 输出包含所有位点的 GVCF（含 `<NON_REF>` 等位基因） |
| `--native-pair-hmm-threads` | 从 `run.params.json` 读取 | PairHMM 向量化计算线程数（核心瓶颈） |
| `-Xmx` | 从 `run.params.json` 读取 | Java 堆内存 |

### `--native-pair-hmm-threads` 选择规则

**必须严格遵循 `run.params.json` 中 `haplotypecaller.threads_per_sample` 的值**，不做自作主张的限制。

PairHMM 是 HaplotypeCaller 的核心计算瓶颈，使用了 AVX-512 向量化加速。在资源充裕的服务器上（如当前的 128 核 2-socket 服务器），GATK 内部会将 PairHMM 计算分发到指定线程数上并行处理。

**规则：**
- `HMM_THREADS = run.params.json.parallelism.haplotypecaller.threads_per_sample`
- 不允许人为设定低于该值的上限
- 如果 `threads_per_sample` × 并发样本数 > 总线程的 80%，可适当降低并发数而非降低单样本线程数
- **⚠️ 上述规则仅适用于 `compute_backend=gatk-cpu`**。Parabricks 模式下 PairHMM 在 GPU
  上并行，**不设置 `--native-pair-hmm-threads`**，改用 `pbrun ... --num-gpus 1 --num-cpu N`

### 关键认知

1. **GVCF 模式**：记录每个位点的变异置信度，包括非变异位点。`<NON_REF>` 符号等位基因使后续联合基因分型可以评估其他样本在该样本位置的变异。
2. **PairHMM**：计算瓶颈，AVX/AVX-512 向量化加速。
3. **MarkDuplicates 必须在之前运行**：HC 要求标记重复后的 BAM。
4. **BAM 必须包含 @RG**：HC 强制要求 Read Group。

### 样本并行模式

```bash
MAX_CONCURRENT=3   # 从 parallelism.parallel_samples 读取
pids=()

for i in "${!samples[@]}"; do
    sample="${samples[$i]}"
    
    if [ -f "${GVCF}" ] && [ -f "${GVCF}.tbi" ]; then
        log_info "GVCF exists, skipping: ${GVCF}"
        continue
    fi
    
    (
        set -euo pipefail
        gatk HaplotypeCaller --java-options "-Xmx${JAVA_XMX}" \
          -R "${REF_FASTA}" \
          -I "${INPUT_BAM}" \
          -O "${GVCF}" \
          -ERC GVCF \
          --native-pair-hmm-threads "${HMM_THREADS}"
        
        # 索引
        TABIX="/home/chenyuxi/anaconda3/envs/Agent/bin/tabix"
        ${TABIX} -p vcf "${GVCF}"
    ) 2>&1 | stdbuf -oL tee -a "${LOG_FILE}" &
    pids+=($!)
    
    # 控制并发
    while [ "${#pids[@]}" -ge "${MAX_CONCURRENT}" ]; do
        sleep 30   # HC 运行时间长，每 30 秒检查一次
        for j in "${!pids[@]}"; do
            if ! kill -0 "${pids[$j]}" 2>/dev/null; then
                wait "${pids[$j]}" || true
                unset 'pids[$j]'
            fi
        done
    done
done
wait
```

## ⚠️ 错误预防清单

| # | 检查项 | 违反后果 |
|---|--------|---------|
| 1 | **BAM 必须有 @RG** — 验证 `samtools view -H BAM \| grep '^@RG'` | `Number of read groups must be >= 1` |
| 2 | **MarkDuplicates 已执行** — HC 需要标记重复后的 BAM | 变异数量可能偏高 |
| 3 | **GVCF 输出需 tabix 索引** | 下游 CombineGVCFs / GenotypeGVCFs 需要 `.tbi` |
| 4 | **`--java-options` 正确使用** | `-J is not a recognized option` |
| 5 | **Parabricks 模式勿设 `--native-pair-hmm-threads`** — PairHMM 在 GPU 并行 | CPU 线程参数无意义/不被识别 |
| 6 | **`pbrun haplotypecaller` 必须加 `--gvcf`** | 输出普通 VCF，Stage 4 CombineGVCFs 失败 |

## --dragen-mode 说明

如果需要 DRAGEN 级别的准确性，可以在 HC 命令行添加 `--dragen-mode`（启用 DRAGSTR 和 BQD/FRD 模型，可替代单独的 BQSR 步骤）。这适用于：
- 希望通过硬过滤获得校准 QUAL 的场景
- 需要 STR 感知 indel 模型（DRAGSTR）

## Parabricks 模式（compute_backend=parabricks）

**`compute_backend == "parabricks"` 时启用**。用 `pbrun haplotypecaller --gvcf`
替代 GATK HaplotypeCaller。PairHMM 计算在 GPU 上并行（数十倍加速），
**此模式下不设置 CPU 的 `--native-pair-hmm-threads`**（见下方差异表）。

### 工具路径

```bash
# 统一 PBRUN() docker wrapper（由 wgs-express-params-propose 在脚本头部展开，见其「PBRUN wrapper」）
# 每样本: PBRUN <宿主GPU索引> <pbrun 子命令> ... ；容器内只暴露 1 张卡 → pbrun 用 --num-gpus 1
```

### haplotypecaller GVCF 标准用法（逐样本绑 1 卡）

```bash
GPU_IDX=$(( i % GPU_COUNT ))

PBRUN "${GPU_IDX}" haplotypecaller \
    --ref "${REF_FASTA}" \
    --in-bam "${INPUT_BAM}" \
    --out-variants "${GVCF}" \
    --gvcf \
    --num-htvc-threads "${per_gpu_cpus}" \
    --num-gpus 1
```

- `--gvcf`：输出 GVCF（等价 GATK `-ERC GVCF`），含 `<NON_REF>` 等位基因
- 输出 `.g.vcf.gz` 时 pbrun 通常自动生成 `.tbi`；若未生成需补 tabix
- 输入仍须是 **BQSR 校正、含 `@RG`** 的 BAM（要求与 CPU 路径相同）
- 备选高精度：`pbrun deepvariant --gvcf`（AI 模型，同输出 GVCF，更适合追求准确率的场景）
- 具体参数名以 `pbrun haplotypecaller --help` 为准

### 与 CPU 路径的关键差异

| 项 | CPU（GATK HC） | GPU（pbrun haplotypecaller） |
|----|----------------|------------------------------|
| PairHMM | `--native-pair-hmm-threads`（CPU 线程） | GPU 并行，**不设该参数** |
| GVCF 索引 | 手动 tabix | 自动 `.tbi`（需验证） |
| 输出 | `{sample}.g.vcf.gz` | 同（命名契约一致） |

## 数据库更新

- 启动时：`INSERT INTO sample_steps (step_name='haplotypecaller', status='running')`
- 完成后：`UPDATE sample_steps SET status='completed' WHERE ...`

## 脚本安全检查清单

1. ✅ BAM 输入有 `@RG` 标签
2. ✅ BAM 输入是 BQSR 校正后的
3. ✅ `-ERC GVCF` 正确设置
4. ✅ `--native-pair-hmm-threads` 合理设置
5. ✅ SESSION-SAFE 保护
6. ✅ 完成标记写入
7. ✅ tbi 索引生成
8. ✅ resume 模式支持
9. ✅ 按 `compute_backend` 选择：parabricks→`pbrun haplotypecaller --gvcf`（不设 HMM 线程）；否则→GATK `-ERC GVCF`
10. ✅ GPU 路径：每样本绑 1 卡、输出命名契约一致、验证 `.tbi` 自动生成
