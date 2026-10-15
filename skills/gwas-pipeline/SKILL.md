---
name: gwas-pipeline
description: >
  全基因组关联分析（GWAS）标准流程编排层。串联 VCF 格式转换 → 质控过滤 → 亲缘矩阵/PCA →
  GEMMA LMM → rMVP 多方法关联 → 结果可视化 → 区域 LD block 与候选基因 等模块化子 skill。
  本 skill 是编排层，负责解析输入、生成各阶段脚本、逐阶段用户审批、后台执行与自动推进。
  每个分析阶段委托给对应的子 skill（见下文一览），子 skill 负责实现细节与错误预防。
  当用户提到 GWAS、全基因组关联分析、关联分析、曼哈顿图、QQ图、GEMMA、gemma、MVP、rMVP、
  plink、GCTA、亲缘关系、PCA、LD衰减、LDBlockShow、候选基因、表型关联 — match this skill。
disable-model-invocation: false
allowed-tools:
  - Read
  - Write
  - Bash
  - Grep
  - TaskOutput
---

# GWAS Pipeline — Orchestration Layer

执行从过滤后 VCF 到候选基因的全基因组关联分析标准流程。本 skill 是**编排层**，
负责协调子 skill 完成 GWAS 各阶段；每个阶段的具体命令/格式/错误预防见对应子 skill 的 `SKILL.md`。

## 子 Skill 一览

| 阶段 | 子 Skill | 功能 |
|:----:|----------|------|
| S0 | `gwas-format-convert` | VCF → PLINK（vcftools `--plink` / plink `--vcf --make-bed`） |
| S0.5 | `gwas-qc-filter` | 缺失率/MAF/HWE 检查与过滤（`-geno -maf -hwe`）；可选全基因组 LD 衰减（PopLDdecay） |
| S1 | `gwas-kinship-pca` | GCTA GRM → 亲缘热图 + PCA → 群体结构协变量 |
| S2 | `gwas-gemma` | GEMMA `-gk 2` 亲缘 + `-lmm 4` 全检验关联 |
| S3 | `gwas-mvp` | rMVP GLM / MLM / FarmCPU 多方法关联 |
| S4 | `gwas-visualization` | CMplot / MVP.Report 曼哈顿/QQ/密度/圈图 + 显著位点提取 |
| S5 | `gwas-ld-block` | LDBlockShow / Haploview 区域 LD block + 候选基因注释（收尾） |

## 核心流程

```
/gwas-pipeline --vcf ET277.new.snp.filter.vcf.gz --prefix et277 \
               --pheno phe.txt --covar c.txt
  ├── S0:    VCF → PLINK ped/map + bed → 用户审批
  ├── S0.5:  --missing/--freq 检查 → 读懂分布 → 过滤（-geno 0.2 -maf 0.01 -hwe 1e-6）→ 用户审批
  │         └── （可选）PopLDdecay 全基因组 LD 衰减
  ├── S1:    GCTA --make-grm → R 对称亲缘矩阵 + 热图 → --pca 10 → PC 散点图 → 用户审批
  ├── S2:    GEMMA -gk 2 亲缘 + -k/-c -lmm 4 关联 → 用户审批
  ├── S3:    rMVP MVP.Data + MVP()（GLM/MLM/FarmCPU）→ 用户审批
  ├── S4:    曼哈顿/QQ/密度/圈图 + 显著位点与区域基因型提取 → 用户审批
  └── S5:    LDBlockShow + ShowLDSVG + Haploview → 候选基因 → 汇总汇报
```

**关键规则**：
1. 每阶段脚本**必须先生成 → 用户审批 → 才能执行**（不可自动执行）
2. **逐阶段串行**，一次只跑一个阶段；GEMMA 与 MVP 可二选一或都跑
3. 每阶段必须消费上一阶段的输出：S0→ped/map；S0.5→过滤 bfile；S1→GRM+PC；S2/S3→关联结果；S4→显著位点；S5→区域基因型
4. 表型 `--pheno` / 协变量 `--covar` 由用户提供（如 `phenotype-extract` 产出的 IMF 表型文件）
5. 存活的文件（`.done`）为准判断完成，**不以 Harness 通知为准**
6. 每阶段结束后自动推进下一阶段（无需用户主动询问）

## 参数

| 参数 | 说明 | 默认 |
|------|------|------|
| `--vcf` | **必填**，输入过滤后 VCF（gz 亦可） | — |
| `--bfile` | 若已有 PLINK bfile 则跳过 S0（替代 `--vcf`） | 自动 |
| `--prefix` | 输出前缀（不提供则从输入文件名推导） | 自动 |
| `--pheno` | 表型文件（FID IID 值…或列式） | 用 fam 第 6 列 |
| `--covar` | 协变量文件（FID IID 性别等） | 无 |
| `--filter` | 覆盖默认过滤：`"genos=M maf=M hwe=M"` | `0.2 0.01 1e-6` |
| `--chr` | 候选区域染色体（触发 S4 区域提取 / S5 LD block） | 全基因组 |
| `--region` | 候选区域 `START:END`（配合 `--chr`） | — |
| `--gemma/--skip-gemma` | 是否跑 S2 GEMMA | 跑 |
| `--mvp/--skip-mvp` | 是否跑 S3 rMVP | 跑 |
| `--threads` | 并行核数（传给 MVP `ncpus` 等） | 自动 |
| `--resume` | 断点续跑（看 `.done`） | 关闭 |

## 运行机制

### 1. 后台运行（SESSION-SAFE）

```bash
# Bash(run_in_background: true) — Harness 跟踪，对话框不阻塞
# nohup ... & — 终端退出后进程继续运行
nohup bash "${SCRIPT_PATH}" > /dev/null 2>&1 &
```

