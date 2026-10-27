---
name: wgs-express-joint-genotyping
description: >
  Combine per-sample GVCFs with CombineGVCFs then jointly genotype with
  GenotypeGVCFs. Single cohort step (not per-sample). Called by the
  orchestration skill (wgs-express-pipeline) during Stage 4.
allowed-tools:
  - Read
  - Write
  - Bash
  - Grep
---

# CombineGVCFs + GenotypeGVCFs — Stage 4

合并所有样本的 GVCF 文件并进行联合基因分型。

**这是群组级（cohort-level）步骤**，不是逐样本的。

**由编排 skill 调用**，不直接暴露给用户。

## 输入

- `run.params.json` — 全部参数（样本列表、参考基因组等）
- `{RUN_DIR}/out/{sample_name}.g.vcf.gz` — 每个样本的 GVCF 文件
- `{RUN_DIR}/out/{sample_name}.g.vcf.gz.tbi` — 对应索引

## 输出

- `{RUN_DIR}/out/{prefix}.combined.g.vcf.gz` — 合并后 GVCF
- `{RUN_DIR}/out/{prefix}.combined.g.vcf.gz.tbi` — 索引
- `{RUN_DIR}/out/{prefix}.vcf.gz` — 联合基因分型后 VCF
- `{RUN_DIR}/out/{prefix}.vcf.gz.tbi` — 索引

## 脚本编制要点

### SESSION-SAFE 样板

完整的 `.done` / `.pid` / 日志配置。

### 工具路径（运行时探测，勿写死本机路径）

阶段脚本经 `conda run -n Agent bash` 执行，工具已在 PATH 中：

```bash
resolve_tool() { command -v "$1" || { echo "✗ 找不到 $1 —— 请激活 conda 环境 Agent" >&2; exit 1; }; }
GATK="$(resolve_tool gatk)"
# 不探测 tabix：GATK 自动写 .tbi（--create-output-variant-index 默认 true），
# 手工补 tabix 反而会因索引已存在而 rc=1 杀死脚本（见下方"索引"段注释）。
```

### CombineGVCFs 标准用法

```bash
# 构建 -V 参数列表
# ⚠️ 展开式 ${sample_names[@]+"${sample_names[@]}"} 不可省（实测 rc=1）：数组为空或
#    **未声明**时（如本脚本声明的是 `samples` 而非 `sample_names`），bash 4.2 +
#    `set -u` 下 `"${sample_names[@]}"` 直接 `unbound variable` 退出，
#    这一段的失败发生在收尾门禁**之前**，磁盘上既无 .done 也无 .failed。
#    `+` 形式对"空/未声明"展开为 0 个词 —— 于是下面能正常走到样本校验（SAMPLES_CSV
#    那段会拦住真正空名单的情况），而不会在数组名不一致时无声消失。
GVCF_INPUTS=""
for sample in ${sample_names[@]+"${sample_names[@]}"}; do
    GVCF="${RUN_DIR}/out/${sample}.g.vcf.gz"
    GVCF_INPUTS+=" -V ${GVCF}"
done

gatk CombineGVCFs --java-options "-Xmx${JAVA_XMX}" \
  -R "${REF_FASTA}" \
  ${GVCF_INPUTS} \
  -O "${COMBINED_GVCF}"
```

### GenotypeGVCFs 标准用法

```bash
gatk GenotypeGVCFs --java-options "-Xmx${JAVA_XMX}" \
  -R "${REF_FASTA}" \
  -V "${COMBINED_GVCF}" \
  -O "${JOINT_VCF}"
```

### 注意：CombineGVCFs 和 GenotypeGVCFs 都是单线程的

这两个工具**不支持多线程**，加速方式：
- 使用大 `-Xmx`（如 `-Xmx48g`）减少 GC 暂停
- 确保 I/O 不成为瓶颈
- 使用 SSD 或 tmpfs 作为临时目录

对于大规模群组（> 200 样本），应考虑使用 `GenomicsDBImport` 替代 `CombineGVCFs`。

### 断点续跑

```bash
# 如果合并后的 GVCF 已存在，跳过 CombineGVCFs
if [ -f "${COMBINED_GVCF}" ]; then
    log_info "Combined GVCF exists, skipping CombineGVCFs"
else
    # CombineGVCFs 命令 ...
fi

# 如果最终 VCF 已存在，跳过 GenotypeGVCFs
if [ -f "${JOINT_VCF}" ]; then
    log_info "Joint VCF exists, skipping GenotypeGVCFs"
else
    # GenotypeGVCFs 命令 ...
fi
```

## ⚠️ 错误预防清单

