---
name: gwas-pheno-align
description: >
  Align phenotype samples with genotype samples before GWAS (Stage S0.2). Handles
  user-authored phenotype tables (xlsx/csv/tsv), genotype sample lists (PLINK .fam /
  VCF / bfile), explicit mapping tables, and declared ID-normalization rules. Unmatched
  samples on either side are acceptable and are written to disk; ambiguous correspondence
  (one-to-many, duplicates, normalization collisions) fails closed. After alignment the
  user MUST confirm before the pipeline continues (approve/check with hashed audit).
  Use when the user mentions 表型和基因型对不上、样本对不上、ID 对应关系、表型对齐、样本匹配、
  表型导入、对应文件、merge phenotype genotype, or when a GWAS run needs a phenotype file.
disable-model-invocation: false
allowed-tools:
  - Read
  - Write
  - Bash
  - Grep
---

# 表型–基因型样本对齐 — Stage S0.2

把用户给的表型表（Excel/CSV，列名和个体编号由用户自己定）与基因型样本（`.fam` / VCF /
bfile）对齐起来，产出一份可审计的映射、交集与各下游工具所需格式，**并强制用户确认后
才允许进入下一步**。

**由编排 skill 调用**（`gwas-pipeline` 的 S0.2，位于 S0 之后、S0.5 之前），也可独立使用。

> **依赖与可移植性**：csv/tsv/txt 用纯标准库读；xlsx 需要 `openpyxl`（Agent 环境已含，缺失时
> 会明确提示如何安装/转存，不会静默失败）。脚本不写死任何本机路径，输入输出全由参数传入，
> 可在任意工作目录运行；判据容器来自 `skills/_shared/verify_core.py`（与本仓库其它校验器一致，
> 需在仓库布局内运行）。

## 什么时候用

- 用户给了一份表型表，但里面的个体编号与 `.fam`/VCF 对不上（前缀、大小写、补零、全角、
  空格、`FID_IID`、Excel 把 `001` 读成 `1`……）
- "谁对应谁"只存在于用户口述或一份手工对应表里
- 之前跑过 GWAS，但**样本数对不上**（例如表型 161 人、基因型 162 人）

## 输入

| 来源 | 说明 |
|---|---|
| `--pheno` | 表型表：`.xlsx` / `.csv` / `.tsv` / `.txt`；支持指定 sheet 与表头行；CSV 兼容 UTF-8 与 GBK/GB18030 |
| `--geno` | 基因型样本来源：`.fam`、`.vcf(.gz)`、或 bfile 前缀（推荐 `.fam`，FID/IID 最完整） |
| `--map` | 可选：两列对应表（来源 ID / 目标 ID），支持 xlsx/csv/tsv |
| `--id-col` | 表型里的 ID 列（列名或 1-based 序号）；省略则自动推断并标记 `id_col_inferred` |
| `--traits` / `--covars` | 性状列 / 协变量列（逗号分隔）；性状省略时自动识别数值列 |
| `--normalize` | ID 规范化规则，逗号分隔，见下 |

## 输出（一个对齐目录）

| 文件 | 用途 |
|---|---|
| `keep.txt` | 交集，`FID IID`，喂 `plink --keep` |
| `pheno_mvp.txt` | `Taxa + 性状`，tab + 表头（rMVP 用） |
| `pheno_gemma_<trait>.txt` | **单列值、按基因型顺序**（GEMMA `-p` 用） |
| `covar_mvp.txt` | 协变量（可选） |
| `mapping_used.tsv` | 每条匹配：原始 ID、归一化 ID、FID、IID、用了哪条规则 |
| `unmatched_pheno.tsv` | 表型侧没对上的行 + 失败原因 + **最接近的候选**（只建议，不采用） |
| `unmatched_geno.tsv` | 基因型侧没有表型的样本 |
| `ambiguous.tsv` | 一对多 / 重复 / 碰撞 —— 必须人工处理 |
| `pheno_aligned.csv` | 审计：原始行 + 匹配结果 + 规则 |
| `align.params.json` | 规则、列、输入哈希、时间 —— 可复现的凭证 |
| `align.report.json` / `align_report.md` | 机器可读 / 人读报告 |
| `approval.json` | **用户确认后**才存在（approve 写入） |

## 四条硬规则（2026-11-02 用户裁定）

1. **两边多出来的样本都可以接受**，取交集；未匹配的样本必须**全部落盘**并告知用户。
   但对应关系**不能乱**：一对多、重复、归一化碰撞一律 hard。
2. **重复个体默认拦下**；只有显式 `--dup-policy mean|first|last` 才聚合。
3. **匹配率过低不拦**，但会标记 `needs_confirmation`，必须人工确认。
4. **对齐之后必须用户确认**才能进入下一步：`approve` 写审批凭证，下游用 `check` 校验
   （产物被改过 → 审批自动失效）。

## 分档匹配（每一行都记录）

| 档 | 规则 | 说明 |
|---|---|---|
| 1 | `map` | 显式映射表（最可信） |
| 2 | `exact_iid` / `exact_fid` / `exact_pair` | 原样相等 |
| 3 | `norm_iid` / `norm_pair` | 按声明的规范化规则相等 |
| — | 模糊候选 | **只写进 unmatched 报告，绝不自动采用** |

规范化规则（`--normalize`）：