### 2. 监测（.done 标记）

- 每阶段脚本末尾 `date > "${RUN_DIR}/.done/{STAGE}.done"`
- GWAS 各阶段通常分钟级，**无需定时 cron 轮询**；前台跑完或浅度等待即可
- 长任务（rMVP 大群体、HaplotypeCall 类似物）超过 ~10 分钟 → 用 `CronCreate` 每 30 分钟查一次 `.done`

### 2.5 自动推进

1. 脚本启动后，检查 `.done` 标记
2. 存在 → 极简汇报（一行）→ 立即进入下一阶段（生成脚本交审批）
3. 不存在 → 等待；用户随时可询问进度

**上下文耗尽恢复**：新开会话说「推进 GWAS — run <run_id>」→ 从 outputs/ 读 `.done` 状态继续，
不重跑已完成阶段（见「场景 B」）。

### 3. 错误自动总结机制

遇到任何管线错误，**必须**：
1. **分析根因**：读日志，定位报错命令与信息
2. **记录到对应子 skill**：在子 skill 的「错误预防清单」表新增一行
3. **记录到本编排层**：在下方「错误预防」表新增一行
4. **告知用户**：一句话说明错在哪、为何、已记录

### 4. 对话推进机制

**场景 A — 对话框开着**：阶段完成 → 汇报一行 → 生成下一阶段脚本交审批。

**场景 B — 对话框关闭后恢复**：用户说「推进 GWAS」→ 状态检查：

```bash
for f in "${RUN_DIR}"/.done/*.done; do echo "✅ $(basename "$f")"; done   # 已完成
# 未完成的 phase 从输出的 *.done 缺失项推出 → 生成下一阶段脚本
```

## 输出目录结构

```
{workdir}/
├── run.params.json        # 参数（输入/表型/协变量/过滤条件）
├── scripts/               # 各阶段脚本 00_format_convert.sh ... 05_ld_block.sh
├── logs/                  # 各阶段日志
├── .done/                 # 完成标记
├── .pid/                  # PID 锁文件（可选）
└── out/                   # 关键产出副本（结果 CSV / 图 / 显著位点）+ 各阶段中间文件
```

## 错误预防（生成每个脚本前检查）

| # | 检查项 | 相关 |
|---|--------|------|
| 1 | gz VCF 用 `vcftools --gzvcf`；`plink --vcf` 直接读 | format-convert |
| 2 | 过滤前先看 missing/frq 分布，`-hwe` 方向别用反（正值=保留 PASS） | qc-filter |
| 3 | 检查 `wc -l` 位点数/个体数符合预期 | format-convert |
| 4 | GEMMA 结果在 `output/` 且前缀带 `.`（`output/.xxx.sXX.txt`），需重命名；**0.98.5+ 亲缘文件不带 `.` 前缀**，用 `ls output/{p}*s*.txt` 定位 | gemma |
| 4.5 | **GEMMA 的 bfile fam 无表型时必须 `-p`** — 否则 `Enforce failed for number of analyzed individuals equals 0` | gemma |
| 5 | GEMMA `-c` 协变量无表头、前两列 FID IID；`-lmm` 前先 `-gk` 算亲缘 | gemma |
| 6 | rMVP `attach.big.matrix` 需 `.desc` 同目录；`filePhe` 交互会挂死脚本；**表型文件需 tab+表头**（否则 `At least 2 columns...`）| mvp |
| 6.5 | **rMVP `priority` 参数已移除** — 用 `ncpus=`；`MVP.Data` 输出表型文件是 `{out}.phe` 非 `.pheno` | mvp |
| 7 | rMVP 分类协变量用 `model.matrix(~as.factor(...))`；P 值取第 8 列 | mvp / visualization |
| 8 | 显著阈值用校正值（0.05/n，常见 3.92e-9/1e-8），别用 0.05 | visualization |
| 9 | LDBlockShow/ShowLDSVG 工具名拼写；`-InGWAS` 需 `-log10(P)` 列 | ld-block |
| 10 | 区域 LD 前确认 bfile 染色体编号与 `-Region`/`--chr` 一致；**候选区位点 ≥10 个**（少则 `division by zero`） | ld-block |
| 10.5 | **LDBlockShow 需 `LD_LIBRARY_PATH=/home/chenyuxi/anaconda3/lib`**（系统 libstdc++ 旧） | ld-block |
| 11 | **R 环境：需 R ≥ 4.0**（本机用 `conda run -n r-gwas Rscript`，含 rMVP/CMplot/pheatmap/reshape2）；系统 R 3.6 编译新版依赖失败 | 全部（R） |
| 12 | 独立二进制（gcta64/gemma/PopLDdecay/LDBlockShow/Haploview）路径用 `command -v` 检测 | 全部 |
| 13 | **gcta64/vcftools 缺失时的替代** — 亲缘/PCA 用 GEMMA `-gk 2` + plink `--pca`；vcftools 的 VCF→PLINK 用 plink `--vcf/--recode` | kinship-pca |
| 14 | **表型与基因型样本必须一致** — 子集 bfile 先 `--keep` 表型样本，再送 GEMMA/rMVP（实测 162/162 匹配才无错） | 全部 |

## 自然语言触发

关键词：`GWAS`、`全基因组关联分析`、`关联分析`、`曼哈顿图`、`QQ图`、`GEMMA`、`rMVP`、
`MVP`、`FarmCPU`、`MLM`、`GLM`、`plink`、`GCTA`、`亲缘关系`、`kinship`、`PCA`、
`LD衰减`、`PopLDdecay`、`LDBlockShow`、`Haploview`、`候选基因`、`显著位点`、`表型关联`、
`基因关联`、`IMF GWAS`
