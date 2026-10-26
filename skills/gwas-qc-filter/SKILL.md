---
name: gwas-qc-filter
description: >
  Perform per-marker and per-individual QC on PLINK files — genotype calling rate
  (--missing), minor allele frequency (--freq), Hardy-Weinberg equilibrium
  (--hardy), then hard-filter with plink -geno/-maf/-hwe --make-bed. Includes an
  optional genome-wide LD decay analysis with PopLDdecay on the filtered data.
  Stage 0.5 of the GWAS pipeline. Called by the orchestration skill (gwas-pipeline).
  当用户提到 质控、过滤、缺失率、MAF、HWE、最小等位基因频率、LD衰减、PopLDdecay — match this skill.
allowed-tools:
  - Read
  - Write
  - Bash
  - Grep
---

# 数据质控与过滤 — Stage S0.5

对 S0 转换的 PLINK 数据做基因型缺失率、最小等位基因频率（MAF）、哈迪-温伯格平衡（HWE）
检查与过滤，生成后续关联分析用的高质量 SNP 集；并可选做全基因组 LD 衰减分析。

**由编排 skill 调用**（`gwas-pipeline` 的 S0.5），也可独立使用。

## 输入

| 来源 | 说明 |
|------|------|
| `{prefix}.ped` / `.map` | S0 转换结果（`plink --file`） |
| `{prefix}.bed` / `.bim` / `.fam` | 已存在的二进制格式（`plink --bfile`） |

## 输出

| 文件 | 说明 |
|------|------|
| `plink.missing` / `plink.frq` 等 | QC 报告（缺失率、MAF 分布） |
| `{prefix}.wy1.snp.{bed,bim,fam}` | **过滤后** bfile（关联分析输入） |
| `{prefix}.LDdecay.vcf` + `.pld` | 可选：LD 衰减输入 VCF 与统计结果 |

## 过滤条件（默认，取自实际项目）

```
-plink 过滤:
  -geno 0.2        # 位点缺失率 > 20% 剔除
  -maf 0.01        # 最小等位基因频率 < 1% 剔除
  -hwe 0.000001    # HWE 检验 p < 1e-6 剔除
  --make-bed       # 输出二进制格式
```

- 较严格项目用 `-geno 0.1 -maf 0.05 -hwe 0.000001`
- 样本级缺失率用 `--mind 0.x`（本流程默认不滤个体，按需添加）

## 核心命令

### 1. 缺失率检查（查看数据集缺失率）

```bash
nohup plink --file cohort --missing > plink_missing.log 2>&1 &
# 产出 plink.lmiss（位点级）/ plink.imiss（个体级）
```

### 2. 最小等位基因频率检查

```bash
nohup plink --file cohort --freq --out MAF_check > MAF_check.log 2>&1 &
# 产出 MAF_check.frq；用 R 画 MAF 分布直方图确认阈值
```

> ⚠️ `nohup ... &` 必须带**重定向**（`> xxx.log 2>&1`）。只写 `&` 时子进程仍持有
> 输出管道，父进程要等它退出才返回 —— 这一步就会把整轮卡住。
> 配 `Bash(background: true)` 使用（参数名是 `background`，不是 `run_in_background`）。

### 3. Hardy-Weinberg 检查（可选）

```bash
plink --file cohort --hardy --out hwe_check
```

### 4. 过滤并生成 bed

```bash
plink --file cohort -geno 0.2 -maf 0.01 -hwe 0.000001 --make-bed --out cohort.wy1.snp
```

### 5. 过滤后复检（确认过滤效果）

```bash
plink --bfile cohort.wy1.snp --missing --out cohort.wy1.snp.missing
plink --bfile cohort.wy1.snp --freq --out cohort.wy1.snp.maf
```

### 6. （可选）全基因组 LD 衰减 — PopLDdecay

```bash
# bed → VCF
plink --bfile cohort.wy1.snp --recode vcf --out cohort.LDdecay

# 计算 LD 衰减（MaxDist 单位 kb；OutType 2 = 每距离段统计）
PopLDdecay -InVCF cohort.LDdecay.vcf -OutStat cohort.LDdecay.pld -MaxDist 100 -OutType 2
```

