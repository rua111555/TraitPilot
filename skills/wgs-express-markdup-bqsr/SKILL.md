---
name: wgs-express-markdup-bqsr
description: >
  Mark PCR and optical duplicates with GATK MarkDuplicates, then perform
  Base Quality Score Recalibration (BQSR) with BaseRecalibrator and ApplyBQSR.
  Per-sample processing. Called by the orchestration skill (wgs-express-pipeline)
  during Stage 2.
allowed-tools:
  - Read
  - Write
  - Bash
  - Grep
---

# MarkDuplicates + BQSR — Stage 2

标记 PCR/光学重复（MarkDuplicates）并进行碱基质量校正（BaseRecalibrator + ApplyBQSR）。

**由编排 skill 调用**，不直接暴露给用户。

## 输入

- `run.params.json` — 全部参数
- `{RUN_DIR}/out/{sample_name}.sorted.bam` — Stage 1 生成的 BAM（带有 `@RG` 标签）

## 输出

每个样本输出：
- `{RUN_DIR}/out/{sample_name}.sorted.markdup.bam` — 标记重复后的 BAM
- `{RUN_DIR}/out/{sample_name}.sorted.markdup.bai` — 索引（**可选**，见下）
- `{RUN_DIR}/out/{sample_name}.sorted.markdup.BQSR.bam` — BQSR 校正后的 BAM
- `{RUN_DIR}/out/{sample_name}.sorted.markdup.BQSR.bai` — 索引（ApplyBQSR 自动产出，**Stage 3 必需**）
- `{RUN_DIR}/out/{sample_name}.recal.table` — BQSR 校正表

> ⚠️ **索引命名不止一种，别只找一个**。合法命名有四种，samtools 与 GATK **四种都认**
> （实测 `samtools idxstats` 对四种均走快路径、`gatk HaplotypeCaller` 均 rc=0）：
> `foo.bam.bai`（`samtools index foo.bam`）、`foo.bai`（GATK/Picard `--CREATE_INDEX true`，
> 把 `.bam` 整个换成 `.bai`）、`foo.bam.csi` 与 `foo.csi`（`samtools index -c`，CSI 格式）。
> 校验器四种都查。早期文档与校验器只认 `.bam.bai`，于是 GATK 真实产出的索引被当成"缺失"
> —— 健康数据被拦（已实测复现并修正）。
>
> ⚠️ **这两个索引的产出方不同，判级也不同**（别照抄成一档）：
> - **BQSR BAM 索引**：ApplyBQSR 的 `create-output-bam-index` **默认 true**，自动产出。
>   Stage 3 的 HaplotypeCaller **没有它直接失败**（实测 `A USER ERROR ... some input
>   files are not indexed`，rc=2）→ 缺一个即**硬性失败**。
> - **markdup BAM 索引**：`gatk MarkDuplicates` 的 `CREATE_INDEX` **默认 false**，
>   上面那条标准命令本就不产出；下游 BaseRecalibrator / ApplyBQSR 读该 BAM
>   **不需要**索引 → 故只**告警**，不拦。需要时显式加 `--CREATE_INDEX true`。

## 脚本编制要点

### SESSION-SAFE 样板（同 Stage 1）

包含完整的 `.done` / `.pid` / 日志配置。

### 注意：MarkDuplicates 和 BQSR 都是单线程

GATK 4.3 非 Spark 版中这两个工具**都是单线程的**。加速方式：
- **样本间并行**：多个样本同时跑
- **给足内存**：`-Xmx`（从 `run.params.json` 的 `java_xmx` 读取）
- **使用 SSD/tmpfs** 作为临时目录

### 工具路径（运行时探测，勿写死本机路径）

阶段脚本经 `conda run -n Agent bash` 执行，工具已在 PATH 中：

```bash
resolve_tool() { command -v "$1" || { echo "✗ 找不到 $1 —— 请激活 conda 环境 Agent" >&2; exit 1; }; }
GATK="$(resolve_tool gatk)"
SAMTOOLS="$(resolve_tool samtools)"
```

### MarkDuplicates 标准用法

```bash
# ✅ 正确 — 使用 --java-options（不是 -J）
gatk MarkDuplicates --java-options "-Xmx${JAVA_XMX}" \
  -I "${INPUT_BAM}" \
  -O "${MARKDUP_BAM}" \
  -M "${MARKDUP_METRICS}" \
  --TMP_DIR "${TMP_DIR}" \
  --REMOVE_DUPLICATES false

# 验证 @RG 存在
samtools view -H "${MARKDUP_BAM}" | grep '^@RG'
```

### BQSR 标准用法

