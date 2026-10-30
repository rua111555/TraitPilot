---
name: gwas-gemma
description: >
  Run single-variant genome-wide association with GEMMA's LMM (-lmm 1 / 4):
  first compute the relatedness matrix with -gk 2 (centered), then associate
  phenotypes against genotypes controlling for kinship + covariates with the
  Wald/LRT/Score test. Parses the output/ directory artifacts (the dot-prefixed
  kinship .sXX.txt needs renaming). Stage 2 of the GWAS pipeline. Called by the
  orchestration skill (gwas-pipeline). 当用户提到 GEMMA、gemma、LMM、混合线性模型、关联分析
  --lmm --gk -k -c — match this skill.
allowed-tools:
  - Read
  - Write
  - Bash
  - Grep
---

# GEMMA LMM — Stage S2

用 GEMMA 做单标记混合线性模型关联分析：先算亲缘矩阵（`-gk 2`），再在模型中
矫正亲缘（`-k`）与固定协变量（`-c`，如性别、PCA）做关联检验。

**由编排 skill 调用**（`gwas-pipeline` 的 S2），也可独立使用。

## 输入

| 来源 | 说明 |
|------|------|
| `{prefix}.snp.{bed,bim,fam}` | S0.5 过滤后 bfile |
| `{prefix}.kin.txt` | 亲缘矩阵（GEMMA `-gk` 产出，见下） |
| `{prefix}.cov.txt` | 协变量文件（FID IID + 各协变量，不带头） |
| 表型 | 表型在 `.fam` 第 6 列，或用 `-p` 指定独立表型文件（FID IID pheno） |

> 表型从体系其他 skill（如 `phenotype-extract` 的 `imf_phenotype_gwas_plink.txt`）
> 或 `phe.txt`（FID IID 株型值）读取；协变量 `c.txt`（FID IID 性别等）读取。

## 输出

| 文件 | 说明 |
|------|------|
| `output/{prefix}.kinship.sXX.txt` | GEMMA 亲缘矩阵（中心化 GRM，`-gk 2` 产出，文件前缀带 `.`） |
| `output/{prefix}_GWAS.assoc.txt` | **关联结果**：chr/rs/ps/n_miss/allele1/allele2/af/beta/se/p_wald/p_lrt/p_score |

> 结果文件在 GEMMA 默认目录 `output/` 下，且以 `.` 开头（隐藏文件）。
> GEMMA `-lmm 4` 同时输出 Wald/LRT/Score 三种检验。

## 核心命令

### 1. 计算亲缘矩阵（centered）

```bash
gemma -bfile cohort.3.snp -gk 2 -o cohort.kinship
# 产出 output/.cohort.kinship.sXX.txt（sXX 为去重后的随机后缀）
mv output/.cohort.kinship.sXX.txt output/cohort.kinship.txt
```

### 2. 关联分析（LMM，Wald 检验为例）

```bash
gemma -bfile cohort.3.snp \
      -k output/cohort.kinship.txt \
      -c cohort.2.snp.txt \
      -lmm 1 \
      -o cohort_GWAS
```

- `-lmm 1` Wald；`2` LRT；`3` Score；`4` 全部三种（建议 `4`，一网打尽）
- `-k` 给定亲缘；`-c` 给协变量；表型默认用 `.fam` 第 6 列，或用 `-p phe.txt`
- `-maf 0.05`、`-miss 0.1` 可在 GEMMA 侧再做一层 QC

### 3. （可选）LOC0 / 分染色体校正亲缘（leave-one-chromosome-out）

大片段选择信号会掩盖同染色体信号时，逐染色体用"排除该染色体"计算的 GRM 跑 LMM：

```bash
# 对每条染色体 ch：
gemma -bfile cohort.3.snp \
      -k output/cohort.kinship.nochr${ch}.grm.txt \
      -lmm 4 -maf 0.01 \
      -o cohort_GWAS_chr${ch}
```

## 关联系数解读

```
CHR  RS             PS          N_MISS  ALLELE1 ALLELE2  AF        BETA     SE       P_WALD
7    0xqzf0002...   30253019    0       A       G        0.2343   0.812    0.131    3.9e-09
```

- `allele1`=效应等位（tested allele）；`beta` 是 `allele1` 的效应量；`af` 为其频率
- `p_wald` / `p_lrt` / `p_score` 三列分别对应三种检验（`-lmm 4` 时列全）

## 脚本模板（SESSION-SAFE）