| 规则 | 作用 | 风险 |
|---|---|---|
| `trim` | 去前后空格 | 无 |
| `fullwidth` | 全角→半角（NFKC） | 无 |
| `casefold` | 统一大小写 | 可能合并大小写不同的两个人（有碰撞检测） |
| `strip-prefix=X` / `strip-suffix=X` | 去固定前后缀 | 可能误删，需碰撞检测 |
| `pad=N` | 纯数字补零到 N 位 | 无 |
| `dedup-suffix` | `et10_et10 → et10`（PLINK recode 后的 VCF 命名） | 有碰撞检测 |
| `digits-only` / `alnum-only` | 只保留数字/字母数字 | **会丢信息，必须显式声明**；碰撞即 hard |

## 标准流程（必须按这个顺序）

```bash
# 1) 对齐（write 产物 + 报告；退出码 0 = 无 hard 失败，仍等待确认）
python3 pheno_align.py analyze \
    --pheno "$PHENO" --geno "$FAM" \
    --id-col Taxa --traits IMF \
    --normalize trim,fullwidth,casefold \
    --outdir "$RUN_DIR/pheno_align"
```

2. **把 `align_report.md` 的摘要念给用户**：匹配数 / 两侧独有 / 规则分布 / 值域告警 /
   歧义。**必须等用户明确答复**（"继续"/"可以"）—— 不允许自行替用户确认。
3. 用户确认后写审批：

```bash
python3 pheno_align.py approve --outdir "$RUN_DIR/pheno_align" \
    --note "用户确认：匹配 161/161，et231 无表型，同意取交集"
```

4. 下游阶段（S0.5 起）开工前校验审批仍有效：

```bash
python3 pheno_align.py check --outdir "$RUN_DIR/pheno_align" || exit 1
```

> ⚠️ 用户**没有**明确答复、或答复是"再改改"时，**不得**调用 `approve`。
> 需要改规则 → 改 `--normalize` / `--map` / `--id-col` 后重跑 `analyze`，
> 旧审批会自动失效（哈希对不上）。

## 门禁清单

**hard（不通过 = 结果不可用）**

- ID 列不存在 / 全空；表型/基因型文件读不到
- 重复个体且未声明 `--dup-policy`
- 一个表型 ID 对到多个基因型个体（`ambiguous.tsv`）
- **归一化碰撞**：两个不同个体被归一成同一个键
- 映射表里来源重复，或两个来源指向同一目标
- 一个都没匹配上（`match.none`）
- 性状列在匹配样本里没有有效数值；常量性状（n≥3 时）
- 协变量缺值（除非 `--fill-covar`）
- 输出与 `keep.txt` 不一致（结构性）

**warn（通过但必须带出）**

- 表型侧 / 基因型侧有未匹配样本（逐条落盘）
- 匹配率低于 `--min-match-rate`（默认 0.8 → `needs_confirmation`）
- 值超出 3×IQR 范围（量纲/录入错误的第一嫌疑）
- 用了会丢字符的规则（digits-only / alnum-only）

## 错误预防清单

| # | 检查项 | 违反后果 |
|---|---|---|
| 1 | **表型文件一律按字符串读**（xlsx 用 `dtype=str`） | Excel 把 `001` 变成 `1`、长数字变科学计数法 → 整批对不上 |
| 2 | **ID 列不要选数值型的行号列**（Excel 常见的自增列） | 匹配率虚高，对到错误的人 |
| 3 | **`--normalize digits-only` 必须配碰撞检测** | `A12`/`B12` 撞成 `12`，两个人合成一个 |
| 4 | **不要为了拉高匹配率而编映射** | 看起来 100% 匹配，实际全是错位 |
| 5 | **同一 IID 出现在多个 FID 下时必须给 `FID_IID` 或映射表** | 只凭 IID 无法确定是哪个人 → 硬拦 |
| 6 | **`keep.txt` 与 GEMMA 的 `-p` 顺序**：GEMMA 按 `.fam` 顺序读值 | 顺序错了 → 表型与基因型错位，结果全废 |
| 7 | **协变量缺值不要静默填 0** | 引入系统性偏差；要么补全，要么显式 `--fill-covar`（均值） |
| 8 | **对齐后必须经用户确认**；产物改动后旧审批自动失效 | 未经确认的分析结果不可追溯 |

## 与 GWAS 管线的关系

```
S0 格式转换 ──→ S0.2 表型对齐（本 skill）──→ S0.5 QC（plink --keep keep.txt）
                                                  └──→ S1…S5 全部在交集上跑
```

- S0.5 及之后每个阶段的汇报都要带出**未匹配样本数**与 `match_rate`；
- 如果 `align.report.json` 的 `needs_confirmation` 为真，编排层必须把它当作"待用户确认"，
  不允许自动推进；
- 多性状各自对齐时会产生不同的交集（本版不做跨性状 `keep_common`）。

## 已知边界

- VCF 作为基因型来源时，样本名只有一列 → 约定 `FID = IID = 样本名`，报告里会标注
  `fid_assumed`；需要完整 FID/IID 时请用 `--geno <bfile 前缀>` 或 `.fam`。
- 值域告警用 3×IQR 做参考，不是临床/生物学阈值；要更严格的判定请给参考区间（后续版本）。
- 模糊候选只做"建议"，不会自动匹配；需要时请用户给映射表。
