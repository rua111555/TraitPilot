---
name: wgs-express-filtering
description: >
  Split joint VCF into SNPs and INDELs, apply variant-level hard filters
  using GATK SelectVariants and VariantFiltration. Final stage of the WGS
  expressive pipeline. Called by the orchestration skill (wgs-express-pipeline)
  during Stage 5.
allowed-tools:
  - Read
  - Write
  - Bash
  - Grep
---

# SelectVariants + VariantFiltration — Stage 5

从联合基因分型后的 VCF 中提取 SNP 和 INDEL，分别应用硬过滤条件，
生成过滤后的最终 VCF 文件。

**这是最终阶段**（cohort-level），完成后管线结束。

**由编排 skill 调用**，不直接暴露给用户。

> **无 GPU/Parabricks 路径**：SelectVariants / VariantFiltration 是轻量 VCF 文本处理
> （秒~分钟级），GPU 无收益。无论 `compute_backend` 为何，本阶段**恒用 GATK CPU**。
> 过滤条件与流程不受后端影响。

## 输入

- `run.params.json` — 全部参数
- `{RUN_DIR}/out/{prefix}.vcf.gz` — 联合基因分型后 VCF

## 输出

- `{RUN_DIR}/out/{prefix}.snp.raw.vcf.gz` — SNP 原始 VCF
- `{RUN_DIR}/out/{prefix}.indel.raw.vcf.gz` — INDEL 原始 VCF
- `{RUN_DIR}/out/{prefix}.snp.filter.vcf.gz` — **最终**过滤后 SNP VCF
- `{RUN_DIR}/out/{prefix}.indel.filter.vcf.gz` — **最终**过滤后 INDEL VCF
- 对应 `.tbi` 索引文件

## 过滤条件

### SNP 硬过滤

```
QD < 2.0 || MQ < 40.0 || FS > 60.0 || SOR > 3.0 || MQRankSum < -12.5 || ReadPosRankSum < -8.0
```

### INDEL 硬过滤

```
QD < 2.0 || FS > 200.0 || SOR > 10.0 || MQRankSum < -12.5 || ReadPosRankSum < -20.0
```

**注意**：INDEL 的 FS 阈值是 200（不是 60），因为真实 INDEL 的自然链偏倚更高。
MQ 和 MQRankSum 在 INDEL 过滤中不适用（不做诊断）。

### ⚠️ 同型合子位点的 "missing => PASS" 陷阱

`MQRankSum`、`ReadPosRankSum` 和 `BaseQRankSum` 是秩和检验统计量，**只在杂合位点上有定义**。
同型合子（hom-alt）位点没有参比等位基因的 reads，因此这些注释缺失（`.`）。

GATK 的 `VariantFiltration` 仅在**值存在且违反阈值时**才标记过滤——缺失值通过 PASS。
如果使用 `bcftools filter` 手动实现，必须为每个 RankSum 项添加显式的 `|| INFO/X = "."` 保护，
否则每个同型合子位点都会无声地失败并消失。

## 脚本编制要点

### SESSION-SAFE 样板

完整的 `.done` / `.pid` / 日志配置。

### 工具路径（运行时探测，勿写死本机路径）

阶段脚本经 `conda run -n Agent bash` 执行，工具已在 PATH 中：

```bash
resolve_tool() { command -v "$1" || { echo "✗ 找不到 $1 —— 请激活 conda 环境 Agent" >&2; exit 1; }; }
GATK="$(resolve_tool gatk)"
# 不探测 tabix：VariantFiltration 自动写 .tbi（--create-output-variant-index
# 默认 true），手工补 tabix 反而因索引已存在而 rc=1 杀死脚本（见 idx 段注释）。
```

### 标准用法

