---
name: wgs-express-haplotypecaller
description: >
  Call germline SNPs and indels with GATK HaplotypeCaller in GVCF mode
  (-ERC GVCF) per sample. Uses local reassembly and PairHMM for accurate
  variant detection. Called by the orchestration skill (wgs-express-pipeline)
  during Stage 3.
allowed-tools:
  - Read
  - Write
  - Bash
  - Grep
---

# HaplotypeCaller GVCF — Stage 3

使用 GATK HaplotypeCaller 的 GVCF 模式为每个样本生成包含所有位点信息的 GVCF 文件。

**由编排 skill 调用**，不直接暴露给用户。

## 输入

- `run.params.json` — 全部参数
- `{RUN_DIR}/out/{sample_name}.sorted.markdup.BQSR.bam` — BQSR 校正后的 BAM

## 输出

每个样本输出：
- `{RUN_DIR}/out/{sample_name}.g.vcf.gz` — GVCF 文件
- `{RUN_DIR}/out/{sample_name}.g.vcf.gz.tbi` — TBI 索引

## 脚本编制要点

### SESSION-SAFE 样板

完整的 `.done` / `.pid` / 日志配置（同前）。

### 工具路径（运行时探测，勿写死本机路径）

阶段脚本经 `conda run -n Agent bash` 执行，工具已在 PATH 中：

```bash
resolve_tool() { command -v "$1" || { echo "✗ 找不到 $1 —— 请激活 conda 环境 Agent" >&2; exit 1; }; }
GATK="$(resolve_tool gatk)"
# 不探测 tabix：GATK 自动写 .tbi（--create-output-variant-index 默认 true），
# 手工补 tabix 反而会因索引已存在而 rc=1 杀死脚本（见下方索引段注释）。
```

### HaplotypeCaller 标准用法

```bash
gatk HaplotypeCaller --java-options "-Xmx${JAVA_XMX}" \
  -R "${REF_FASTA}" \
  -I "${INPUT_BAM}" \
  -O "${GVCF}" \
  -ERC GVCF \
  --native-pair-hmm-threads "${HMM_THREADS}"
```

### 参数说明

| 参数 | 值 | 说明 |
|------|-----|------|
| `-ERC GVCF` | GVCF 模式 | 输出包含所有位点的 GVCF（含 `<NON_REF>` 等位基因） |
| `--native-pair-hmm-threads` | 从 `run.params.json` 读取 | PairHMM 向量化计算线程数（核心瓶颈） |
| `-Xmx` | 从 `run.params.json` 读取 | Java 堆内存 |

### `--native-pair-hmm-threads` 选择规则

**必须严格遵循 `run.params.json` 中 `haplotypecaller.threads_per_sample` 的值**，不做自作主张的限制。

PairHMM 是 HaplotypeCaller 的核心计算瓶颈，使用了 AVX-512 向量化加速。在资源充裕的服务器上（如当前的 128 核 2-socket 服务器），GATK 内部会将 PairHMM 计算分发到指定线程数上并行处理。

**规则：**
- `HMM_THREADS = run.params.json.parallelism.haplotypecaller.threads_per_sample`
- 不允许人为设定低于该值的上限
- 如果 `threads_per_sample` × 并发样本数 > 总线程的 80%，可适当降低并发数而非降低单样本线程数
- **⚠️ 上述规则仅适用于 `compute_backend=gatk-cpu`**。Parabricks 模式下 PairHMM 在 GPU
  上并行，**不设置 `--native-pair-hmm-threads`**，改用 `pbrun ... --num-gpus 1 --num-cpu N`

### 关键认知

1. **GVCF 模式**：记录每个位点的变异置信度，包括非变异位点。`<NON_REF>` 符号等位基因使后续联合基因分型可以评估其他样本在该样本位置的变异。
2. **PairHMM**：计算瓶颈，AVX/AVX-512 向量化加速。
3. **MarkDuplicates 必须在之前运行**：HC 要求标记重复后的 BAM。
4. **BAM 必须包含 @RG**：HC 强制要求 Read Group。

