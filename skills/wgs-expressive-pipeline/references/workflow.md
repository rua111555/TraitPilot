# WGS Expressive Pipeline 工作流参考文档

## 概述

本 skill 是传统 `wgs-pipeline` 的升级版，**不修改原有 skill**，完全独立运行。

核心设计理念：

1. **模块化架构** — 管线拆分为 7 个独立子 skill，分工明确
2. **编排层驱动** — `wgs-expressive-pipeline` 作为编排入口，协调子 skill 调用
3. **数据库驱动** — 所有文件路径、样本信息、配置参数均从 SQLite 数据库读取
4. **LLM 自主推理** — 工具参数（内存、线程、过滤条件）由 LLM 根据实际情况推理
5. **代码动态生成** — 每一阶段的执行脚本由 LLM 在运行时生成，而非预置
6. **用户审核** — 参数和代码在执行前都需用户确认
7. **全链路可溯源** — 生成的代码、参数配置、执行日志全部保存

## 文件结构

```
skills/
├── wgs-expressive-pipeline/           # 编排层（入口）
│   ├── SKILL.md                       # 编排逻辑、监测/数据库/后台机制
│   ├── scripts/
│   │   ├── db_query.py                # 数据库查询工具
│   │   └── gpu_preflight.sh           # GPU/Parabricks 环境预检（S0 用，输出 JSON）
│   └── references/
│       ├── workflow.md                # 本文档
│       └── parabricks-setup.md        # Parabricks docker 安装/授权/冒烟测试指南
│
├── wgs-express-server-detect/         # 子 skill: 服务器深度检测
│   └── SKILL.md
│
├── wgs-express-params-propose/        # 子 skill: 参数提案
│   ├── SKILL.md
│   └── scripts/db_query.py → (symlink to expressive-pipeline/scripts/db_query.py)
│
├── wgs-express-fastp-qc/             # 子 skill: fastp FASTQ 质控（可选）
│   ├── SKILL.md
│   └── usage-guide.md
│
├── wgs-express-align-sort/            # 子 skill: BWA MEM + samtools sort
│   ├── SKILL.md
│   └── usage-guide.md
│
├── wgs-express-markdup-bqsr/          # 子 skill: MarkDuplicates + BQSR
│   ├── SKILL.md
│   └── usage-guide.md
│
├── wgs-express-haplotypecaller/       # 子 skill: HaplotypeCaller GVCF
│   └── SKILL.md
│
├── wgs-express-joint-genotyping/      # 子 skill: 联合基因分型
│   └── SKILL.md
│
└── wgs-express-filtering/            # 子 skill: SNP/INDEL 过滤
    └── SKILL.md
```

> **GPU 加速（可选）**：S0 执行 `scripts/gpu_preflight.sh` 探测 GPU + docker + Parabricks
> 就绪度；就绪且冒烟测试通过后，Stage 0 将 `compute_backend` 设为 `parabricks`，
> 阶段 1-4 的脚本改用 `PBRUN()` docker wrapper（见 `parabricks-setup.md` §4）。


### 入口点

用户总是通过 `wgs-expressive-pipeline` 调用：
```
/wgs-expressive-pipeline --samples 1,2,10 --prefix COHORT
```

## 模块化架构详解

### 编排层职责

`wgs-expressive-pipeline/SKILL.md` 负责：

1. **参数解析** — 解析 `--samples`、`--prefix`、`--threads`、`--db`、`--resume`
2. **会话管理** — 创建运行目录，管理 `.done` / `.pid` 锁文件
3. **用户交互** — 展示参数、脚本 → 等待用户审批 → 执行
4. **后台执行** — `nohup` + `run_in_background` 双层保护
5. **状态跟踪** — 监测 `.done` 标记，自动推进下一阶段
6. **恢复连接** — 用户说「推进 WGS」时检查各阶段状态
7. **数据库更新** — 记录 `pipeline_runs` 和 `sample_steps`
8. **任务编排** — 按 S0 → Stage 0 → Stage 0.5（可选）→ Stage 1-5 顺序，逐阶段调用子 skill

### 子 Skill 职责

每个子 skill 的 `SKILL.md` 包含：

| 子 Skill | 阶段 | 核心功能 |
|----------|:----:|---------|
| `wgs-express-server-detect` | S0 | CPU/内存/负载检测 + 自动计算并行度 |
| `wgs-express-params-propose` | 0 | 查询 DB + 推理参数 + 保存 run.params.json |
| `wgs-express-fastp-qc` | 0.5 | **可选** fastp FASTQ 质控（去接头/质量过滤/QC报告） |
| `wgs-express-align-sort` | 1 | BWA MEM + samtools sort（逐样本） |
| `wgs-express-markdup-bqsr` | 2 | MarkDuplicates + BQSR（逐样本） |
| `wgs-express-haplotypecaller` | 3 | HaplotypeCaller GVCF（逐样本） |
| `wgs-express-joint-genotyping` | 4 | CombineGVCFs + GenotypeGVCFs（群组级） |
| `wgs-express-filtering` | 5 | SelectVariants + VariantFiltration（群组级） |

每个子 skill 的 SKILL.md 中包含：
- 详细的技术规范
- 脚本模板（SESSION-SAFE 样板）
- 错误预防清单（从实际运行经验总结）
- 安全检查清单（生成脚本前逐条检查）

