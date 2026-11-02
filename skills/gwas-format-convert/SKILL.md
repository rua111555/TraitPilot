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
vcftools --gzvcf COHORT.new.snp.filter.vcf.gz --plink --out cohort
```

### 2. 直接 VCF → PLINK bed（alternative，可跳过 ped/map 中介）

```bash
plink --vcf COHORT.new.snp.filter.vcf.gz --make-bed --out cohort --allow-extra-chr
```

> 默认路径：`--vcf` 按 `#CHROM` 编号转换；若原始染色体名为 `chr1`/`SSC7` 等带前缀或非常规编号，
> 可能需要 `--allow-extra-chr` 或 `--chr-set` 处理。default 按常见二倍体核型（约 18~20 条常染色体 + X）兼容；**其他物种需按实际核型设 `--chr-set`**。

### 3. 检查样本与位点数量

```bash
wc -l cohort.map cohort.ped   # map 行数=位点数；ped 行数=个体数
```

### 4. 过滤后 bed → VCF（LD 分析用）

```bash
plink --bfile cohort.wy1.snp --recode vcf --out cohort.LDdecay
plink --bfile cohort.fm.snp --recode vcf --out LDdecay
```

## 脚本模板（SESSION-SAFE）

```bash
#!/bin/bash
set -euo pipefail

RUN_DIR="..."                 # 从 gwas-pipeline 生成
LOG_FILE="${RUN_DIR}/logs/00_format_convert.log"
DONE_FILE="${RUN_DIR}/.done/00_format_convert.done"
FAILED_FILE="${RUN_DIR}/.failed/00_format_convert.failed"
PID_FILE="${RUN_DIR}/.pid/00_format_convert.pid"
mkdir -p "$(dirname "${LOG_FILE}")" "$(dirname "${DONE_FILE}")" "$(dirname "${PID_FILE}")"
# ⚠️ 开工先清掉本阶段自己的陈旧 .done：重跑一个曾成功过的阶段时，上一轮的 .done
#    与本轮存活的 pid 并存，状态判定只看到 .done 就报「已完成」，用户据此推进下一
#    阶段，而本阶段还在写同一个产物。（只清本阶段自己的，别抹掉其它阶段。）
rm -f "${DONE_FILE}"
echo $$ > "${PID_FILE}"
trap 'rm -f "${PID_FILE}"' EXIT

# 工具路径（运行时探测，勿写死本机路径；阶段脚本经 `conda run -n Agent` 执行）
PLINK="$(command -v plink || { echo "✗ 找不到 plink —— 请激活 conda 环境 Agent" >&2; exit 1; })"
# ⚠ vcftools 常缺失：优先直接用 plink --vcf --make-bed 替代（见「↑ 直接 VCF → PLINK bed」）
VCFTOOLS="$(command -v vcftools || echo '')"

# 输入/输出前缀（由 gwas-pipeline 生成脚本时替换为实际值）
IN_VCF_GZ="..."               # 原始 VCF(.gz) 路径
PREFIX="..."                  # 输出前缀（不带扩展名），如 cohort

# gz VCF → ped/map
# ⚠️ 两条路径都写全：vcftools 在很多机器上**没有装**（本机实测就没有），
#    只写 `${VCFTOOLS} ...` 时变量为空 → 该行变成 ` --gzvcf ...` → 以 127 失败，
#    而错误发生在**写任何标记之前**，四态判定只会说"已中断"。
if [ -n "${VCFTOOLS}" ]; then
    ${VCFTOOLS} --gzvcf "${IN_VCF_GZ}" --plink --out "${PREFIX}"
else
    echo "[INFO] 未找到 vcftools，改用 plink --vcf --recode 出 ped/map" >&2
    ${PLINK} --vcf "${IN_VCF_GZ}" --recode --out "${PREFIX}"
fi

# 数量核对
n_map=$(wc -l < "${PREFIX}.map"); n_ped=$(wc -l < "${PREFIX}.ped")
echo "SNPs: ${n_map}, individuals: ${n_ped}"

# ─── 阶段收尾：先校验，再决定写哪个标记 ───
# 定位校验器：由 gwas-pipeline 生成脚本时写进 run.params.json 的 verifier_path
#（**绝对路径** —— 本脚本位于 {RUN_DIR}/scripts/，与仓库无固定相对关系）。
# ⚠️ 末尾 `|| true` 不可省：赋值语句在 `set -e` 下会因命令替换失败当场退出，
#    下面的兜底就永远到不了（磁盘上既无 .done 也无 .failed）。
VERIFIER="$(python -c 'import json,sys; print(json.load(open(sys.argv[1])).get("verifier_path",""))' "${RUN_DIR}/run.params.json" 2>/dev/null || true)"
if [ -z "${VERIFIER}" ] || [ ! -f "${VERIFIER}" ]; then
    mkdir -p "$(dirname "${FAILED_FILE}")"
    { echo "stage=00_format_convert"; echo "time=$(date '+%F %T')"
      echo "reason=校验器未指定或不可读：'${VERIFIER}'（环境问题，非数据问题）"
      echo "  查 ${RUN_DIR}/run.params.json 的 verifier_path：必须是非空**绝对**路径，且文件还在"
    } > "${FAILED_FILE}"
    echo "[FATAL] 定位不到校验器: '${VERIFIER}'" >&2
    exit 1
fi

# 删掉旧报告：跑完后它只要存在，就必然是本轮的产物（否则会把上一轮的报告当成本轮结论）
rm -f "${RUN_DIR}/.verify/00_format_convert.json"
if ! python "${VERIFIER}" \
        --stage 00_format_convert \
        --run-dir "${RUN_DIR}" \
        --prefix "${PREFIX}" \
        --vcf "${IN_VCF_GZ}"; then
    mkdir -p "$(dirname "${FAILED_FILE}")"
    {
        echo "stage=00_format_convert"; echo "time=$(date '+%F %T')"
        rep="${RUN_DIR}/.verify/00_format_convert.json"
        if [ -f "${rep}" ]; then
            echo "--- 校验报告（本轮）---"; cat "${rep}"
        else
            echo "--- 没有本轮校验报告 ---"
            echo "失败发生在校验器产出报告之前：多为环境问题（位置/解释器/写权限），不是数据问题。"
        fi
    } > "${FAILED_FILE}"
    echo "[ERROR] 阶段校验未通过，不写 .done（编排层不得推进）" >&2
    exit 1
fi

date > "${DONE_FILE}"
rm -f "${RUN_DIR}/.failed/00_format_convert.failed"   # 清掉上一轮的陈旧失败标记
echo "[DONE] 00_format_convert 校验通过"
```