### 样本并行模式

```bash
MAX_CONCURRENT=3   # 从 parallelism.parallel_samples 读取
pids=()
FAILED=0           # ⚠️ 必须在并发池之前初始化：下面用 FAILED=$((FAILED+1)) 累计，
                   #    未初始化时在 `set -u` 下会直接崩（unbound variable）

for i in "${!samples[@]}"; do
    sample="${samples[$i]}"
    
    if [ -f "${GVCF}" ] && [ -f "${GVCF}.tbi" ]; then
        log_info "GVCF exists, skipping: ${GVCF}"
        continue
    fi
    
    (
        set -euo pipefail
        gatk HaplotypeCaller --java-options "-Xmx${JAVA_XMX}" \
          -R "${REF_FASTA}" \
          -I "${INPUT_BAM}" \
          -O "${GVCF}" \
          -ERC GVCF \
          --native-pair-hmm-threads "${HMM_THREADS}"
        
        # 索引：**不要**再补 `tabix -p vcf "${GVCF}"`。
        # GATK 的 `--create-output-variant-index` 默认即 true，上面这条命令已经写出了
        # `${GVCF}.tbi`（本机实跑复核）。重复调用 tabix 会因"索引已存在"**退出码 1**：
        #     [tabix] the index file exists. Please use '-f' to overwrite.
        # 而这里在 `set -euo pipefail` 的子 shell 里 —— 脚本会当场死在这一行，
        # **连后面的结果校验门禁都到不了**，健康数据既不写 .done 也不写 .failed，
        # 编排层把环境问题误判成"已中断"。真要用 tabix 就加 -f，但那是多余动作。
    ) 2>&1 | stdbuf -oL tee -a "${LOG_FILE}" &
    pids+=($!)
    
    # 控制并发
    while [ "${#pids[@]}" -ge "${MAX_CONCURRENT}" ]; do
        sleep 30   # HC 运行时间长，每 30 秒检查一次
        for j in "${!pids[@]}"; do
            if ! kill -0 "${pids[$j]}" 2>/dev/null; then
                # ⚠️ 不要 `wait ... || true` —— 那会把失败彻底吞掉（退出码丢失，
                #    set -e 不触发，脚本照常写 .done）。累计下来交给收尾门禁裁决。
                wait "${pids[$j]}" || { FAILED=$((FAILED+1)); log_error "PID ${pids[$j]} failed"; }
                unset 'pids[$j]'
            fi
        done
    done
done
# ⚠️ 展开式 ${pids[@]+"${pids[@]}"} 不可省（实测 rc=1）：上面的并发池用
#    `unset 'pids[$j]'` 摘除已结束的 pid，于是进入这里时**有效元素数 =
#    样本数 % MAX_CONCURRENT**。余数为 0 时数组已空 —— 样本数恰为 MAX_CONCURRENT
#    的整数倍、MAX_CONCURRENT=1、或 resume 全部跳过（本阶段的 resume 判断是
#    `if [ -f "${GVCF}" ] && [ -f "${GVCF}.tbi" ]`，全跳过是常规情形）都会命中。
#    bash 4.2 下 `"${pids[@]}"` 对空数组当场报 `pids[@]: unbound variable` 并 rc=1
#    退出，**收尾门禁根本执行不到**：磁盘上既无 .done 也无 .failed，编排层归为
#    "已中断"并引用户去查一个不存在的进程。`+` 形式在数组为空时展开为 0 个词，
#    非空时行为与原来完全一致（两者都实测过）。
for pid in ${pids[@]+"${pids[@]}"}; do
    wait "${pid}" || { FAILED=$((FAILED+1)); log_error "Subprocess ${pid} failed"; }
done
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
    { echo "stage=03_haplotypecaller"; echo "time=$(date '+%F %T')"
      echo "reason=校验器未指定或不可读：'${VERIFIER}'"
      echo "这是环境问题，不是数据问题。逐项核对："
      echo "  1) ${RUN_DIR}/run.params.json 是否存在、能否被 python json.load 解析"
      echo "  2) 其中的 verifier_path 是否为**非空**且为**绝对**路径"
      echo "  3) 该路径指向的文件是否还在（仓库被移动过？）"
    } > "${RUN_DIR}/.failed/03_haplotypecaller.failed"
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
    { echo "stage=03_haplotypecaller"; echo "time=$(date '+%F %T')"
      echo "reason=run.params.json 的 sample_names 为空，样本守恒判据无法运行"
    } > "${RUN_DIR}/.failed/03_haplotypecaller.failed"
    exit 1
fi

# 先删掉本阶段的旧报告：跑完后它只要存在，就必然是本轮的产物。
rm -f "${RUN_DIR}/.verify/03_haplotypecaller.json"
if ! python "${VERIFIER}" \
        --stage 03_haplotypecaller --run-dir "${RUN_DIR}" --samples "${SAMPLES_CSV}"; then
    mkdir -p "${RUN_DIR}/.failed"
    { echo "stage=03_haplotypecaller"; echo "time=$(date '+%F %T')"
      # ⚠️ 只在报告**属于本轮**时才附上：校验器没跑起来时（路径错/python 缺失），
      #    .verify/ 里可能还留着上一次运行的文件，照抄会把排查引向数据而非环境。
      rep="${RUN_DIR}/.verify/03_haplotypecaller.json"
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
    } > "${RUN_DIR}/.failed/03_haplotypecaller.failed"
    log_error "结果校验未通过，不写 .done"; exit 1
fi
date > "${DONE_FILE}"
rm -f "${RUN_DIR}/.failed/03_haplotypecaller.failed"   # 清掉上一轮的陈旧失败标记

```

