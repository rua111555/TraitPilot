---
name: wgs-express-params-propose
description: >
  Query the SQLite database and propose pipeline parameters based on server
  detection results. Called by the orchestration skill (wgs-express-pipeline)
  during Stage 0.
allowed-tools:
  - Read
  - Write
  - Bash
  - Grep
---

# Parameter Proposal — Stage 0

基于数据库信息 + 服务器检测结果，推理各工具参数，提交用户审核。

**由编排 skill 调用**，不直接暴露给用户。

## 前提条件

- Stage S0（服务器检测）已完成，`server_detection`（含 `gpu_detection`）和 `parallelism` 参数已确定
- `--samples` 和 `--prefix` 由编排 skill 传入
- `--backend <gatk-cpu|parabricks>`（可选）：用户显式覆盖后端决策；缺省按「后端决策」规则自动推理

## 查询数据库

使用 `db_query.py` 获取所有必需路径：

```bash
conda run -n Agent python "$(dirname "${BASH_SOURCE[0]:-$0}")/../wgs-expressive-pipeline/scripts/db_query.py" \
  --action query-all --samples <SAMPLE_NUMBERS>
# --db 缺省取 $PIPELINE_DB，其次 $GENO_DIR/pipeline.db；需要时显式 --db <路径>
```

获取内容：
- **参考基因组**：fasta 路径、dict 路径、fai 路径
- **各样本 fastq**：路径、大小、样本名映射
- **配置**：known INDEL/SNP 路径、输出基目录

## 参数推理

依据服务器检测结果推理各工具参数：

| 参数 | 推理依据 |
|------|---------|
| `java.xmx` | 系统可用内存。推荐 `min(48, available_mem_gb / parallel_samples)` |
| `bwa.threads` | CPU 核数 + BWA I/O 特性（>16 收益递减） |
| `samtools.sort.threads` | 可用核数 + `-m 2G` |
| `haplotypecaller.hmm_threads` | 可用核数，推荐 4-8 |
| `filter` 表达式 | GATK Best Practices 标准 |
| `--filter-name` | 标准命名惯例 |

### 详细策略

```
线程分配（GATK 4.3 非 Spark 版核心工具是单线程的，重点靠样本间并行）：
  - bwa mem -t min(16, usable_cores // parallel_samples)
  - samtools sort -@ min(8, usable_cores / 2 / parallel_samples) -m 2G
  - MarkDuplicates/BaseRecalibrator/ApplyBQSR: 单线程，给足 -Xmx
  - HaplotypeCaller --native-pair-hmm-threads min(8, usable_cores / parallel_samples / 4)
  - CombineGVCFs/GenotypeGVCFs: 单线程

Java Xmx 分配（**按 S0 探测到的可用内存计算，勿套用某一台机器的固定值**）：
  - 取 S0 的 `available_mem_gb`，按并行样本数分摊后留余量
  - 推荐 `-Xmx` = min(48, available_mem_gb / parallel_samples) GB（GATK 堆空间足够即可，减少 GC）
```

### 后端决策：compute_backend

依据 S0 的 `gpu_detection` 决策本管线的计算后端：

```python
# 输入来自 server_detection.gpu_detection
GPU_PRESENT   = server_detection["gpu_detection"]["gpu_present"]
PARABRICKS_READY = server_detection["gpu_detection"]["parabricks_ready"]
SMOKE_TEST_PASSED = server_detection["gpu_detection"].get("smoke_test_passed", False)
GPU_CLASS     = server_detection["gpu_detection"].get("gpu_class", "unknown")

# 决策规则
if GPU_PRESENT and PARABRICKS_READY:
    if GPU_CLASS == "datacenter" or SMOKE_TEST_PASSED:
        compute_backend = "parabricks"     # 自动提案（仍需用户审批）
    else:
        compute_backend = "gatk-cpu"       # consumer 卡冒烟未过 → 回退
        # 提示用户运行 references/parabricks-setup.md 中的冒烟测试后重审
else:
    compute_backend = "gatk-cpu"           # 无 GPU 或 Parabricks 未就绪
```

- 用户可用 `--backend parabricks|gatk-cpu` 手动覆盖
- **冒烟测试**定义见 `wgs-expressive-pipeline/references/parabricks-setup.md`：
  `pbrun version` → 1 小样本 `fq2bam` → 1 个 tiny `haplotypecaller --gvcf` 三重验证
- consumer 卡（RTX 4090，非官方支持）**必须**冒烟通过才走 Parabricks

### Parabricks 参数（仅 backend=parabricks 时）

在 `run.params.json` 中写入 `parabricks` 块：

```json
{
  "parabricks": {
    "runtime": "docker",
    "image": "nvcr.io/nvidia/clara/clara-parabricks:4.3.2-1",
    "mounts": ["<数据根>:<数据根>", "<示例:$HOME>:<示例:$HOME>"],
    "gpus_to_use": 8,
    "parallel_samples": 8,
    "per_gpu_cpus": 16,
    "smoke_test": "passed",
    "note": "gpu_class=consumer(非官方支持)，预检与冒烟测试均已通过"
  }
}
```

