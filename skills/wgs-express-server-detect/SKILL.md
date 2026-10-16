---
name: wgs-express-server-detect
description: >
  Detect server CPU topology, memory, current load, and automatically compute
  optimal parallelism for WGS pipeline stages. Called by the orchestration skill
  (wgs-express-pipeline) during Stage S0.
allowed-tools:
  - Read
  - Write
  - Bash
  - Grep
---

# Server Detection — Stage S0

检测服务器状态并自动计算 WGS 管线的最佳并行度参数。

**由编排 skill 调用**，不直接暴露给用户。

## 检测项目

### 1. CPU 拓扑检测

```bash
lscpu | grep -E "Thread|Core|Socket|NUMA|Model name"
nproc
```

**分析要点**：
- 物理 socket 数、每 socket 核心数、每核心线程数
- NUMA 节点拓扑 → 指导 numa 亲和性策略

### 2. 当前系统负载检测

```bash
top -bn1 | head -5            # load average + CPU idle%
ps aux | wc -l                # 总进程数
```

**分析要点**：
- load average：若接近或超过 nproc，系统繁忙 → 减少线程
- CPU idle%：若 < 20%，留更多余量

### 3. 可用内存检测

```bash
free -g
```

**分析要点**：决定 `java.xmx` 和 samtools `-m` 参数

### 4. 进程亲和性检查

```bash
ps aux --sort=-%cpu | head -20
cat /proc/stat | grep "^cpu[0-9]" | awk '{print $1, ($2+$4)*100/($2+$4+$5)}' | head -40
```

### 5. GPU 检测（用于 Parabricks 加速决策）

**当服务器存在 NVIDIA GPU 时，可启用 Parabricks（GPU 加速版 GATK）替代 CPU 上的
BWA/samtools/gatk。此检测决定 `compute_backend`（详见 Stage 0）。**

```bash
# 首选：nvidia-smi 查询（含 VRAM / 算力 / 当前占用）
nvidia-smi --query-gpu=index,name,memory.total,memory.used,compute_cap,utilization.gpu,driver_version --format=csv
nvidia-smi -L                          # GPU 型号列表
nvidia-smi -L | wc -l                  # GPU 数量（query-gpu=count 会逐卡重复，勿用）

# 兜底：nvidia-smi 不存在时用 lspci
lspci | grep -iE "nvidia|Radeon|3D controller"
```

**分析要点**：
- **GPU 数量、型号、单卡 VRAM** → 决定 `parallel_samples`（样本并行上限）与单卡显存是否够用
- **compute capability** → Parabricks 要求 CC ≥ 7.0；本机 RTX 4090 为 CC 8.9，达标
- **gpu_class 分类**：datacenter（V100/T4/A100/H100/L40S/L4/RTX-A6000 等，官方验证）vs
  consumer（GeForce/RTX 4090 等**非官方支持**）→ 记入 `parabricks_ready`，consumer 卡**必须先过冒烟测试**
- **当前利用率/已用显存** → 若其他用户占用 GPU，减少分配或提示

**Parabricks 可用性检查（`parabricks_ready`）**——统一执行预检脚本，一次探测
GPU/docker/toolkit/镜像/容器内 pbrun，输出 JSON：

```bash
conda run -n Agent bash skills/wgs-expressive-pipeline/scripts/gpu_preflight.sh
# 必读字段:
#   gpu_detection.*          GPU 检测结果（数量/型号/显存/算力/利用率）
#   docker_ok                docker daemon 是否可访问（无权限→ json 内给出修复命令）
#   toolkit_ok / nvidia_runtime_ok   nvidia-container-toolkit 就绪？
#   parabricks_image         本地已拉取的 NGC 镜像（空→ 需 docker pull）
#   pbrun_version            容器内 pbrun version（license 初判）
#   parabricks_ready         上述全过才为 true
```

`parabricks_ready` 判定 = `gpu_present ∧ docker_ok ∧ toolkit_ok ∧ 镜像存在 ∧ 容器内 pbrun version 通过`。
安装/授权步骤见 `references/parabricks-setup.md`（docker 方式，含加组/NGC 登录/拉镜像/license/冒烟测试）。

## 自动计算最佳并行度

LLM **必须**依据检测结果计算以下参数：

```python
TOTAL_CORES     = # 从 lscpu 获取
SOCKETS         = # 物理 socket 数
CORES_PER_SOCKET = # 每 socket 物理核数
THREADS_PER_CORE = # 每核超线程数
LOAD_AVG        = # 从 top 获取的 1min load average
CPU_IDLE        = # CPU idle 百分比
AVAIL_MEM_GB    = # 从 free -g 获取

# 可用核心估算
busy_by_load  = min(LOAD_AVG, TOTAL_CORES)
free_cores    = TOTAL_CORES - busy_by_load
safety_margin = max(1, int(free_cores * 0.1))
usable_cores  = max(4, free_cores - safety_margin)

# 样本并行度决策
parallel_samples = min(N_samples, max(1, usable_cores // 8))
```

### GPU/Parabricks 并行度决策（仅当 gpu_present and parabricks_ready）

