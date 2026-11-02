---
name: gwas-pheno-align
usage: |
  GWAS 表型–基因型对齐的使用指南：常见对不上的形态与对应规则、映射表格式、
  各下游工具所需的输出格式、强制确认流程，以及排查方法。
---

# GWAS 表型对齐 — Usage Guide

## 依赖

- csv/tsv/txt：纯标准库；xlsx：`openpyxl`（Agent 环境已含）
- 无本机路径硬编码；任意工作目录可跑；`skills/_shared/verify_core.py` 需随仓库一起存在

## 典型调用

```bash
# 最常见：Excel 表型 + PLINK fam，ID 列叫 Taxa，性状叫 IMF
python3 skills/gwas-pheno-align/scripts/pheno_align.py analyze \
    --pheno "IMF表型.xlsx" --geno runs/gwas_001/gwas_sub.fam \
    --id-col Taxa --traits IMF \
    --normalize trim,fullwidth,casefold \
    --outdir runs/gwas_001/pheno_align
```

跑完先看 `runs/gwas_001/pheno_align/align_report.md`，确认后：

```bash
python .../pheno_align.py approve --outdir runs/gwas_001/pheno_align \
    --note "用户确认：匹配 161/161，et231 无表型，同意取交集"
```

下游开工时：

```bash
python .../pheno_align.py check --outdir runs/gwas_001/pheno_align || exit 1
plink --bfile ... --keep runs/gwas_001/pheno_align/keep.txt --make-bed --out ...
```

## 常见"对不上"的形态

| 现象 | 推荐规则 | 说明 |
|---|---|---|
| 大小写不同（`ET10` vs `et10`） | `casefold` | 默认开启 |
| 前后有空格、全角字符 | `trim,fullwidth` | 默认开启 |
| 有统一前缀/后缀（`ET-001`） | `strip-prefix=ET-` | 碰撞检测会兜底 |
| 数字补零不一致（`1` vs `001`） | `digits-only,pad=3` | **会丢字母**，必须显式声明 |
| VCF 样本名是 `et10_et10`（PLINK recode） | `dedup-suffix` | 只合并 `X_X` 形态 |
| 表型用耳号、基因型用测序编号 | `--map 对应表.xlsx` | 最可信；不要靠猜 |
| Excel 把 `001` 读成 `1` | 无（读入即按字符串） | 本工具默认处理 |
| 同一个个体的多行记录 | `--dup-policy mean/first/last` | 默认拦下，需显式声明 |

## 映射表格式

两列即可（首行可带表头，含 `source/target/表型/基因型/ID` 等字样会自动跳过）：

```csv
source,target
耳号-001,et10
耳号-002,et101_et101
```

目标列支持三种写法：

- `et10`（IID，要求 IID 在基因型里唯一）
- `et10 et10` / `et10_et10` / `et10:et10`（FID + IID）

约束：**来源不能重复、目标不能被两个来源占用** —— 命中即报"对应关系混乱"。

## 输出怎么用

| 下游 | 用哪个文件 | 注意 |
|---|---|---|
| `plink --keep` | `keep.txt` | 交集，`FID IID` |
| GEMMA `-p` | `pheno_gemma_<trait>.txt` | **单列值、按基因型顺序**；历史事故就是喂了三列 |
| GEMMA `-c` | `covar_mvp.txt` 的列（去掉 ID 列，按同顺序） | 协变量缺值默认拦下 |
| rMVP `filePhe` | `pheno_mvp.txt` | `Taxa + 性状`，tab + 表头 |
| 人工核对 | `pheno_aligned.csv`、`mapping_used.tsv` | 每行都有规则 |

## 强制确认流程（不可跳过）

```
analyze → 读 align_report.md → 把摘要给用户 → 用户明确说"继续"
        → approve --note "..." → 下游 check 通过才允许跑
```

- 用户没答复 / 说"再改改" → **不得** approve；改规则或映射后重跑 analyze。
- `approve` 会记录参数与产物的哈希；**产物被改动后旧审批自动失效**，必须重新确认。
- 被 hard 拦下的对齐**不能**审批（`approve` 会拒绝）。

## 排查

| 症状 | 先看 | 常见原因 |
|---|---|---|
| 匹配 0 个 | `unmatched_pheno.tsv` 的 suggestions | ID 列选错 / 两边不是同一批人 / 规则太严 |
| 匹配率低 | `align_report.md` 的规则分布 | 少了 `casefold` / 前缀没去掉 / 需要映射表 |
| 报"归一化碰撞" | `align_report.json` 的 `collision.*` | 用了 `digits-only` 等丢字符规则；收紧规则或改用映射表 |
| 报"一对多" | `ambiguous.tsv` | 同一 IID 在多个 FID 下；改用 `FID_IID` 或映射表 |
| 报"重复" | `align_report.json` 的 `dup.*` | 同一个人多行；确认后加 `--dup-policy` |
| 值域告警 | `align_report.md` | 量纲/录入错误（例如 11.59 落在 0.68~6.06 之外） |

## 真实数据示例（本仓库验证过）

- `IMF表型.xlsx`（161 人）+ `gwas_sub.fam`（162 人）→ 匹配 161，基因型独有 `et231`，
  表型独有 0，需要用户确认。
- `mvp_pheno.tab`（162 行，含 `et231=11.5881`）→ 匹配 162，值域告警 1 条
  （同列真实范围 0.68~6.06）—— 这正是"对齐之后还要看值"的例子。

## 已知边界

- 不生成跨性状的统一 `keep`（本版按性状各自对齐）。
- 值域判定用 3×IQR，不是生物学阈值；需要严格阈值时请在报告上人工确认。
- 模糊匹配只给候选，不自动采用。
