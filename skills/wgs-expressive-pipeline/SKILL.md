---
name: wgs-expressive-pipeline
description: >
  Run a WGS germline variant calling pipeline with full LLM autonomy and traceability,
  driven by modular sub-skills. The orchestration layer handles monitoring (.done/.pid),
  database queries, background execution (nohup + run_in_background), and auto-advancement.
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

执行全基因组测序标准流程。本 skill 是**编排层**，负责协调子 skill、维护 4 大核心机制。
每阶段的具体实现规范参考对应子 skill 的 `SKILL.md`。

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

## 4 大核心机制

### 1. 后台运行机制（SESSION-SAFE）

每阶段用**两层保护**启动：
```bash
# Bash(run_in_background: true) — Harness 跟踪，对话框不阻塞
# nohup ... & — 终端退出后进程继续运行
nohup conda run -n Agent bash "${SCRIPT_PATH}" &
```

### 2. 监测机制（.done + .pid 双重确认）

- **`.done` 标记**：脚本结束时写入 `date > "${RUN_DIR}/.done/STAGE.done"`
- **`.pid` 锁文件**：脚本启动时 `echo $$ > "${RUN_DIR}/.pid/STAGE.pid"`，`trap 'rm -f' EXIT`
- 完成状态以 `.done` 文件为准，**不以 Harness 通知为准**

### 2.5 自动推进机制

**关键规则：后台进程完成后，编排层必须主动检查进度并推进，不能被动等待用户询问。**

方式：
1. 脚本启动后，调用 **CronCreate** 定时检查 `.done` 标记
2. 每次触发检查 `.done` 文件是否存在
3. 汇报保持**极简**（一行：`第 X/N 样本 | Y 个 BAM | 无报错`），不加分析过程
4. 存在 → 立即进入下一阶段（生成脚本 → 交用户审批）
5. 不存在 → 继续等待

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

### 4. 对话推进机制

**场景 A — 对话框开着**：Harness 通知到达 → 检查 `.done` 标记 → ✅ 则生成下一阶段脚本交审批

**场景 B — 对话框关闭后恢复**：用户说「推进 WGS」→ 执行恢复检查：

```bash
for stage_script in "${RUN_DIR}"/scripts/*.sh; do
    stage_name=$(basename "${stage_script}" .sh)
    [ -f "${RUN_DIR}/.done/${stage_name}.done" ] && echo "✅ 已完成" && continue
    pid_file="${RUN_DIR}/.pid/${stage_name}.pid"
    log_file="${RUN_DIR}/logs/${stage_name}.log"
    if [ -f "${pid_file}" ] && kill -0 "$(cat "${pid_file}")" 2>/dev/null; then
        echo "🕐 运行中"; continue
    fi
    [ -f "${log_file}" ] && echo "❌ 已中断" || echo "⬜ 未开始"
done
```
→ 已完成（中间步）：生成下一阶段脚本交审批
→ 已完成（最后步）：汇报最终产出
→ 运行中：告知进度
→ 已中断：读日志报错，修复后重审

## 输出目录结构

```
{output_dir_base}/temp/runs/{run_id}/        # output_dir_base 来自 run.params.json（缺省 $GENO_DIR/temp）
├── run.params.json     # 所有参数（server_detection + parallelism）
├── scripts/            # 各阶段生成的脚本
│   ├── 01_bwa_sort.sh ...
│   └── 05_filtering.sh
├── logs/               # 执行日志（stdbuf 实时刷新）
├── .done/              # 完成标记
├── .pid/               # PID 锁文件
└── out/                # BAM / VCF 输出
```

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

## 自然语言触发

关键词：`WGS`、`全基因组`、`测序`、`变异检测`、`variant calling`、`germline`、`跑管线`、`生信`、`BWA`、`GATK`、`HaplotypeCaller`、`GPU 加速`、`Parabricks`、`pbrun`