> **本阶段最容易出的错是「产出的不是 GVCF」**：GATK 漏加 `-ERC GVCF`（或 pbrun 漏
> `--gvcf`）时，命令**照样成功退出**、`.tbi` 照样生成、文件大小也正常 —— 但它是普通 VCF，
> Stage 4 的 CombineGVCFs 会失败。校验器查头部的 `##ALT=<ID=NON_REF` 声明来识别，
> 把错误挡在这一步（完整模板见编排层 SKILL.md §2.2）。

## ⚠️ 错误预防清单

| # | 检查项 | 违反后果 |
|---|--------|---------|
| 1 | **BAM 必须有 @RG** — 验证 `samtools view -H BAM \| grep '^@RG'` | `Number of read groups must be >= 1` |
| 2 | **MarkDuplicates 已执行** — HC 需要标记重复后的 BAM | 变异数量可能偏高 |
| 3 | **GVCF 必须带 `.tbi` 索引** —— 由 GATK 自动写出（`--create-output-variant-index` 默认 true），**不要**再手工 `tabix` | 下游 CombineGVCFs / GenotypeGVCFs 需要 `.tbi`。手工补 tabix 会因索引已存在而 rc=1，在 `set -e` 下杀死脚本（详见上方索引段注释） |
| 4 | **`--java-options` 正确使用** | `-J is not a recognized option` |
| 5 | **Parabricks 模式勿设 `--native-pair-hmm-threads`** — PairHMM 在 GPU 并行 | CPU 线程参数无意义/不被识别 |
| 6 | **`pbrun haplotypecaller` 必须加 `--gvcf`** | 输出普通 VCF，Stage 4 CombineGVCFs 失败 |

## --dragen-mode 说明

如果需要 DRAGEN 级别的准确性，可以在 HC 命令行添加 `--dragen-mode`（启用 DRAGSTR 和 BQD/FRD 模型，可替代单独的 BQSR 步骤）。这适用于：
- 希望通过硬过滤获得校准 QUAL 的场景
- 需要 STR 感知 indel 模型（DRAGSTR）

