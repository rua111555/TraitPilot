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
FAILED_FILE="${RUN_DIR}/.failed/00.5_qc_filter.failed"
PID_FILE="${RUN_DIR}/.pid/00.5_qc_filter.pid"
mkdir -p "$(dirname "${LOG_FILE}")" "$(dirname "${DONE_FILE}")" "$(dirname "${PID_FILE}")"
# ⚠️ 开工先清掉本阶段自己的陈旧 .done：重跑一个曾成功过的阶段时，上一轮的 .done
#    与本轮存活的 pid 并存，状态判定只看到 .done 就报「已完成」，用户据此推进下一
#    阶段，而本阶段还在写同一个产物。（只清本阶段自己的，别抹掉其它阶段。）
rm -f "${DONE_FILE}"
echo $$ > "${PID_FILE}"
trap 'rm -f "${PID_FILE}"' EXIT

PLINK="$(command -v plink || { echo "✗ 找不到 plink —— 请激活 conda 环境 Agent" >&2; exit 1; })"

# 输入/输出占位（由 gwas-pipeline 生成脚本时替换为实际值）
PREFIX="..."                    # 上游（S0）产出的 PLINK 前缀，不带扩展名
PED_NAME="${PREFIX}"            # 无扩展名；--file 会找 ${PED_NAME}.ped/.map
OUT_BFILE="${PREFIX}.wy1.snp"   # 过滤后的 bfile（下游 --bfile 的输入）
PHENO_KEEP=""                   # S0.2 对齐产出的 keep.txt；**没跑对齐就留空**（默认空）

# 过滤阈值：**同一份声明**既喂给 plink，也喂给门禁 —— 门禁拿它证明参数真的生效。
GENO=0.2; MAF=0.01; HWE=0.000001

# ─── 过滤前 QC（看分布、选阈值）───
${PLINK} --file "${PED_NAME}" --missing --freq --out "${PREFIX}.pre"

# ─── 过滤 ───
# ⚠️ 有 keep.txt 就先 `--keep` 把分析集固定下来：S0.2 对齐决定"谁有表型"，
#    后面所有阶段都只在这批人上做。没有 keep.txt 时保持原行为（不做样本子集）。
if [ -n "${PHENO_KEEP}" ] && [ -f "${PHENO_KEEP}" ]; then
    ${PLINK} --file "${PED_NAME}" --keep "${PHENO_KEEP}" \
        -geno "${GENO}" -maf "${MAF}" -hwe "${HWE}" \
        --make-bed --out "${OUT_BFILE}"
else
    ${PLINK} --file "${PED_NAME}" -geno "${GENO}" -maf "${MAF}" -hwe "${HWE}" \
        --make-bed --out "${OUT_BFILE}"
fi

# ─── 过滤后 QC（门禁的判据读它）───
# ⚠️ 这一段**不可省**。日志里那句 "N variants remaining" 是 plink 的**自报**：参数
#    没传进去时它照样打印一个数字，看起来完全正常。门禁要的是过滤后**自己的实测值**
#    （.frq 的 min(MAF)、.lmiss 的 max(F_MISS)、.hwe 的 min(P)）—— 只有它能证明
#    -maf/-geno/-hwe 真的生效了。一趟跑三个分析，成本约一遍 .bed 扫描。
QC_PREFIX="${OUT_BFILE}.qc"
${PLINK} --bfile "${OUT_BFILE}" --freq --missing --hardy --out "${QC_PREFIX}"

