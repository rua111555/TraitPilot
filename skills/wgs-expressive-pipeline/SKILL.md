---
name: wgs-expressive-pipeline
description: >
  Run a WGS germline variant calling pipeline with full LLM autonomy and traceability,
  driven by modular sub-skills. The orchestration layer handles monitoring (.done/.pid),
  database queries, background execution (nohup + background), and auto-advancement.
  Each analysis stage delegates to a dedicated sub-skill for implementation details.
  When user mentions WGS, 全基因组, 测序, 变异检测, variant calling, 跑管线 — match this skill.
disable-model-invocation: false
allowed-tools:
  - Read
  - Write
  - Bash
  - Grep
  - TaskOutput
---

# WGS Expressive Pipeline — Orchestration Layer

执行全基因组测序标准流程。本 skill 是**编排层**，负责协调子 skill、维护 5 大核心机制。
每阶段的具体实现规范参考对应子 skill 的 `SKILL.md`。

> **结果校验是一等机制（§2.2）**：每阶段脚本写 `.done` 前必须先跑 `stage_verify.py`
> 校验结果值；未通过则写 `.failed` 且**不写** `.done`，编排层据此停止推进。
> 这是防「中途错误传到后面步骤」的关键 —— 光靠 `.done` 存在与否判成功是不够的。

## 子 Skill 一览

| 阶段 | 子 Skill | 功能 |
|:----:|----------|------|
| S0 | `wgs-express-server-detect` | CPU/内存/负载检测 → 自动计算并行度 |
| 0 | `wgs-express-params-propose` | 查询 DB → 推理参数 → 保存 `run.params.json` |
| 0.5 | `wgs-express-fastp-qc` | **可选** fastp FASTQ 质控（去接头/质量过滤/QC报告） |
| 1 | `wgs-express-align-sort` | BWA MEM + samtools sort（逐样本）|
| 2 | `wgs-express-markdup-bqsr` | MarkDuplicates + BQSR（逐样本）|
| 3 | `wgs-express-haplotypecaller` | HaplotypeCaller GVCF（逐样本）|
| 4 | `wgs-express-joint-genotyping` | CombineGVCFs + GenotypeGVCFs（群组级）|
| 5 | `wgs-express-filtering` | SelectVariants + VariantFiltration（群组级）|

## 核心流程

```
/wgs-expressive-pipeline --samples 1,2,10 --prefix COHORT
  ├── S0:  服务器检测 + **GPU 检测**（参考 wgs-express-server-detect）→ 用户审批
  ├── 0:   参数提案 + **后端决策** compute_backend（参考 wgs-express-params-propose）→ 用户审批
  │         └── GPU 存在 + Parabricks 就绪 + 冒烟通过 → parabricks；否则 gatk-cpu
  ├── 0.5: **可选** fastp QC（参考 wgs-express-fastp-qc）→ 用户审批
  ├── 1-5: 逐阶段生成脚本（按 compute_backend 选择 GATK 或 Parabricks 模板）→ 用户审批 → 后台启动 → 完成自动推进
  └── 完成后：汇报最终 VCF 产出
```

**关键规则**：
1. 每阶段脚本必须生成 → 用户审批 → 才能执行（不可自动执行）
2. 逐阶段串行，一次只跑一个阶段
3. 每阶段参考对应子 skill 的 `SKILL.md` 获取详细规范、脚本模板、错误预防清单
4. 脚本中所有工具用 `command -v` 运行时探测（`conda run -n Agent` 下工具已在 PATH；GPU 路径额外含 `pbrun` 或其容器镜像）
5. 完成后自动推进到下一阶段
6. **`compute_backend=parabricks` 时，阶段 1-4 使用对应子 skill 的「Parabricks 模式」模板；
   阶段 5 过滤恒用 GATK；任何阶段 Parabricks 失败 → 该阶段回退 GATK CPU 路径**
7. **Parabricks 环境未就绪** → S0 报告 `parabricks_ready=false`，提示用户按
   `references/parabricks-setup.md` 安装并过冒烟测试；未过冒烟前始终走 `gatk-cpu`

## 参数

