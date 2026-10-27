---
name: wgs-express-fastp-qc
description: >
  Run all-in-one FASTQ preprocessing with fastp — adapter trimming via
  paired-end overlap analysis, quality/length filtering, 2-color poly-G
  removal, base correction, optional dedup/UMI/merge, and HTML/JSON reports.
  Called by the orchestration skill (wgs-express-pipeline) as Stage 0.5
  before alignment. Can also be used independently for FASTQ QC.
allowed-tools:
  - Read
  - Write
  - Bash
  - Grep
---

# fastp QC — Stage 0.5（可选）

使用 fastp 进行 FASTQ 质控：去接头、质量过滤、poly-G 去除，一条命令完成
原本需要 Cutadapt + Trimmomatic + FastQC 三个工具的流程。

**由编排 skill 调用**，也可独立使用。

**关键词**：`fastp`、`FASTQ 预处理`、`质控`、`去接头`、`adapter trimming`

## 输入

| 来源 | 说明 |
|------|------|
| `run.params.json` | 样本列表、输出目录等 |
| 数据库 `fastq_samples` 表 | 原始 FASTQ 文件路径（`read1_path` / `read2_path`） |

## 输出

每个样本在 `{RUN_DIR}/out/` 下生成：
| 文件 | 说明 |
|------|------|
| `{sample_name}.clean_1.fq.gz` | 清洗后 R1 |
| `{sample_name}.clean_2.fq.gz` | 清洗后 R2 |
| `{sample_name}.fastp.html` | HTML 可视化报告 |
| `{sample_name}.fastp.json` | JSON 报告（供 MultiQC 聚合） |

> 清洗后的 FASTQ 直接作为 Stage 1（BWA）的输入，不写回数据库。
> 数据库中 `fastq_samples` 表的路径始终指向原始 FASTQ 文件。

## 核心原理

1. **双端接头通过 OVERLAP ANALYSIS 自动检测** — R1 与 R2 反向互补比对，
   找到插入片段重叠区域，切除延伸超出部分。无需 adapter 序列即可切到单碱基级别。
   同一机制驱动 `--correction`：用高质量端修正低质量端的碱基。

2. **`--dedup` 是基于 SEQUENCE 身份的 FASTQ 级别去重** — 无法区分 PCR 重复与
   高表达转录本。**WGS 流程禁止使用**，应在比对后用 `MarkDuplicates` 做坐标去重。

3. **Poly-G 剪切对双色仪器（NextSeq/NovaSeq）自动开启** — G 是无信号碱基，
   建议保留开启。

## 工具定位

| 场景 | 使用 fastp? | 替代方案 |
|------|-----------|---------|
| 批量 PE WGS/WES 预处理 | ✅ 默认 | — |
| Small-RNA 3' adapter + 严格长度筛选 | ❌ | cutadapt |
| UMI 分子计数 / ctDNA 一致性序列 | 仅提取 UMI | umi_tools / fgbio |
| 比对后去重 | ❌ | GATK MarkDuplicates（Stage 2） |

## 核心命令

```bash
# ─── 标准 Illumina PE（WGS 默认） ───
fastp -i raw_R1.fq.gz -I raw_R2.fq.gz \
      -o clean_R1.fq.gz -O clean_R2.fq.gz \
      --detect_adapter_for_pe \
      --cut_right --cut_window_size 4 --cut_mean_quality 20 \
      -q 20 -l 36 -w 8 \
      -h sample.html -j sample.json

# ─── NovaSeq / NextSeq（2色，加 poly-G） ───
fastp -i raw_R1.fq.gz -I raw_R2.fq.gz \
      -o clean_R1.fq.gz -O clean_R2.fq.gz \
      --detect_adapter_for_pe --trim_poly_g \
      --cut_right --cut_window_size 4 --cut_mean_quality 20 \
      -q 20 -l 36 -w 8 \
      -h sample.html -j sample.json

# ─── 重叠碱基校正（PE only，可选） ───
fastp -i raw_R1.fq.gz -I raw_R2.fq.gz \
      -o clean_R1.fq.gz -O clean_R2.fq.gz --correction
```