```bash
# ─── 1. 提取 SNP ───
gatk SelectVariants --java-options "-Xmx${JAVA_XMX}" \
  -R "${REF_FASTA}" \
  -V "${JOINT_VCF}" \
  --select-type-to-include SNP \
  -O "${SNP_RAW}"

# ─── 2. 提取 INDEL ───
gatk SelectVariants --java-options "-Xmx${JAVA_XMX}" \
  -R "${REF_FASTA}" \
  -V "${JOINT_VCF}" \
  --select-type-to-include INDEL \
  -O "${INDEL_RAW}"

# ─── 3. SNP 过滤 ───
gatk VariantFiltration --java-options "-Xmx${JAVA_XMX}" \
  -R "${REF_FASTA}" \
  -V "${SNP_RAW}" \
  --filter-expression "QD < 2.0" --filter-name "QD2" \
  --filter-expression "FS > 60.0" --filter-name "FS60" \
  --filter-expression "MQ < 40.0" --filter-name "MQ40" \
  --filter-expression "MQRankSum < -12.5" --filter-name "MQRankSum-12.5" \
  --filter-expression "ReadPosRankSum < -8.0" --filter-name "ReadPosRankSum-8" \
  --filter-expression "SOR > 3.0" --filter-name "SOR3" \
  -O "${SNP_FILTER}"

# ─── 4. INDEL 过滤 ───
gatk VariantFiltration --java-options "-Xmx${JAVA_XMX}" \
  -R "${REF_FASTA}" \
  -V "${INDEL_RAW}" \
  --filter-expression "QD < 2.0" --filter-name "QD2" \
  --filter-expression "FS > 200.0" --filter-name "FS200" \
  --filter-expression "ReadPosRankSum < -20.0" --filter-name "ReadPosRankSum-20" \
  --filter-expression "SOR > 10.0" --filter-name "SOR10" \
  -O "${INDEL_FILTER}"

# ─── 5. 索引：**不需要**单独做 ───
# GATK 的 `--create-output-variant-index` 默认即 true，上面两条 VariantFiltration
# 已各自写出 `${SNP_FILTER}.tbi` / `${INDEL_FILTER}.tbi`（本机实跑复核）。
# ⚠ 不要再补 `tabix -p vcf ...`：索引已存在时 tabix **退出码 1**
#   （`[tabix] the index file exists. Please use '-f' to overwrite.`），
#   而本脚本头部是 `set -euo pipefail` —— 脚本会死在门禁**之前**，
#   健康的过滤结果既不写 .done 也不写 .failed，编排层误判成"已中断"。
```

## 过滤验证

过滤后应进行质量控制验证：

```bash
# 查看各 FILTER 分类的计数
bcftools query -f '%FILTER\n' "${SNP_FILTER}" | sort | uniq -c

# 计算 Ti/Tv 比
bcftools stats "${SNP_FILTER}" | grep '^TSTV'

# 预期：WGS Ti/Tv 约 2.0-2.1
```

## ⚠️ 错误预防清单

| # | 检查项 | 违反后果 |
|---|--------|---------|
| 1 | **SNP 和 INDEL 使用不同过滤条件** | 阈值不适用导致真假混合 |
| 2 | **`--java-options` 正确使用** | `-J is not a recognized option` |
| 3 | **输出文件必须带 `.tbi` 索引** —— 由 GATK 自动写出，**不要**手工 `tabix` | 下游工具无法使用 `.vcf.gz`。手工补 tabix 会因索引已存在而 rc=1，在 `set -e` 下杀死脚本（见上方索引段注释） |
| 4 | **过滤后验证 Ti/Tv** | 未检测的过度过滤 |

## 最终输出汇报

管线完成后，LLM **必须**向用户汇报最终产出：

```
✅ WGS Pipeline Complete!
Final outputs:
  SNP VCF:   {RUN_DIR}/out/{prefix}.snp.filter.vcf.gz
  INDEL VCF: {RUN_DIR}/out/{prefix}.indel.filter.vcf.gz
  SNP stats: {SNP_PASS_COUNT} PASS, {SNP_FILTER_COUNT} filtered (Ti/Tv: {TITV})
  INDEL stats: {INDEL_PASS_COUNT} PASS, {INDEL_FILTER_COUNT} filtered
```

## 数据库更新

- 启动时：`INSERT INTO sample_steps (step_name='filtering', status='running')`
- 完成后：
  - `UPDATE sample_steps SET status='completed' WHERE step_name='filtering'`
  - `UPDATE pipeline_runs SET status='completed', completed_at=datetime('now') WHERE run_id='...'`

## 收尾：结果校验门禁（**必须先过校验才写 `.done`**）