| 参数 | 说明 | 默认 |
|------|------|------|
| `--samples` | **必填**，逗号分隔样本编号 | — |
| `--prefix` | VCF 输出前缀（不提供则 LLM 自动生成） | 自动 |
| `--threads` | 线程数上限 | `40` |
| `--db` | SQLite 数据库路径 | `$PIPELINE_DB`，其次 `$GENO_DIR/pipeline.db` |
| `--resume` | 断点续跑 | 关闭 |
| `--backend` | 计算后端覆盖：`gatk-cpu` / `parabricks` | 自动（GPU+Parabricks 就绪则 parabricks） |

## 5 大核心机制

### 1. 后台运行机制（SESSION-SAFE）

每阶段用**两层保护**启动：
```bash
# Bash(background: true) — Harness 跟踪，本轮工具调用立即返回，对话框不阻塞
# nohup ... & + 重定向 — 终端退出后进程继续运行，且父进程不会等在管道上
nohup conda run -n Agent bash "${SCRIPT_PATH}" > "${RUN_DIR}/nohup.out" 2>&1 &
```

> ⚠️ 两个**必须**写对的地方（写错会导致这一轮卡住到命令结束）：
> - 参数名是 **`background`**，不是 `run_in_background`（后者会被忽略）
> - 必须把 stdout/stderr **重定向**走（`> ... 2>&1`）。只写 `nohup ... &` 而不重定向时，
>   子进程仍持有父进程的输出管道，父进程要等到子进程退出才返回 —— 等于没有后台化。

### 2. 监测机制（.done + .pid + .verify 三重确认）

- **`.done` 标记**：阶段脚本**通过结果校验后**才写入 `date > "${RUN_DIR}/.done/STAGE.done"`
- **`.pid` 锁文件**：脚本启动时 `echo $$ > "${RUN_DIR}/.pid/STAGE.pid"`，`trap 'rm -f' EXIT`
- **`.failed` 标记**：校验未通过时写 `"${RUN_DIR}/.failed/STAGE.failed"`（含失败原因），
  **不写 `.done`** —— 编排层据此**停止推进**并报错
- **`.verify/STAGE.json`**：校验器落盘的机器可读报告（每条判据的结论与实测值）
- 完成状态以 `.done` 文件为准，**不以 Harness 通知为准**

> ⚠️ `.done` 必须是**有条件**写入的。历史缺陷：脚本收尾用 `wait "${pid}" || log_error ...`
> 吞掉子进程失败（`||` 使退出码变 0，`set -e` 不触发），随后无条件 `date > .done` ——
> 编排层据此认为"完成"并推进，下游于是拿到空 BAM / 假 GVCF / 样本数不对的 VCF。
> 这类错误在下游往往不立刻报错（GATK 能读空 BAM），要到很久以后以"变异数异常少"的面貌
> 出现，届时已无法追溯是哪一步坏掉的。

### 2.2 阶段结果校验（写 .done 前的门禁）

每个阶段脚本在**写 `.done` 之前**必须调用校验器，按结果值判定该阶段是否真的成功：

