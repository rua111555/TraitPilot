# Parabricks 环境预备流程（docker 容器方式）

> 被 `wgs-expressive-pipeline`（编排层）、`wgs-express-server-detect`（S0）、
> `wgs-express-params-propose`（Stage 0）引用。Parabricks 官方分发为 **NGC 容器镜像**，
> 本指南按 **docker** 方式全流程落地（本机已具备 toolkit，见 §0）。

## 0. 本机现状（已核实）

| 项 | 状态 |
|----|------|
| 系统 / Docker | CentOS 7 (内核 3.10) / Docker 26.0.0，**daemon 已运行** |
| GPU | 8× NVIDIA RTX 4090（CC 8.9，24GB/卡），驱动 550.67，CUDA 12.4 |
| nvidia-container-toolkit | ✅ 已装（`nvidia-ctk` / `nvidia-container-cli` 均在） |
| docker nvidia runtime | ✅ 已在 `/etc/docker/daemon.json` 注册（`runtimes.nvidia`） |
| 当前用户 docker 权限 | ✅ 已在 docker 组（`groups` 含 docker，免 sudo 可用） |
| NGC 镜像 | ✅ 已拉取 **4.3.2-1**（内置 CUDA 12.3） |
| ⚠ RTX 4090 | 消费级卡，**非 Parabricks 官方支持列表** → 必须冒烟测试通过才启用 |

> **结论**：toolkit/runtime/镜像/docker 权限均已就位；`pbrun version` 无 license 提示；
> 消费卡启用前 **必须**按 §2 过冒烟测试。

### 0.1 版本 ↔ 驱动/CUDA 兼容（本机 2026-09 实测，务必遵守）

| 镜像 tag | 内置 CUDA | 要求主机驱动 | 本机（550.67 / CUDA 12.4） |
|---|---|---|---|
| **4.3.2-1** ⭐ | 12.3.101 | ≥ 545.x | ✅ 可用（已实测：预检通过、`pbrun version` OK、GPU init OK） |
| 4.4.x | 12.x（≤12.4） | ≥ 545~550 | ✅ 大概率可用 |
| 4.5.0-1 | 12.8.90 | ≥ 570.124.06 | ❌ 启动被 nvidia-container-toolkit 拦截（`requirement error: cuda>=12.8`） |
| 4.6.0-1 / 4.7.1-1 | 12.9.79 | ≥ ~580 | ❌ 同上（`requirement error: cuda>=12.9`） |

> ⚠ `NVIDIA_DISABLE_REQUIRE=1` 绕过 toolkit 校验**无效**：4.5.0 实测容器能起、
> `nvidia-smi`/`pbrun` 正常，但 CUDA 运行时硬失败 **err 804（SystemDriverNotCompatible）**。
> **没有软件绕过办法，只有升级驱动（≥570，最好 ≥580）** 才能跑 4.5+。

## 1. 一次性准备（需要你手动执行，涉及 sudo / 账号）

### 1.1 让当前用户能调用 docker（Claude 自动化前提）

```bash
sudo usermod -aG docker "$USER"
# 重要：上述只改组成员；当前 SSH 会话需重新登录才生效
# 验证（重新登录后）：
docker info >/dev/null && echo "docker OK"      # 无 sudo 也能跑才通过
```

> 若无法 sudo：请系统管理员执行同理命令。**这一步不解决，skill 无法自动调容器**。
> 临时替代：每步用 `sudo docker ...`（sudo 需密码，不适合自动化，仅应急）。

### 1.2 NGC 账号与 `docker login`

1. 注册/登录 <https://ngc.nvidia.com>（免费 NVIDIA 账号；Parabricks 有免费学术/入门许可）
2. 右上角账号 → **Setup** → **Generate API Key**（保存好，只显示一次）
3. 本机登录：

```bash
docker login nvcr.io
# Username: $oauthtoken
# Password: <NGC API Key>
```

### 1.3 拉取 Parabricks 镜像

```bash
# ⚠ 版本选择必须匹配本机驱动/CUDA（见 §0.1 兼容表）：
#   驱动 550.67 / CUDA 12.4 时拉 **4.3.2-1**（内置 CUDA 12.3）；4.5.0+ 要求驱动 ≥570，本机跑不了。
docker pull nvcr.io/nvidia/clara/clara-parabricks:4.3.2-1
```

> 镜像较大（数 GB），首次拉取较久。若拉取超时/失败，先排查本机到 `nvcr.io` 的 egress
> （yum/proxy 环境需放行），与内网 registry-mirror 无关（mirror 不代理 nvcr.io）。

### 1.4 验证 GPU 容器 + pbrun 基础可用（关卡 1）

```bash
docker run --rm --gpus all \
  -u "$(id -u):$(id -g)" \
  -v "${GENO_DIR:-$PWD}":"${GENO_DIR:-$PWD}" \
  -w "${GENO_DIR:-$PWD}" \
  nvcr.io/nvidia/clara/clara-parabricks:<版本> \
  pbrun version
# 期望：打印 pbrun 版本号。
# 若提示需 license（不同版本报错不同）：见 §3。
```