```bash
# Step 1: BaseRecalibrator — 建立校正模型
gatk BaseRecalibrator --java-options "-Xmx${JAVA_XMX}" \
  -R "${REF_FASTA}" \
  -I "${MARKDUP_BAM}" \
  --known-sites "${KNOWN_SNPS}" \
  --known-sites "${KNOWN_INDELS}" \
  -O "${RECAL_TABLE}"

# Step 2: ApplyBQSR — 应用校正
gatk ApplyBQSR --java-options "-Xmx${JAVA_XMX}" \
  -R "${REF_FASTA}" \
  -I "${MARKDUP_BAM}" \
  --bqsr-recal-file "${RECAL_TABLE}" \
  -O "${BQSR_BAM}"
```

### 样本并行模式

```bash
MAX_CONCURRENT=3   # 从 parallelism.parallel_samples 读取
pids=()
FAILED=0           # ⚠️ 必须在并发池之前初始化：累计失败数时若未定义，
                   #    在 `set -u` 下会直接崩（unbound variable）

for i in "${!samples[@]}"; do
    sample="${samples[$i]}"
    
    # resume 检查
    if [ -f "${BQSR_BAM}" ]; then
        log_info "BQSR BAM exists, skipping: ${BQSR_BAM}"
        continue
    fi
    
    (
        set -euo pipefail
        # MarkDuplicates
        # BaseRecalibrator
        # ApplyBQSR
    ) 2>&1 | stdbuf -oL tee -a "${LOG_FILE}" &
    pids+=($!)
    
    # 控制并发...
done
wait
```

## 收尾：结果校验门禁（**必须先过校验才写 `.done`**）

```bash
# ─── 日志函数（本块自足：用了就必须定义，别指望别处）───
# ⚠️ 本段不可省。本块调用了 log_error（以及日志类的其它函数），但**早期版本全文档
#    都没有定义** —— 六个阶段 skill 里只有 align-sort / fastp-qc 定义了它们。
#    LLM 照抄本块生成的脚本，首次调用处当场 `log_error: command not found`（rc=127）。
#    要命的是这些调用**全在失败分支里**：本该"写 .failed 并打印原因"的路径反过来
#    崩在写标记之前，磁盘上 .done/.failed 都不存在，编排层四态判定归为"已中断"、
#    引用户去查一个不存在的进程 —— 真实原因只是这段函数没定义。
#    与 align-sort / fastp-qc 的写法保持一致。
log_info()  { echo "[$(date '+%Y-%m-%d %H:%M:%S')] [INFO]  $*"; }
log_done()  { echo "[$(date '+%Y-%m-%d %H:%M:%S')] [DONE]  $*"; }
log_error() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] [ERROR] $*"; }
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
    { echo "stage=02_markdup_bqsr"; echo "time=$(date '+%F %T')"
      echo "reason=校验器未指定或不可读：'${VERIFIER}'"
      echo "这是环境问题，不是数据问题。逐项核对："
      echo "  1) ${RUN_DIR}/run.params.json 是否存在、能否被 python json.load 解析"
      echo "  2) 其中的 verifier_path 是否为**非空**且为**绝对**路径"
      echo "  3) 该路径指向的文件是否还在（仓库被移动过？）"
    } > "${RUN_DIR}/.failed/02_markdup_bqsr.failed"
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
    { echo "stage=02_markdup_bqsr"; echo "time=$(date '+%F %T')"
      echo "reason=run.params.json 的 sample_names 为空，样本守恒判据无法运行"
    } > "${RUN_DIR}/.failed/02_markdup_bqsr.failed"
    exit 1
fi

# 先删掉本阶段的旧报告：跑完后它只要存在，就必然是本轮的产物。
rm -f "${RUN_DIR}/.verify/02_markdup_bqsr.json"
if ! python "${VERIFIER}" \
        --stage 02_markdup_bqsr --run-dir "${RUN_DIR}" --samples "${SAMPLES_CSV}" \
        --prev-reads-file "${RUN_DIR}/.verify/01_bwa_sort.json"; then
    mkdir -p "${RUN_DIR}/.failed"
    { echo "stage=02_markdup_bqsr"; echo "time=$(date '+%F %T')"
      # ⚠️ 只在报告**属于本轮**时才附上：校验器没跑起来时（路径错/python 缺失），
      #    .verify/ 里可能还留着上一次运行的文件，照抄会把排查引向数据而非环境。
      rep="${RUN_DIR}/.verify/02_markdup_bqsr.json"
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
    } > "${RUN_DIR}/.failed/02_markdup_bqsr.failed"
    log_error "结果校验未通过，不写 .done"; exit 1
fi
date > "${DONE_FILE}"
rm -f "${RUN_DIR}/.failed/02_markdup_bqsr.failed"   # 清掉上一轮的陈旧失败标记

```

