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
| FASTQ 文件 | 每样本 `{sample_number}_1.fq.gz` / `{sample_number}_2.fq.gz` |

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
echo $$ > "${PID_FILE}"
trap 'rm -f "${PID_FILE}"' EXIT

# ─── 日志函数 ───
log_info()  { echo "[$(date '+%Y-%m-%d %H:%M:%S')] [INFO]  $*"; }
log_step()  { echo "[$(date '+%Y-%m-%d %H:%M:%S')] [STEP]  $*"; }
log_done()  { echo "[$(date '+%Y-%m-%d %H:%M:%S')] [DONE]  $*"; }
log_error() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] [ERROR] $*"; }
```

### 工具全路径（⚠️ 必须使用 conda 绝对路径）

```bash
BWA="/home/chenyuxi/anaconda3/envs/Agent/bin/bwa"
SAMTOOLS="/home/chenyuxi/anaconda3/envs/Agent/bin/samtools"
NUMACTL="/home/chenyuxi/anaconda3/envs/Agent/bin/numactl"
```

### NUMA 亲和性配置

```bash
NUMA_NODE0_CPUS="0-31,64-95"
NUMA_NODE1_CPUS="32-63,96-127"
assign_numa_cmd() {
    local idx=$1
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

for i in "${!samples[@]}"; do
    sample="${samples[$i]}"
    
    # 检查是否已存在（resume 模式）
    if [ -f "${BAM}" ]; then
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
                wait "${pids[$j]}" || true
                unset 'pids[$j]'
            fi
        done
        sleep 10
    done
done

# 等待所有后台作业完成
for pid in "${pids[@]}"; do
    wait "${pid}" || log_error "Subprocess ${pid} failed"
done
```

### 完成标记

```bash
# ─── 完成标记 ───
log_info "All samples completed for Stage 1"
date > "${DONE_FILE}"
log_done "Stage 1 complete"
```

## Parabricks 模式（compute_backend=parabricks）

当 `run.params.json` 中 `compute_backend == "parabricks"`（且 `parabricks.smoke_test` 已通过），
本阶段用 `pbrun fq2bam` **一步完成比对 + 坐标排序 + 索引**，替代 BWA+samtools sort。

### 工具路径

```bash
# 统一 PBRUN() docker wrapper（由 wgs-express-params-propose 在脚本头部展开，见其「PBRUN wrapper」）
# 每样本: PBRUN <宿主GPU索引> <pbrun 子命令> ... ；容器内只暴露 1 张卡 → pbrun 用 --num-gpus 1
```

### fq2bam 标准用法（PE，逐样本绑 1 卡）

```bash
# i: 样本循环下标; GPU_COUNT / PER_GPU_CPUS: 读自 run.params.json.parabricks
GPU_IDX=$(( i % GPU_COUNT ))

PBRUN "${GPU_IDX}" fq2bam \
    --ref "${REF_FASTA}" \
    --in-fq "${R1}" "${R2}" \
    --out-bam "${BAM}" \
    --num-cpu "${per_gpu_cpus}" \
    --num-gpus 1 \
    --low-memory --bwa-nstreams 1 \
    --tmp-dir "${TMP_DIR}"
```

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
| Read Group | 必须手工 `-R` | fq2bam 自动生成（符合 `SM` 即可） |
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
3. ✅ 所有命令在 `conda run -nAgent` 环境下执行
4. ✅ 工具使用 conda 绝对路径
5. ✅ `set -euo pipefail`
6. ✅ SESSION-SAFE 保护（`.done` + `.pid` + nohup）
7. ✅ 样本并行 + NUMA 绑定（多样本时）
8. ✅ 完成标记写入
9. ✅ 中间文件清理（删除 SAM 临时文件）
10. ✅ 若 `--resume` 开启，输出文件存在性检查
11. ✅ 按 `compute_backend` 选命令路径：`parabricks`→`pbrun fq2bam`（禁 BWA）；否则→BWA+samtools
12. ✅ GPU 路径：`--out-bam` 符合契约、每样本绑 1 卡（`--num-gpus`）、`pbrun fq2bam --help` 复核参数名
