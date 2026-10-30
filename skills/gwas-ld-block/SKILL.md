---
name: gwas-ld-block
description: >
  Regional linkage disequilibrium analysis for significant GWAS loci: LDBlockShow
  (region LD blocks, -Region/-InGWAS/-InGFF/-TopSite, ShowLDSVG for SVG rendering)
  and Haploview (ped/info per-chrom, GAB block output) to delimit LD blocks and
  annotate candidate genes. Includes SVG→PNG conversion with ImageMagick convert.
  Stage 5 (final) of the GWAS pipeline. Called by the orchestration
  skill (gwas-pipeline). 当用户提到 LD block、LDBlockShow、Haploview、连锁不平衡、LD区段、
  候选基因、Block、单倍型 — match this skill.
allowed-tools:
  - Read
  - Write
  - Bash
  - Grep
---

# 区域 LD Block + 候选基因分析 — Stage S5

对显著位点所在候选区域做连锁不平衡（LD）Block 分析，定位 LD 区段并采用
GFF 注释候选基因。LDBlockShow 能同时把 GWAS 的 -log10 P 与基因模型叠加到
三角 PD 图上；Haploview 是经典单倍型 Block 方案（GAB 输出）。

**由编排 skill 调用**（`gwas-pipeline` 的 S5，收尾阶段），也可独立使用。

## 输入

| 来源 | 说明 |
|------|------|
| `{bfile}.{bed,bim,fam}` | 过滤后 bfile（`-InPlink`）或区域 VCF（`-InVCF`） |
| `{TRAIT}.pvalue` 文件 | GWAS 结果 `-log10(P)` 表（用于叠加 GWAS 轨道，可选） |
| `*.gff` | 参考基因组注释（`-InGFF`，可选；用与参考基因组同版本的注释文件即可） |
| `{region}.ped/.info` | Haploview 输入（plink `--recode HV` 生成，按染色体拆分） |
| `java` + `Haploview.jar` | Haploview 4.2 运行环境（`java -jar`） |

## 输出

| 文件 | 说明 |
|------|------|
| `{OutPut}.{region}.svg/png/pdf` | LDBlockShow 三角图（可含 GWAS / 基因轨道） |
| `{OutPut}.svg` | ShowLDSVG 精排版 SVG → `convert` 转 PNG |
| `{OutPut}.blocks` / `*.GAB.svg` | Haploview 单倍型 Block 输出 |
| `{OutPut}.LD.png` | Haploview SVG → PNG 转换结果 |

## 核心命令

### 1. LDBlockShow — 基本区域 LD

```bash
# 从过滤后 bfile 出发（推荐，快）
LDBlockShow -InPlink cohort.3.snp -OutPut fm \
    -Region 7:28253019:30637296 -OutPng
# 从 VCF 出发
LDBlockShow -InVCF COHORT.new.snp.filter.vcf.gz -OutPut file \
    -Region 7:28253019:32253019 -OutPdf

# 选显著位点特异的标记子集（-SeleVar 1 = 只保留区域内双等位标记）
LDBlockShow -InPlink ABFT.snp -OutPut ABFT \
    -Region 7:28253019:30637296 -OutPng -SeleVar 2
```

### 2. LDBlockShow — 叠加 GWAS 与基因注释

```bash
LDBlockShow -InPlink ABFT.SNP2 -OutPut ABFT8 \
    -Region 7:30235925:30268212 \
    -InGWAS gwas.pvalue \
    -InGFF GCF_000003025.6_Sscrofa11.1_genomic.gff \
    -OutPng -SeleVar 3 -TopSite
```

- `-InGWAS`：GWAS 结果表，**必须包含** 3 列：`chr pos -log10(P)`（或 `chr pos P`）
- `-InGFF`：注释 `gff`/`gff3` 均可；`-crGene yellow:lightblue:pink:orange` 区分正负链/内含子外显子
- `-TopSite`：标记的**峰顶位置**（如 `7:30253019`），LD 图上以指定 SNP 为中心
- `-SeleVar 3`：选择保留标记的过滤强度（1 最严保留全部，2 中等，3 宽松）

### 3. ShowLDSVG — 出版物级 SVG 排版

