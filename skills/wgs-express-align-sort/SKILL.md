---
name: wgs-express-align-sort
description: >
  Align paired-end FASTQ reads to the reference genome with BWA MEM and sort
  to coordinate-sorted BAM using samtools sort. Per-sample processing with
  NUMA affinity support. Called by the orchestration skill (wgs-express-pipeline)
  during Stage 1.
allowed-tools:
  - Read
  - Write
  - Bash
  - Grep
---

# BWA MEM + samtools sort — Stage 1

将双端 FASTQ 通过 BWA MEM 比对到参考基因组，经 samtools view 转为 BAM，
再用 samtools sort 排序，生成坐标排序的 BAM 文件。

**由编排 skill 调用**，不直接暴露给用户。

## 输入

| 来源 | 说明 |
|------|------|
| `run.params.json` | 所有参数（参考基因组路径、样本列表、线程数等） |
| FASTQ 文件 | 见下方**输入契约**（默认原始；跑了 Stage 0.5 则消费 clean） |

### 输入契约：跟随 `fastq_dir`，两种形态

本阶段消费哪一份 FASTQ **不是固定的**，由 `run.params.json` 的 `fastq_dir` 决定 ——
**默认原始、声明了 clean 就消费 clean**：

| 本次是否跑 Stage 0.5 | `fastq_dir` | 文件形态 |
|:---:|---|---|
| **否**（可选阶段，常规路径） | `<GENO>/fastq` | `{fastq_dir}/{sample_number}/{sample_number}_1.fq.gz`（**原始**） |
| **是** | `{RUN_DIR}/out` | `{fastq_dir}/{sample_name}.clean_1.fq.gz`（fastp 清洗后） |

两组键都要**同时**从 `run.params.json` 读出，不能只读一半 —— 原契约只按
`{sample_number}` 拼路径，跑了 Stage 0.5 时那个路径下的文件**是存在的**（原始
FASTQ 一直在），于是脚本不会报错、只是**静默地消费了错的那一份**：fastp 明确
剔除的 read 又回到 BAM 里（实测 1400 对里 400 对 `-l 36` 丢弃 → BAM 2800 条
而非应得的 2000 条），而 Stage 0.5 与 Stage 1 的门禁**双双放行**。

⚠️ **两侧口径必须一致**：fastp 的输出表（`{sample_name}.clean_1/2.fq.gz`，
`out/` 下）就是这里的第二行。改任一侧的名字或目录，另一侧必须同步改 ——
`tests/test_defect_class_guards.py` 的交接一致防线会报红。

> **与 `reads_anchor` 判据的联动**：本阶段门禁的 `reads_anchor` 拿 Stage 0.5 报告的
> `primary_totals`（clean FASTQ 实测条数）与本阶段 BAM 的 primary 数对账。
> **只有真的消费了 clean FASTQ，两个数才可比** —— 消费原始 FASTQ 时 BAM 必然
> 多出被剔除的那些 read，对账必然失败。故"消费哪一份"与"能不能过门禁"是同一件事：
> 门禁的失败文案里已点出这一可能（"本阶段消费的**不是** Stage 0.5 的产物"）。

### 样板块里的赋值（逐样本循环内，**不可省**）

```bash
    # ⚠️ 这两组都要先赋值再用（早期版本只写 `bwa mem ... "${R1}" "${R2}"`，
    #    而 R1/R2 在**全仓没有任何赋值点** —— 文档自相矛盾：交接处指名的是未赋值
    #    的变量。抽该围栏加 `set -u` 真跑：`line 4: SAMPLE: unbound variable`, rc=1）
    # ⚠️ 路径形态**跟随 fastq_dir**（见「输入契约」）：
    #    · 跑了 Stage 0.5 → fastq_dir={RUN_DIR}/out，用 {sample_name}.clean_1/2.fq.gz
    #    · 未跑 Stage 0.5 → fastq_dir=<GENO>/fastq，用 {sample_number}/{sample_number}_1/2.fq.gz
    #    生成脚本时按本次运行的实际形态**二选一**，不要两组都写（会读到不存在的那组）。
    sample_number="${sample_numbers[$i]}"
    sample_name="${sample_names[$i]}"
    R1="${fastq_dir}/${sample_name}.clean_1.fq.gz"      # ← 跑了 Stage 0.5 的形态
    R2="${fastq_dir}/${sample_name}.clean_2.fq.gz"
    # R1="${fastq_dir}/${sample_number}/${sample_number}_1.fq.gz"   # ← 未跑 Stage 0.5
    # R2="${fastq_dir}/${sample_number}/${sample_number}_2.fq.gz"
    SAMPLE="${sample_name}"                             # 供 RG_STR 用
    SAM_FILE="${RUN_DIR}/out/${sample_name}.sam"
    BAM="${RUN_DIR}/out/${sample_name}.sorted.bam"
```