## 2. 冒烟测试（三关全过才算 `smoke_test_passed=true`）

抽 1 个真实待跑的小样本 逐关验证（**全部用 docker wrapper**，见 §4）：

```bash
PB_IMAGE=nvcr.io/nvidia/clara/clara-parabricks:<版本>
eval 'PBRUN() { local gpu="${1}"; shift; docker run --rm --gpus "device=${gpu}" \
    -u "$(id -u):$(id -g)" -v "${GENO_DIR:-$PWD}":"${GENO_DIR:-$PWD}" -w "$(pwd)" "${PB_IMAGE}" pbrun "$@"; }'

# 关卡 2：fq2bam（比对+排序+索引）—— 容器只暴露 1 卡 → pbrun 选卡用 --num-gpus 1（4.3.x 无 --gpu）
# ⚠ 4090 24GB + 全基因组 ref：必须加 --low-memory 与 --bwa-nstreams 1，否则加载索引即 OOM
PBRUN 0 fq2bam --ref ref.fa --in-fq sample_1.fq.gz sample_2.fq.gz \
      --out-bam smoke/smoke.sorted.bam --num-cpu 8 --num-gpus 1 \
      --low-memory --bwa-nstreams 1 --tmp-dir smoke/tmp
ls -la smoke/smoke.sorted.bam smoke/smoke.sorted.bam.bai     # 需都存在

# 关卡 3：haplotypecaller GVCF（GPU PairHMM，验证消费卡可跑）
# ⚠ 4.3.2 输出参数是 --out-variants、线程参数是 --num-htvc-threads（非 --out-vcf/--num-cpu）
PBRUN 0 haplotypecaller --ref ref.fa --in-bam smoke/smoke.sorted.bam \
      --out-variants smoke/smoke.g.vcf.gz --gvcf --num-htvc-threads 8 --num-gpus 1
ls -la smoke/smoke.g.vcf.gz smoke/smoke.g.vcf.gz.tbi         # 需都存在
```

运行期间另开终端确认 GPU 真在被用：

```bash
watch -n1 nvidia-smi --query-gpu=index,utilization.gpu,memory.used --format=csv
```

**判定**：三关全过 → `smoke_test_passed=true`，`compute_backend=parabricks`；
任一关失败 → 记入各阶段 SKILL.md 错误预防清单，回退 `gatk-cpu` 修复后再试。

> ✅ **本机实测（2026-09-18，GPU5，150k reads 子样本自 `108_1.fq.gz`）**：
> `fq2bam`（`--num-gpus 1 --low-memory --bwa-nstreams 1 --num-cpu 8`）→ `smoke.sorted.bam`+`.bai` OK；
> `haplotypecaller`（`--out-variants ... --gvcf --num-htvc-threads 8 --num-gpus 1`）→ `smoke.g.vcf.gz`+`.tbi` OK。
> 输出均属主为当前用户（wrapper 用 `-u $(id -u):$(id -g)`），已计入冒烟通过记录。

## 3. License（仅当 `pbrun version` / 工具报错提示时）

部分版本容器需要 license。以运行提示为准，常见做法：

```bash
# 在 NGC 授权页下载 license json 放到本机，例如 $HOME/.nvidia/license.json
docker run --rm --gpus all \
  -v "${GENO_DIR:-$PWD}":"${GENO_DIR:-$PWD}" \
  -v "$HOME/.nvidia/license.json":/lic/license.json \
  -e PARABRICKS_LICENSE=/lic/license.json \
  nvcr.io/nvidia/clara/clara-parabricks:<版本> pbrun version
```

> env 变量名 / `--lic` 参数因版本而异——**以容器里 `pbrun version` 或 `pbrun <tool> --help`
> 的实际提示为准**，不要死记。

## 4. PBRUN wrapper 约定（skill 调用 Parabricks 的唯一入口）

为保证编排层生成的每阶段脚本调用一致，**统一使用下面的 wrapper 函数**。
它由 `wgs-express-params-propose` 在生成脚本时写入脚本头部，各阶段子 skill 直接使用。

```bash
# —— 从 run.params.json 读取 ——
PB_IMAGE="<parabricks.image>"                 # 如 nvcr.io/nvidia/clara/clara-parabricks:4.3.2-1
# 挂载数据根与家目录，用同一宿主路径映射（容器内外路径一致）；勿写死某台机器的绝对路径
DATA_ROOT="${GENO_DIR:-$PWD}"
PB_MOUNTS=("${DATA_ROOT}:${DATA_ROOT}" "${HOME}:${HOME}")   # parabricks.mounts，按需增删
PB_PERMISSION="-u $(id -u):$(id -g)"          # 容器写出的文件属主=当前用户，避免 root 文件

# —— 统一 wrapper：$1=宿主 GPU 索引，其余透传给 pbrun ——
PBRUN() {
    local gpu="${1}"; shift
    docker run --rm ${PB_PERMISSION} \
        --gpus "device=${gpu}" \
        ${PB_MOUNTS[@]/#/-v } \
        -w "$(pwd)" \
        "${PB_IMAGE}" pbrun "$@"
}
```