```bash
# ─── 阶段收尾：先校验，再决定写哪个标记 ───
# ⚠️ 本块刻意做成**自足可抄**：下面用到的每个变量**和函数**都在本块内出现定义或显式确认。
#    ⚠️ 函数也算：本块用了 `log_error`/`log_done`，早期版本**只调用不定义**，
#    而六个阶段 skill 里只有 align-sort / fastp-qc 定义了它们。LLM 照抄本模板生成的
#    脚本在首次调用处当场 `log_error: command not found`（rc=127）—— 那正好发生在
#    **失败分支**里，于是"本该写 .failed 并报原因"的路径反而崩在写标记之前：
#    磁盘上 .done/.failed 都不存在，四态判定归为"已中断"，用户被引去查进程。
#    故这里补上定义（与 align-sort / fastp-qc 的写法一致，含 log_done/log_info）。
log_info()  { echo "[$(date '+%Y-%m-%d %H:%M:%S')] [INFO]  $*"; }
log_done()  { echo "[$(date '+%Y-%m-%d %H:%M:%S')] [DONE]  $*"; }
log_error() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] [ERROR] $*"; }
#    `RUN_DIR` 是唯一无法给默认值的 —— 它**就是**写 .done/.failed 的目的地。
#    注意确认方式用 `[ -z ]` 判空，**不是** `: "${RUN_DIR:?}"`：后者在空值/未定义时
#    立刻 exit（set -e），磁盘上既无 .done 也无 .failed，编排层归为"已中断"并引用户
#    去查进程（与下方 VERIFIER 段同一坑，实测复现）。
#    （此处确实无处可写 .failed —— 那正是"必须先有 RUN_DIR"的原因 —— 但把原因**打出来**，
#     比让用户去追一个不存在的进程有用。）
RUN_DIR="${RUN_DIR:-}"          # 由阶段脚本开头定义（见 §2.1 SESSION-SAFE 样板）
if [ -z "${RUN_DIR}" ] || [ ! -d "${RUN_DIR}" ]; then
    echo "[FATAL] RUN_DIR 未定义或不是目录：'${RUN_DIR}' —— .done/.failed 无处可写" >&2
    echo "        这是脚本开头漏了赋值（见 §2.1 样板），不是数据问题。" >&2
    exit 1
fi
DONE_FILE="${RUN_DIR}/.done/01_bwa_sort.done"   # 实际使用时替换为当前阶段的标记名
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

# ─── 样本名单：同样必须先兜底再使用 ───
# ⚠️ **本段不可省**。本模板是"完整可抄"的权威版本，六个阶段 skill 各自都有这一段；
#    早期本模板漏了它，而下面直接用了 ${SAMPLES_CSV} —— LLM 照抄生成的脚本在
#    `set -u` 下当场 `SAMPLES_CSV: unbound variable` 退出（实测 rc=1），
#    磁盘上**既无 .done 也无 .failed**，编排层四态判定归为"已中断"并引用户去查进程，
#    真实原因却只是本模板少了一行。同理：`|| true` 不可省（见上方 VERIFIER 段的说明）。
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
if ! python "${VERIFIER}" \
        --stage 01_bwa_sort \
        --run-dir "${RUN_DIR}" \
        --samples "${SAMPLES_CSV}"; then
    # 校验未通过 —— 不写 .done，写 .failed 并带上原因
    mkdir -p "${RUN_DIR}/.failed"
    {
        echo "stage=01_bwa_sort"
        echo "time=$(date '+%Y-%m-%d %H:%M:%S')"
        rep="${RUN_DIR}/.verify/01_bwa_sort.json"
        # 报告已在调用校验器前被删除，所以这里只要存在就**必然是本轮的**。
        # 校验器没跑起来时（路径错/python 缺失）该文件不存在 —— 必须如实说明，
        # 否则会把一个纯环境问题误报成"校验把好数据拦下了"。
        if [ -f "$rep" ]; then
            echo "--- 校验报告（本轮）---"; cat "$rep"
        else
            echo "--- 没有本轮校验报告 ---"
            echo "失败发生在校验器产出报告之前：多为环境问题（${VERIFIER} 不可读 /"
            echo "python 缺失 / RUN_DIR 无写权限），而不是数据问题。"
            echo "先看上面的错误行与 ${RUN_DIR}/nohup.out。"
        fi
    } > "${RUN_DIR}/.failed/01_bwa_sort.failed"
    log_error "阶段校验未通过，不写 .done（编排层不得推进）"
    exit 1
fi

date > "${DONE_FILE}"
rm -f "${RUN_DIR}/.failed/01_bwa_sort.failed"   # 清掉上一轮的陈旧失败标记
log_done "Stage 1 complete (校验通过)"
```

> **为什么必须清 `.failed`**：不清的话，重跑该阶段并通过后，磁盘上同时存在 `.done` 与
> `.failed`。编排层的四态判定**优先读 `.failed`**（那是刻意的 —— 校验未通过时最该被看见），
> 于是会把这个**已经修好并通过**的阶段报成"校验未通过"，用户被误导去查一个不存在的问题。
> 陈旧状态和真实状态必须只留一个（同错误预防 #19）。

**校验器**：`skills/wgs-expressive-pipeline/scripts/stage_verify.py`（`--stage` 取脚本名去 `.sh`）。
不加 `--json` 输出人类可读报告；报告同时落盘 `{RUN_DIR}/.verify/{stage}.json`。
退出码 0=通过（含仅告警），1=有硬性判据未过。

**判据分级**：
- `hard` 不过 → 判失败，**不写 `.done`**（上面这段就是处理它）
- `warn` 不过 → 仍写 `.done`，但告警在报告里；**编排层汇报时要带出来**（如重复率偏高、Ti/Tv 偏移）