## 阶段结果校验（写 `.done` 前的门禁）

收尾段的 `gwas_verify.py --stage 00_format_convert` 判的是**结果值**，不是"命令跑完了"：

| 判据 | 级别 | 抓什么 |
|---|---|---|
| `.bed` 字节数 == `3 + n_var × ⌈n_ind/4⌉` | hard | 写了一半 / 被截断的 bfile（不需要任何工具，纯结构等式） |
| `.bed` 魔数 `6c1b01` | hard | 拿错文件 / 不是 PLINK 1 SNP-major |
| `.bim`/`.fam` 均 6 列、位点数 > 0、个体数 > 0 | hard | 空数据集、列错位 |
| FID/IID 不重复 | hard | 下游按 ID 连接时结果串行 |
| `.fam` 样本集 == 输入 VCF 样本表头 | hard | **转格式时丢了样本**（最隐蔽的一类：结果里少人，没人会报错） |
| 位点数 vs VCF 记录数 | warn | 多等位拆分/丢弃；差异要能解释 |
| `--min-snps` / `--min-samples` | 声明才 hard | 规模不达预期 |

> 样本名比对允许几种常见约定（`IID` / `FID` / `FID_IID` / `IID_IID` / `FID:IID`），
> 但要求**在某个约定下集合完全相等** —— 实测本项目的 GWAS 产物里 `.fam` 是
> `FID=IID=et10`、VCF 表头是 `et10_et10`，只比 IID 会把 162 个样本全判成"丢了"。

## ⚠️ 错误预防清单

| # | 检查项 | 违反后果 |
|---|--------|---------|
| 1 | **gzip VCF 用 `--gzvcf`**（`--vcf` 读不了 `.gz`） | vcftools 报错 / 读到空文件 |
| 2 | **vcftools 对 `--vcf` 中的多等位/非 SNP 位点会报错** — 输入应是过滤后的 SNP-only VCF | 转换中断 |
| 3 | **`wc -l` 必做** — 确认位点数/个体数与预期一致再进入下游 | 空数据集跑完整个管线才发现 |
| 4 | **`--allow-extra-chr`** — 非标准染色体名（`chr7`/`SSC7` / X/Y） | plink 拒绝或丢弃染色体 |
| 5 | **工具路径** — plink 用 `command -v` 运行时探测（勿写死本机路径）；vcftools 同样检测 | 子 shell 丢 PATH |
| 6 | **别用 plink2 的 `--export-bed` 参数名混到 plink1.9** | 参数不识别 |
| 7 | **写 `.done` 前必须过阶段门禁**（`gwas_verify.py --stage 00_format_convert`）：未过则写 `.failed` + `exit 1`，**不得**无条件写 `.done` | 中途错误传到下游且无人察觉（本阶段最常见的形态是"样本丢了但结果是好的"） |

## QC 输出汇报

```
✅ 格式转换完成
VCF:      COHORT.new.snp.filter.vcf.gz
SNPs:     {N_MAP}
个体:     {N_PED}
输出:     {PREFIX}.ped/.map → {PREFIX}.bed/.bim/.fam
```

## 相关 Skill

- `gwas-qc-filter` — 下一阶段：缺失率/MAF/HWE 过滤
- `gwas-pipeline` — 编排层