- `runtime`: **`docker`**（官方分发为 NGC 容器）。若未来 NGC 恢复 native tarball，可用
  `runtime=native` + `binary=/path/pbrun`（本参数结构兼容两种）
- `image`: 本地已拉取的 NGC 镜像 tag（见 `references/parabricks-setup.md` §1.3）
- `mounts`: **必须**覆盖数据目录、参考基因组、输出目录所在宿主路径；否则容器内看不到文件
- `gpus_to_use`: ≤ S0 的 `gpu_count`；分配方式为每样本绑 1 张宿主卡（见下方 PBRUN wrapper）
- 该块决定阶段子 skill 选择 Parabricks 命令模板（各阶段的「Parabricks 模式」）

### PBRUN docker wrapper（生成脚本时写入每阶段脚本头部）

编排层为每阶段生成的脚本**必须**以此 wrapper 调用 pbrun（单一定义点，阶段子 skill 复用）：

```bash
# —————— 从 run.params.json 展开 ——————
PB_IMAGE="nvcr.io/nvidia/clara/clara-parabricks:4.3.2-1"
PB_PERMISSION="-u $(id -u):$(id -g)"      # 容器写出的文件属主=当前用户
# 每样本: PBRUN <宿主GPU索引> <pbrun 子命令> ...
PBRUN() {
    local gpu="${1}"; shift
    # 挂载数据根与家目录（用同一宿主路径映射，容器内外路径一致）
    local GENO; GENO="$(sk_geno_dir 2>/dev/null || echo "${GENO_DIR:-$PWD}")"
    docker run --rm ${PB_PERMISSION} \
        --gpus "device=${gpu}" \
        -v "${GENO}:${GENO}" -v "${HOME}:${HOME}" \
        -w "$(pwd)" \
        "${PB_IMAGE}" pbrun "$@"
}
```

**关键正确性约定**：
1. `--gpus "device=<宿主卡号>"` 只暴露 1 张卡 → **容器内 pbrun 一律 `--num-gpus 1`**
   （⚠ 4.3.x **没有** `--gpu` 参数：`--gpu 0` 会与 `--gpuwrite/--gpusort` 前缀歧义报错；
   各工具输入/输出/线程参数名亦不同，生成脚本前查 `pbrun <tool> --help`，速查表见
   `parabricks-setup.md` §4.1）
2. 输出/临时文件目录必须在已挂载路径下；`-w "$(pwd)"` 让相对路径映射到宿主对应目录
3. `scripts/gpu_preflight.sh` 用同一镜像验证过 `pbrun` 可跑，脚本才允许走 Parabricks 路径

## 输出：`run.params.json`

LLM **必须**生成以下 JSON 文件并保存至 `{RUN_DIR}/run.params.json`。
**所有路径必须来自 `db_query.py` 的查询结果或用户输入**（`<GENO>` = 数据根，见环境变量
`GENO_DIR`，缺省 `~/genomics_data`，切勿把某一台机器的路径写进生成物）：

```json
{
  "run_id": "wgs_20260716_143000",
  "output_prefix": "COHORT",
  "samples": ["1", "2", "10"],
  "sample_names": ["sample1", "sample2", "sample10"],
  "reference": {
    "fasta": "<GENO>/reference/ref.fa",
    "dict": "<GENO>/reference/ref.dict",
    "fai": "<GENO>/reference/ref.fa.fai"
  },
  "known_sites": {
    "indels": "<GENO>/snp_indel/clean.indels.vcf.gz",
    "snps": "<GENO>/snp_indel/clean.snp.vcf.gz"
  },
  "output_dir_base": "<GENO>/temp",
  "fastq_dir": "<GENO>/fastq",
  "verifier_path": "<REPO>/skills/wgs-expressive-pipeline/scripts/stage_verify.py",
  "min_variants": 0,
  "compute_backend": "parabricks",
  "server_detection": {
    "total_cores": 128,
    "available_mem_gb": 200,
    "gpu_detection": {
      "gpu_present": true,
      "gpu_count": 8,
      "gpu_models": ["NVIDIA GeForce RTX 4090"],
      "gpu_vram_gb": 24,
      "compute_cap": "8.9",
      "gpu_class": "consumer",
      "parabricks_ready": true,
      "smoke_test_passed": true
    }
  },
  "parallelism": {
    "parallel_samples": 3,
    "bwa_threads": 8,
    "samtools_sort_threads": 4,
    "haplotypecaller_hmm_threads": 4,
    "java_xmx": "32g",
    "max_concurrent_pairs": 6
  },
  "parabricks": {
    "runtime": "docker",
    "image": "nvcr.io/nvidia/clara/clara-parabricks:4.3.2-1",
    "mounts": ["<数据根>:<数据根>", "<示例:$HOME>:<示例:$HOME>"],
    "gpus_to_use": 8,
    "parallel_samples": 8,
    "per_gpu_cpus": 16,
    "smoke_test": "passed"
  },
  "run_dir": "<GENO>/temp/runs/wgs_20260716_143000",
  "resume": false,
  "threads": 40,
  "bwa_threads": 8,
  "samtools_sort_threads": 4,
  "java_xmx": "32g",
  "haplotypecaller_hmm_threads": 4
}
```

