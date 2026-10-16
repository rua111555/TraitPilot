---
name: gwas-mvp
description: >
  Run the R package rMVP for multi-method GWAS (GLM / MLM / FarmCPU) in one call:
  MVP.Data builds binary genotype/kin/pc maps, MVP() runs the three models on each
  trait with optional kinship (K) and principal-component (nPC) corrections,
  threshold-based P cutoff, and emits per-method per-trait CSV results. Stage 3 of
  the GWAS pipeline. Called by the orchestration skill (gwas-pipeline).
  当用户提到 rMVP、MVP、GLM、MLM、FarmCPU、多方法关联、关联分析 R 包 — match this skill.
allowed-tools:
  - Read
  - Write
  - Bash
  - Grep
---

# rMVP GLM / MLM / FarmCPU — Stage S3

用 `rMVP` 一次跑通用线性模型（GLM）、混合线性模型（MLM）与 FarmCPU 三种关联方法，
输出按方法×表型的 CSV，供 S4 可视化与候选位点提取。

**由编排 skill 调用**（`gwas-pipeline` 的 S3），也可独立使用。

## 输入

| 来源 | 说明 |
|------|------|
| `{prefix}.snp.{bed,bim,fam}` | S0.5 过滤后 bfile |
| `phe.txt` | 表型：col1=个体ID，后续=各性状 |
| `c.txt` | 协变量：col1=个体ID，后续=性别等 |
| R | `rMVP` 及其依赖 `bigmemory`、`CMplot` |

> rMVP 的 `MVP.Data` 会自算亲缘（`fileKin=TRUE`）与 PCA（`filePC=TRUE`），
> 无需外带 GCTA 结果；也可用 `fileKin` / `filePC` 指向已有矩阵文件。

## 输出

| 文件 | 说明 |
|------|------|
| `mvp.fm.*.desc` / `.mvp.fm.*.desc` | bigmemory 映射：`geno` / `kin` / `pc` + `.geno.map` |
| `{phe}.{method}.csv`（如 `Last.FarmCPU.csv` / `IMF.GLM.csv`） | 每表型×方法关联结果（含 `P.value`） |

> 每性状会产出 `{trait}.GLM.csv`、`.MLM.csv`、`.FarmCPU.csv`。
> CSV 列：`SNP CHROM POS 效应列... P.value`（第 8 列即 P 值，S4 取 `c(1,2,3,8)`）。

## 核心流程

### 1. 构建数据（MVP.Data）

```r
library(rMVP)
MVP.Data(fileBed = "et277.3.snp",
         filePhe = NULL,       # 交互选择表型；脚本化请给值 filePhe="phe.txt"
         fileKin = TRUE,       # 计算亲缘矩阵
         filePC  = TRUE,       # 计算主成分
         out     = "mvp.fm")
```

### 2. 挂载数据 + 读入表型/协变量

```r
genotype <- attach.big.matrix("mvp.fm.geno.desc")
phenotype <- read.table("phe.txt", head = TRUE)        # 第一列个体ID
map <- read.table("mvp.fm.geno.map", head = TRUE)      # SNP CHROM POS
Kinship <- attach.big.matrix("mvp.fm.kin.desc")
covar <- read.table("c.txt", head = TRUE)              # 第一列个体ID
Covariates <- model.matrix(~as.factor(sex), data = covar)   # 性别作为固定效应
Covariates_PC <- bigmemory::as.matrix(attach.big.matrix("mvp.fm.pc.desc"))
```

> `model.matrix` 把分类协变量转成 0/1 哑变量；连续协变量直接列即可。

### 3. 单性状多方法关联（MVP）

```r
imMVP <- MVP(
  phe = phenotype,
  geno = genotype,
  map = map,
  K = Kinship,
  CV.GLM    = Covariates,     # GLM 协变量
  CV.MLM    = Covariates,     # MLM 协变量
  CV.FarmCPU = Covariates,    # FarmCPU 协变量
  nPC.GLM = 3, nPC.MLM = 3, nPC.FarmCPU = 3,
  ncpus = 4,                  # 线程数（⚠ 当前 rMVP 版本无 priority 参数，用 ncpus）
  vc.method = "BRENT",        # 方差分量估计
  method.bin = "static",
  threshold = 0.05,           # 显著性（默认 0.05 / nSNP 未知时按 0.05）
  method = c("MLM", "GLM", "FarmCPU")
)
```

### 4. 多性状循环（每性状一遍，避免一次全量内存）

```r
for (trait_col in 2:ncol(phenotype)) {
  imMVP <- MVP(
    phe  = phenotype[, c(1, trait_col)],
    geno = genotype,
    map  = map,
    K    = Kinship,
    CV.GLM = Covariates, CV.MLM = Covariates, CV.FarmCPU = Covariates,
    nPC.GLM = 3, nPC.MLM = 3, nPC.FarmCPU = 3,
    ncpus = 4,
    vc.method = "BRENT",
    maxLoop = 10,
    method.bin = "static",
    threshold = 0.05,
    method = c("GLM", "MLM", "FarmCPU")
  )
  gc()
}
```