```python
GPU_COUNT   = # nvidia-smi 检测的 GPU 数量
GPU_VRAM_GB = # 单卡显存（GB）

# 样本并行度改由 GPU 数主导（每个 pbrun 任务绑 1 张卡）
parallel_samples = min(N_samples, GPU_COUNT)

# 每张 GPU 配套的 CPU 线程（fq2bam 用 --num-cpu / haplotypecaller 用 --num-htvc-threads / 选卡统一 --num-gpus）
per_gpu_cpus   = min(16, max(4, usable_cores // GPU_COUNT))
# 显存不足时降并发
if GPU_VRAM_GB < 32:
    parallel_samples = min(parallel_samples, GPU_COUNT // 2)
```

| 工具 | Parabricks 写法 | 说明 |
|------|----------------|------|
| 比对+排序+去重 | `pbrun fq2bam --num-cpu N --num-gpus 1` | 一步完成，替代 bwa+samtools+MarkDuplicates |
| BQSR | `pbrun bqsr --num-gpus 1` / `pbrun applybqsr --num-gpus 1` | 替代 BaseRecalibrator/ApplyBQSR |
| 变异检测 | `pbrun haplotypecaller --gvcf --num-gpus 1` | GPU PairHMM，替代 GATK HC |
| 联合分型 | `pbrun genotypegvcf --num-gpus 1` | 4.3.x 命名（**无** `genotypecaller`），替代 GenotypeGVCFs |
| 过滤 | 无 | 恒用 GATK SelectVariants+VariantFiltration（CPU） |

### 各阶段线程分配参考（CPU/GATK 回退路径）

| 工具 | 线程支持 | 推荐 |
|------|---------|------|
| `bwa mem` | `-t` | `min(16, usable_cores / parallel_samples)` |
| `samtools sort` | `-@` | `min(8, usable_cores / 2 / parallel_samples)` |
| `MarkDuplicates` | 单线程 | 增大 `-Xmx` |
| `BaseRecalibrator` | 单线程 | 增大 `-Xmx` |
| `ApplyBQSR` | 单线程 | 增大 `-Xmx` |
| `HaplotypeCaller` | `--native-pair-hmm-threads` | `min(8, usable_cores / parallel_samples / 4)` |
| `CombineGVCFs` | 单线程 | 增大 `-Xmx` |
| `GenotypeGVCFs` | 单线程 | 增大 `-Xmx` |

### 输出格式

检测结果必须组织为 JSON 格式返回给编排 skill：

```json
{
  "server_detection": {
    "total_cores": 128,
    "sockets": 2,
    "cores_per_socket": 32,
    "threads_per_core": 2,
    "numa_nodes": 2,
    "numa_node0_cpus": "0-31,64-95",
    "numa_node1_cpus": "32-63,96-127",
    "cpu_model": "Intel(R) Xeon(R) Platinum 8358P @ 2.60GHz",
    "load_average": 90.5,
    "cpu_idle_pct": 30.2,
    "total_mem_gb": 503,
    "available_mem_gb": 200,
    "other_user_processes": 347,
    "estimated_free_cores": 38,
    "usable_cores": 34,
    "gpu_detection": {
      "gpu_present": true,
      "gpu_count": 8,
      "gpu_models": ["NVIDIA GeForce RTX 4090"],
      "gpu_vram_gb": 24,
      "compute_cap": "8.9",
      "gpu_util_pct": 0,
      "gpu_mem_used_gb": 0.001,
      "driver_version": "550.67",
      "cuda_version": "12.4",
      "gpu_class": "consumer",
      "parabricks_ready": true,
      "parabricks_check": {
        "runtime": "docker",
        "image": "nvcr.io/nvidia/clara/clara-parabricks:4.3.2-1",
        "pbrun_version": "4.3.2",
        "license": "ok"
      }
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
  "parallelism_parabricks": {
    "parallel_samples": 8,
    "per_gpu_cpus": 16,
    "gpus_to_use": 8,
    "smoke_test_passed": true,
    "note": "gpu_class=consumer(非官方支持)，已通过冒烟测试后方可启用"
  }
}
```

**核心原则**：
1. 优先样本并行（单线程工具多，多跑样本比给 BWA 开 16 线程更划算）
2. NUMA 绑定：2+ 样本时绑定到不同 NUMA node
3. 后台作业池：使用 `wait -n` 控制并行度
4. 动态调整：后续阶段可重新评估负载
5. **GPU 存在且 Parabricks 可用时，优先走 GPU 路径**：样本并行由 GPU 卡数主导，每个 pbrun 任务绑 1 张卡（`--num-gpus 1`）
6. **consumer 卡（4090 等）不作数**：需先冒烟测试通过（见 `references/parabricks-setup.md`），否则回退 CPU GATK

## 错误处理

- 如果 `lscpu`/`nproc` 不可用，LLM **必须**返回合理的默认值（如 4 核、4GB 内存）
- 如果 `free -g` 显示 0（某些容器环境），尝试 `free -m` 或 `cat /proc/meminfo`
- 负载检测最差情况 fallback：假设系统空闲，但保守使用 50% 资源
- `nvidia-smi` 不存在或报 "no devices" → `gpu_present=false`，`compute_backend` 走 `gatk-cpu`
- `pbrun` 未安装 / NGC 镜像未拉取 / license 不可用 → `parabricks_ready=false`，回退 GATK 并提示用户参考 `references/parabricks-setup.md` 完成安装
- Parabricks 运行 OOM（4090 等 24GB 卡）→ 降低并行样本数或加 `--tmp-dir` 到高速盘，仍失败则回退 CPU