**与 `--resume` 的关系（易被改错，务必保持）**：
- resume 的跳过是**样本级**的（`if [ -f "${BAM}" ]; then ... continue; fi`），**不是整阶段跳过**。
  所以门禁总会执行，能校验整个队列。
- 门禁本身**不认 resume 语义**，对所有样本一视同仁 —— 这是刻意的：resume 时"跳过的样本"
  其产物是上一次留下的，**恰恰更需要校验**（可能是上一次失败留下的空文件）。
- ⚠️ **禁止**把 resume 实现成"检测到 `.done` 就整阶段 `exit 0`"：那样门禁被绕过，
  且会保留一个由**未启用校验的旧版本**写下的 `.done`。若确要阶段级跳过，必须
  **重跑一次校验**再决定是否保留 `.done`。

**各阶段的核心判据**（完整清单见 `stage_verify.py` 各 `@stage` 函数）：

| 阶段 | 最容易漏掉的**硬性**判据（不过即不写 `.done`） |
|------|---------------------------------------------|
| 0.5 fastp | **read 数直接数 clean FASTQ 本体**（不再采信 JSON）且 > 0；R1/R2 配对守恒；**clean FASTQ 的 gzip 流完整**（截断的 .gz 仍能通过"存在且非空"，要到 BWA 读一半才炸）；JSON 可解析且其 per-mate 读数与 FASTQ **逐端一致**（防陈旧/混轮次产物） |
| 1 align | **reads 数 > 0**（`quickcheck` 对空 BAM 也放行，只有计数能抓）；**`mapped` = 0（全 unmapped）即拦**（参考基因组选错/物种不符的头号症状 —— 注意这是 `mapped_ratio` 这条 **hard** 判据，与下面 warn 级的 `mapped_rate` ≥90% 是**两条不同的判据**）；`@RG` 存在且 `SM` 与样本号一致；`.bai` 存在 |
| 2 markdup/bqsr | **每个样本都有 BQSR BAM**（Stage 3 的输入契约；缺一个即拦）；**Stage 1 有产出的样本不得消失**（reads 守恒只比数值，丢整个样本要看集合差） |
| 3 HC | **头部含 `##ALT=<ID=NON_REF>`**（漏 `-ERC GVCF` 的头号症状，Stage 4 必失败）；**GVCF 内部声明的样本名与文件名一致**（否则样本串了） |
| 4 joint | **VCF 样本数 == 输入 GVCF 数**（样本在中间被丢掉） |
| 5 filter | **VCF 样本集 == 上游样本集**（最后一棒，下游没人再核）；SNP/INDEL **各自**都有 PASS 位点；FILTER 不得整列是 `.`（未记录判定 = 没过滤） |

> ⚠️ 这几条**不是**硬性的（只告警，仍写 `.done`）：Stage 2 与 Stage 1 的 reads 守恒
> （±10% 带，且需 `--prev-reads-file`）、Ti/Tv 是否落在 1.8-2.3、**`mapped_rate` 比对率是否 ≥90%**、
> 保留率是否 ≥50%、重复率是否 ≤40%、markdup BAM 是否齐（parabricks 路径下可能本就不产出）、
> **未传 `--min-variants` 时的位点数下限**。
> 表里不要把 warn 级写成"硬性" —— 读者会按表去找拦截，而实际只收到一行告警。
>
> ⚠️ **反过来的错误同样要防**：上面 warn 列表里的「比对率 ≥90%」专指 `mapped_rate` 一档。
> 另有一条 **hard** 的 `mapped_ratio`（`mapped` = 0，即**全 unmapped** → 不写 `.done`）。
> 两条别混为一谈：`mapped_ratio` 拦的是"参考基因组选错/物种不符"，`mapped_rate` 告警的是
> "比对质量偏低"。本表早期漏记了 `mapped_ratio`，读者会以为"比对率低只是收到一行告警"，
> 实际整个阶段被判失败停止推进，排查时又按 warn 的定位去找、找不到这条硬失败。