> 传 `--prev-reads-file` 会**与 Stage 1 做 reads 守恒检查**（判据为 **warn** 级：偏差
> 只告警不拦截）。丢样本这件事由**另一条硬性判据**负责：Stage 1 报告里有、本阶段却没有
> 的样本会被 `reads_conservation_lost` 拦下 —— 守恒循环只比"还在的样本"的数值，
> 整份消失的样本根本不进那个集合，必须靠集合差才能发现。
> 注意 `parabricks` 后端下 fq2bam 已含 markdup 步骤，**markdup BAM 可能本就不单独产出**
> —— 故缺 markdup BAM 只告警。但 **BQSR BAM 是硬性要求**：Stage 3 的输入契约写死了
> `{sample}.sorted.markdup.BQSR.bam`，少一个即不写 `.done`（fq2bam 的产出契约是
> `.sorted.bam`，BQSR 仍由本阶段产出）。

## ⚠️ 错误预防清单

| # | 检查项 | 违反后果 |
|---|--------|---------|
| 1 | **GATK 参数用 `--java-options`** — 不是 `-J` | `-J is not a recognized option` |
| 2 | **GATK 参数名因工具而异** — `--TMP_DIR` / `--tmp-dir` | 参数不被识别 |
| 3 | **验证 BAM 包含 `@RG`** — 如果 Stage 1 未加 RG 则此处报错 | `Number of read groups must be >= 1` |
| 4 | **`known-sites` 参数指定** — BQSR 需要已知多态性位点 | BQSR 无法建立校正模型 |
| 5 | **`compute_backend=parabricks` 时禁用 GATK** — 用 `pbrun markdup/bqsr/applybqsr` | 参数不识别 / GPU 白开 |
| 6 | **pbrun 参数名以 `--help` 为准**（如 `--in-bam`/`--out-recal-file`/`--in-recal-file`） | 参数不被识别 |
| 7 | **Stage 1 已用 fq2bam（4.3.x 默认即 markdup）的样本勿重复 markdup** | 浪费算力；resume 存在性检查跳过 |
| 8 | **索引有四种合法命名，别只找一种** —— `foo.bam.bai`（samtools index）/ `foo.bai`（GATK-Picard `--CREATE_INDEX true`）/ `foo.bam.csi` / `foo.csi`（`samtools index -c`）。校验器四种都认；下游工具/人工核对时也别只找一种 | 误判"缺索引"，健康数据被拦 |
| 9 | **`gatk MarkDuplicates` 的 `CREATE_INDEX` 默认 false** —— 不改就是没索引；而下游 BaseRecalibrator/ApplyBQSR 读该 BAM 并不需要索引，故只告警。**BQSR BAM 的索引则必需**（ApplyBQSR 的 `create-output-bam-index` 默认 true 自动产出；缺了 Stage 3 直接 rc=2） | 索引该有没有/不该有却有 |
| 10 | **`--CREATE_INDEX` 不是 walker 参数** —— 对 ApplyBQSR 传它会报 `CREATE_INDEX is not a recognized option`；walker 用 `--create-output-bam-index` / `--create-output-variant-index`（均默认 true，实测 4.3.0.0） | 参数不被识别 / 以为关掉了索引其实没有 |

### GATK 参数名差异注意

| 参数含义 | MarkDuplicates 写法 | BaseRecalibrator 写法 |
|---------|-------------------|---------------------|
| 临时目录 | `--TMP_DIR` | `--tmp-dir` |
| 参考基因组 | `-R`（通用） | `-R`（通用） |

使用 `gatk <ToolName> --help` 确认具体参数名。

## Parabricks 模式（compute_backend=parabricks）

**`compute_backend == "parabricks"` 时启用**，用 `pbrun markdup` + `pbrun bqsr` + `pbrun applybqsr`
替代 GATK MarkDuplicates / BaseRecalibrator / ApplyBQSR。逐样本绑 1 卡。

> ⚠ **4.3.x 的 `pbrun fq2bam` 默认就已做 MarkDuplicates + BQSR**（见 `parabricks-setup.md` §4.1）。
> 若上游 align-sort 阶段用 fq2bam 且未单独关闭，本阶段 **markdup 应跳过**，仅对增量情况做
> BQSR 校准；否则会重复标记。参数名速查见 `parabricks-setup.md` §4.1。

### 工具路径