```bash
ShowLDSVG -InPreFix ABFT8 -OutPut ABFT8 \
    -InGWAS gwas.pvalue -Cutline 8.4 \
    -InGFF GCF_000003025.6_Sscrofa11.1_genomic.gff \
    -crGene yellow:lightblue:pink:orange \
    -TopSite 7:30253019 -OutPng
```

- `-InPreFix`：上一步 `LDBlockShow` 的 `-OutPut` 前缀
- `-Cutline`：显著性线（`-log10(0.05/n)`，8.4 ≈ 3.92e-9）
- 输出 SVG；`-OutPng` 让程序顺带转 PNG

### 4. （可选）SVG → PNG

```bash
convert -density 100 abft1.hv.LD.SVG abft1.hv.LD.png
# ImageMagick；density 控制分辨率（100~300）
```

### 5. Haploview（经典单倍型 Block，GAB）

```bash
# 区域 VCF → PLINK HV 格式（--recode HV 会按染色体拆分 ped/info）
vcftools --vcf LDdecay.vcf --chr 7 --from-bp 28253019 --to-bp 30637296 \
         --recode --out ABFT.block.region
plink --vcf abft1.vcf --recode HV --out abft1.hv --allow-extra-chr

# Haploview -nogui 跑 Block（GAB=Gabriel 法）
java -jar Haploview.4.2.jar -nogui -skipcheck -memory 1024 \
    -pedfile abft1.hv.chr-7.ped -info abft1.hv.chr-7.info \
    -svg -out abft1.hv \
    -ldcolorscheme DEFAULT -blockoutput GAB
```

- `--recode HV` 生成 `*.chr-7.ped` + `*.chr-7.info`（按染色体）
- GAB = Gabriel 等 2002 法则；`-blockoutput LIST` 输出 block 列表，`GAB` + 显性 D'。
- 结果 `abft1.hv.LD.SVG` → `convert` 转 `abft1.hv.LD.png`

## 脚本模板（SESSION-SAFE）

```bash
#!/bin/bash
set -euo pipefail
RUN_DIR="..."
LOG_FILE="${RUN_DIR}/logs/05_ld_block.log"
DONE_FILE="${RUN_DIR}/.done/05_ld_block.done"
PID_FILE="${RUN_DIR}/.pid/05_ld_block.pid"
mkdir -p "$(dirname "${LOG_FILE}")" "$(dirname "${DONE_FILE}")" "$(dirname "${PID_FILE}")"
# ⚠️ 开工先清掉本阶段自己的陈旧 .done：重跑一个曾成功过的阶段时，上一轮的 .done
#    与本轮存活的 pid 并存，状态判定只看到 .done 就报「已完成」，用户据此推进下一
#    阶段，而本阶段还在写同一个产物。（只清本阶段自己的，别抹掉其它阶段。）
rm -f "${DONE_FILE}"
echo $$ > "${PID_FILE}"; trap 'rm -f "${PID_FILE}"' EXIT

LDBLOCK="$(command -v LDBlockShow || find "$HOME" /opt /usr/local -maxdepth 5 -name LDBlockShow -type f 2>/dev/null | head -1)"
SHOWSVG="$(command -v ShowLDSVG || printf '%s/../../ShowLDSVG' "$(dirname "${LDBLOCK}")")"

# ⚠ LDBlockShow 需新版 libstdc++（详见错误预防 5.5）。仅当系统库过旧时才前插 conda lib，
#   新机器系统库够新则不动 LD_LIBRARY_PATH（避免遮蔽更新的库）：
#   该函数**定义在** skills/_shared/env.sh（本块不自带），故必须显式 source ——
#   直接调用会以 rc=127（`sk_ensure_ld_path: command not found`）死在收尾门禁之前。
#   路径按仓库布局解析：从仓库根执行即为下面这行；脚本在别处跑时改用绝对路径。
source skills/_shared/env.sh
sk_ensure_ld_path

# 输入/输出占位（由 gwas-pipeline 生成脚本时替换为实际值）
BFILE="..."                   # 区域 PLINK 前缀（plink --extract 后的区域 bfile）
OUTPREFIX="..."               # 输出前缀（不带扩展名）
CHR="..."                     # 区域染色体编号（须与 bfile/bim 一致）
START="..."                   # 区域起点（bp）
END="..."                     # 区域终点（bp）
GWAS_PVALUE="..."             # GWAS 结果三列：chr  pos  -log10(p)
GFF="..."                     # 注释 GFF 文件
CUTLINE="..."                 # 显著线（-log10 尺度，如 8.4 ≈ 3.92e-9）
TOPSITE="..."                 # 峰顶位点（须在 -Region 范围内）

# ─── LDBlockShow：区域 + GWAS + GFF ───
"${LDBLOCK}" -InPlink "${BFILE}" -OutPut "${OUTPREFIX}" \
    -Region "${CHR}:${START}:${END}" \
    -InGWAS "${GWAS_PVALUE}" -InGFF "${GFF}" \
    -OutPng -SeleVar 3 -TopSite

# ─── ShowLDSVG 精排 ───
"${SHOWSVG}" -InPreFix "${OUTPREFIX}" -OutPut "${OUTPREFIX}" \
    -InGWAS "${GWAS_PVALUE}" -Cutline "${CUTLINE}" \
    -InGFF "${GFF}" -crGene yellow:lightblue:pink:orange \
    -TopSite "${TOPSITE}" -OutPng

# ─── SVG → PNG ───
convert -density 150 "${OUTPREFIX}.svg" "${OUTPREFIX}.png"

cp *.{png,svg,pdf} "${RUN_DIR}/out/" 2>/dev/null || true
date > "${DONE_FILE}"
```