> **「变异数异常少」为什么默认只告警**：本机制要防的头号失效就是它（见上文 §2.1），
> 但合理位点数取决于参考基因组大小/队列大小/深度/物种，**差几个数量级**
> （人类 3Gb vs 本仓库探针用的 200kb 迷你 ref），没有可硬编码的普适下限 ——
> 拍一个默认值必然要么拦健康数据、要么松到没用。故由**调用方显式声明**：声明后未达到
> 即 hard 拦截；未声明则登记一条 warn 明说"未做该判定"，**绝不静默通过**（否则报告全绿
> 会让人以为已把关）。与表型侧 `nir_agreement` 同一范式：显式阈值 = 用户的验收标准 → hard。
>
> **声明通路（两条，任选）**：
> 1. **`run.params.json` 里写 `"min_variants": N`**（推荐）—— 与 `verifier_path` 同一范式：
>    Stage 0 解析落盘，阶段侧读回使用。**本仓库只提供 CLI 开关是不够的**：阶段脚本由 LLM
>    现场生成，没有任何一处会把该参数写进命令行，于是这条判据会**恒停在 warn** ——
>    又是"声称可声明、实际无从声明"的纸面具备。故校验器在 CLI 未给时**回落到
>    `run.params.json`**。（若写了该键却**无法生效**，如 `"1M"`/`0`/`-5`/`3.5`，
>    校验器判 **hard 不通过**而非静默降级 —— 想开却没生效必须被看见。）
> 2. `--min-variants N` 显式传给校验器（优先级高于 `run.params.json`）。

> 校验器路径必须**由 Stage 0 写进 `run.params.json`**（`verifier_path`）：
> 生成的阶段脚本位于 `{RUN_DIR}/scripts/`，与仓库无固定相对关系，无法用相对路径
> 定位校验器。这与仓库既有规则一致 —— 所有路径经 Stage 0 解析后显式传入，
> 不写死本机路径（见错误预防 #5.1）。

### 2.5 自动推进机制

**关键规则：后台进程完成后，编排层必须主动检查进度并推进，不能被动等待用户询问。**

方式：
1. 脚本启动后，用 **`TaskOutput(log_path=...)`**（或 Read/Bash 读日志）查看进度；
   **不要在工具调用里 `sleep` 轮询** —— 那会把这一轮一直占住，对话框就不会解放。
2. 每次检查 `.done` 与 `.failed` 文件（**两者都要看**）
3. 汇报保持**极简**（一行：`第 X/N 样本 | Y 个 BAM | 无报错`），不加分析过程
4. `.done` 存在 → 读 `.verify/STAGE.json`，**把 warn 项带进汇报** → 进入下一阶段（生成脚本 → 交用户审批）
5. `.failed` 存在 → **停止推进**，读失败原因如实报给用户，修复后重新审批该阶段
6. 两者都不存在 → **结束本轮**，如实告知「还在跑，下次问我时我再查」

**检查间隔策略：**
- 大样本量（>50 样本）管线运行时，间隔不可过密
- BWA 等长任务：**每 3 小时**检查一次
- 每样本耗时数小时的任务，30 分钟检查只会浪费上下文
- 用户随时可以主动询问进度，无需等待定时触发

**上下文耗尽时的恢复方式：**
如果对话上下文过长，用户可以开新对话并说 `推进 WGS — run_id <run_id>`，编排层应执行场景 B 的恢复检查，从 outputs/目录读取 stage 状态继续推进，不需要重新生成任何脚本或重跑管线。

### 3. 错误自动总结机制

**任何时候遇到管线运行错误，必须自动执行以下步骤，不得跳过：**

1. **分析根因**：读取日志文件，找出报错的具体命令和错误信息
2. **记录到对应子 skill**：将根因、解决方案写入对应子 skill 的 SKILL.md 或 usage-guide.md 的「错误预防清单」中（新增一条或更新已有条目）
3. **记录到编排层**：在编排层 SKILL.md 的「错误预防」表格中新增一行
4. **告知用户**：一句话说明错在哪、为什么、已记录

**记录格式（子 skill）：**
```markdown
| N | **新发现的错误描述** — 日志关键信息 | 违反后果 |
```
**记录格式（编排层）：**
```markdown
| N | 新发现的错误描述 | 相关阶段 |
```

**不得做：** 修复完就过去了、下次同样的错再犯一次、让用户帮你总结错误。

### 4. 数据库机制

