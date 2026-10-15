---
name: gwas-kinship-pca
description: >
  Compute genetic relationship matrix (GRM) with GCTA --make-grm, convert the
  compressed half-matrix to a full symmetric kinship file in R (reshape2 acast),
  draw a kinship heatmap with pheatmap, then run PCA with gcta64 --grm --pca and
  plot PC1 vs PC2. Population structure analysis used as covariates in GWAS.
  Stage 1 of the GWAS pipeline. Called by the orchestration skill (gwas-pipeline)
  and feeds GEMMA (kinship), rMVP (K + PCs). 当用户提到 亲缘关系矩阵、GCTA、GRM、kinship、
  PCA、主成分、群体结构、热图 — match this skill.
allowed-tools:
  - Read
  - Write
  - Bash
  - Grep
---

# GCTA 亲缘关系矩阵 + PCA — Stage S1

用 GCTA 基于过滤后 SNP 计算遗传关系矩阵（GRM），在 R 中转换为全对称亲缘矩阵并画热图；
随后基于 GRM 做主成分分析（PCA），产出群体结构协变量。

**由编排 skill 调用**（`gwas-pipeline` 的 S1），也可独立使用。

## 输入

| 来源 | 说明 |
|------|------|
| `{prefix}.wy1.snp.{bed,bim,fam}` | S0.5 过滤后 bfile |
| R 环境 | `reshape2`、`pheatmap`（`Rscript` 运行） |

## 输出

| 文件 | 说明 |
|------|------|
| `{prefix}.snp.kinship.grm.gz` / `.grm.id` | GCTA 原始 GRM（三列 V1 V2 值） |
| `{prefix}.snp.kinship.txt` | **全对称亲缘矩阵**（行名/列名=个体 ID） |
| `heatmap.kinship.pdf` | 亲缘矩阵热图 + 直方图 |
| `{prefix}.10.pca.eigenvec` / `.eigenval` | 前 10 个主成分 |
| `PCA.{...}.pdf` | PC1 vs PC2 散点图 |

> GRM 同时是 GEMMA `-k`、rMVP `K=` 的输入（GEMMA/rMVP 各自有独立 GRM 计算，
> 见对应 skill）。GCTA GRM 主要服务于 PCA 与群体结构展示。

## 核心命令

### 1. 计算 GRM

```bash
# 默认算法（等位基因计数，牺牲精度换速度的算法 1：--make-grm-alg 1）
gcta64 --bfile et277.3.snp --autosome --make-grm --out et277.3.snp.kinship
gcta64 --bfile et277.3.snp --autosome --make-grm-gz --make-grm-alg 1 --out et277.3.snp.kinship
```

> `--make-grm-weig`（加权 GRM）可替代默认，用于极端不平衡标记群体但更费时。
> `--autosome` 排除性染色体。

### 2. R：半矩阵 → 全对称矩阵

GCTA 输出的 `.grm.gz` 只有下三角（V1,V2,值），需要补全为上三角得到对称矩阵：

```r
library(reshape2)
tmp <- read.table(gzfile("et277.3.snp.kinship.grm.gz"), header = F, stringsAsFactors = F)
ids <- read.table("et277.3.snp.kinship.grm.id", header = F, stringsAsFactors = F)
tmp <- tmp[, c(1, 2, 4)]

# 下三角 → 矩阵（acast drop=F 保证方形）→ 补全上三角
result_matrix <- acast(tmp, V1 ~ V2, value.var = "V4", drop = F)
makeSymm <- function(m) {
  m[upper.tri(m)] <- t(m)[upper.tri(m)]
  return(m)
}
result_full <- makeSymm(result_matrix)
result_df <- as.data.frame(result_full)
row.names(result_df) <- ids$V2
colnames(result_df) <- ids$V2
write.table(result_df, file = "et277.3.snp.kinship.txt",
            row.names = T, col.names = NA, sep = "\t", quote = F)
```

### 3. R：亲缘矩阵热图 + 直方图

```r
library(pheatmap)
kinship <- read.table("et277.3.snp.kinship.txt", header = F, row.names = 1, skip = 1)
colnames(kinship) <- row.names(kinship)
diag(kinship) <- NA                    # 对角线置 NA，热图对角无值
pdf("heatmap.kinship.pdf")
hist_data <- hist(as.matrix(kinship), xlab = "Kinship", col = "red",
                  main = "Histogram of Kinship", freq = F)
color <- colorRampPalette(c("white", "red", "red4"), bias = 0.5)(500)
par(mar = c(10, 10, 10, 10))
pheatmap(kinship, color = color, border_color = F,
         fontsize_row = 0.3, fontsize_col = 0.3,
         cellwidth = 1, cellheight = 1,
         treeheight_col = 20, treeheight_row = 20)
dev.off()
```