| # | 检查项 | 违反后果 |
|---|--------|---------|
| 1 | **所有 GVCF 文件必须存在且已索引** | CombineGVCFs 报错 |
| 2 | **GVCF 文件名与样本名一一对应** | 合并后样本命名错误 |
| 3 | **`--java-options` 正确使用** | `-J is not a recognized option` |
| 4 | **给足 -Xmx**（CombineGVCFs 内存需求随样本数线性增长） | OutOfMemoryError |
| 5 | **Parabricks 模式用 `pbrun genotypecaller`**，勿用 GATK GenotypeGVCFs | 参数不识别 / GPU 白开 |
| 6 | **`--in-vcf` 必须是合并后 GVCF**（`combined.g.vcf.gz`） | 输入错误，联合分型失败 |
| 7 | **超大群组（>100 样本）加 `--run-partition`** | 单卡 OOM |

### 内存建议

| 样本数 | 建议 -Xmx |
|--------|-----------|
| 1-5 | 32g |
| 5-20 | 48g |
| 20-100 | 64g+ |
| 100+ | 考虑 GenomicsDBImport |

## Parabricks 模式（compute_backend=parabricks）

**`compute_backend == "parabricks"` 时启用**。用 `pbrun genotypegvcf` 替代
GATK GenotypeGVCFs（⚠ 4.3.x **没有** `genotypecaller` 子命令，工具名是 `genotypegvcf`）。

### 工具路径

```bash
# 统一 PBRUN() docker wrapper（由 wgs-express-params-propose 在脚本头部展开，见其「PBRUN wrapper」）
# PBRUN <宿主GPU索引> <pbrun 子命令> ...
# ⚠️ 注意：本子命令**不接受** `--num-gpus`（见下方说明）。
```

### genotypegvcf 标准用法（单 GVCF 转 VCF）

```bash
GPU_IDX=0   # 群组级任务通常只取 1 张卡

PBRUN "${GPU_IDX}" genotypegvcf \
    --ref "${REF_FASTA}" \
    --in-gvcf "${COMBINED_GVCF}" \
    --out-vcf "${JOINT_VCF}" \
    --num-threads "${per_gpu_cpus}"
```

> ⚠️ **不要给 `genotypegvcf` 加 `--num-gpus 1`**（实测 clara-parabricks 4.3.2-1）：
> 该子命令是唯一**不接受**这个参数的 —— `pbrun genotypegvcf --num-gpus 1 ...`
> 当场 `rc=2`、`pbrun: error: unrecognized arguments: --num-gpus 1`、`out/` 零产物；
> 把它删掉后同样输入 `rc=0` 并正常产出 VCF + `.tbi`。这不是全局约定问题：
> 同容器里 `pbrun {markdup,bqsr,applybqsr,haplotypecaller,fq2bam} -h` 的 Run Options
> **每一个都有** `--num-gpus`（默认 1），只有 `genotypegvcf` 没有 —— 别按兄弟工具类推。
> 后果分两种，都比"报错停下"更难发现：
>   · 若按本 skill 清单要求做了失败累计（`PBRUN ... || FAILED=...`）→ 脚本继续走到
>     门禁，报 `[FAIL] joint.exists`、写 `.failed`（至少留痕）
>   · 若照抄上面模板（裸调用）→ `set -euo pipefail` 下**当场死在 pbrun 这一行**，
>     磁盘上 `.done`/`.failed` **都不存在**，编排层四态判定归为"已中断"、
>     引用户去查一个不存在的进程
> 另外该子命令**确实不需要 GPU**：容器不带 `--gpus` 时仍 `rc=0` 正常产出，
> 与"绑 1 卡"的旧注释相反。

- `--in-gvcf` 为合并后 GVCF（`{prefix}.combined.g.vcf.gz`）。**合并（CombineGVCFs）本身
  轻量，可保持 GATK CPU 执行**（非瓶颈），仅联合分型交给 GPU
- ⚠ 4.3.x 的 `genotypegvcf` 接受单个 `--in-gvcf`（无 `genotypecaller`、无 `--run-partition`）。
  大规模群组：合并仍走 GATK CombineGVCFs（CPU），联合分型对合并后的单 GVCF 调用一次
  `genotypegvcf`；超大群组若 GPU 内存不足则整体回退 GATK GenotypeGVCFs（CPU）
- 输出命名与 CPU 路径契约一致（`{prefix}.vcf.gz`），并验证 `.tbi` 生成
- 联合分型类单线程工具在 CPU 上耗时随样本数线性增长，GPU 优势随样本数放大

## 数据库更新