```bash
conda run -n Agent python "$(dirname "$0")/scripts/db_query.py" --action query-all --samples N
```
（`--db` 缺省 `$PIPELINE_DB` / `$GENO_DIR/pipeline.db`；脚本路径按仓库内 `skills/wgs-expressive-pipeline/scripts/` 相对定位）
- 启动阶段：INSERT `pipeline_runs` / `sample_steps` (status='running')
- 完成后：UPDATE `sample_steps` / `pipeline_runs` (status='completed')

### 5. 对话推进机制

**场景 A — 对话框开着**：Harness 通知到达 → 检查 `.done` / `.failed` 标记 → ✅ 则生成下一阶段脚本交审批

**场景 B — 对话框关闭后恢复**：用户说「推进 WGS」→ 执行恢复检查：

```bash
for stage_script in "${RUN_DIR}"/scripts/*.sh; do
    stage_name=$(basename "${stage_script}" .sh)
    pid_file="${RUN_DIR}/.pid/${stage_name}.pid"
    log_file="${RUN_DIR}/logs/${stage_name}.log"
    # ⚠️ 顺序要紧：.failed 必须先判 —— 校验未通过时**不会**有 .done，
    #    但也不该被当成「已中断」（进程正常退出、日志正常），否则会误导用户去查日志
    if [ -f "${RUN_DIR}/.failed/${stage_name}.failed" ]; then
        echo "❌ ${stage_name} 校验未通过（读 .failed 文件看原因）"; continue
    fi
    if [ -f "${RUN_DIR}/.done/${stage_name}.done" ]; then
        # 通过校验的阶段：把 warn 项带出来，别让告警被"✅"盖住
        # ⚠️ 不能用 `grep -o '"warnings": \[[^]]*\]'`：.verify/*.json 是 indent=2 的
        #    **多行** JSON，`[` 与 `]` 分处两行，而 grep 不跨行 —— 该模式必然匹配不到，
        #    再加上 2>/dev/null，所有软告警（比对率低、重复率高、Ti/Tv 偏移）都传不到用户。
        #    用 python 解析才是可靠的（本阶段的 env 里 python 一定可用）。
        warn=$(python -c 'import json,sys
try:
    d=json.load(open(sys.argv[1]))
    w=d.get("warnings") or []
    print("; ".join(w))
except Exception:
    pass' "${RUN_DIR}/.verify/${stage_name}.json" 2>/dev/null)
        echo "✅ ${stage_name} 已完成 ${warn:+| 告警: $warn}"; continue
    fi
    if [ -f "${pid_file}" ] && kill -0 "$(cat "${pid_file}")" 2>/dev/null; then
        echo "🕐 ${stage_name} 运行中"; continue
    fi
    [ -f "${log_file}" ] && echo "⚠️ ${stage_name} 已中断（进程不在，但无 .done/.failed）" \
                         || echo "⬜ ${stage_name} 未开始"
done
```
→ 已完成（中间步）：读 `.verify/*.json` → 生成下一阶段脚本交审批（带出 warn）
→ 已完成（最后步）：汇报最终产出
→ **校验未通过**：读 `.failed` 与 `.verify/*.json` 报根因，修复后重审该阶段
→ 运行中：告知进度
→ 已中断：读日志报错，修复后重审
→ 未开始：从未完成的最早阶段继续

## 输出目录结构

```
{output_dir_base}/temp/runs/{run_id}/        # output_dir_base 来自 run.params.json（缺省 $GENO_DIR/temp）
├── run.params.json     # 所有参数（server_detection + parallelism + verifier_path）
├── scripts/            # 各阶段生成的脚本
│   ├── 01_bwa_sort.sh ...
│   └── 05_filtering.sh
├── logs/               # 执行日志（stdbuf 实时刷新）
├── .done/              # 完成标记（**通过结果校验后**才写）
├── .failed/            # 校验未通过的标记 + 失败原因（有它就不该有同名 .done）
├── .verify/            # 校验器报告 {stage}.json（每条判据的结论与实测值）
├── .pid/               # PID 锁文件
└── out/                # BAM / VCF 输出
```

> 阶段状态的四态判定（场景 B 的恢复检查据此分支）：
> `.failed` → 校验未通过（**最优先判**）｜`.done` → 已完成且校验通过｜
> `.pid` 且进程活 → 运行中｜其余 → 未开始 / 已中断
>
> `.failed` 之所以最优先：它的存在意味着**这一阶段的数据不可信**，必须停下来修，
> 而不是继续推进。代价是必须有配套的清理 —— 阶段重跑并通过时要在写 `.done` 的同时
> `rm -f` 同阶段的 `.failed`，否则已修好的阶段会被这条优先级误报成"未通过"
> （实测踩到过）。两者是**同一个设计的两半，不能只做一半**。

