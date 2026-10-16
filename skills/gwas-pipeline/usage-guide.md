---
name: gwas-pipeline
usage: |
  全基因组关联分析（GWAS）编排 skill 的使用指南。包含一次典型跑法的端到端示例、
  每个阶段的输入前置条件、常见配置调整，以及运行前后的检查清单。
---

# GWAS Pipeline — Usage Guide

## 典型调用

```bash
/gwas-pipeline --vcf COHORT.new.snp.filter.vcf.gz \
               --prefix cohort \
               --pheno imf_phenotype_gwas_plink.txt \
               --covar c.txt
```

LLM 接到该请求后逐阶段执行，每阶段：
1. 调用对应子 skill（读其 SKILL.md）
2. 依据 `run.params.json` 生成该阶段脚本
3. 展示给用户→ 审批 → 后台执行 → 查 `.done` → 自动推进

## 输入前置条件

| 输入 | 说明 | 缺省行为 |
|------|------|---------|
| 过滤后 VCF | `*.snp.filter.vcf.gz`（来自 WGS 过滤管线） | `--vcf` 必须给 |
| 表型 | `FID IID trait`（可来自 `phenotype-extract` 的 GWAS 格式） | 用 `.fam` 第 6 列 |
| 协变量 | `FID IID 性别…`（无表头） | 无协变量运行 |

## 数据流（阶段依赖链）

```
VCF ─→ S0 format-convert ─→ .ped/.map ─→ S0.5 qc-filter ─→ .bed/.bim/.fam
              │                                      │
              │                                      ├─→ S1 kinship-pca → GRM+PC → (协变量)
              │                                      ├─→ S2 gemma ────────────────→ assoc.txt
              │                                      └─→ S3 mvp ──────────────────→ *.method.csv
                                                                      │
                                                                    S4 visualization → 显著位点/图
                                                                              │
                                                                    S5 ld-block → LD block+候选基因
```

## 每阶段脚本要点速查

| 阶段 | 脚本 | 关键命令 | 输出 |
|------|------|---------|------|
| S0 | `00_format_convert.sh` | `vcftools --gzvcf ... --plink` / `plink --vcf --make-bed` | `.ped/.map` |
| S0.5 | `00.5_qc_filter.sh` | `plink --missing/--freq` + `-geno 0.2 -maf 0.01 -hwe 1e-6 --make-bed` | `.bed` |
| S1 | `01_kinship_pca.sh` | `gcta64 --make-grm` → R 补对称+热图 → `--pca 10` → R 散点 | `.kinship.txt` + `.eigenvec` |
| S2 | `02_gemma.sh` | `gemma -gk 2` → 重命名 `.sXX.txt` → `-k -c -lmm 4` | `output/{p}_GWAS.assoc.txt` |
| S3 | `03_mvp.sh` | `MVP.Data` → `attach.big.matrix` → `MVP(method=c(GLM,MLM,FarmCPU))` | `{trait}.{method}.csv` |
| S4 | `04_visualize.sh` | `CMplot/MVP.Report` + P 阈值过滤 + `plink --extract` | `.pdf/.jpg` + `Sig.*.txt` |
| S5 | `05_ld_block.sh` | `LDBlockShow -InGWAS -InGFF` + `ShowLDSVG` + `Haploview` | `.svg/.png` + Block |

## 常见配置调整

- **过滤条件**：`--filter "geno=0.1 maf=0.05 hwe=1e-6"`（更严格）；小样本 `maf=0.05`
- **显著性阈值**：默认两条线 `c(3.92e-9, 3.92e-7)`；`--sig ` 覆盖
- **候选区域**：`--chr 7 --region 28253019:30637296` 触发 S4 区域提取 + S5 LD block
- **只跑一部分阶段**：如 `--skip-gemma` 只跑 rMVP；或直接调用子 skill（`/gwas-gemma`）

## ⚠️ 运行前/后检查清单

```bash
# 运行前
command -v plink bcftools tabix Rscript gcta64 gemma   # 工具齐备（缺失则提示安装/路径）
Rscript -e 'library(rMVP); library(CMplot)'            # R 包可用

# 每阶段后
ls -l {stage 输出}                                       # 输出存在非空
wc -l < bfile.bim                                       # 位点数符合预期
head -1 output/{PREFIX}_GWAS.assoc.txt                  # GEMMA 结果表头正确
```

## 与 wgs-expressive-pipeline 配合

- 输入 VCF 通常来自 WGS 管线的 S5 过滤产物（`{prefix}.snp.filter.vcf.gz`）
- 表型可由 `phenotype-extract` 预提取（IMF 等），输出格式直接可读
- 两者可串联：`WGS 管线` → `GWAS 管线`（同项目前缀对齐即可）