### 4. 基于 GRM 的 PCA

```bash
gcta64 --grm et277.3.snp.kinship --pca 10 --out et277.10.pca
# 产出 et277.10.pca.eigenvec（Fid ID PC1..PC10）与 .eigenval
```

### 5. R：PCA 作图

```r
dd <- read.table("et277.10.pca.eigenvec", header = F)
names(dd) <- c("Fid", "ID", "PC1", "PC2")
# 按群体/品种着色（示例：group1 红、group2 蓝；实际按 fam 分组调整）
grp <- c(rep("red", n1), rep("blue", n2))
pdf("PCA.fum.pdf")
plot(dd$PC1, dd$PC2, pch = 19, col = grp,
     main = "PCA", xlab = "pc1", ylab = "pc2")
legend("topright", legend = c("group1", "group2"), pch = 19, col = c("red", "blue"))
dev.off()
```

> 可选方案：`plink --pca`、`EIGENSOFT`、`gcta --pca`。PCs 可作为 rMVP/GEMMA 协变量。

## 脚本模板（SESSION-SAFE）

```bash
#!/bin/bash
set -euo pipefail
RUN_DIR="..."
LOG_FILE="${RUN_DIR}/logs/01_kinship_pca.log"
DONE_FILE="${RUN_DIR}/.done/01_kinship_pca.done"
PID_FILE="${RUN_DIR}/.pid/01_kinship_pca.pid"
mkdir -p "$(dirname "${LOG_FILE}")" "$(dirname "${DONE_FILE}")" "$(dirname "${PID_FILE}")"
echo $$ > "${PID_FILE}"; trap 'rm -f "${PID_FILE}"' EXIT

GCTA="$(command -v gcta64)"             # 独立二进制，按实际路径检测
BFILE="${PREFIX}.wy1.snp"

# ─── GRM ───
${GCTA} --bfile "${BFILE}" --autosome --make-grm --out "${PREFIX}.3.snp.kinship"

# ─── R：对称矩阵 + 热图 ───
Rscript kinship_symm.R "${PREFIX}"
Rscript kinship_heatmap.R "${PREFIX}"

# ─── PCA ───
${GCTA} --grm "${PREFIX}.3.snp.kinship" --pca 10 --out "${PREFIX}.10.pca"
Rscript pca_plot.R "${PREFIX}"

date > "${DONE_FILE}"
```

## ⚠️ 错误预防清单

| # | 检查项 | 违反后果 |
|---|--------|---------|
| 1 | **`--grm` 输入是 GRM 前缀（对应 grm.gz/.id），不是 bfile** | PCA 找不到输入 |
| 2 | **`--make-grm` 先生成，`--pca` 后运行**（PCA 依赖 GRM） | GRM 目录不存在报错 |
| 3 | **`.grm.gz` 是下三角**，必须 acast + makeSymm 补对称 | 下游读取 NaN/不完整 |
| 4 | **`acast(drop = F)`** 保证矩阵方形 | 缺失个体组合导致非方阵报错 |
| 5 | **`read.table(gzfile(...))`** — gz 直接用 `read.table(".grm.gz")` 报 no lines | 读取失败 |
| 6 | **`diag(kinship) <- NA` 且 `pheatmap` 无需对角** — 原始 `kinship.txt` 对角线为基因型自相关，画图前先置 NA | 热图对角一片深色误导 |
| 7 | **heatmap 前 `par(mar=c(10,10,10,10))`** — 大样本后字体挤压 | 标签重叠不可读 |
| 8 | **个体 ID 顺序** — 矩阵行/列必须对应 `.grm.id` 顺序 | 样本对错 |
| 9 | **`--make-grm-gz + --make-grm-alg 1` 是压缩输出** — 需配对使用；缺 `-gz` 出 `.grm`（大）非 `.grm.gz` | R 脚本 `gzfile` 读不了 `.grm` |

## 输出汇报

```
✅ 亲缘矩阵 + PCA 完成
亲缘矩阵:  {PREFIX}.3.snp.kinship.txt（{N}×{N}）
热图:      heatmap.kinship.pdf
PCA:       {PREFIX}.10.pca.eigenvec（前 10 PCs）
群体结构:  PC1..PC{nPC} 作为 GEMMA/MVP 协变量
```

## 相关 Skill

- `gwas-qc-filter` — 上一阶段：过滤 bfile
- `gwas-gemma` — 用本阶段亲缘（或 GEMMA 自带 `-gk 2`）做 LMM
- `gwas-mvp` — rMVP 自带 `K=` + `nPC=`，可用本阶段结果校验