## Parabricks 模式（compute_backend=parabricks）

**`compute_backend == "parabricks"` 时启用**。用 `pbrun haplotypecaller --gvcf`
替代 GATK HaplotypeCaller。PairHMM 计算在 GPU 上并行（数十倍加速），
**此模式下不设置 CPU 的 `--native-pair-hmm-threads`**（见下方差异表）。

### 工具路径

```bash
# 统一 PBRUN() docker wrapper（由 wgs-express-params-propose 在脚本头部展开，见其「PBRUN wrapper」）
# 每样本: PBRUN <宿主GPU索引> <pbrun 子命令> ... ；容器内只暴露 1 张卡 → pbrun 用 --num-gpus 1
```

### haplotypecaller GVCF 标准用法（逐样本绑 1 卡）

```bash
GPU_IDX=$(( i % GPU_COUNT ))

PBRUN "${GPU_IDX}" haplotypecaller \
    --ref "${REF_FASTA}" \
    --in-bam "${INPUT_BAM}" \
    --out-variants "${GVCF}" \
    --gvcf \
    --num-htvc-threads "${per_gpu_cpus}" \
    --num-gpus 1
```

- `--gvcf`：输出 GVCF（等价 GATK `-ERC GVCF`），含 `<NON_REF>` 等位基因
- 输出 `.g.vcf.gz` 时 pbrun 通常自动生成 `.tbi`；**若未生成，用 `tabix -f -p vcf`**
  （`-f` 不可省：索引已存在时 tabix 退出码 1，在 `set -e` 下会杀死脚本）；
  更稳的做法是直接用 `tabix -f -p vcf "${GVCF}"` 无条件覆盖一次
- 输入仍须是 **BQSR 校正、含 `@RG`** 的 BAM（要求与 CPU 路径相同）
- 备选高精度：`pbrun deepvariant --gvcf`（AI 模型，同输出 GVCF，更适合追求准确率的场景）
- 具体参数名以 `pbrun haplotypecaller --help` 为准

### 与 CPU 路径的关键差异

| 项 | CPU（GATK HC） | GPU（pbrun haplotypecaller） |
|----|----------------|------------------------------|
| PairHMM | `--native-pair-hmm-threads`（CPU 线程） | GPU 并行，**不设该参数** |
| GVCF 索引 | GATK 自动写 `.tbi` | 自动 `.tbi`（需验证） |
| 输出 | `{sample}.g.vcf.gz` | 同（命名契约一致） |

## 数据库更新

- 启动时：`INSERT INTO sample_steps (step_name='haplotypecaller', status='running')`
- 完成后：`UPDATE sample_steps SET status='completed' WHERE ...`

## 脚本安全检查清单

1. ✅ BAM 输入有 `@RG` 标签
2. ✅ BAM 输入是 BQSR 校正后的
3. ✅ `-ERC GVCF` 正确设置
4. ✅ `--native-pair-hmm-threads` 合理设置
5. ✅ SESSION-SAFE 保护
6. ✅ **子进程失败必须累计**（`|| { FAILED=$((FAILED+1)); ... }`），**禁止** `wait ... || true`（会吞掉退出码）
7. ✅ **写 `.done` 前过校验门禁**：`stage_verify.py --stage 03_haplotypecaller`（查 `##ALT=<ID=NON_REF` 确认是真 GVCF），未过则写 `.failed` + `exit 1`
8. ✅ tbi 索引生成
9. ✅ resume 模式支持
10. ✅ 按 `compute_backend` 选择：parabricks→`pbrun haplotypecaller --gvcf`（不设 HMM 线程）；否则→GATK `-ERC GVCF`
11. ✅ GPU 路径：每样本绑 1 卡、输出命名契约一致、验证 `.tbi` 自动生成