- 启动时：`INSERT INTO sample_steps (step_name='joint_genotyping', status='running')`
- 完成后：`UPDATE sample_steps SET status='completed' WHERE ...`

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
    { echo "stage=04_joint_genotyping"; echo "time=$(date '+%F %T')"
      echo "reason=校验器未指定或不可读：'${VERIFIER}'"
      echo "这是环境问题，不是数据问题。逐项核对："
      echo "  1) ${RUN_DIR}/run.params.json 是否存在、能否被 python json.load 解析"
      echo "  2) 其中的 verifier_path 是否为**非空**且为**绝对**路径"
      echo "  3) 该路径指向的文件是否还在（仓库被移动过？）"
    } > "${RUN_DIR}/.failed/04_joint_genotyping.failed"
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
    { echo "stage=04_joint_genotyping"; echo "time=$(date '+%F %T')"
      echo "reason=run.params.json 的 sample_names 为空，样本守恒判据无法运行"
    } > "${RUN_DIR}/.failed/04_joint_genotyping.failed"
    exit 1
fi

# ─── cohort 级输出前缀（本阶段独有：校验器靠它定位 combined/joint VCF）───
# ⚠️ 早期版本这里直接写 `${OUTPUT_PREFIX}`，而**全仓库没有任何一处给它赋值**
#    （实测 `grep -rn 'OUTPUT_PREFIX=' .` 零命中）—— 脚本头部是 `set -euo pipefail`，
#    变量展开当场 abort：rc=1、`OUTPUT_PREFIX: unbound variable`，磁盘上既无 .done
#    也无 .failed，收尾门禁一次都没跑起来。畸形配置与健康配置**同样死法**
#    （死在展开阶段，与数据无关），故不需要真实数据就能撞上。
#    现在从 run.params.json 的 `output_prefix` 读回（与 verifier_path / sample_names
#    同一范式：Stage 0 解析落盘、阶段侧读回）；读不到则**退化为空串** ——
#    校验器的 `--prefix` 是可选参数，它会自己登记一条 hard 的 prefix 判据，
#    把"拿不到前缀就无法定位输出 VCF"如实报出来，而不是让脚本先崩掉。
OUTPUT_PREFIX="$(python -c 'import json,sys; print(json.load(open(sys.argv[1])).get("output_prefix",""))' "${RUN_DIR}/run.params.json" 2>/dev/null || true)"

# 先删掉本阶段的旧报告：跑完后它只要存在，就必然是本轮的产物。
rm -f "${RUN_DIR}/.verify/04_joint_genotyping.json"
if ! python "${VERIFIER}" \
        --stage 04_joint_genotyping --run-dir "${RUN_DIR}" \
        --prefix "${OUTPUT_PREFIX}" --samples "${SAMPLES_CSV}"; then
    mkdir -p "${RUN_DIR}/.failed"
    { echo "stage=04_joint_genotyping"; echo "time=$(date '+%F %T')"
      # ⚠️ 只在报告**属于本轮**时才附上：校验器没跑起来时（路径错/python 缺失），
      #    .verify/ 里可能还留着上一次运行的文件，照抄会把排查引向数据而非环境。
      rep="${RUN_DIR}/.verify/04_joint_genotyping.json"
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
    } > "${RUN_DIR}/.failed/04_joint_genotyping.failed"
    log_error "结果校验未通过，不写 .done"; exit 1
fi
date > "${DONE_FILE}"
rm -f "${RUN_DIR}/.failed/04_joint_genotyping.failed"   # 清掉上一轮的陈旧失败标记

```

> **本阶段的核心判据是「VCF 样本数 == 输入 GVCF 数」**：样本在中间被丢掉时，
> 命令往往**成功退出**、VCF 也非空（只是少了几列样本），下游关联分析会静默地用错样本集 ——
> 这是最难事后发现的错误之一，必须在出口处拦（完整模板见编排层 SKILL.md §2.2）。

## 脚本安全检查清单

1. ✅ 所有输入 GVCF 都存在且已 `.tbi` 索引
2. ✅ -Xmx 足够大（根据样本数）
3. ✅ SESSION-SAFE 保护
4. ✅ **写 `.done` 前过校验门禁**：`stage_verify.py --stage 04_joint_genotyping`（查样本数守恒），未过则写 `.failed` + `exit 1`
5. ✅ 输出 VCF 带 `.tbi` 索引 —— 由 GATK 自动写出（`--create-output-variant-index` 默认 true），**不要**手工 `tabix`；确需重建用 `tabix -f`（裸 `tabix` 在索引已存在时 rc=1，`set -e` 下杀死脚本、够不到门禁）
6. ✅ resume 模式支持
7. ✅ 按 `compute_backend` 选择：parabricks→`pbrun genotypegvcf`（Combine 可留 GATK CPU）；否则→GATK
8. ✅ GPU 路径：`--in-gvcf` 为合并 GVCF、输出契约一致、`.tbi` 已验证