## 脚本模板

### SESSION-SAFE 样板

```bash
#!/bin/bash
set -euo pipefail

# ============================================
# 配置
# ============================================
RUN_DIR="..."           # 从 run.params.json 读取
LOG_FILE="${RUN_DIR}/logs/00_fastp_qc.log"
DONE_FILE="${RUN_DIR}/.done/00_fastp_qc.done"
PID_FILE="${RUN_DIR}/.pid/00_fastp_qc.pid"

mkdir -p "${RUN_DIR}/logs" "${RUN_DIR}/.done" "${RUN_DIR}/.pid" "${RUN_DIR}/out"
echo $$ > "${PID_FILE}"
trap 'rm -f "${PID_FILE}"' EXIT

# ─── 日志函数 ───
log_info()  { echo "[$(date '+%Y-%m-%d %H:%M:%S')] [INFO]  $*"; }
log_error() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] [ERROR] $*"; }
log_done()  { echo "[$(date '+%Y-%m-%d %H:%M:%S')] [DONE]  $*"; }
```

### 工具路径（运行时探测，勿写死本机路径）

```bash
# ⚠️ 不要写 `FASTQ_DIR="${FASTQ_DIR:-$(sk_geno_dir)/fastq}"`（实测 rc=127）：
#    `sk_geno_dir` 是 `skills/_shared/env.sh` 里的 shell **函数**，而本 SKILL.md 并
#    没有一处让你去 source 它（对比 align-sort 明确写了「可 source skills/_shared/env.sh」）。
#    照抄该行时命令替换失败 —— 而 `VAR="$(cmd)"` 在 `set -euo pipefail` 下是**赋值**
#    语句，命令替换失败会**当场退出整个脚本**（不是走后面的兜底），于是脚本停在
#    工具探测段，比收尾门禁早了几百行，磁盘上同样既无 .done 也无 .failed。
#    更坏的是它把「函数不可用」伪装成「回退到默认目录」的语义，排查时最容易被
#    误读成环境变量问题。
#    这里直接把 GENO_DIR 的默认值展开写出来（与 sk_geno_dir 的定义等价），
#    不依赖任何外部函数：
FASTQ_DIR="${FASTQ_DIR:-${GENO_DIR:-$HOME/genomics_data}/fastq}"   # 优先用 run.params.json 的 fastq_dir
OUT_DIR="${RUN_DIR}/out"

FASTP="$(command -v fastp || { echo "✗ 找不到 fastp —— 请激活 conda 环境 Agent" >&2; exit 1; })"
```

### 逐样本处理