```bash
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
    { echo "stage=05_filtering"; echo "time=$(date '+%F %T')"
      echo "reason=校验器未指定或不可读：'${VERIFIER}'"
      echo "这是环境问题，不是数据问题。逐项核对："
      echo "  1) ${RUN_DIR}/run.params.json 是否存在、能否被 python json.load 解析"
      echo "  2) 其中的 verifier_path 是否为**非空**且为**绝对**路径"
      echo "  3) 该路径指向的文件是否还在（仓库被移动过？）"
    } > "${RUN_DIR}/.failed/05_filtering.failed"
    exit 1
fi
# ─── 样本名单（本 stage 无样本循环，但门禁需要它做样本集守恒）───
# 同上：加 `|| true`，否则 run.params.json 读不出时 set -e 直接退出、
# 下面的"样本名单为空"分支（会写 .failed）根本到不了。
SAMPLES_CSV="$(python -c 'import json,sys; print(",".join(json.load(open(sys.argv[1])).get("sample_names") or []))' "${RUN_DIR}/run.params.json" 2>/dev/null || true)"
if [ -z "${SAMPLES_CSV}" ]; then
    log_error "样本名单为空（run.params.json 的 sample_names 缺失或为 []）—— 不写 .done"
    mkdir -p "${RUN_DIR}/.failed"
    { echo "stage=05_filtering"; echo "time=$(date '+%F %T')"
      echo "reason=run.params.json 的 sample_names 为空，样本守恒判据无法运行"
    } > "${RUN_DIR}/.failed/05_filtering.failed"
    exit 1
fi

# 先删掉本阶段的旧报告：跑完后它只要存在，就必然是本轮的产物。
rm -f "${RUN_DIR}/.verify/05_filtering.json"
if ! python "${VERIFIER}" \
        --stage 05_filtering --run-dir "${RUN_DIR}" --prefix "${OUTPUT_PREFIX}" \
        --samples "${SAMPLES_CSV}"; then
    mkdir -p "${RUN_DIR}/.failed"
    { echo "stage=05_filtering"; echo "time=$(date '+%F %T')"
      # ⚠️ 只在报告**属于本轮**时才附上：校验器没跑起来时（路径错/python 缺失），
      #    .verify/ 里可能还留着上一次运行的文件，照抄会把排查引向数据而非环境。
      rep="${RUN_DIR}/.verify/05_filtering.json"
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
    } > "${RUN_DIR}/.failed/05_filtering.failed"
    log_error "结果校验未通过，不写 .done"; exit 1
fi
date > "${DONE_FILE}"
rm -f "${RUN_DIR}/.failed/05_filtering.failed"   # 清掉上一轮的陈旧失败标记

```

> 校验器在本阶段查五件事：SNP/INDEL **各自**都有 PASS 位点（**只看 SNP 会漏掉
> 「INDEL 被滤光」** —— INDEL 的 FS/MQRankSum 阈值比 SNP 严得多，这是真实且常见的失败）、
> Ti/Tv 落在 1.8-2.3、**产出样本数与上游一致**、以及没有大批位点的 FILTER 是 `.`
> （`.` 是"未记录过滤判定"，说明 VariantFiltration 没跑）。
> ⚠️ **必须传 `--samples`**：本阶段是**最后一棒**，下游没有任何阶段会再核样本集。
> 早期版本的调用行漏了 `--samples`，于是"样本列被换掉 / 丢一半"的 VCF 照样放行 ——
> 而上面这段说明当时已写着"产出样本数与上游一致"（纸面具备）。
> 若本阶段脚本里还没有 `sample_names` 数组，**必须**从 `run.params.json` 读出来
> （与其它阶段一致），不能因为"过滤是队列级操作、不按样本循环"就省掉它。
> 上面「过滤验证」那段 Ti/Tv 命令原本只是**打印**给人看，现在接进了门禁（判据为 warn 级：
> 偏离则告警但不拦截，因为 Ti/Tv 受数据本身影响）。

## 脚本安全检查清单

1. ✅ SNP 和 INDEL 分别提取
2. ✅ SNP 和 INDEL 使用不同的过滤条件
3. ✅ `--java-options` 正确使用
4. ✅ 输出带 `.tbi` 索引（GATK 自动产出，无需手工 tabix）
5. ✅ SESSION-SAFE 保护
6. ✅ **写 `.done` 前过校验门禁**：`stage_verify.py --stage 05_filtering`（SNP/INDEL 各自非空、Ti/Tv），未过则写 `.failed` + `exit 1`
7. ✅ 最终产出汇报（含校验器的 warn 项）
8. ✅ 数据库状态更新
9. ✅ 本阶段**恒用 GATK CPU**（无 Parabricks 路径），`-Xmx` 取自 `parallelism.java_xmx`