## ⚠️ 错误预防清单

| # | 检查项 | 违反后果 |
|---|--------|---------|
| 1 | **工具名拼写** — 是 `LDBlockShow` / `ShowLDSVG`，常见转录本里 `LDBlockShow-1.36LDBlockShow` 连写错误 | 命令找不到 |
| 2 | **`-InGWAS` 格式** — 必须 `chr　pos　-log10(p)` 三列；给原始 P 会解析为 log10 出错 | 轨道错乱 |
| 3 | **`-Cutline` 是 -log10 尺度** — `-Cutline 8.4` ≈ 3.92e-9；给 0.05 会画在地板上 | 显著线画错 |
| 4 | **`-TopSite` 中心位点在区域内** — 不在 `-Region` 范围内会警告/空 | 图无中心峰 |
| 5 | **`-Region` 用起点:终点，染色体编号要与 bfile/bim 一致** — 非编号/额外 chr 需 `--allow-extra-chr` | 区域为空 |
| 5.5 | **LDBlockShow/ShowLDSVG 二进制需新版 libstdc++** — 系统 `/usr/lib64` 旧时会报 `GLIBCXX_3.4.29 not found`。**仅在系统库过旧时**用 `sk_ensure_ld_path`（见 `skills/_shared/env.sh`）前插 conda `lib`；新机器系统库够新则**不要**设置 `LD_LIBRARY_PATH`（会遮蔽新库） | 命令无法启动 / 或误遮蔽新库 |
| 5.6 | **候选区域标记太少（<2 个）时报 division by zero / SNPNumber:0** — 随机抽样或低密度区常见；实测 25 个位点窗口成功（SNPNumber≈14），优先选 ≥10 个位点的窗口 | SVG 空白 / `Illegal division by zero` |
| 6 | **Haploview 输入是 per-chrom 文件**（`--recode HV` 拆分）— 直接用总 ped 会失败 | 报 `NO_VARIANTS` 等 |
| 7 | **Haploview 需 `-info` 文件匹配 `-pedfile`** | INFO 错误 |
| 8 | **Haploview 大区域内存** — 窗口 > 百万 bp / 数千标记用 `-memory 2048+` | OOM 崩溃 |
| 9 | **`convert`（ImageMagick）SVG 需依赖 `librsvg`/`inkscape`** — 无则转 PNG 空白 | 输出空白图 |

## 输出汇报

```
✅ LD Block 分析完成
区域:  {CHR}:{START}-{END}
Block: {N_BLOCK} 个 LD 区段；峰顶位点 {TOP_SITE}
候选基因: {GENE_LIST}（对应 GFF 注释）
图:    {OUTPREFIX}.svg/.png
```

## 相关 Skill（收尾）

- `gwas-visualization` — 上一阶段：显著位点/区域基因型输入
- `gwas-pipeline` — 编排层；本阶段是 GWAS 管线最后一步
