---
name: wgs-express-joint-genotyping
description: >
  Combine per-sample GVCFs with CombineGVCFs then jointly genotype with
  GenotypeGVCFs. Single cohort step (not per-sample). Called by the
  orchestration skill (wgs-express-pipeline) during Stage 4.
allowed-tools:
  - Read
  - Write
  - Bash
  - Grep
---

# CombineGVCFs + GenotypeGVCFs — Stage 4

合并所有样本的 GVCF 文件并进行联合基因分型。

**这是群组级（cohort-level）步骤**，不是逐样本的。

**由编排 skill 调用**，不直接暴露给用户。

## 输入

- `run.params.json` — 全部参数（样本列表、参考基因组等）
- `{RUN_DIR}/out/{sample_name}.g.vcf.gz` — 每个样本的 GVCF 文件
- `{RUN_DIR}/out/{sample_name}.g.vcf.gz.tbi` — 对应索引

## 输出

- `{RUN_DIR}/out/{prefix}.combined.g.vcf.gz` — 合并后 GVCF
- `{RUN_DIR}/out/{prefix}.combined.g.vcf.gz.tbi` — 索引
- `{RUN_DIR}/out/{prefix}.vcf.gz` — 联合基因分型后 VCF
- `{RUN_DIR}/out/{prefix}.vcf.gz.tbi` — 索引

## 脚本编制要点

### SESSION-SAFE 样板

完整的 `.done` / `.pid` / 日志配置。

### 工具路径（运行时探测，勿写死本机路径）

阶段脚本经 `conda run -n Agent bash` 执行，工具已在 PATH 中：

```bash
resolve_tool() { command -v "$1" || { echo "✗ 找不到 $1 —— 请激活 conda 环境 Agent" >&2; exit 1; }; }
GATK="$(resolve_tool gatk)"
TABIX="$(resolve_tool tabix)"
```

### CombineGVCFs 标准用法

```bash
# 构建 -V 参数列表
GVCF_INPUTS=""
for sample in "${sample_names[@]}"; do
    GVCF="${RUN_DIR}/out/${sample}.g.vcf.gz"
    GVCF_INPUTS+=" -V ${GVCF}"
done

gatk CombineGVCFs --java-options "-Xmx${JAVA_XMX}" \
  -R "${REF_FASTA}" \
  ${GVCF_INPUTS} \
  -O "${COMBINED_GVCF}"
```

### GenotypeGVCFs 标准用法

```bash
gatk GenotypeGVCFs --java-options "-Xmx${JAVA_XMX}" \
  -R "${REF_FASTA}" \
  -V "${COMBINED_GVCF}" \
  -O "${JOINT_VCF}"
```

### 注意：CombineGVCFs 和 GenotypeGVCFs 都是单线程的

这两个工具**不支持多线程**，加速方式：
- 使用大 `-Xmx`（如 `-Xmx48g`）减少 GC 暂停
- 确保 I/O 不成为瓶颈
- 使用 SSD 或 tmpfs 作为临时目录

对于大规模群组（> 200 样本），应考虑使用 `GenomicsDBImport` 替代 `CombineGVCFs`。

### 断点续跑

```bash
# 如果合并后的 GVCF 已存在，跳过 CombineGVCFs
if [ -f "${COMBINED_GVCF}" ]; then
    log_info "Combined GVCF exists, skipping CombineGVCFs"
else
    # CombineGVCFs 命令 ...
fi

# 如果最终 VCF 已存在，跳过 GenotypeGVCFs
if [ -f "${JOINT_VCF}" ]; then
    log_info "Joint VCF exists, skipping GenotypeGVCFs"
else
    # GenotypeGVCFs 命令 ...
fi
```

## ⚠️ 错误预防清单

| # | 检查项 | 违反后果 |
|---|--------|---------|
| 1 | **所有 GVCF 文件必须存在且已索引** | CombineGVCFs 报错 |
| 2 | **GVCF 文件名与样本名一一对应** | 合并后样本命名错误 |
| 3 | **`--java-options` 正确使用** | `-J is not a recognized option` |
| 4 | **给足 -Xmx**（CombineGVCFs 内存需求随样本数线性增长） | OutOfMemoryError |
| 5 | **Parabricks 模式用 `pbrun genotypecaller`**，勿用 GATK GenotypeGVCFs | 参数不识别 / GPU 白开 |
| 6 | **`--in-vcf` 必须是合并后 GVCF**（`combined.g.vcf.gz`） | 输入错误，联合分型失败 |
| 7 | **超大群组（>100 样本）加 `--run-partition`** | 单卡 OOM |

### 内存建议

| 样本数 | 建议 -Xmx |
|--------|-----------|
| 1-5 | 32g |
| 5-20 | 48g |
| 20-100 | 64g+ |
| 100+ | 考虑 GenomicsDBImport |

## Parabricks 模式（compute_backend=parabricks）

**`compute_backend == "parabricks"` 时启用**。用 `pbrun genotypegvcf` 替代
GATK GenotypeGVCFs（⚠ 4.3.x **没有** `genotypecaller` 子命令，工具名是 `genotypegvcf`）。

### 工具路径

```bash
# 统一 PBRUN() docker wrapper（由 wgs-express-params-propose 在脚本头部展开，见其「PBRUN wrapper」）
# PBRUN <宿主GPU索引> <pbrun 子命令> ... ；容器内只暴露 1 张卡 → pbrun 用 --num-gpus 1
```

### genotypegvcf 标准用法（单 GVCF 转 VCF，绑 1 卡）

```bash
GPU_IDX=0   # 群组级任务通常只取 1 张卡

PBRUN "${GPU_IDX}" genotypegvcf \
    --ref "${REF_FASTA}" \
    --in-gvcf "${COMBINED_GVCF}" \
    --out-vcf "${JOINT_VCF}" \
    --num-threads "${per_gpu_cpus}" \
    --num-gpus 1
```

- `--in-gvcf` 为合并后 GVCF（`{prefix}.combined.g.vcf.gz`）。**合并（CombineGVCFs）本身
  轻量，可保持 GATK CPU 执行**（非瓶颈），仅联合分型交给 GPU
- ⚠ 4.3.x 的 `genotypegvcf` 接受单个 `--in-gvcf`（无 `genotypecaller`、无 `--run-partition`）。
  大规模群组：合并仍走 GATK CombineGVCFs（CPU），联合分型对合并后的单 GVCF 调用一次
  `genotypegvcf`；超大群组若 GPU 内存不足则整体回退 GATK GenotypeGVCFs（CPU）
- 输出命名与 CPU 路径契约一致（`{prefix}.vcf.gz`），并验证 `.tbi` 生成
- 联合分型类单线程工具在 CPU 上耗时随样本数线性增长，GPU 优势随样本数放大

## 数据库更新

- 启动时：`INSERT INTO sample_steps (step_name='joint_genotyping', status='running')`
- 完成后：`UPDATE sample_steps SET status='completed' WHERE ...`

## 脚本安全检查清单

1. ✅ 所有输入 GVCF 都存在且已 `.tbi` 索引
2. ✅ -Xmx 足够大（根据样本数）
3. ✅ SESSION-SAFE 保护
4. ✅ 完成标记写入
5. ✅ 输出 VCF 已 tabix 索引
6. ✅ resume 模式支持
7. ✅ 按 `compute_backend` 选择：parabricks→`pbrun genotypegvcf`（Combine 可留 GATK CPU）；否则→GATK
8. ✅ GPU 路径：`--in-gvcf` 为合并 GVCF、输出契约一致、`.tbi` 已验证