# ─── 阶段收尾：先校验，再决定写哪个标记 ───
# 定位校验器：由 gwas-pipeline 生成脚本时写进 run.params.json 的 verifier_path
#（**绝对路径** —— 本脚本位于 {RUN_DIR}/scripts/，与仓库无固定相对关系）。
# ⚠️ 末尾 `|| true` 不可省：赋值语句在 `set -e` 下会因命令替换失败当场退出，
#    下面的兜底就永远到不了（磁盘上既无 .done 也无 .failed）。
VERIFIER="$(python -c 'import json,sys; print(json.load(open(sys.argv[1])).get("verifier_path",""))' "${RUN_DIR}/run.params.json" 2>/dev/null || true)"
if [ -z "${VERIFIER}" ] || [ ! -f "${VERIFIER}" ]; then
    mkdir -p "$(dirname "${FAILED_FILE}")"
    { echo "stage=00.5_qc_filter"; echo "time=$(date '+%F %T')"
      echo "reason=校验器未指定或不可读：'${VERIFIER}'（环境问题，非数据问题）"
      echo "  查 ${RUN_DIR}/run.params.json 的 verifier_path：必须是非空**绝对**路径，且文件还在"
    } > "${FAILED_FILE}"
    echo "[FATAL] 定位不到校验器: '${VERIFIER}'" >&2
    exit 1
fi

# 删掉旧报告：跑完后它只要存在，就必然是本轮的产物（否则会把上一轮的报告当成本轮结论）
rm -f "${RUN_DIR}/.verify/00.5_qc_filter.json"
if ! python "${VERIFIER}" \
        --stage 00.5_qc_filter \
        --run-dir "${RUN_DIR}" \
        --prefix "${OUT_BFILE}" \
        --input-bfile "${PED_NAME}" \
        --qc-prefix "${QC_PREFIX}" \
        --maf "${MAF}" --geno "${GENO}" --hwe "${HWE}" \
        --keep-file "${PHENO_KEEP:-}"; then
    mkdir -p "$(dirname "${FAILED_FILE}")"
    {
        echo "stage=00.5_qc_filter"; echo "time=$(date '+%F %T')"
        rep="${RUN_DIR}/.verify/00.5_qc_filter.json"
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
rm -f "${RUN_DIR}/.failed/00.5_qc_filter.failed"   # 清掉上一轮的陈旧失败标记
echo "[DONE] 00.5_qc_filter 校验通过"
```

## 阶段结果校验（写 `.done` 前的门禁）

收尾段的 `gwas_verify.py --stage 00.5_qc_filter` 判的是**过滤后的结果值**：

| 判据 | 级别 | 抓什么 |
|---|---|---|
| 输出 bfile 三件套 + `.bed` 字节数 == `3 + n_var × ⌈n_ind/4⌉` | hard | 写了一半的 bfile |
| 位点数 `0 < out ≤ in` | hard | 位点变多（输出不是这份输入的过滤结果）/ 全部滤光 |
| 样本集 == 输入（除非参数显式声明 `keep`/`remove`） | hard | **静默丢样本** —— 下游关联少人，没有任何环节会报错 |
| 过滤后 `.frq` 行数 == 位点数，且 `min(MAF) ≥ -maf` | hard | `-maf` 没生效 / `.frq` 是过滤前的陈旧产物 |
| 过滤后 `.lmiss` 的 `max(F_MISS) ≤ -geno` | hard | `-geno` 没生效 |
| 过滤后 `.hwe`（只取 `TEST=ALL*`）的 `min(P) ≥ -hwe` | hard | `-hwe` 没生效，或方向写反 |
| 位点保留率 | warn | 过滤阈值是否过严（用户的判断，不是门禁的判断） |
| `--min-snps` / `--min-samples` | 声明才 hard | 规模不达预期 |

> **为什么必须读过滤后自己的 `.frq/.lmiss/.hwe`**：plink 日志里的 "N variants remaining"
> 是**自报**，参数没传进去时它照样打印一个数。门禁要的是实测值 —— 这与 WGS 侧
> "判据读本体、不读派生数据"是同一条原则。

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
| 9 | **写 `.done` 前必须过阶段门禁**（`gwas_verify.py --stage 00.5_qc_filter`）：未过则写 `.failed` + `exit 1`，**不得**无条件写 `.done` | 过滤没生效/滤光/丢样本，却一路传到关联结果 |
| 10 | **过滤后 QC 必须留 `.frq/.lmiss/.hwe` 三份**（`--freq --missing --hardy` 一趟出） | 门禁没有实测值可读 → fail-closed 拦下（这是刻意的，不是误报） |

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
