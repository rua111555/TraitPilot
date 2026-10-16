---
name: gwas-visualization
description: >
  Visualize GWAS results and extract significant SNPs. Load rMVP/GEMMA result
  tables (CSV column 8 = P), draw Manhattan (m), density (d), QQ (q) and circular
  (c) plots with CMplot / MVP.Report, apply significance thresholds (0.05/n
  Bonferroni), filter significant SNPs (by chrom, region, and P), and export the
  significant SNP list + region genotypes for downstream LD / haplotype analysis.
  Stage 4 of the GWAS pipeline. Called by the orchestration skill (gwas-pipeline).
  当用户提到 曼哈顿图、QQ图、CMplot、MVP.Report、显著位点、阈值、圈图、GWAS 作图 — match this skill.
allowed-tools:
  - Read
  - Write
  - Bash
  - Grep
---

# GWAS 结果可视化 + 显著位点提取 — Stage S4

把 GEMMA / rMVP 的关联结果画成曼哈顿图、QQ 图、密度图、圈图，并据此**提取标记
显著位点 + 区域基因型**，供 S5 LD block 与候选基因分析。

**由编排 skill 调用**（`gwas-pipeline` 的 S4），也可独立使用。

## 输入

| 来源 | 说明 |
|------|------|
| `${TRAIT}.{GLM|MLM|FarmCPU}.csv` | rMVP 结果（第 8 列 = P 值） |
| `output/{PREFIX}_GWAS.assoc.txt` | GEMMA 结果（`p_wald` 列） |
| `{bfile}.{bed,bim,fam}` | 过滤后 bfile（提取位点基因型用） |

## 输出

| 文件 | 说明 |
|------|------|
| `.pdf` / `.jpg` 图像 | Manhattan / QQ / 密度 / 圈图 |
| `{TRAIT}.sig.txt` / `Sig.*.txt` | 显著 SNP 列表（P < 阈值） |
| `{TRAIT}.region.genotype` | 候选区域基因型（plink `--recode`） |

## 解读：P 值取哪一列

- rMVP CSV 第 8 列 = 该方法的 P 值：`dat[, c(1,2,3,8)]`（SNP, CHROM, POS, P）
- GEMMA assoc.txt：`p_wald` / `p_lrt` / `p_score` 均可
- 多性状比较时 cbind 各性状的 P 列制造 `newdata`

## 核心命令

### 1. 读取结果（rMVP；多性状合并示例）

```r
library(rMVP)
library(CMplot)
dat1  <- read.csv("Last.FarmCPU.csv",     head = TRUE)
dat2  <- read.csv("Mid.FarmCPU.csv",      head = TRUE)
dat3  <- read.csv("Shoulder.FarmCPU.csv", head = TRUE)
newdat1 <- dat1[, c(1, 2, 3, 8)]   # SNP CHR POS P
newdat2 <- dat2[, c(1, 2, 3, 8)]
newdat3 <- dat3[, c(1, 2, 3, 8)]
df  <- cbind(newdat1, newdat2, newdat3)   # SNP CHR POS P1 P2 P3 ...
data <- df[, c(1, 2, 3, 12, 4, 8)]        # 重排为 SNP CHR POS(P3) P1 P2
```

### 2. 曼哈顿图 / QQ图 / 密度图

```r
# —— 曼哈顿（单性状；threshold 给显著性线）——
CMplot(data, plot.type = "m", LOG10 = TRUE, threshold = NULL,
       chr.labels = paste(c(1:18, "X"), sep = ""),
       file = "pdf", file.output = TRUE, verbose = TRUE)
CMplot(data, plot.type = "m", LOG10 = TRUE, threshold = 3.92e-9,
       chr.labels = paste(c(1:18, "X"), sep = ""),
       file = "jpg", file.output = TRUE, verbose = TRUE)

# —— QQ 图 ——
CMplot(data, plot.type = "q", conf.int.col = NULL, box = TRUE,
       file = "pdf", file.output = TRUE, verbose = TRUE)

# —— 密度图（全基因组标记密度）——
CMplot(data, plot.type = "d", bin.size = 1e6,
       chr.den.col = c("darkgreen", "yellow", "red"),
       chr.labels = paste(c(1:18, "X"), sep = ""),
       file = "pdf", file.output = TRUE, verbose = TRUE)
```

### 3. MVP.Report 高级图（圈图 / 多轨 QQ）