> `mvp.fm` 名字出现 `attach.big.matrix` 时需 `.desc` 文件与 `.raw`/`.map` 同目录。

## 脚本模板（SESSION-SAFE）

```bash
#!/bin/bash
set -euo pipefail
RUN_DIR="..."
LOG_FILE="${RUN_DIR}/logs/03_mvp.log"
DONE_FILE="${RUN_DIR}/.done/03_mvp.done"
PID_FILE="${RUN_DIR}/.pid/03_mvp.pid"
mkdir -p "$(dirname "${LOG_FILE}")" "$(dirname "${DONE_FILE}")" "$(dirname "${PID_FILE}")"
echo $$ > "${PID_FILE}"; trap 'rm -f "${PID_FILE}"' EXIT

# R 脚本：MVP.Data → MVP() 循环；见上文「核心流程」
Rscript mvp_assoc.R "${PREFIX}" "${PHE_TXT}" "${C_TXT}"

# 结果聚合：每性状每方法 CSV = {trait}.{method}.csv，统一拷到 out/
cp *.{GLM,MLM,FarmCPU}.csv "${RUN_DIR}/out/" 2>/dev/null || true

date > "${DONE_FILE}"
```

## ⚠️ 错误预防清单

| # | 检查项 | 违反后果 |
|---|--------|---------|
| 1 | **`attach.big.matrix` 需 `.desc` 且与地图文件同目录** | 找不到 big matrix 描述符 |
| 2 | **`phe.txt` / `c.txt` 第一列是样品 ID，与 `.fam` 个体一一对应** | 表型/协变量错配 |
| 3 | **`filePhe = NULL` 会交互式弹窗** — 脚本化必须给 `filePhe="phe.txt"` | nohup/后台跑挂死等待输入 |
| 3.5 | **`filePhe` 表型文件需 tab 分隔 + 表头**（第一列 ID，第二列起性状）— 默认 `sep.phe="\t"`、`header=TRUE`，空格分隔/无表头报 `ERROR: At least 2 columns in the phenotype file should be specified` | MVP.Data 卡在表型读入 |
| 3.6 | **MVP.Data 输出表型文件名是 `{out}.phe`（不是 `.pheno`）** — 挂载时 `read.table("{out}.phe", header=TRUE)` | `cannot open file '{out}.pheno'` |
| 4 | **分类协变量要用 `model.matrix(~as.factor(...))`** — 直接放数值会当连续变量 | 协变量系数错误 |
| 5 | **`vc.method="BRENT"` 于大数据慢** — 数据量大换 `"EMMA"` 或 `"fastGWA"` | 单性状数十分钟~小时 |
| 6 | **`priority` 参数在当前 rMVP 版本不存在** — `MVP()` 报 `unused argument (priority = "speed")`；并发用 `ncpus=`（如 `ncpus=4`） | 脚本一步跑不了 |
| 6.5 | **`ncpus` 默认取全部逻辑核** — 大规模全基因组会 OOM/抢满；显式给 `ncpus=4~16` | 机器被拖垮 / 内存峰值过高 |
| 7 | **多性状一次全算内存翻倍** — 循环跑+`gc()` 更稳 | 内存峰值过高 |
| 8 | **`method.bin="static"` 时阈值默认 0.05** — 显著性应由 `threshold = 0.05/n` 换算或事后用 BH 校正 | 假阳性显著位点 |
| 9 | **R 版本/依赖** — rMVP 依赖强编译包（RcppArmadillo/bigmemory/dplyr），**R ≥ 4.0 才稳**；系统 R 3.6 编译新版 C++ 依赖会失败（`GLIBCXX` / `C++14 required`）— 用 conda R 4.3 环境 `r-gwas`（`conda run -n r-gwas Rscript`，需先补装 conda gxx 工具链） | `library(rMVP)` 失败或安装崩溃 |
| 10 | **结果 CSV 的第 8 列是 P 值** — S4 作图取 `[,c(1,2,3,8)]` | 作图取错列 |

## 输出汇报

```
✅ rMVP 关联完成
表型 × 方法:  {TRAITS} × (GLM / MLM / FarmCPU) = {N} 个结果 CSV
显著位点(0.05/n):  {N_SIG}
结果:  {TRAIT}.{GLM|MLM|FarmCPU}.csv
```

## 相关 Skill

- `gwas-kinship-pca` — rMVP 的 `K=`/`nPC=` 亦可用其 PCA 结果校验
- `gwas-visualization` — 下一阶段：`MVP.Report`/`CMplot` 直接消费结果 CSV
- `gwas-gemma` — 同为 LMM，注意 `pvce` 的 GM 位点差异