> ⚠️ 上面两组 `R1/R2` **只能启用一组**（另一组留在注释里作对照）。`fastq_dir`
> 从 `run.params.json` 读出，**不要在脚本里写死 `$GENO_DIR/fastq`** ——
> 那样即便编排层把 `fastq_dir` 指向了 `out/`，脚本仍旧消费原始 FASTQ。

## 输出

每个样本输出：
- `{RUN_DIR}/out/{sample_name}.sorted.bam` — 坐标排序 BAM
- `{RUN_DIR}/out/{sample_name}.sorted.bam.bai` — BAM 索引

## 脚本模板

LLM **必须**依据 `run.params.json` 生成该阶段的 shell 脚本，
**必须**包含以下所有要素：

### SESSION-SAFE 样板

```bash
#!/bin/bash
set -euo pipefail

# ============================================
# 配置
# ============================================
RUN_DIR="..."   # 从 run.params.json 读取
LOG_DIR="${RUN_DIR}/logs"
LOG_FILE="${LOG_DIR}/01_bwa_sort.log"
DONE_DIR="${RUN_DIR}/.done"
DONE_FILE="${DONE_DIR}/01_bwa_sort.done"
PID_DIR="${RUN_DIR}/.pid"
PID_FILE="${PID_DIR}/01_bwa_sort.pid"

mkdir -p "${LOG_DIR}" "${DONE_DIR}" "${PID_DIR}" "${RUN_DIR}/out"
# ⚠️ 开工先清掉**本阶段自己的**陈旧 .done —— 不清的话，重跑期间磁盘上会同时存在
#    「陈旧 .done」与「存活 pid」，而编排层四态判定里 .done 优先于 pid 存活检查
#    （它必须排在 .failed 之后，但 .failed 只在**校验未通过**时存在）。
#    实测：nohup sleep 构造出"运行中"的阶段 + 一个上一轮留下的 .done，场景 B 当场报
#    `✅ 01_bwa_sort 已完成`，而进程还在跑 —— 用户据此以为可以推进下一阶段。
#    清掉后同一 fixture 正确报 `🕐 运行中`。
rm -f "${DONE_FILE}"
echo $$ > "${PID_FILE}"
trap 'rm -f "${PID_FILE}"' EXIT

# ─── 日志函数 ───
log_info()  { echo "[$(date '+%Y-%m-%d %H:%M:%S')] [INFO]  $*"; }
log_step()  { echo "[$(date '+%Y-%m-%d %H:%M:%S')] [STEP]  $*"; }
log_done()  { echo "[$(date '+%Y-%m-%d %H:%M:%S')] [DONE]  $*"; }
log_error() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] [ERROR] $*"; }
```

### 工具路径（⚠️ 运行时探测，禁止写死本机 conda 绝对路径）

阶段脚本经 `conda run -n Agent bash` 执行，工具已在 PATH 中；用 `command -v` 探测即可，
**不要**写 `/home/<用户>/anaconda3/envs/Agent/bin/...`（换机器即失效）。若 `Agent` 环境不在
PATH（直接跑脚本时），可 `source skills/_shared/env.sh` 后用 `sk_tool bwa` 解析。

```bash
# 运行时探测（任一工具缺失即早失败，避免跑到一半才报错）
resolve_tool() { command -v "$1" || { echo "✗ 找不到 $1 —— 请激活 conda 环境 Agent" >&2; exit 1; }; }
BWA="$(resolve_tool bwa)"
SAMTOOLS="$(resolve_tool samtools)"
NUMACTL="$(command -v numactl || echo '')"     # 可选：缺省则不绑 NUMA
```

### NUMA 亲和性配置（可选；核数从 `lscpu`/`numactl -H` 探测，勿写死）