## 恢复连接检查

当用户重新打开 Claude 并询问 WGS 进度时，由编排层执行恢复检查：

```bash
RUN_DIR="${RUN_DIR:-$GENO_DIR/temp/runs/{run_id}}"   # 实际值来自 run.params.json 的 run_dir
for stage_script in "${RUN_DIR}"/scripts/*.sh; do
    stage_name=$(basename "${stage_script}" .sh)
    log_file="${RUN_DIR}/logs/${stage_name}.log"
    pid_file="${RUN_DIR}/.pid/${stage_name}.pid"
    done_file="${RUN_DIR}/.done/${stage_name}.done"
    
    if [ -f "${done_file}" ]; then echo "✅ 已完成"
    elif [ -f "${pid_file}" ] && kill -0 "$(cat "${pid_file}")" 2>/dev/null; then echo "🕐 运行中"
    elif [ -f "${log_file}" ]; then echo "❌ 已中断"
    else echo "⬜ 未开始"
    fi
done
```

## 与传统 wgs-pipeline 对照

| 功能 | 传统 wgs-pipeline | wgs-expressive-pipeline |
|------|------------------|------------------------|
| 样本映射 | `SAMPLE_MAP` 数组 | `fastq_samples` 表 |
| 参考基因组路径 | 脚本参数 `--ref` | `reference_genome` 表 |
| known sites | 脚本参数 `--known-indels/snps` | `pipeline_config` 表 |
| 工具参数 | 固定值 | LLM 推理 + 用户审核 |
| 执行脚本 | 预置 `wgs_pipeline.sh` | 运行时逐阶段生成 |
| 代码可追溯 | 仅日志 | 独立脚本文件保存 |
| 输出隔离 | 共用 temp 目录 | 按 run_id 独立输出目录 |
| 数据库记录 | 手动 | 自动记录 |
| **架构** | 单体 SKILL (1 个) | **模块化 (1 编排 + 7 子 skill)** |

## 已知常见问题 & 解决方案

### 1. BWA + samtools 管道中的 BAM 头部读取失败

**问题：** `samtools sort: failed to read header from "-"`

**解决方案：** BWA 输出写到 SAM 文件，stderr 单独捕获，禁止管道：
```bash
bwa mem -t ${THREADS} -R "${RG_STR}" "${REF}" "${R1}" "${R2}" \
  1> "${SAM_FILE}" \
  2> >(while IFS= read -r line; do echo "[$(date)] [BWA] $line"; done >> "${LOG_FILE}")
samtools sort -@ ${THREADS} -m 2G -o "${BAM}" "${SAM_FILE}"
rm -f "${SAM_FILE}"
```

### 2. GATK `-J` 参数不支持

**问题：** `gatk MarkDuplicates -J "-Xmx48g"` 报错

**解决方案：** 使用 `--java-options`：
```bash
gatk MarkDuplicates --java-options "-Xmx48g" -I in.bam -O out.bam
```

### 3. GATK 工具参数名差异

**注意：** GATK 4.3 中不同工具可能用不同的参数名：
- MarkDuplicates: `--TMP_DIR`
- BaseRecalibrator: `--tmp-dir`

**解决方案：** 用 `gatk <ToolName> --help` 确认。

### 4. BAM 缺少 Read Group

**问题：** `BaseRecalibrator` 报错 `Number of read groups must be >= 1`

**解决方案：** BWA 时必须加 `-R`：
```bash
RG_STR="@RG\\tID:${SAMPLE}\\tSM:${SAMPLE}\\tLB:${SAMPLE}\\tPL:ILLUMINA"
bwa mem -t ${THREADS} -R "${RG_STR}" "${REF}" "${R1}" "${R2}"
```

### 5. conda 环境中 PATH 问题

**解决方案：** 所有命令通过 `conda run -n Agent` 执行，工具在 PATH 中用 `command -v` 探测（勿写死本机 conda 绝对路径）。

## 添加新样本

1. 在 `$GENO_DIR/fastq/`（`GENO_DIR` 缺省 `~/genomics_data`）下创建 `{编号}/` 目录
2. 放入 fastq 文件：`{编号}_1.fq.gz` 和 `{编号}_2.fq.gz`
3. 运行数据库初始化脚本：
   ```bash
   conda run -n Agent python "$(dirname "$0")/../scripts/init_db.py"
   ```
4. 验证数据已写入：
   ```bash
   conda run -n Agent python "$(dirname "$0")/../scripts/db_query.py" \
     --action query-samples --samples <新编号>
   ```

## 相关 Skill 导航

```
用户入口
  └── wgs-expressive-pipeline (编排层)
        ├── 参考 wgs-express-server-detect  → S0 服务器检测
        ├── 参考 wgs-express-params-propose → Stage 0 参数提案
        ├── 参考 wgs-express-fastp-qc       → Stage 0.5 **可选** fastp QC
        ├── 参考 wgs-express-align-sort     → Stage 1 BWA+sort
        ├── 参考 wgs-express-markdup-bqsr   → Stage 2 MarkDup+BQSR
        ├── 参考 wgs-express-haplotypecaller → Stage 3 HaplotypeCaller
        ├── 参考 wgs-express-joint-genotyping → Stage 4 联合分型
        └── 参考 wgs-express-filtering      → Stage 5 过滤
```