**关键正确性约定**：
1. **`--gpus device=<宿主卡号>`** 只把那一张卡暴露进容器 → **容器内 pbrun 默认 `--num-gpus 1`**
   （⚠️ **例外只有一个：`genotypegvcf` 不接受该参数** —— 加上即 `rc=2` `unrecognized arguments`
   且零产物；本表 §4.1 那一行是唯一要另写的。别按兄弟工具类推：其余
   `fq2bam/haplotypecaller/markdup/bqsr/applybqsr` 都接受，实测 clara-parabricks 4.3.2-1
   `pbrun <tool> -h` 的 `--num-gpus` 命中数分别为 `1/1/1/1/1/0`）
   （4.3.x **没有** `--gpu` 参数：写 `--gpu 0` 会与 `--gpuwrite/--gpusort/--gpusort` 前缀歧义报错
   `ambiguous option`；更不要写 `--gpu <宿主卡号>`，会选错/越界）
2. 输出/临时文件目录必须在 `PB_MOUNTS` 已挂载的路径下；`-w "$(pwd)"` 保证相对路径落在宿主对应目录
3. 样本并行 = GPU 卡数：`GPU_IDX=$(( i % parallel_samples ))`，每样本一个 `PBRUN` 调用
4. 单卡 24GB 超配时报 cuda OOM → 降 `parallel_samples` 或回退 CPU

### 4.1 4.3.x 实用参数速查（本机 `pbrun <tool> --help` 核对）

> ⚠️ 早期这里的表头写「**已逐一核对**」—— 而 `genotypegvcf` 那一行恰恰是**没核对到**的那行
> （它写着 `--num-gpus 1`，实测该子命令不接受该参数）。一句"已逐一核对"会让读者跳过
> 复核，所以这里不再声称全覆盖：下表是**核对过的结果**，但**新增/改动任何一行前请自己
> 再核一次** `pbrun <tool> --help`（表头不再为未核对的行背书）。

| 工具 | 选卡 | CPU 线程参数 | 关键输入/输出参数 |
|---|---|---|---|
| `fq2bam` | `--num-gpus 1` | `--num-cpu N` | `--ref` `--in-fq R1 R2` `--out-bam`；**默认内含 markdup+BQSR**；24GB 卡加 `--low-memory --bwa-nstreams 1` |
| `haplotypecaller` | `--num-gpus 1` | `--num-htvc-threads N` | `--ref` `--in-bam` `--out-variants` `--gvcf` |
| `markdup` | `--num-gpus 1` | `--num-worker-threads N` | **`--ref`（必需，缺则 rc=2）** `--in-bam` `--out-bam` `--out-duplicate-metrics` |
| `bqsr` | `--num-gpus 1` | 以 `--help` 为准 | `--ref` `--in-bam` **`--knownSites`**（驼峰，非 `--known-sites`）`--out-recal-file`；**无 `--out-bam`**（bqsr 只产出校正表，一步到 BAM 是 `applybqsr` 的事 —— 早期把它记到了 bqsr 头上） |
| `applybqsr` | `--num-gpus 1` | 以 `--help` 为准 | `--ref` `--in-bam` `--in-recal-file` `--out-bam` |
| `genotypegvcf` | **（不接受 `--num-gpus`）** | `--num-threads N` | `--ref` `--in-gvcf` `--out-vcf`（4.3.x 联合分型工具，替代 GATK GenotypeGVCFs；**无** `genotypecaller`）。⚠ **本行曾写 `--num-gpus 1`，实测是错的**：`pbrun genotypegvcf` 是唯一不接受该参数的子命令，加上即 `rc=2` `unrecognized arguments` 且零产物 |

> fq2bam 在 4.3.x **默认即做 markduplicates+BQSR**（`--knownSites` 可选增强）。
> 若管线仍按「stage2 单独 markdup+bqsr」设计，会重复处理；对 parabricks 后端建议直接消费
> fq2bam 的一站式 BAM，或以 `pbrun bqsr`（`--in-bam` 已 markdup 的）增量校正。

## 5. 运行期注意事项

| 事项 | 建议 |
|------|------|
| GPU 被他人占用 | `nvidia-smi` 复查 util/mem；拥挤时减少 `gpus_to_use` |
| 24GB 显存 OOM | 降并行样本数；`--tmp-dir` 指向已挂载的高速盘；仍失败回退 CPU |
| 参数名不确定 | `PBRUN 0 <tool> --help`（不要照抄 GATK 写法） |
| 输出契约 | GPU 路径输出命名必须与 GATK 路径一致（下游阶段直接消费） |
| 文件属主 | wrapper 已带 `-u $(id -u):$(id -g)`，勿去掉 |
| license 过期 | 重新申请/更新；期间回退 `gatk-cpu` |

## 6. 回退策略（兜底）

任何阶段 Parabricks 失败 → **该阶段**脚本切回 GATK CPU 路径（两条分支由 `compute_backend`
切换，见各阶段 SKILL.md）。管线仍可整体完成，只是该阶段无加速。