```bash
# 探测本机 NUMA 拓扑（S0 已采集）；无 numactl 或无多 NUMA 节点时 NUMACTL="" 即为直跑
NUMA_NODE0_CPUS="${NUMA_NODE0_CPUS:-}"     # 从 run.params.json.server_detection.numa_node0_cpus 读取
NUMA_NODE1_CPUS="${NUMA_NODE1_CPUS:-}"
assign_numa_cmd() {
    local idx=$1
    [ -z "${NUMACTL}" ] && { echo ""; return; }        # 无 numactl → 不绑
    [ -z "${NUMA_NODE0_CPUS}" ] && { echo ""; return; } # 未探测到 NUMA → 不绑
    if (( idx % 2 == 0 )); then
        echo "${NUMACTL} --physcpubind=${NUMA_NODE0_CPUS}"
    else
        echo "${NUMACTL} --physcpubind=${NUMA_NODE1_CPUS}"
    fi
}
```

### ⚠️ 必须注意的错误预防清单

| # | 检查项 | 违反后果 |
|---|--------|---------|
| 1 | **BWA 输出禁止管道** — 不要用 `bwa ... 2>&1 \| samtools ...` | `samtools sort: failed to read header from "-"`，BAM 为空 |
| 2 | **BWA 必须加 `-R` (Read Group)** — 格式 `@RG\\tID:{sample}\\tSM:{sample}\\tLB:{sample}\\tPL:ILLUMINA` | GATK 后续工具报错 `Number of read groups must be >= 1` |
| 3 | **`compute_backend=parabricks` 时禁用 BWA** — 用 `pbrun fq2bam` | 重复比对、GPU 资源白开 |
| 4 | **fq2bam 输出命名必须符合输出契约**（`{sample}.sorted.bam` + `.bai`） | 下游 Stage 2/3 找不到输入 BAM |
| 5 | **consumer 卡（4090 24GB）超配 OOM** — 报 cuda out of memory | 降低 `parallel_samples` 或该阶段回退 CPU |
| 6 | **`pbrun fq2bam` 必须显式传 `--read-group-sm "${sample_name}"`**（本机实跑复核） | 不传时 fq2bam 自动生成**写死的常量读组** `@RG ID:<prefix>.1 LB:lib1 PL:bar SM:sample` —— 所有样本都是 `SM:sample`。Stage 1 的 `readgroup`（hard）每样本必拦；该 SM 还会继承进 GVCF 样本列，使 Stage 3 的 `samples`/`identity`（hard）连锁失败；全队列同名会让 Stage 4 合并失败。而 parabricks 是 GPU 就绪时的**默认后端**，等于默认路径全阶段不可用 |

### BWA + Read Group 标准写法

```bash
# ✅ 正确方式 — 分两步：BWA 写 SAM 文件，stderr 单独捕获到日志
RG_STR="@RG\\tID:${SAMPLE}\\tSM:${SAMPLE}\\tLB:${SAMPLE}\\tPL:ILLUMINA"

log_info "Starting BWA MEM for sample ${sample_name}..."

bwa mem -t ${BWA_THREADS} -R "${RG_STR}" \
  "${REF_FASTA}" \
  "${R1}" "${R2}" \
  1> "${SAM_FILE}" \
  2> >(while IFS= read -r line; do echo "[$(date)] [BWA] $line"; done >> "${LOG_FILE}")

log_info "BWA MEM done. Sorting with samtools..."

${SAMTOOLS} sort -@ ${SORT_THREADS} -m 2G -o "${BAM}" "${SAM_FILE}"
${SAMTOOLS} index "${BAM}"

rm -f "${SAM_FILE}"
log_done "Sample ${sample_name} BAM generated: ${BAM}"
```

### 样本并行处理

