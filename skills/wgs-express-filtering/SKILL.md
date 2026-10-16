---
name: wgs-express-filtering
description: >
  Split joint VCF into SNPs and INDELs, apply variant-level hard filters
  using GATK SelectVariants and VariantFiltration. Final stage of the WGS
  expressive pipeline. Called by the orchestration skill (wgs-express-pipeline)
  during Stage 5.
allowed-tools:
  - Read
  - Write
  - Bash
  - Grep
---

# SelectVariants + VariantFiltration — Stage 5

从联合基因分型后的 VCF 中提取 SNP 和 INDEL，分别应用硬过滤条件，
生成过滤后的最终 VCF 文件。

**这是最终阶段**（cohort-level），完成后管线结束。

**由编排 skill 调用**，不直接暴露给用户。

> **无 GPU/Parabricks 路径**：SelectVariants / VariantFiltration 是轻量 VCF 文本处理
> （秒~分钟级），GPU 无收益。无论 `compute_backend` 为何，本阶段**恒用 GATK CPU**。
> 过滤条件与流程不受后端影响。

## 输入

- `run.params.json` — 全部参数
- `{RUN_DIR}/out/{prefix}.vcf.gz` — 联合基因分型后 VCF

## 输出

- `{RUN_DIR}/out/{prefix}.snp.raw.vcf.gz` — SNP 原始 VCF
- `{RUN_DIR}/out/{prefix}.indel.raw.vcf.gz` — INDEL 原始 VCF
- `{RUN_DIR}/out/{prefix}.snp.filter.vcf.gz` — **最终**过滤后 SNP VCF
- `{RUN_DIR}/out/{prefix}.indel.filter.vcf.gz` — **最终**过滤后 INDEL VCF
- 对应 `.tbi` 索引文件

## 过滤条件

### SNP 硬过滤

```
QD < 2.0 || MQ < 40.0 || FS > 60.0 || SOR > 3.0 || MQRankSum < -12.5 || ReadPosRankSum < -8.0
```

### INDEL 硬过滤

```
QD < 2.0 || FS > 200.0 || SOR > 10.0 || MQRankSum < -12.5 || ReadPosRankSum < -20.0
```

**注意**：INDEL 的 FS 阈值是 200（不是 60），因为真实 INDEL 的自然链偏倚更高。
MQ 和 MQRankSum 在 INDEL 过滤中不适用（不做诊断）。

### ⚠️ 同型合子位点的 "missing => PASS" 陷阱

`MQRankSum`、`ReadPosRankSum` 和 `BaseQRankSum` 是秩和检验统计量，**只在杂合位点上有定义**。
同型合子（hom-alt）位点没有参比等位基因的 reads，因此这些注释缺失（`.`）。

GATK 的 `VariantFiltration` 仅在**值存在且违反阈值时**才标记过滤——缺失值通过 PASS。
如果使用 `bcftools filter` 手动实现，必须为每个 RankSum 项添加显式的 `|| INFO/X = "."` 保护，
否则每个同型合子位点都会无声地失败并消失。

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

### 标准用法

```bash
# ─── 1. 提取 SNP ───
gatk SelectVariants --java-options "-Xmx${JAVA_XMX}" \
  -R "${REF_FASTA}" \
  -V "${JOINT_VCF}" \
  --select-type-to-include SNP \
  -O "${SNP_RAW}"

# ─── 2. 提取 INDEL ───
gatk SelectVariants --java-options "-Xmx${JAVA_XMX}" \
  -R "${REF_FASTA}" \
  -V "${JOINT_VCF}" \
  --select-type-to-include INDEL \
  -O "${INDEL_RAW}"

# ─── 3. SNP 过滤 ───
gatk VariantFiltration --java-options "-Xmx${JAVA_XMX}" \
  -R "${REF_FASTA}" \
  -V "${SNP_RAW}" \
  --filter-expression "QD < 2.0" --filter-name "QD2" \
  --filter-expression "FS > 60.0" --filter-name "FS60" \
  --filter-expression "MQ < 40.0" --filter-name "MQ40" \
  --filter-expression "MQRankSum < -12.5" --filter-name "MQRankSum-12.5" \
  --filter-expression "ReadPosRankSum < -8.0" --filter-name "ReadPosRankSum-8" \
  --filter-expression "SOR > 3.0" --filter-name "SOR3" \
  -O "${SNP_FILTER}"

# ─── 4. INDEL 过滤 ───
gatk VariantFiltration --java-options "-Xmx${JAVA_XMX}" \
  -R "${REF_FASTA}" \
  -V "${INDEL_RAW}" \
  --filter-expression "QD < 2.0" --filter-name "QD2" \
  --filter-expression "FS > 200.0" --filter-name "FS200" \
  --filter-expression "ReadPosRankSum < -20.0" --filter-name "ReadPosRankSum-20" \
  --filter-expression "SOR > 10.0" --filter-name "SOR10" \
  -O "${INDEL_FILTER}"

# ─── 5. 索引 ───
${TABIX} -p vcf "${SNP_FILTER}"
${TABIX} -p vcf "${INDEL_FILTER}"
```

## 过滤验证

过滤后应进行质量控制验证：

```bash
# 查看各 FILTER 分类的计数
bcftools query -f '%FILTER\n' "${SNP_FILTER}" | sort | uniq -c

# 计算 Ti/Tv 比
bcftools stats "${SNP_FILTER}" | grep '^TSTV'

# 预期：WGS Ti/Tv 约 2.0-2.1
```

## ⚠️ 错误预防清单

| # | 检查项 | 违反后果 |
|---|--------|---------|
| 1 | **SNP 和 INDEL 使用不同过滤条件** | 阈值不适用导致真假混合 |
| 2 | **`--java-options` 正确使用** | `-J is not a recognized option` |
| 3 | **输出文件 tabix 索引** | 下游工具无法使用 |
| 4 | **过滤后验证 Ti/Tv** | 未检测的过度过滤 |

## 最终输出汇报

管线完成后，LLM **必须**向用户汇报最终产出：

```
✅ WGS Pipeline Complete!
Final outputs:
  SNP VCF:   {RUN_DIR}/out/{prefix}.snp.filter.vcf.gz
  INDEL VCF: {RUN_DIR}/out/{prefix}.indel.filter.vcf.gz
  SNP stats: {SNP_PASS_COUNT} PASS, {SNP_FILTER_COUNT} filtered (Ti/Tv: {TITV})
  INDEL stats: {INDEL_PASS_COUNT} PASS, {INDEL_FILTER_COUNT} filtered
```

## 数据库更新

- 启动时：`INSERT INTO sample_steps (step_name='filtering', status='running')`
- 完成后：
  - `UPDATE sample_steps SET status='completed' WHERE step_name='filtering'`
  - `UPDATE pipeline_runs SET status='completed', completed_at=datetime('now') WHERE run_id='...'`

## 脚本安全检查清单

1. ✅ SNP 和 INDEL 分别提取
2. ✅ SNP 和 INDEL 使用不同的过滤条件
3. ✅ `--java-options` 正确使用
4. ✅ 输出 tabix 索引
5. ✅ SESSION-SAFE 保护
6. ✅ 完成标记写入
7. ✅ 最终产出汇报
8. ✅ 数据库状态更新
9. ✅ 本阶段**恒用 GATK CPU**（无 Parabricks 路径），`-Xmx` 取自 `parallelism.java_xmx`
