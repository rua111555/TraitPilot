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
2. **会话管理** — 创建运行目录，管理 `.done` / `.pid` / `.failed` / `.verify` 文件
3. **用户交互** — 展示参数、脚本 → 等待用户审批 → 执行
4. **后台执行** — `nohup` + **重定向** + `Bash(background: true)` 三层保护
5. **状态跟踪** — 监测 `.done` / `.failed` 标记，自动推进下一阶段
6. **结果校验门禁** — 每阶段写 `.done` 前必须过 `stage_verify.py`；未过则写 `.failed` 并停止推进
7. **恢复连接** — 用户说「推进 WGS」时检查各阶段状态
8. **数据库更新** — 记录 `pipeline_runs` 和 `sample_steps`
9. **任务编排** — 按 S0 → Stage 0 → Stage 0.5（可选）→ Stage 1-5 顺序，逐阶段调用子 skill

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
    failed_file="${RUN_DIR}/.failed/${stage_name}.failed"

    # ⚠️ .failed 必须最先判：校验未通过时没有 .done，但也不该被当成「已中断」
    if [ -f "${failed_file}" ]; then echo "❌ ${stage_name} 校验未通过（读 .failed 看原因）"
    elif [ -f "${done_file}" ]; then echo "✅ ${stage_name} 已完成"
    elif [ -f "${pid_file}" ] && kill -0 "$(cat "${pid_file}")" 2>/dev/null; then echo "🕐 ${stage_name} 运行中"
    elif [ -f "${log_file}" ]; then echo "⚠️ ${stage_name} 已中断"
    else echo "⬜ ${stage_name} 未开始"
    fi
done
```

> 四态判定：`.failed`（校验未通过）→ `.done`（已完成且通过校验）→ 运行中 → 未开始/已中断。
> `.failed` 优先级最高，因为它的存在意味着**这一阶段的数据不可信**，必须停下来修，
> 而不是继续推进。

## 阶段结果校验（结果值门禁）

**要防的失效**：脚本收尾的 `wait "${pid}" || log_error ...` 吞掉子进程失败（`||` 使退出码
变 0，`set -e` 不触发），脚本继续走到末尾**无条件**写 `.done`，编排层据此认为"完成"并
推进下一阶段 —— 下游于是拿到空 BAM / 假 GVCF / 样本数不对的 VCF。

这类错误在下游往往**不立刻报错**（GATK 能读一个空 BAM），要到很久以后以"变异数异常少"
的面貌出现，届时已无法从数值上追溯是哪一步坏掉的。所以必须在每步的**出口**拦住。

校验器：`skills/wgs-expressive-pipeline/scripts/stage_verify.py`

| 阶段 | `--stage` | 核心**硬性**判据（不过即不写 `.done`） |
|------|-----------|-----------------------------------|
| 0.5 fastp | `00_fastp_qc` | 清洗后 FASTQ 非空**且 gzip 流完整**（截断的 .gz 也能"存在且非空"）；`fastp.json` 可解析；`after_filtering.total_reads > 0` |
| 1 align | `01_bwa_sort` | **reads 数 > 0**（`quickcheck` 对空 BAM 放行）；**`mapped_ratio`：`mapped` = 0（全 unmapped）即拦**（参考基因组选错/物种不符）；`@RG` 存在且 `SM` 与样本号一致；`.bai` 存在；样本齐 |
| 2 markdup/bqsr | `02_markdup_bqsr` | **每样本都有 BQSR BAM**（Stage 3 输入契约）；Stage 1 有产出的样本不得消失 |
| 3 HC | `03_haplotypecaller` | **头部含 `##ALT=<ID=NON_REF`**（漏 `-ERC GVCF` 的典型症状）；GVCF 内部样本名 == 文件名 |
| 4 joint | `04_joint_genotyping` | **VCF 样本数 == 输入 GVCF 数**；位点数 > 0（仅拦全空；"变异数异常少"需调用方传 `--min-variants`，见下） |
| 5 filter | `05_filtering` | **VCF 样本集 == 上游样本集**；SNP/INDEL **各自**有 PASS 位点；FILTER 不得整列是 `.` |

> 只告警（`warn`，仍写 `.done`）的有：reads 守恒（±10%，需 `--prev-reads-file`）、
> Ti/Tv ∈ 1.8-2.3、**`mapped_rate` 比对率 ≥90%**、保留率 ≥50%、重复率 ≤40%、markdup BAM 是否齐
> （parabricks 路径下可能本就不产出）、**未声明 `--min-variants` 时的位点数下限**。
> ⚠️ 别把 warn 级写进"硬性判据"列 —— 读者会照着找拦截，而实际只拿到一行告警。
> ⚠️ 也**别把 hard 级漏出硬性列**：上表 warn 里只提 `mapped_rate`，但映射侧还有一条 **hard**
> 的 `mapped_ratio`（全 unmapped 即拦）。早期此表两条都没写清，读者据此以为"比对率低只是告警"。

### 「变异数异常少」为什么默认不拦

上文说这类错误「要到很久以后以'变异数异常少'的面貌出现」，所以门禁理应直接卡住它。
但**没有普适的位点数下限**：合理位点数取决于参考基因组大小（人类 3Gb vs 本仓库探针用的
200kb 迷你 ref，差 4 个数量级）、队列大小、测序深度、物种。拍一个硬编码默认值，必然
要么拦健康数据，要么松到没用 —— 两种结局都让判据被绕过。

故 `--min-variants` 由**调用方显式声明**（按本次运行的参考基因组与队列规模推算）：
声明后未达到即 **hard** 拦截；未声明则登记一条 **warn**，明说"未做该判定" ——
**不静默通过**（否则报告全绿会让人以为变异数已被把关，又一处"纸面具备"）。
这一分级与本仓库表型侧 `nir_agreement` 的范式一致（显式阈值=用户的验收标准→hard）。

**声明通路**：Stage 0 在 `run.params.json` 里写 `"min_variants": N`（校验器读回使用，
与 `verifier_path` 同范式），或直接给校验器传 `--min-variants N`（优先级更高）。
⚠️ 只有 CLI 开关**不算**通路：阶段脚本由 LLM 现场生成，不会有人把该参数写进命令行，
判据便恒停在 warn —— 本项目称之为"纸面具备"。写了该键却无法生效（非正整数）时
校验器判 **hard**，不静默降级。

判据分两级：`hard` 未过 → 不写 `.done`（写 `.failed` 并 `exit 1`）；`warn` 未过 → 仍写
`.done`，但告警写进 `.verify/<stage>.json`，编排层汇报时应带出（如重复率偏高、Ti/Tv 偏移）。

**为什么是独立脚本而不是写进生成模板**：阶段脚本由 LLM 每阶段现场生成，文字清单会漂移
（编排层错误预防表专门记录"同类错误复发"即是证据）。校验逻辑做成**固定资产**后，可单测、
可 review、可版本化，不受生成过程的随机性影响。

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