```bash
# ─── 样本伪并行（wait -n 池） ───
MAX_CONCURRENT=3   # 从 run.params.json parallelism.parallel_samples 读取
pids=()
FAILED=0           # ⚠️ 累计失败样本数，供收尾门禁判定

for i in "${!samples[@]}"; do
    sample="${samples[$i]}"
    
    # 检查是否已存在（resume 模式）
    # ⚠️ **必须用 `-s`（非空）而不是 `-f`**：门禁的 `have()` 判"存在"是
    #    `isfile ∧ 大小 > 0`，**0 字节算不存在**。用 `-f` 时，一个 0 字节/被截断的
    #    产物会让 resume 打印 skipping、直接跳到收尾；而收尾门禁照样判 hard FAIL
    #    → **重跑逐字相同、永久卡死**（实测：0 字节 BAM）。resume 判存在性的
    #    严格度必须 **≥** 门禁对该产物的存在判据 —— 否则重跑无法自愈。
    #    （但**内容级**问题——如普通 VCF 冒充 GVCF——`-s` 覆盖不到，那类要靠
    #    门禁的判据拦下后**先清产物再跑**，见门禁失败文案。）
    if [ -s "${BAM}" ]; then
        log_info "BAM exists, skipping sample ${sample}: ${BAM}"
        continue
    fi
    
    NUMA_CMD=$(assign_numa_cmd $i)
    
    (
        set -euo pipefail
        # ... 上述 BWA+sort 流程 ...
    ) &
    pids+=($!)
    
    # 控制并发数
    while [ "${#pids[@]}" -ge "${MAX_CONCURRENT}" ]; do
        for j in "${!pids[@]}"; do
            if ! kill -0 "${pids[$j]}" 2>/dev/null; then
                wait "${pids[$j]}" || { FAILED=$((FAILED+1)); log_error "PID ${pids[$j]} failed"; }
                unset 'pids[$j]'
            fi
        done
        sleep 10
    done
done

# 等待所有后台作业完成
# ⚠️ 不要写 `wait "${pid}" || log_error ...` 然后不管 —— `||` 会把退出码变成 0，
#    后面的 `set -e` 不再触发，脚本照常走到末尾写 .done，编排层于是误判"完成"。
#    必须把失败**累计**下来，交给收尾的校验门禁裁决。
# ⚠️ 展开式 ${pids[@]+"${pids[@]}"} 不可省（实测 rc=1）：上面的并发池用
#    `unset 'pids[$j]'` 摘除已结束的 pid，于是进入这里时**有效元素数 =
#    样本数 % MAX_CONCURRENT**。余数为 0 时数组已空 —— 样本数恰为 MAX_CONCURRENT
#    的整数倍、MAX_CONCURRENT=1、或 --resume 全部跳过（=0 个样本）都会命中，都是
#    常规配置而非边角。bash 4.2 下 `"${pids[@]}"` 对空数组当场报
#    `pids[@]: unbound variable` 并 rc=1 退出，**收尾门禁根本执行不到**：
#    磁盘上既无 .done 也无 .failed，编排层归为"已中断"并引用户去查一个不存在的
#    进程，真实原因只是这一行。`+` 形式在数组为空/未声明时展开为 0 个词，
#    非空时行为与原来完全一致（两者都实测过）。
for pid in ${pids[@]+"${pids[@]}"}; do
    wait "${pid}" || { FAILED=$((FAILED+1)); log_error "Subprocess ${pid} failed"; }
done
[ "${FAILED}" -gt 0 ] && log_error "本阶段有 ${FAILED} 个子进程非零退出（见收尾校验）"
```

### 完成标记（**必须先过结果校验**）

```bash
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
    { echo "stage=01_bwa_sort"; echo "time=$(date '+%F %T')"
      echo "reason=校验器未指定或不可读：'${VERIFIER}'"
      echo "这是环境问题，不是数据问题。逐项核对："
      echo "  1) ${RUN_DIR}/run.params.json 是否存在、能否被 python json.load 解析"
      echo "  2) 其中的 verifier_path 是否为**非空**且为**绝对**路径"
      echo "  3) 该路径指向的文件是否还在（仓库被移动过？）"
    } > "${RUN_DIR}/.failed/01_bwa_sort.failed"
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
    { echo "stage=01_bwa_sort"; echo "time=$(date '+%F %T')"
      echo "reason=run.params.json 的 sample_names 为空，样本守恒判据无法运行"
    } > "${RUN_DIR}/.failed/01_bwa_sort.failed"
    exit 1
fi

# 先删掉本阶段的旧报告：跑完后它只要存在，就必然是本轮的产物。
rm -f "${RUN_DIR}/.verify/01_bwa_sort.json"
# ⚠️ **必须传 --prev-reads-file**（指向 Stage 0.5 的报告），否则 reads_anchor 判据
#    永远登记"未提供基准、未做该判定" —— 那是**纸面具备**：判据在，从不生效。
#    这条判据是**唯一**能发现「合法但更短的 BAM」的（BGZF 块边界截断后贴回 EOF 块：
#    quickcheck / 全量计数 / idxstats / 坐标有序**全部通过**，只有与上游 FASTQ
#    的条数对账能发现它）。若本次未跑 Stage 0.5（可选阶段），该文件不存在，
#    判据会如实登记"没有基准"，属预期而非失败。
PREV_QC="${RUN_DIR}/.verify/00_fastp_qc.json"
if ! python "${VERIFIER}" \
        --stage 01_bwa_sort --run-dir "${RUN_DIR}" --samples "${SAMPLES_CSV}" \
        --prev-reads-file "${PREV_QC}"; then
    mkdir -p "${RUN_DIR}/.failed"
    { echo "stage=01_bwa_sort"; echo "time=$(date '+%F %T')"
      echo "failed_subprocesses=${FAILED}"
      # ⚠️ 只在报告**属于本轮**时才附上：校验器没跑起来时（路径错/python 缺失），
      #    .verify/ 里可能还留着上一次运行的文件，照抄会把排查引向数据而非环境。
      rep="${RUN_DIR}/.verify/01_bwa_sort.json"
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
    } > "${RUN_DIR}/.failed/01_bwa_sort.failed"
    log_error "结果校验未通过，不写 .done"
    exit 1
fi

log_info "All samples completed for Stage 1"
date > "${DONE_FILE}"
rm -f "${RUN_DIR}/.failed/01_bwa_sort.failed"   # 清掉上一轮的陈旧失败标记
log_done "Stage 1 complete (校验通过)"
```