**LD 衰减绘图**（输出 `png`/`pdf`）：

```bash
# LDBlockShow 工具包里的 Plot_OnePop.pl 画单群体衰减曲线
perl $LDBlockShow/bin/Plot_OnePop.pl -inK ${PREFIX}.LDdecay.pld.gz -out ${PREFIX}.LDdecay.png
```

> ⚠ 转录本/兼容脚本中常见的工具名拼写错误：是 `PopLDdecay`（不是
> `PopLDdecayPopLDdecay`、不是 `PopLDdecay-1.36PopLDdecay`）。用 `PopLDdecay --help` 确认。

## 脚本模板（SESSION-SAFE）

```bash
#!/bin/bash
set -euo pipefail

RUN_DIR="..."
LOG_FILE="${RUN_DIR}/logs/00.5_qc_filter.log"
DONE_FILE="${RUN_DIR}/.done/00.5_qc_filter.done"
PID_FILE="${RUN_DIR}/.pid/00.5_qc_filter.pid"
mkdir -p "$(dirname "${LOG_FILE}")" "$(dirname "${DONE_FILE}")" "$(dirname "${PID_FILE}")"
echo $$ > "${PID_FILE}"
trap 'rm -f "${PID_FILE}"' EXIT

PLINK="$(command -v plink || { echo "✗ 找不到 plink —— 请激活 conda 环境 Agent" >&2; exit 1; })"
PED_NAME="${PREFIX}"          # 无扩展名；--file 会找 ${PED_NAME}.ped/.map

# ─── QC 报告 ───
${PLINK} --file "${PED_NAME}" --missing
${PLINK} --file "${PED_NAME}" --freq --out MAF_check

# ─── 过滤 ───
${PLINK} --file "${PED_NAME}" -geno 0.2 -maf 0.01 -hwe 0.000001 \
    --make-bed --out "${PREFIX}.wy1.snp"

# ─── 过滤后复检 ───
${PLINK} --bfile "${PREFIX}.wy1.snp" --missing --out "${PREFIX}.wy1.snp.missing"
${PLINK} --bfile "${PREFIX}.wy1.snp" --freq --out "${PREFIX}.wy1.snp.maf"

date > "${DONE_FILE}"
```

## ⚠️ 错误预防清单

| # | 检查项 | 违反后果 |
|---|--------|---------|
| 1 | **`--file`（本地文本）与 `--bfile`（二进制）别混用** | 找不到输入文件 |
| 2 | **过滤前先看 `--missing`/`--freq` 分布** — 用数据说话定阈值，别盲选 | 过度过滤或过滤不足 |
| 3 | **HWE 阈值方向** — `-hwe` 默认保留 P 值 ≥ 阈值；负值如 `-hwe 1e-6` 是"仅保留显著违例位点"，别用反 | 位点全被滤掉/全保留 |
| 4 | **PopLDdecay 工具名拼写** — 常见 `PopLDdecayPopLDdecay` 连写错误 | 找不到命令 |
| 5 | **PopLDdecay 大样本量内存/时间** — 全基因组数百样本要限 `-MaxDist 500` 内部限制或分染色体 | OOM / 长时间运行 |
| 6 | **位点数下降过快要回查** — 过滤后剩余位点 < 预期 20%，检查 VCF 是否已含过滤标记 | 关联分析功效不足 |
| 7 | **X 染色体 HWE/MAF 按性染色体验算与常染色体不同** — 需要时分开处理或剔除 | 偏离 HWE 误滤 |
| 8 | **过滤后必须复检**（missing/MAF 二次报告） | 过滤生效与否无从确认 |

## QC 输出汇报

```
✅ 质控过滤完成
原始位点:    {N_BEFORE}
过滤后位点:  {N_AFTER}（剩 {PCT}%）
删除标准:    -geno 0.2 / -maf 0.01 / -hwe 1e-6
```

## 相关 Skill

- `gwas-format-convert` — 上一阶段：VCF → PLINK
- `gwas-kinship-pca` — 下一阶段：GCTA GRM（用过滤后 bfile）
- `gwas-ld-block` — 区域 LD 分析（本阶段为全基因组 LD 衰减，两者互补）