```bash
#!/bin/bash
set -euo pipefail
RUN_DIR="..."
LOG_FILE="${RUN_DIR}/logs/02_gemma.log"
DONE_FILE="${RUN_DIR}/.done/02_gemma.done"
PID_FILE="${RUN_DIR}/.pid/02_gemma.pid"
mkdir -p "$(dirname "${LOG_FILE}")" "$(dirname "${DONE_FILE}")" "$(dirname "${PID_FILE}")" "${RUN_DIR}/out"
# ⚠️ 开工先清掉本阶段自己的陈旧 .done：重跑一个曾成功过的阶段时，上一轮的 .done
#    与本轮存活的 pid 并存，状态判定只看到 .done 就报「已完成」，用户据此推进下一
#    阶段，而本阶段还在写同一个产物。（只清本阶段自己的，别抹掉其它阶段。）
rm -f "${DONE_FILE}"
echo $$ > "${PID_FILE}"; trap 'rm -f "${PID_FILE}"' EXIT

GEMMA="$(command -v gemma || find / -maxdepth 4 -name 'gemma*' -type f 2>/dev/null | head -1)"

# 输入/输出占位（由 gwas-pipeline 生成脚本时替换为实际值）
PREFIX="..."                  # PLINK 前缀（不带扩展名），与上游阶段一致
COVAR_TXT="..."               # 协变量文件（FID IID + 各列，无表头）
BFILE="${PREFIX}.3.snp"

# ─── 亲缘 ───
${GEMMA} -bfile "${BFILE}" -gk 2 -o "${PREFIX}.kinship"
KFILE=$(ls output/${PREFIX}.kinship.s*.txt 2>/dev/null | head -1 || ls output/.${PREFIX}.kinship.s*.txt | head -1)
mv "${KFILE}" output/${PREFIX}.kinship.txt

# ─── 关联（-lmm 4 全检验） ───
${GEMMA} -bfile "${BFILE}" \
    -k output/${PREFIX}.kinship.txt \
    -c "${COVAR_TXT}" \
    -lmm 4 -maf 0.05 -miss 0.1 \
    -o ${PREFIX}_GWAS

cp output/${PREFIX}_GWAS.assoc.txt "${RUN_DIR}/out/"
date > "${DONE_FILE}"
```

## ⚠️ 错误预防清单

| # | 检查项 | 违反后果 |
|---|--------|---------|
| 1 | **GEMMA 结果在 `output/` 目录 & 前缀带 `.`** — `output/.xxx.sXX.txt` 是隐藏文件，处理前先 `ls -a output` | 找不到亲缘/结果文件 |
| 1.5 | **GEMMA 0.98.5+ 亲缘文件名不带 `.` 前缀** — 形如 `output/{prefix}.kinship.sXX.txt`（`sXX` 为随机后缀），用 `ls output/{prefix}*s*.txt` 取实际文件再重命名 | 依赖转录本的 `mv .xxx.sXX.txt` 写法找不到文件 |
| 2 | **`-gk 2` 生成的 `.sXX.txt` 文件需重命名**（去掉隐藏前缀与随机后缀） | `-k` 无法指到稳定路径 |
| 3 | **`-k` 用 GRM（`-gk`）产物，`-c` 是协变量（FID IID+列）** — 两者是不同文件 | 传错文件报解析错误 |
| 4 | **协变量文件无表头、第一二列是 FID IID** | GEMMA 解析失败或协变量偏移 |
| 5 | **表型默认读 `.fam` 第 6 列**；用 `-p` 时同样 FID IID pheno 无表头 | 表型全 NA / -9 无数 |
| 5.5 | **`-bfile` 的 fam 无表型时必须显式 `-p`（或把表型写入 fam 第 6 列）** — 实测 `plink --make-bed` 生成的 fam 表型列为 `-9`/`No phenotypes present`，GEMMA `-gk` 直接报 `ERROR: Enforce failed for number of analyzed individuals equals 0. in src/param.cpp ProcessCvtPhen`；加 `-p pheno_file`（FID IID value 三列）即解决 | 亲缘/关联一步都跑不了 |
| 6 | **`-lmm` 需先有 `-k`（或 `-gk` 先算）** | `No relatedness matrix` 报错 |
| 7 | **样本数 < 位点数 / 个体极少时到亲缘矩阵奇异** — 加少量 ridge 或检查重复个体 | `Ginv` 求逆失败 |
| 8 | **多等位/异常 chr 名** — GEMMA 按 `.bim` 第 1 列编号，染色体别名需 `--chr-set` 先统一 | 位点被丢 / 编号错误 |

## 输出汇报

```
✅ GEMMA 关联完成
表型:    {PHENO}
方法:    LMM (-lmm {1|4})，亲属+协变量校正
SNPs:    {N_SNP}，P<{SIG_THRESH} 显著位点数: {N_SIG}
结果:    output/{PREFIX}_GWAS.assoc.txt
```

## 相关 Skill

- `gwas-kinship-pca` — GCTA GRM 与群体结构；GEMMA 用自带 `-gk 2`（或 `-k` 传入）
- `gwas-visualization` — 下一阶段：对 `.assoc.txt` 画曼哈顿/QQ
- `gwas-mvp` — 同基因型的多方法关联（GLM/MLM/FarmCPU），结果相互验证