> **为什么必须清 `.failed`**：不清的话，重跑该阶段并通过后，磁盘上同时存在 `.done` 与
> `.failed`。编排层的四态判定**优先读 `.failed`**（那是刻意的 —— 校验未通过时最该被看见），
> 于是会把这个**已经修好并通过**的阶段报成"校验未通过"，用户被误导去查一个不存在的问题。
> 陈旧状态和真实状态必须只留一个。清空动作放在 `exit 1` 之后的成功路径上，位置要对。

> **为什么不能只看 `wait` 的返回值**：即使所有子进程都返回 0，BAM 仍可能是**空的**
> （比对 0 条 read 也可能退出 0）。而 `samtools quickcheck` 对空 BAM 是**放行**的
> （它只验文件结构完整）。只有读 count 才抓得住 —— 校验器的 `read_count` 判据就是干这个的。

## Parabricks 模式（compute_backend=parabricks）

当 `run.params.json` 中 `compute_backend == "parabricks"`（且 `parabricks.smoke_test` 已通过），
本阶段用 `pbrun fq2bam` **一步完成比对 + 坐标排序 + 索引**，替代 BWA+samtools sort。

### 工具路径

```bash
# 统一 PBRUN() docker wrapper（由 wgs-express-params-propose 在脚本头部展开，见其「PBRUN wrapper」）
# 每样本: PBRUN <宿主GPU索引> <pbrun 子命令> ... ；容器内只暴露 1 张卡 → pbrun 用 --num-gpus 1（⚠️ 唯一例外：genotypegvcf 不接受该参数，见 parabricks-setup.md §4.1）
```

### fq2bam 标准用法（PE，逐样本绑 1 卡）

```bash
# i: 样本循环下标; GPU_COUNT / PER_GPU_CPUS: 读自 run.params.json.parabricks
GPU_IDX=$(( i % GPU_COUNT ))

PBRUN "${GPU_IDX}" fq2bam \
    --ref "${REF_FASTA}" \
    --in-fq "${R1}" "${R2}" \
    --out-bam "${BAM}" \
    --read-group-sm "${sample_name}" --read-group-lb "${sample_name}" --read-group-pl ILLUMINA \
    --num-cpu "${per_gpu_cpus}" \
    --num-gpus 1 \
    --low-memory --bwa-nstreams 1 \
    --tmp-dir "${TMP_DIR}"
```

> ⚠ **`--read-group-sm` 是必需项，不是可选优化**（本机实跑 `pbrun fq2bam` 复核）：
> 不传它，fq2bam 会**自动生成一个写死的常量读组** `@RG\tID:<prefix>.1\tLB:lib1\tPL:bar\tSM:sample` ——
> 其中 `SM:sample` 与真实样本号无关，**每个样本都是同一个 `sample`**。
> 后果是连锁的，且全部以"健康数据被拦"的面貌出现：
> · Stage 1 门禁 `bam[s].readgroup` 是 **hard**（要求 `SM == 文件名里的样本号`）→ 每样本必拦；
> · 那个 `SM:sample` 会**继承到下游**：`pbrun haplotypecaller` 从 BAM 读 RG，GVCF 的
>   `#CHROM` 样本列就是 `sample`，于是 Stage 3 的 `gvcf[s].samples` / `gvcf[s].identity`
>   两条 hard 判据也拦（实测 GVCF 内部样本名随源 BAM 的 SM 走）；
> · 若整个队列都是 `SM:sample`，Stage 4 `CombineGVCFs` 会**合并失败或把多样本压成一个**。
> 而 `compute_backend=parabricks` 是 GPU+冒烟通过时的**默认路径**，即不传此参数时默认路径全阶段不可用。
> `--read-group-pl` 用 `ILLUMINA`，与 CPU 路径的 `RG_STR` 保持一致（同一份数据在两条后端下
> 产出的 BAM 头部应完全一致，否则下游按平台分流的工具会拿到不同结果）。`--read-group-lb`
> 传样本号，使同一样本的多个文库可被正确识别。

