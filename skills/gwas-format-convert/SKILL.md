---
name: gwas-format-convert
description: >
  Convert GWAS input VCF to PLINK binary format (bed/bim/fam) using vcftools --plink
  or plink --vcf --make-bed. Stage 0 of the GWAS pipeline. Handles both plain and
  gzip-compressed VCF, checks genotype/individual counts with wc -l, and includes
  the batch-recode to VCF needed by LD 分析 tools. Called by the orchestration
  skill (gwas-pipeline) during Stage S0. 当用户提到 plink --file、--bfile、ped/map、
  vcftools --plink、VCF 转格式 — match this skill.
allowed-tools:
  - Read
  - Write
  - Bash
  - Grep
---

# VCF → PLINK 格式转换 — Stage S0

把 GWAS 输入 VCF（通常来自 WGS 过滤管线的 `*.snp.filter.vcf.gz`）转换为 PLINK 格式，
作为后续质控过滤、GCTA / GEMMA / rMVP 关联分析的统一输入。

**由编排 skill 调用**（`gwas-pipeline` 的 S0），也可独立使用。

## 输入

| 来源 | 说明 |
|------|------|
| `{prefix}.vcf` / `{prefix}.vcf.gz` | 过滤后的全基因组 SNP VCF（可直接用 `--gzvcf` 读 gz） |
| 参考基因组信息 | map/bed 格式转换不需要参考 fasta |

## 输出

| 文件 | 说明 |
|------|------|
| `{prefix}.ped` / `{prefix}.map` | `vcftools --plink` 中介格式（用于 plink `--file` 的 QC 检查） |
| `{prefix}.bed` / `.bim` / `.fam` | filter 后用 `--make-bed` 生成的二进制格式（供 `--bfile` 使用） |
| `{prefix}.LDdecay.vcf`（可选） | plink `--recode vcf` 批量转回 VCF（供 PopLDdecay / LDBlockShow） |

> 先转出 `.ped/.map`（因为 Stage S0.5 缺失/MAF 检查基于 `plink --file`），
> 再在过滤后生成 `.bed`（后续 `gcta64`/`gemma` 等用 `--bfile`）。

## 核心命令

### 1. VCF → PLINK ped/map（vcftools）

```bash
# 普通 VCF
vcftools --vcf LDdecay.vcf --plink --out zx

# gzip 压缩 VCF（⚠ 必须用 --gzvcf，不是 --vcf）
vcftools --gzvcf ET277.new.snp.filter.vcf.gz --plink --out et277
```

### 2. 直接 VCF → PLINK bed（alternative，可跳过 ped/map 中介）

```bash
plink --vcf ET277.new.snp.filter.vcf.gz --make-bed --out et277 --allow-extra-chr
```

> 默认路径：`--vcf` 按 `#CHROM` 编号转换；若原始染色体名为 `chr1`/`SSC7` 等带前缀或非常规编号，
> 可能需要 `--allow-extra-chr` 或 `--chr-set` 处理。default 兼容常见猪 18 autosomes + X。

### 3. 检查样本与位点数量

```bash
wc -l et277.map et277.ped   # map 行数=位点数；ped 行数=个体数
```

### 4. 过滤后 bed → VCF（LD 分析用）

```bash
plink --bfile et277.wy1.snp --recode vcf --out et277.LDdecay
plink --bfile et277.fm.snp --recode vcf --out LDdecay
```

## 脚本模板（SESSION-SAFE）

```bash
#!/bin/bash
set -euo pipefail

RUN_DIR="..."                 # 从 gwas-pipeline 生成
LOG_FILE="${RUN_DIR}/logs/00_format_convert.log"
DONE_FILE="${RUN_DIR}/.done/00_format_convert.done"
PID_FILE="${RUN_DIR}/.pid/00_format_convert.pid"
mkdir -p "${LOG_DIR}" "${DONE_DIR}" "${PID_DIR}"
echo $$ > "${PID_FILE}"
trap 'rm -f "${PID_FILE}"' EXIT

# 工具路径（运行时探测，勿写死本机路径；阶段脚本经 `conda run -n Agent` 执行）
PLINK="$(command -v plink || { echo "✗ 找不到 plink —— 请激活 conda 环境 Agent" >&2; exit 1; })"
# ⚠ vcftools 常缺失：优先直接用 plink --vcf --make-bed 替代（见「↑ 直接 VCF → PLINK bed」）
VCFTOOLS="$(command -v vcftools || echo '')"

# gz VCF → ped/map
${VCFTOOLS} --gzvcf "${IN_VCF_GZ}" --plink --out "${PREFIX}"

# 数量核对
n_map=$(wc -l < "${PREFIX}.map"); n_ped=$(wc -l < "${PREFIX}.ped")
log_info "SNPs: ${n_map}, individuals: ${n_ped}"

date > "${DONE_FILE}"
```

## ⚠️ 错误预防清单

| # | 检查项 | 违反后果 |
|---|--------|---------|
| 1 | **gzip VCF 用 `--gzvcf`**（`--vcf` 读不了 `.gz`） | vcftools 报错 / 读到空文件 |
| 2 | **vcftools 对 `--vcf` 中的多等位/非 SNP 位点会报错** — 输入应是过滤后的 SNP-only VCF | 转换中断 |
| 3 | **`wc -l` 必做** — 确认位点数/个体数与预期一致再进入下游 | 空数据集跑完整个管线才发现 |
| 4 | **`--allow-extra-chr`** — 非标准染色体名（`chr7`/`SSC7` / X/Y） | plink 拒绝或丢弃染色体 |
| 5 | **工具路径** — plink 用 `command -v` 运行时探测（勿写死本机路径）；vcftools 同样检测 | 子 shell 丢 PATH |
| 6 | **别用 plink2 的 `--export-bed` 参数名混到 plink1.9** | 参数不识别 |

## QC 输出汇报

```
✅ 格式转换完成
VCF:      ET277.new.snp.filter.vcf.gz
SNPs:     {N_MAP}
个体:     {N_PED}
输出:     {PREFIX}.ped/.map → {PREFIX}.bed/.bim/.fam
```

## 相关 Skill

- `gwas-qc-filter` — 下一阶段：缺失率/MAF/HWE 过滤
- `gwas-pipeline` — 编排层