```r
# —— 圈图（circular）——
MVP.Report(data, plot.type = "c", r = 0.4, col = c("grey30", "grey60"),
    chr.labels = paste("Chr", c(1:18, "X"), sep = ""),
    threshold = c(1e-8, 1e-6), cir.chr.h = 1.5, amplify = TRUE,
    threshold.lty = c(1, 2), threshold.col = c("red", "blue"),
    signal.line = 1, signal.col = c("red", "green"),
    chr.den.col = c("darkgreen", "yellow", "red"), bin.size = 1e6,
    outward = FALSE, file.type = "pdf", memo = "", dpi = 200)

# —— 多轨 QQ（multi-trait）——
MVP.Report(data, plot.type = "q", col = c("dodgerblue1", "olivedrab3", "darkgoldenrod1"),
    threshold = 3.92e-9, signal.pch = 19, signal.cex = 1.5, signal.col = "red",
    box = FALSE, conf.int = TRUE, multracks = TRUE,
    file.type = "pdf", memo = "", dpi = 200)
```

### 4. 显著性阈值

```r
# Bonferroni：0.05 / 有效独立标记数（常见 1e-6 ~ 1e-8 区间）
# 以本测试数据集约 12.7 万位点为例 → 0.05 / 127000 ≈ 3.92e-9（按实际位点数重算）
# 建议同时给建议线（如 1e-6 / 3.92e-7）与严格线（0.05/n）
c(3.92e-9, 3.92e-7)   # 项目常见两档
```

### 5. 提取显著位点（P < 阈值 + 候选区域）

```r
# 按染色体 + 区域 + P 过滤
sig <- result[result$CHROM == 7, ]
sig <- sig[sig$POS > 28775091 & sig$POS < 30637297, ]
sig <- sig[sig$P_value < 3.92e-07, ]
write.table(sig, file = "ABFTfarm.txt",
            row.names = T, col.names = NA, sep = "\t", quote = F)

# 整列位点显著提取（SNP 列名改成你的 P 值列）
sg <- data[data$P < 3.92e-7, ]
write(sg$SNP, file = "Sig.d.txt")
```

### 6. 提取区域/位点基因型（喂给 LD block）

```bash
# SNP 列表 → 子集 bfile
plink --bfile cohort.3.snp --extract Sig.d.txt --make-bed --out Sig.d
plink --bfile cohort.3.snp --extract snp.txt --recode --out snp

# 剔除位点
plink --bfile cohort.3.snp --exclude nos.txt --make-bed --out bf1

# 提取指定区域的 VCF（区域 LD 用）
vcftools --vcf LDdecay.vcf --chr 7 --from-bp 28253019 --to-bp 30637296 \
         --recode --out ABFT.block.region
```

## 脚本模板（SESSION-SAFE）

```bash
#!/bin/bash
set -euo pipefail
RUN_DIR="..."
LOG_FILE="${RUN_DIR}/logs/04_visualize.log"
DONE_FILE="${RUN_DIR}/.done/04_visualize.done"
PID_FILE="${RUN_DIR}/.pid/04_visualize.pid"
mkdir -p "$(dirname "${LOG_FILE}")" "$(dirname "${DONE_FILE}")" "$(dirname "${PID_FILE}")"
echo $$ > "${PID_FILE}"; trap 'rm -f "${PID_FILE}"' EXIT

# 上游产出的显著位点清单与区域基因型拷入 out/
cp "${SIG_TXT}" "${RUN_DIR}/out/" 2>/dev/null || true
cp *.ped "${RUN_DIR}/out/" 2>/dev/null || true

date > "${DONE_FILE}"
```

## ⚠️ 错误预防清单

| # | 检查项 | 违反后果 |
|---|--------|---------|
| 1 | **取 P 列别错** — rMVP CSV 第 8 列是 P；GEMMA 是 `p_wald` 等 | 把效应量当 P 画图 |
| 2 | **多性状 cbind 需保持 SNP/CHR/POS 列对齐** | 图线条错位 |
| 3 | **染色体标签** — 按物种实际核型设置；有非编号染色体先处理 | `chr.labels` 长度不匹配报错 |
| 4 | **阈值要用校正值** — 全基因组显著线是 0.05/n，不是 0.05 | 假阳性显著 |
| 5 | **`plot.type="c"` 对 >3 性状需重排列（重排为 SNP CHR POS + 各性状 P）** | 圈图渲染失败 |
| 6 | **`file.output=FALSE` 时图不进文件** — 不加 `memo` 易丢图 | 产出文件缺失 |
| 7 | **plink `--extract` 文本是每行一个 SNP 名** | 误排成表导致全删 |

## 输出汇报

```
✅ 可视化完成
图:      Manhattan / QQ / density / circular（pdf+jpg）
显著线:  3.92e-9（严格） & 3.92e-7（建议）
显著位点:  {N_SIG}，候选区域 {CHR}:{START}-{END}，基因型已导出
```

## 相关 Skill

- `gwas-gemma` / `gwas-mvp` — 上游关联结果
- `gwas-ld-block` — 下一阶段：区域 LD block + 候选基因（用本阶段显著位点/基因型）