```bash
# ─── 从数据库获取样本信息 ───
# samples 由编排层从 --samples 参数传入
# 实际运行时从 run.params.json 读取 sample_numbers 和 sample_names

THREADS=8   # 从 run.params.json 的 parallelism 读取

for i in "${!sample_numbers[@]}"; do
    sn="${sample_numbers[$i]}"
    name="${sample_names[$i]}"

    R1="${FASTQ_DIR}/${sn}/${sn}_1.fq.gz"
    R2="${FASTQ_DIR}/${sn}/${sn}_2.fq.gz"
    O1="${OUT_DIR}/${name}.clean_1.fq.gz"
    O2="${OUT_DIR}/${name}.clean_2.fq.gz"
    HTML="${OUT_DIR}/${name}.fastp.html"
    JSON="${OUT_DIR}/${name}.fastp.json"

    # resume 检查
    # ⚠️ **必须把 JSON 也算进去**，不能只看两个 clean FASTQ（实测 rc 陷阱）：
    #    fastp 的 JSON/HTML 是在**跑完之后**才写的，而 clean FASTQ 是**边跑边写**的。
    #    实测 `kill -9` 掉一个正在跑的 fastp：`clean_1/2.fq.gz` 已落盘（2 MB 的半截
    #    内容）、`fastp.json` 与 `.html` **都不存在**。此时若 resume 只看 O1/O2，
    #    就会跳过该样本；而收尾门禁要求 JSON（`report[s].` 那几条判据要拿它与
    #    FASTQ 实际读数对账）→ **重跑仍跳过、门禁仍失败**，死循环，
    #    用户看到的提示却是"重跑本阶段即可"。
    #    把 JSON 纳入 resume 条件后：缺 JSON ⇒ 该样本重跑 fastp，
    #    clean FASTQ 被完整覆盖重写 —— 正是我们想要的（半截文件本来就该丢弃）。
    #    一般原则：**resume 判存在性的产物集，必须是门禁要求集的超集** ——
    #    否则"产物残缺 + 重跑"会卡死在"跳过 → 门禁不过 → 再跳过"之间。
    if [ -f "${O1}" ] && [ -f "${O2}" ] && [ -f "${JSON}" ]; then
        log_info "Clean FASTQ + report exist, skipping sample ${name}: ${O1}"
        continue
    fi

    log_info "Processing sample ${name} (${sn})..."

    ${FASTP} \
        -i "${R1}" -I "${R2}" \
        -o "${O1}" -O "${O2}" \
        --detect_adapter_for_pe \
        --cut_right --cut_window_size 4 --cut_mean_quality 20 \
        -q 20 -l 36 \
        -w "${THREADS}" \
        -h "${HTML}" -j "${JSON}"

    log_done "Sample ${name} done"
done

# ─── 收尾门禁：校验通过才写 .done ───
# ─── 定位校验器（⚠ 不要写 `conda run -n Agent python`）───
# 本脚本已由 `conda run -n Agent bash` 启动，该进程 PATH 中**没有 conda 可执行文件**
# （只有 <conda>/envs/Agent/bin 与系统目录），嵌套调用会以 127 失败：
# `conda: command not found`。那样数据再健康也会走失败分支，每阶段恒写 .failed、
# 永不写 .done。`python` 就在同一环境的 PATH 上，直接调用即可。
# ⚠️ 末尾的 `|| true` 不可省：`VAR="$(cmd)"` 是**赋值**语句，cmd 失败时
#    `set -e` 会**当场退出整个脚本**（实测 rc=1），下面的 if 兜底永远到不了 ——
#    于是"run.params.json 不存在/是非法 JSON"这两类配置下磁盘上仍无任何标记。
#    加了 `|| true` 才能把空值交给下面的显式判空处理。
VERIFIER="$(python -c 'import json,sys; print(json.load(open(sys.argv[1])).get("verifier_path",""))' "${RUN_DIR}/run.params.json" 2>/dev/null || true)"
# ⚠️ **不要**写成 `: "${VERIFIER:?...}"` —— 变量为空时它立刻 exit 1（set -e），
#    会**绕过下面写 .failed 的兜底**：磁盘上既无 .done 也无 .failed，四态判定归为
#    "已中断"并引用户去查进程，而真实原因只是配置读不到。
#    实测 5 种畸形 run.params.json：「无文件 / 非法 JSON / 缺键 / 空串」这 4 种
#    全都无标记，只有「有 verifier_path 但文件不存在」走到了兜底。故改为显式判空。
if [ -z "${VERIFIER}" ] || [ ! -f "${VERIFIER}" ]; then
    # ⚠️ 这里必须写 .failed：只 exit 1 会让编排层看到「无 .done 也无 .failed」，
    #    于是按"已中断"处理、引用户去查进程 —— 而真实原因是环境问题（编排层没写
    #    verifier_path、仓库被移动、或 RUN_DIR 下 run.params.json 不可读）。
    log_error "定位不到校验器: '${VERIFIER}'（环境问题，非数据问题）"
    mkdir -p "${RUN_DIR}/.failed"
    { echo "stage=00_fastp_qc"; echo "time=$(date '+%F %T')"
      echo "reason=校验器未指定或不可读：'${VERIFIER}'"
      echo "这是环境问题，不是数据问题。逐项核对："
      echo "  1) ${RUN_DIR}/run.params.json 是否存在、能否被 python json.load 解析"
      echo "  2) 其中的 verifier_path 是否为**非空**且为**绝对**路径"
      echo "  3) 该路径指向的文件是否还在（仓库被移动过？）"
    } > "${RUN_DIR}/.failed/00_fastp_qc.failed"
    exit 1
fi
# ─── 样本名单（从 run.params.json 兜底，不依赖脚本里声明了哪个数组名）───
# ⚠️ 模板里样本循环用的数组名不完全统一（`samples` / `sample_number`/ `sample_names`）。
#    若这里只认 `sample_names` 而脚本只声明了 `samples`，在 bash 4.2 + `set -u` 下
#    `${sample_names[*]}` 会直接 `unbound variable` —— 结果是 .done 与 .failed **都不存在**，
#    编排层把它当成"已中断"，而不是"校验没跑"。故此处显式解析，并在为空时报错。
# 同上：加 `|| true`，否则 run.params.json 读不出时 set -e 直接退出、
# 下面的"样本名单为空"分支（会写 .failed）根本到不了。
SAMPLES_CSV="$(python -c 'import json,sys; print(",".join(json.load(open(sys.argv[1])).get("sample_names") or []))' "${RUN_DIR}/run.params.json" 2>/dev/null || true)"
if [ -z "${SAMPLES_CSV}" ]; then
    log_error "样本名单为空（run.params.json 的 sample_names 缺失或为 []）—— 不写 .done"
    mkdir -p "${RUN_DIR}/.failed"
    { echo "stage=00_fastp_qc"; echo "time=$(date '+%F %T')"
      echo "reason=run.params.json 的 sample_names 为空，样本守恒判据无法运行"
    } > "${RUN_DIR}/.failed/00_fastp_qc.failed"
    exit 1
fi

# 先删掉本阶段的旧报告：跑完后它只要存在，就必然是本轮的产物。
rm -f "${RUN_DIR}/.verify/00_fastp_qc.json"
if ! python "${VERIFIER}" \
        --stage 00_fastp_qc --run-dir "${RUN_DIR}" --samples "${SAMPLES_CSV}"; then
    mkdir -p "${RUN_DIR}/.failed"
    { echo "stage=00_fastp_qc"; echo "time=$(date '+%F %T')"
      # ⚠️ 只在报告**属于本轮**时才附上：校验器没跑起来时（路径错/python 缺失），
      #    .verify/ 里可能还留着上一次运行的文件，照抄会把排查引向数据而非环境。
      rep="${RUN_DIR}/.verify/00_fastp_qc.json"
      # 报告在调用校验器**之前**已被删除（见上方 rm -f），所以此处只要存在，
      # 就一定是**本轮**产出的。不存在 ⇔ 校验器没跑起来或没落盘。
      if [ -f "$rep" ]; then
          echo "--- 校验报告（本轮）---"; cat "$rep"
      else
          echo "--- 没有本轮校验报告 ---"
          echo "失败发生在校验器产出报告之前：多为环境问题（${VERIFIER} 不可读 /"
          echo "python 缺失 / RUN_DIR 无写权限），而不是数据问题。"
          echo "先看上面的错误行与 ${RUN_DIR}/nohup.out。"
      fi
    } > "${RUN_DIR}/.failed/00_fastp_qc.failed"
    log_error "结果校验未通过，不写 .done"; exit 1
fi

date > "${DONE_FILE}"
rm -f "${RUN_DIR}/.failed/00_fastp_qc.failed"   # 清掉上一轮的陈旧失败标记
log_done "Stage 0.5 (fastp QC) complete (校验通过)"
```