> 说明：`compute_backend` 为 `parabricks` 时，阶段 1-4 的脚本按 `parabricks` 块生成
> Parabricks 命令；阶段 5 过滤无论何种后端恒用 GATK（CPU）。`parallelism` 块始终保留，
> 作为回退 GATK 路径与 Java 工具（MarkDuplicates/过滤）的线程基准。
>
> **`verifier_path` 必须写成绝对路径**（`<REPO>` = 本仓库根，即 `skills/` 的上一级）。
> 阶段脚本位于 `{RUN_DIR}/scripts/`，与仓库无固定相对关系，只能靠这里解析出的绝对路径
> 定位校验器。
>
> **`min_variants` = 位点数下限**，供 Stage 3/4/5 的"变异数异常少"判据使用：
> 校验器在调用方未传 `--min-variants` 时**读回本字段**（与 `verifier_path` 同范式）。
> 取值按**本次运行的参考基因组大小与队列规模**推算 —— 没有普适默认值（人类全基因组
> 队列通常 ≥ 数百万位点；本仓库探针用的 200kb 迷你 ref 则差 4 个数量级）。
> **必须与用户确认后写入**，不要自己拍一个数：拍高了拦健康数据，拍低了形同虚设。
> 用户明确不做此判定时写 `0`（= 未声明，校验器登记一条 warn 明说"未做该判定"）。
> ⚠️ 不要写 `"1M"` 这类字符串、负数或小数 —— 校验器会判 **hard 不通过**（想开却没生效
> 必须被看见，而不是静默退回"未声明"）。
>
> 调用方是**阶段脚本自己**（不是编排层代劳）：每个阶段 skill 的收尾门禁段都从
> `${RUN_DIR}/run.params.json` 读出本字段赋给 `VERIFIER`，再以
> `python "${VERIFIER}" --stage ...` 调用。本字段缺失时，门禁段会写 `.failed`
> 并以 `exit 1` 结束 —— 属有意为之：**校验器定位不到就不放行**（宁可早失败，
> 也不要静默跳过校验）。注意这类失败是**环境问题**而非数据问题，`.failed` 里会
> 明确写出这一点，免得排查被引向数据。

## 用户审核

LLM **必须**以表格形式展示所有参数给用户审核：

```
===== Server Status =====   （数值全部来自 S0 的 server_detection，不要照抄示例）
CPU: {cpu_model}
Sockets: {sockets} × {cores_per_socket} cores × {threads_per_core} threads = {total_cores} logical CPUs
Memory: {total_mem_gb}GB total, ~{avail_mem_gb}GB available
Load: {load_avg}
GPU: {gpu_count} × {gpu_model} ({gpu_vram_gb}GB, CC {compute_cap}) — parabricks ready: {✓/✗} ({gpu_class})

===== Parameter Proposal =====
Run ID: wgs_20260716_143000
Samples: 1 (sample1), 2 (sample2), 10 (sample10)

Parameter                         Value           Reasoning
─────────────────────────────────────────────────────────────────
compute_backend                   {parabricks|gatk-cpu}   {GPU 存在 + 冒烟通过 → GPU 加速 / 否则 CPU}
parallelism.parallel_samples      {N}             (GATK 回退基准)
parabricks.parallel_samples       {N}             GPU 卡数主导
parabricks.gpus_to_use            {N}             每样本绑 1 卡（--gpus device=N）
parabricks.per_gpu_cpus           {N}             每 GPU 配套 CPU 线程
parabricks.runtime                docker          NGC 容器
parabricks.image                  {NGC 镜像 tag}
bwa.threads                       {N}             ...
samtools.sort.threads             {N}             ...
java.xmx                          {Xmx}g          ...
...

Please review. Do you approve? (y/N)
```

## run_id 命名规范

- 格式：`wgs_YYYYMMDD_HHMMSS`
- 使用 `date +%Y%m%d_%H%M%S` 生成

## prefix 自动生成策略

如果用户未提供 `--prefix`：
1. 优先从数据库中样本所属项目推断
2. 从样本编号组合生成（如 "S1_2_10"）
3. 使用当前日期（如 "WGS_20260716"）
4. 向用户询问项目名缩写（可选）

## 错误处理

- 数据库不存在 → 提示用户运行 `init_db.py`
- 样本在数据库中不存在 → 提示可用样本列表
- 必备配置项缺失（known sites 等）→ LLM 使用合理默认值并提示用户
- `gpu_detection` 缺失（老 S0 版本）→ 视为无 GPU，`compute_backend=gatk-cpu`
- 冒烟测试未过但用户仍坚持 Parabricks → 明确警告风险后放行，并逐阶段监视 OOM，失败即回退 GATK