## 错误预防（生成每个脚本前检查）

| # | 检查项 | 相关 |
|---|--------|------|
| 1 | BWA 禁止管道（写 SAM 文件，stderr 单独捕获） | align-sort |
| 2 | BWA 必须加 `-R`（Read Group） | align-sort |
| 3 | GATK 用 `--java-options`，不是 `-J` | 全部 |
| 4 | 各 GATK 工具参数名可能不同，用 `--help` 确认 | 全部 |
| 5 | 所有命令在 `conda run -n Agent` 下执行 | 全部 |
| 5.1 | **工具用 `command -v` 运行时探测**（禁止写死本机 conda 绝对路径） | 全部 |
| 6 | **HaplotypeCaller HMM 线程必须严格遵循 `run.params.json` 的 `threads_per_sample`**，不可凭感觉设定（已在 haplotypecaller SKILL.md 中记录了具体规则） | haplotypecaller |
| 7 | **FASTQ gzip 损坏导致 BWA 失败**（`invalid compressed data` / `the 2nd file has fewer sequences`）——启动前需批量 `gzip -t` 校验，R1/R2 配对必须一致 | align-sort |
| 8 | **wait+unset 并发池缺陷导致样本被跳过**（Stage 1 首跑跳过 18/161）——控制并发池时勿用 `unset` 破坏数组索引，改用标记法或独立计数器 | align-sort |
| 9 | **consumer 卡（RTX 4090 等）非 Parabricks 官方支持**——必须冒烟测试通过才允许 `compute_backend=parabricks`，否则回退 `gatk-cpu` | params-propose/全部 |
| 10 | **Parabricks 未就绪即走 GPU 路径会白跑**（无 pbrun / 无镜像 / docker 无权限）——S0 预检 `parabricks_ready`，就绪才提案，否则提示安装（见 `references/parabricks-setup.md`） | server-detect/params |
| 11 | **pbrun 参数名与 GATK 不一致**——用 `pbrun <tool> --help` 确认，勿照抄 GATK 写法 | 阶段 1-4 |
| 12 | **GPU OOM（24GB 消费卡）**——降低并行样本数或转回 CPU GATK | 阶段 1-4 |
| 13 | **收尾用 `wait "${pid}" \|\| log_error ...` 会吞掉子进程失败**（`\|\|` 使 rc=0，`set -e` 不触发）→ 脚本继续跑到末尾**无条件**写 `.done` → 编排层误判"完成"并推进。必须改为 `|| FAILED=1` 累计，并在写 `.done` 前过校验门禁 | 全部 |
| 14 | **`.done` 必须有条件写入**——先跑 `stage_verify.py`，通过才写 `.done`，否则写 `.failed` 并 `exit 1`。无条件写 `.done` 是"中途错误传到后面"的根源（见 §2.2） | 全部 |
| 15 | **`samtools quickcheck` 对空 BAM 放行**（它只验结构完整，不看有无数据）——`.bam` 存在 + quickcheck 过**不足以**说明比对成功，必须再查 reads 数（`idxstats`） | align-sort/全部 BAM 阶段 |
| 16 | **GVCF 判据要看头部 `##ALT=<ID=NON_REF` 声明**，不是字面 `<NON_REF>` —— 头部里 ID 值不带尖括号，带括号的形态只出现在记录行 ALT 列。找错地方会把真 GVCF 判成假的 | haplotypecaller/joint |
| 17 | **`bcftools stats` 的 TSTV 行第 5 个字段才是比值**（第 3/4 是 ts/tv 计数）——取错列会把 tv 计数（如 20）报成 Ti/Tv=20.00 | filtering |
| 18 | **校验器路径须由 Stage 0 写入 `run.params.json`**（`verifier_path`）——阶段脚本在 `{RUN_DIR}/scripts/`，与仓库无相对关系，相对路径定位不到 | params-propose/全部 |
| 19 | **成功路径必须 `rm -f` 同阶段的陈旧 `.failed`** —— 不清的话重跑通过后 `.done` 与 `.failed` 并存，而四态判定优先读 `.failed`，已修好的阶段被误报成"校验未通过"（实测踩到） | 全部 |
| 20 | **禁止把 `--resume` 实现成"见 `.done` 就整阶段 `exit 0`"** —— 那样校验门禁被绕过，且会保留由旧版本写下的 `.done`。resume 的跳过必须停在**样本级**，让门禁仍能校验全队列（见 §2.2「与 --resume 的关系」） | 全部 |
| 21 | **`read_totals` 必须由 Stage 1 真正写出**，否则 Stage 2 的 reads 守恒检查永远静默跳过（声称存在、从不生效）。同理，`--prev-reads-file` 缺失/无记录时须**告警**而非静默通过 | align-sort/markdup |
| 22 | **BAM 索引有四种合法命名，判据必须都认** —— `foo.bam.bai`（`samtools index foo.bam`）、`foo.bai`（GATK/Picard `--CREATE_INDEX true`，`.bam` 整个换成 `.bai`）、`foo.bam.csi` 与 `foo.csi`（`samtools index -c`）。samtools 与 GATK 四种都认（实测）；只认一种会把 GATK 真实产出的索引判成"缺失"，**健康数据被拦**（实测：ApplyBQSR 产出 `S1.sorted.markdup.BQSR.bai`，旧校验器报缺 `...bam.bai`，rc=1） | 全部 BAM 阶段 |
| 23 | **索引判级按"下游是否真的需要"定，不要一律 hard** —— BQSR BAM 索引：HaplotypeCaller 无它直接 rc=2（实测 `A USER ERROR ... not indexed`）→ hard；markdup BAM 索引：`gatk MarkDuplicates` 的 `CREATE_INDEX` **默认 false**、下游读它也不需要索引 → warn。一律 hard 会在标准命令下稳定拦下健康数据 | markdup/全部 |
| 24 | **GATK 里 `--CREATE_INDEX`（Picard 风格）与 `--create-output-bam-index`（walker 风格）是两套参数** —— MarkDuplicates 只认前者且默认 false；ApplyBQSR/HaplotypeCaller 等 walker 只认后者且默认 true（实测：对 ApplyBQSR 传 `--CREATE_INDEX` 报 `not a recognized option`）。写参数前用 `gatk <Tool> --help` 确认，勿跨工具照抄 | 全部 |
| 25 | **`samtools idxstats` 读的是索引，不是 BAM** —— 重跑 / `--resume` 跳过 index / 覆写产物时，索引停在上一轮，idxstats 便拿**旧值**作答：0 reads 的空 BAM 报出上一轮 reads 数，`read_count`/`mapped_ratio`/`mapped_rate` 三条判据同时被骗过、门禁全绿放行（实测）。校验器的 `{label}.index_stale` 判据采信 samtools 自己的 `[W::hts_idx_load3] ... older than ...` 警告并回退全量计数。**改产物后必须重建索引** | 全部 BAM 阶段 |
| 26 | **GATK walker 自动写 `.tbi`/`.bai`，不要再手工 `tabix`** —— `--create-output-variant-index` / `--create-output-bam-index` 默认均为 true（实测 HaplotypeCaller、SelectVariants、VariantFiltration 都直接产出 `.tbi`）。手工补 `tabix -p vcf` 会因索引已存在而 **rc=1**，配合阶段脚本的 `set -euo pipefail` 会**死在门禁之前**：健康数据既不写 `.done` 也不写 `.failed`，编排层误判成"已中断"。真要用就加 `-f` | 全部 VCF 阶段 |
| 27 | **`pbrun fq2bam` 必须传 `--read-group-sm "${sample}"`** —— 不传则自动生成写死常量读组 `SM:sample`，所有样本同名：Stage 1 `readgroup`(hard) 必拦，且该 SM 继承进 GVCF 样本列使 Stage 3 `samples`/`identity`(hard) 连锁失败。parabricks 是 GPU 就绪时的默认后端 | align-sort/全部 |

## 自然语言触发

关键词：`WGS`、`全基因组`、`测序`、`变异检测`、`variant calling`、`germline`、`跑管线`、`生信`、`BWA`、`GATK`、`HaplotypeCaller`、`GPU 加速`、`Parabricks`、`pbrun`