> **为什么必须清 `.failed`**：不清的话，重跑该阶段并通过后磁盘上会同时存在 `.done` 与
> `.failed`。编排层的四态判定**优先读 `.failed`**，于是把这个已修好并通过的阶段报成
> "校验未通过"，用户被误导去查一个不存在的问题。

> 校验器对每样本判：`.clean_1/2.fq.gz` 存在非空、**gzip 流完整**、**read 数 > 0**、
> **R1/R2 条数相等**，且 `fastp.json` 可解析、其 `read1/read2_after_filtering` 与
> **实际数出的**条数逐端一致。保留率过低（<80%）作为**告警**报出 ——
> 过滤过猛会丢真实信号，但那是可调参的问题，不该拦下整条管线。
>
> ⚠️ **read 数是数 FASTQ 本体得到的，不是从 JSON 读的**。JSON 只是 fastp
> "打算"写多少条：脚本在写盘中途被杀（`kill -9`/磁盘满/NFS 抖动）时，
> 截断的 `.fq.gz` 配一份完好的 JSON，**只读 JSON 的判据会全绿放行**。
> 实测三种形态在旧实现下全部 rc=0：10 条 read 的文件配 90 万条的 JSON、
> R1/R2 条数严重不等、0 条 read 的合法 gzip 空流（`gzip -t` 对它也返回 0）。
> 对账用的 `read1_after_filtering` / `read2_after_filtering` 是 fastp 的顶层键，
> 实测（1.1.0，四种配置）与两个 clean 文件的**实际条数完全相等**。
>
> ⚠️ **`--merge` 会被拦**：合并模式下 per-mate 键消失、多出 `merged_and_filtered`，
> 而合并后的 read 写进 `--merged_out` —— 本管线 Stage 1 的输入契约是 PE 的两份
> clean FASTQ，**没有任何环节消费那个文件**：部分合并时这些 read 会在 Stage 1
> 静默丢失。WGS 管线不要用 `--merge`。