```bash
# 统一 PBRUN() docker wrapper（由 wgs-express-params-propose 在脚本头部展开，见其「PBRUN wrapper」）
# 每样本: PBRUN <宿主GPU索引> <pbrun 子命令> ... ；容器内只暴露 1 张卡 → pbrun 用 --num-gpus 1
```

### markdup + BQSR 标准用法

```bash
GPU_IDX=$(( i % GPU_COUNT ))

# 1) 标记重复（对应 GATK MarkDuplicates）
# ⚠️ `--ref` 是**必需项**（不是可选优化）：实测 `pbrun markdup` 不给它时当场
#    `rc=2`、`pbrun: error: the following arguments are required: --ref`、零产物。
#    GATK 的 MarkDuplicates 里 -R 也是必需的，但两边的报错时机不同 —— 这里在
#    argparse 阶段就拒绝，所以脚本在 `set -e` 下会**死在 markdup 这一行**，
#    磁盘上 .done/.failed 都不存在（编排层误判"已中断"）。
PBRUN "${GPU_IDX}" markdup \
    --ref "${REF_FASTA}" \
    --in-bam "${INPUT_BAM}" \
    --out-bam "${MARKDUP_BAM}" \
    --out-duplicate-metrics "${MARKDUP_METRICS}" \
    --num-worker-threads "${per_gpu_cpus}" \
    --num-gpus 1 \
    --tmp-dir "${TMP_DIR}"

# 2) 建立校正模型（对应 BaseRecalibrator）
PBRUN "${GPU_IDX}" bqsr \
    --ref "${REF_FASTA}" \
    --in-bam "${MARKDUP_BAM}" \
    --knownSites "${KNOWN_SNPS}" \
    --knownSites "${KNOWN_INDELS}" \
    --out-recal-file "${RECAL_TABLE}" \
    --num-gpus 1

# 3) 应用校正（对应 ApplyBQSR）
PBRUN "${GPU_IDX}" applybqsr \
    --ref "${REF_FASTA}" \
    --in-bam "${MARKDUP_BAM}" \
    --in-recal-file "${RECAL_TABLE}" \
    --out-bam "${BQSR_BAM}" \
    --num-gpus 1
```

> `--knownSites` 是**驼峰**拼写（非 GATK 的 `--known-sites`，实测 `pbrun bqsr -h`）。
> markdup 的 CPU 线程参数是 `--num-worker-threads`（非 `--num-cpu`）；bqsr/applybqsr
> 的线程参数以 `pbrun <tool> --help` 为准（本机 4.3.x 未显式公开，未设亦可正常跑）。

- 输出命名与 CPU 路径**契约一致**（`.sorted.markdup.bam` / `.BQSR.bam` / `.recal.table`），
  Stage 3 直接消费
- **Stage 1 用 fq2bam 时 4.3.x 已默认做 MarkDuplicates**：本阶段跳过 markdup，仅跑 bqsr+applybqsr
  （resume 存在性检查已涵盖）
- ⚠️ **`pbrun bqsr` 没有 `--out-bam`**（实测 4.3.2-1 的 `pbrun bqsr -h`：只有 `--out-recal-file`，
  没有 `--out-bam`）。故**不存在**"一步完成 bqsr+apply"的写法 —— bqsr 只产出校正表，
  必须再跑一次 `applybqsr`（即上面模板的 2)+3) 两步）。
  （早期本节写着"支持 `--out-bam` 时可一步完成"，是把 `applybqsr` 的参数记到了 `bqsr` 头上。）
- 均单 GPU 任务，并行度 `parallel_samples = min(N, gpus_to_use)`

## 数据库更新

- 启动时：`INSERT INTO sample_steps (step_name='markdup_bqsr', status='running')`
- 完成后：`UPDATE sample_steps SET status='completed' WHERE ...`

## 脚本安全检查清单

1. ✅ `--java-options` 正确使用
2. ✅ 已知 sites 路径存在且正确
3. ✅ RemoveDuplicates 设为 `false`（标准做法：标记而非删除）
4. ✅ 临时目录使用 SSD 或 tmpfs
5. ✅ SESSION-SAFE 保护
6. ✅ **写 `.done` 前过校验门禁**：`stage_verify.py --stage 02_markdup_bqsr`（带 `--prev-reads-file` 做 reads 守恒），未过则写 `.failed` + `exit 1`
7. ✅ resume 模式支持（输出文件存在性检查）
8. ✅ 工具用 `command -v` 运行时探测（**禁止**写死本机 conda 绝对路径）
9. ✅ 按 `compute_backend` 选择：parabricks→pbrun markdup/bqsr/applybqsr；否则→GATK
10. ✅ GPU 路径：每样本绑 1 卡、名称契约一致、`pbrun <tool> --help` 复核参数