> `--tmp-dir` 必须在 `parabricks.mounts` 已挂载的路径下（容器看不到未挂载目录）。
> ⚠ `--low-memory --bwa-nstreams 1` 是 **4090 24GB + 全基因组 ref 的必需项**：缺省流数下加载
> 参考索引会 `cudaSafeCall ... out of memory`（本机实测）。参数速查见 `parabricks-setup.md` §4.1。

- `--out-bam` 命名保持 `{sample}.sorted.bam`，与 CPU 路径**输出契约一致**（Stage 2/3 直接消费）
- fq2bam 内部完成坐标排序并生成 `.bai` 索引
- **⚠ 4.3.x 的 fq2bam 默认即做 MarkDuplicates + BQSR**（可选 `--knownSites` 驼峰增强）。
  因此 Stage 2 对 parabricks 后端 **不要再重复 markdup**（可直接消费本 BAM，Stage 2 只做增量
  BQSR/校验）。与 CPU 路径的 BAM 均已有 markdup+打标签/BQSR 状态，满足下游要求
- 具体参数名/行为以 `pbrun fq2bam --help` 为准

### 与 CPU 路径的差异注意

| 项 | CPU（BWA） | GPU（fq2bam） |
|----|-----------|---------------|
| Read Group | 必须手工 `-R` | **必须显式传 `--read-group-sm`**（`--read-group-lb`/`--read-group-pl` 一并给）。**不传不会"自动符合 `SM`"**：fq2bam 生成的是写死的常量 `SM:sample`，与样本号无关，门禁 `readgroup`(hard) 会拦下每一个样本（详见下方 Parabricks 段） |
| 输出 | `sorted.bam` | `sorted.bam`（默认同契约） |
| 并行维度 | CPU 核数 | GPU 卡数（每样本 1 卡） |
| SAM 临时文件 | 有（需清理） | 无 |
| gzip 完整性命中 | 是（`gzip -t` 预检仍在启动前做） | 是，损坏同样中断 |

## 数据库更新

LLM 在执行前后应更新数据库：
- 启动时：`INSERT INTO sample_steps (..., step_name='alignment', status='running')`
- 完成后：`UPDATE sample_steps SET status='completed' WHERE ...`

## 脚本安全检查清单（生成脚本前逐条检查）

1. ✅ **BWA 输出禁止管道** — SAM 写到文件，stderr 单独捕获
2. ✅ **BWA 必须加 `-R` (Read Group)**
3. ✅ 所有命令在 `conda run -n Agent` 环境下执行
4. ✅ 工具用 `command -v` 运行时探测（**禁止**写死本机 conda 绝对路径）
5. ✅ `set -euo pipefail`
6. ✅ SESSION-SAFE 保护（`.done` + `.pid` + nohup）
7. ✅ 样本并行 + NUMA 绑定（多样本时）
8. ✅ **子进程失败必须累计**（`|| { FAILED=$((FAILED+1)); ... }`），**禁止** `wait ... || true` 或 `|| log_error` 单独了事（会吞掉退出码）
9. ✅ **写 `.done` 前必须过校验门禁**：`stage_verify.py --stage 01_bwa_sort`，未过则写 `.failed` + `exit 1`
10. ✅ 中间文件清理（删除 SAM 临时文件）
11. ✅ 若 `--resume` 开启，输出文件存在性检查
12. ✅ 按 `compute_backend` 选命令路径：`parabricks`→`pbrun fq2bam`（禁 BWA）；否则→BWA+samtools
13. ✅ GPU 路径：`--out-bam` 符合契约、每样本绑 1 卡（`--num-gpus`）、`pbrun fq2bam --help` 复核参数名