## JSON 报告摘要

```python
import json

with open('sample.fastp.json') as f:
    r = json.load(f)

after = r['summary']['after_filtering']
print(f"reads: {after['total_reads']}, Q30: {after['q30_rate']:.2%}")
print(f"dup rate: {r['duplication']['rate']:.2%}")  # 诊断用
```

## ⚠️ 错误预防清单

| # | 检查项 | 违反后果 |
|---|--------|---------|
| 1 | **WGS 流程禁止 `--dedup`** — fastp 序列去重无法区分生物/技术重复 | 变异检测假阴性 |
| 2 | **RNA-seq 禁止 `--dedup`** — 同上的理由 | 表达量严重偏低 |
| 3 | **单端数据必须提供 `--adapter_sequence`** — SE 无 overlap | adapter 残留 |
| 4 | **PE 双端文件顺序正确: `-i R1 -I R2`** | 接头检测和 read 命名错误 |
| 5 | **多色平台显式指定 `--trim_poly_g`** — 自动检测可能漏掉 | poly-G 残留 |
| 6 | **清洗后文件不入数据库** — DB 始终存原始 FASTQ 路径 | 原始数据引用丢失 |

## 脚本安全检查清单（生成脚本前逐条检查）

1. ✅ 不使用 `--dedup`（WGS 流程禁用）
2. ✅ PE 数据已加 `--detect_adapter_for_pe`
3. ✅ 根据平台决定是否加 `--trim_poly_g`
4. ✅ 线程数合理（不超服务器核数）
5. ✅ HTML/JSON 报告已开启（-h -j）
6. ✅ SESSION-SAFE 保护（`.done` + `.pid`）
7. ✅ **写 `.done` 前过校验门禁**：`stage_verify.py --stage 00_fastp_qc`（清洗后 FASTQ 非空、JSON 可解析、reads>0），未过则写 `.failed` + `exit 1`
8. ✅ resume 模式支持（输出文件存在性检查）
9. ✅ 清洗后输出命名与下游 Stage 1 期望的 BWA 输入一致

## 产出汇报

```
✅ fastp QC 完成
样本:   {SAMPLE_COUNT} 个处理
清洗率: {SAMPLE_A}: 180万→175万 (97.2%), Q30: 94.5%
        {SAMPLE_B}: 200万→192万 (96.0%), Q30: 93.8%
报告:   {RUN_DIR}/out/*.fastp.html
聚合:   multiqc {RUN_DIR}/out/ -o multiqc_report/
```
