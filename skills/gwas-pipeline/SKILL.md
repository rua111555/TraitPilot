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
| **S0.2** | `gwas-pheno-align` | **表型 ↔ 基因型样本对齐**：分档匹配、差异落盘、三种下游格式、**强制用户确认** |
| S0.5 | `gwas-qc-filter` | 缺失率/MAF/HWE 检查与过滤（`-geno -maf -hwe`）；可选全基因组 LD 衰减（PopLDdecay） |
| S1 | `gwas-kinship-pca` | GCTA GRM → 亲缘热图 + PCA → 群体结构协变量 |
| S2 | `gwas-gemma` | GEMMA `-gk 2` 亲缘 + `-lmm 4` 全检验关联 |
| S3 | `gwas-mvp` | rMVP GLM / MLM / FarmCPU 多方法关联 |
| S4 | `gwas-visualization` | CMplot / MVP.Report 曼哈顿/QQ/密度/圈图 + 显著位点提取 |
| S5 | `gwas-ld-block` | LDBlockShow / Haploview 区域 LD block + 候选基因注释（收尾） |

## 核心流程

```
/gwas-pipeline --vcf COHORT.new.snp.filter.vcf.gz --prefix cohort \
               --pheno phe.txt --covar c.txt
  ├── S0:    VCF → PLINK ped/map + bed → 用户审批
  ├── S0.2:  表型 ↔ 基因型对齐（gwas-pheno-align）→ 展示差异 → **用户确认** → keep.txt
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
1.5 **S0.2 对齐之后必须由用户确认**才允许进入 S0.5：用户没明确答复、或说"再改改"时，不得调用 `approve`；对齐产物被改动后旧审批自动失效（哈希校验）
2. **逐阶段串行**，一次只跑一个阶段；GEMMA 与 MVP 可二选一或都跑
3. 每阶段必须消费上一阶段的输出：S0→ped/map；S0.5→过滤 bfile；S1→GRM+PC；S2/S3→关联结果；S4→显著位点；S5→区域基因型
4. 表型 `--pheno` / 协变量 `--covar` 由用户提供（如 `phenotype-extract` 产出的 IMF 表型文件）
5. **`.done` + 可采信的 `.verify` 报告**才算完成（见 §2.6），不以 Harness 通知为准
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
# Bash(background: true) — Harness 跟踪，本轮工具调用立即返回，对话框不阻塞
# nohup ... & + 重定向 — 终端退出后进程继续运行，父进程不会等管道
nohup bash "${SCRIPT_PATH}" > "${RUN_DIR}/nohup.out" 2>&1 &
```

> ⚠️ 参数名是 **`background`**（不是 `run_in_background`，后者会被忽略）；
> 且 `nohup ... &` **必须重定向** stdout/stderr，否则子进程持有输出管道、
> 父进程要等子进程退出才返回 —— 这一轮会被卡住直到命令结束。

### 2. 监测（.done 标记）

- 每阶段脚本末尾 `date > "${RUN_DIR}/.done/{STAGE}.done"`
- GWAS 各阶段通常分钟级，**无需定时轮询**；前台跑完或浅度等待即可
- 长任务（rMVP 大群体、HaplotypeCall 类似物）超过 ~10 分钟 → 用
  **`TaskOutput(log_path=...)`** 查看进度；**不要用 `sleep` 在工具调用里轮询**
  （会把这一轮占住，对话框不解放）。不存在 `CronCreate` 工具。

### 2.5 自动推进

1. 脚本启动后，检查 `.done` / `.failed` / `.pid`（**三者都要看**）
2. `.failed` 存在 → **停止推进**，读失败原因如实告知用户（修复后重新审批该阶段）
3. `.pid` 进程仍存活 → 判定「运行中」，**即使 `.done` 也在**（那是上一轮留下的陈旧标记）
4. `.done` 存在 → **先过硬门禁**：`.verify/<stage>.json` 必须存在、可解析、`stage` 匹配、
   `pass=true`、`checks` 非空；过了才极简汇报并进入下一阶段
5. `.done` 存在但报告不可采信 → **不算完成、不推进**：先判断该阶段到底跑没跑过
   （跑过就重跑门禁；旧版本脚本/手工标记就删掉 `.done` 重跑该阶段）
6. 都不存在 → 还在跑，结束本轮，等用户下次询问

**上下文耗尽恢复**：新开会话说「推进 GWAS — run <run_id>」→ 从 outputs/ 读标记状态继续，
不重跑已完成阶段（见「场景 B」）。

### 2.6 阶段结果校验（写 `.done` 前的门禁）

**要防的失效**：GWAS 阶段的失败大多是**静默**的 —— `plink -maf` 没生效、位点被滤光、
样本在 `--keep` 里丢了一半、关联结果只剩表头。命令都返回 0，`.done` 照写，错误一路
带到最后作图（或带进论文）。

**校验器**：`skills/gwas-pipeline/scripts/gwas_verify.py`（`--stage` 取脚本名去 `.sh`），
与 WGS 门禁**共用** `skills/_shared/verify_core.py` 的 hard/warn 语义与报告格式。

当前已接入判据的阶段（**7 个数据阶段全覆盖**；`.verify` 报告是编排层硬门禁的输入）：

| 阶段 | `--stage` | 核心硬判据 |
|---|---|---|
| S0 格式转换 | `00_format_convert` | `.bed` 字节数 == `3 + n_var×⌈n_ind/4⌉`；魔数；`.bim/.fam` 结构；FID/IID 唯一；**样本集 == 输入 VCF** |
| S0.5 QC 过滤 | `00.5_qc_filter` | 输出 bfile 结构；`0 < out ≤ in`；样本集守恒（除显式 keep/remove）；**过滤后实测值证明参数生效**：`min(MAF) ≥ -maf`、`max(F_MISS) ≤ -geno`、`min(P_HWE) ≥ -hwe` |
| S1 亲缘/PCA | `01_kinship_pca` | GRM 行数 == 个体数、`.grm.gz` 4 列；对称矩阵方阵且行名 == 样本集；**eigenvec 行数/ID 集合 == 样本集**、eigenval 降序且 PC1 > 0 |
| S2 GEMMA | `02_gemma` | `p_wald` 列存在且**可解析比例 ≥ 90%**；**beta 不得整列 0**（模型没拟合）；结果位点数 ≤ 输入；有 `##` 元信息时与实际行数对账 |
| S3 rMVP | `03_mvp` | 每个 trait×method 的 CSV 都存在；P 值列可定位（`P.value` 或 `<trait>.<method>`）；P 可解析 ≥ 90%；**三种方法的位点集合一致**；P 不得全等 |
| S4 可视化 | `04_visualize` | 图**文件头**必须是真图片（PDF/PNG/JPEG）；**显著位点清单必须能由上游结果按阈值重算出来**；区域基因型行数 == 区域位点数 |
| S5 LD block | `05_ld_block` | 区域位点数 ≥ 10（工具会除零）；SVG 含 `<svg`；`.site.gz` 非空、`.blocks.gz` 可解压；峰顶位点须在上游结果中 |

> **回退路径的边界**：S1 在没有 gcta64 时按错误预防 #13 回退到 `plink --pca`，
> 此时 GRM / 对称矩阵 / 热图**本就不产出**，门禁如实登记「未核对」而不是假装核过；
> PCA 数值产物仍按 hard 判。

**`run.params.json` 契约**（编排层在生成 S0 脚本前写入；阶段脚本与校验器都读它）：

| 键 | 用途 |
|---|---|
| `verifier_path` | `gwas_verify.py` 的**绝对**路径（阶段脚本在 `{RUN_DIR}/scripts/`，与仓库无相对关系） |
| `prefix` | S0 的输出前缀（ped/map） |
| `vcf` | S0 的输入 VCF（样本集对账用） |
| `input_bfile` | S0.5 的输入前缀（= S0 的 `prefix`） |
| `qc_prefix` | 过滤后 QC 报告前缀（默认 `<输出 bfile>.qc`） |
| `filter` | `{"geno":0.2,"maf":0.01,"hwe":1e-6}` 或 `"genos=0.2 maf=0.01 hwe=1e-6"` |
| `min_snps` / `min_samples` | 规模下限：正整数判 hard，`0` = 显式不做，省略 = 登记 warn |
| `keep` / `remove` | 显式声明样本筛选时，S0.5 的"样本减少"从 hard 降为 warn |
| `bfile` | S1/S2/S4/S5 的输入 bfile 前缀（= S0.5 的输出） |
| `grm_prefix` / `pca_prefix` | S1 的 GRM / PCA 输出前缀 |
| `assoc` | S2 的 GEMMA 结果路径（默认 `{RUN_DIR}/out/<prefix>_GWAS.assoc.txt`） |
| `trait` / `methods` / `csv_dir` / `map` | S3：表型名、方法列表、CSV 目录（默认 `{RUN_DIR}/out`）、位点表 |
| `source` / `sig_file` / `sig_thresh` / `images_dir` | S4：上游结果、显著位点清单、阈值（默认 `0.05/n`）、图片目录 |
| `region` / `region_gt` | S4/S5：`chr:start-end` 与区域基因型文件 |
| `out_prefix` / `top_site` | S5：LDBlockShow 输出前缀与峰顶位点 |

> **`verifier_path` 必须是绝对路径**：阶段脚本位于 `{RUN_DIR}/scripts/`，与仓库没有
> 固定相对关系，相对路径定位不到校验器 —— 那样每个阶段都会以"定位不到校验器"写
> `.failed`，数据再健康也推进不了。

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
# ⚠️ 空目录防护不可省：`"${RUN_DIR}"/scripts/*.sh` 无匹配时 bash **不展开** glob
#    （全仓未设 nullglob），字面量 `*.sh` 会原样进循环体 → 报出一行 `⬜ * 未开始`，
#    读者以为"有个阶段还没开始"。`[ -e ] || continue` 是三种防护里最省事的一种。
for stage_script in "${RUN_DIR}"/scripts/*.sh; do
    [ -e "${stage_script}" ] || continue
    stage_name=$(basename "${stage_script}" .sh)
    log_file="${RUN_DIR}/logs/${stage_name}.log"
    pid_file="${RUN_DIR}/.pid/${stage_name}.pid"

    # ⚠️ 判序三段，两头都不能动（与 WGS 编排层同一条，每一段都是实测踩出来的）：
    #    ① `.failed` 最先：校验未通过时**不会**有 .done，但也不该被当成「已中断」
    #    ② **pid 存活第二，必须早于 `.done`**：陈旧 `.done` + 本轮存活进程并存时，
    #       `.done` 先判会把"正在跑"报成「已完成」，用户据此推进下一阶段
    #    ③ `.done` 第三：此时才是本轮的结论 —— 且**还要过 `.verify` 门禁**（见下）
    if [ -f "${RUN_DIR}/.failed/${stage_name}.failed" ]; then
        echo "❌ ${stage_name} 校验未通过（读 .failed 文件看原因）"; continue
    fi
    if [ -f "${pid_file}" ] && kill -0 "$(cat "${pid_file}")" 2>/dev/null; then
        echo "🕐 ${stage_name} 运行中"; continue
    fi
    if [ -f "${RUN_DIR}/.done/${stage_name}.done" ]; then
        # ── 硬门禁：`.done` 存在 ≠ 已校验（与 WGS 编排层同源）──
        # ⚠️ **只对已接入校验器的阶段生效**（列表见 `GATED_STAGES`，必须与
        #    `gwas_verify.py` 的 `_STAGES` 注册表同步 —— 由
        #    `tests/test_gwas_gate_template.py::test_gated_stage_list_matches_verifier_registry`
        #    钉住：校验器新增阶段却忘了加进列表 → 该阶段会被**静默**当成"未接入"
        #    而放行，正是本门禁要防的形态）。
        #    其余（将来新增、尚未接入的）阶段 —— 如实报「已完成但**未接入**校验」，
        #    既不放行成"已校验"，也不误拦成"校验未通过"。
        GATED_STAGES="00_format_convert 00.5_qc_filter 01_kinship_pca 02_gemma 03_mvp 04_visualize 05_ld_block"
        case " ${GATED_STAGES} " in
        *" ${stage_name} "*)
            # 四类一律不采信：missing（无报告/不可解析）、stage_mismatch（别的阶段的报告）、
            # blocked（pass=false 却写了 .done）、empty（checks 为空 = 一条判据都没跑）。
            verdict=$(python -c 'import json,sys
p, stage = sys.argv[1], sys.argv[2]
try:
    d = json.load(open(p, encoding="utf-8"))
except Exception:
    print("missing"); sys.exit(0)
if d.get("stage") != stage:
    print("stage_mismatch"); sys.exit(0)
if d.get("pass") is not True:
    print("blocked"); sys.exit(0)
if not (d.get("checks") or []):
    print("empty"); sys.exit(0)
print("ok")' "${RUN_DIR}/.verify/${stage_name}.json" "${stage_name}" 2>/dev/null || true)
            if [ "${verdict}" != "ok" ]; then
                echo "⛔ ${stage_name} 有 .done 但**校验报告不可采信**（${verdict:-取不到}）——"
                echo "   不算完成，不推进；重跑该阶段门禁："
                echo "   gwas_verify.py --stage ${stage_name} --run-dir \"${RUN_DIR}\""
                continue
            fi
            echo "✅ ${stage_name} 已完成（校验通过）" ;;
        *)
            echo "🟡 ${stage_name} 已完成，但该阶段**尚未接入结果校验** ——"
            echo "   不声称已校验（结果未经结果值把关），请人工确认后再依赖其产物" ;;
        esac
        continue
    fi
    [ -f "${log_file}" ] && echo "⚠️ ${stage_name} 已中断（进程不在，但无 .done/.failed）" \
                         || echo "⬜ ${stage_name} 未开始"
done
# 未完成的 phase 从输出的缺失项推出 → 生成下一阶段脚本
```

> 判定顺序：`.failed` → **pid 存活** → `.done`+报告可采信（已完成）→ `.done`+报告不可采信
> （**有标记、未校验，不推进**）→ 未开始/已中断。
>
> **`.done` 只是必要条件，不是「完成」的证明**：GWAS 阶段脚本是 LLM 现场生成的，
> "脚本真的调用了校验器"没有任何运行时证据。编排层必须自己再核 `.verify/<stage>.json`
> （存在 / `stage` 匹配 / `pass=true` / `checks` 非空）—— 只守脚本那一端，等于把门禁
> 交给模板的自觉性。

## 输出目录结构

```
{workdir}/
├── run.params.json        # 参数（输入/表型/协变量/过滤条件）+ verifier_path 等门禁契约
├── pheno_align/           # S0.2 对齐产物：keep.txt / pheno_gemma_* / mapping_used.tsv / 报告
├── scripts/               # 各阶段脚本 00_format_convert.sh ... 05_ld_block.sh
├── logs/                  # 各阶段日志
├── .done/                 # 完成标记（**通过结果校验后**才写）
├── .failed/               # 校验未通过 + 失败原因（有它就不该有同名 .done）
├── .verify/               # 校验器报告 {stage}.json（编排层硬门禁读它）
├── .pid/                  # PID 锁文件
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
| 7 | rMVP 分类协变量用 `model.matrix(~as.factor(...))`；**P 值按列名取**（实测列名 `<trait>.<method>`、在第 9 列；老文档写死的「第 8 列」是 SE） | mvp / visualization |
| 8 | 显著阈值用校正值（0.05/n，常见 3.92e-9/1e-8），别用 0.05 | visualization |
| 9 | LDBlockShow/ShowLDSVG 工具名拼写；`-InGWAS` 需 `-log10(P)` 列 | ld-block |
| 10 | 区域 LD 前确认 bfile 染色体编号与 `-Region`/`--chr` 一致；**候选区位点 ≥10 个**（少则 `division by zero`） | ld-block |
| 10.5 | **LDBlockShow 需新版 libstdc++** — 仅系统库过旧时用 `sk_ensure_ld_path`（`skills/_shared/env.sh`）前插 conda lib；系统库够新则别设 `LD_LIBRARY_PATH` | ld-block |
| 11 | **R 环境：需 R ≥ 4.0**（用 `conda run -n r-gwas Rscript`，含 rMVP/CMplot/pheatmap/reshape2）；系统 R 3.6 编译新版依赖失败 | 全部（R） |
| 12 | 独立二进制（gcta64/gemma/PopLDdecay/LDBlockShow/Haploview）路径用 `command -v` 检测 | 全部 |
| 13 | **gcta64/vcftools 缺失时的替代** — 亲缘/PCA 用 GEMMA `-gk 2` + plink `--pca`；vcftools 的 VCF→PLINK 用 plink `--vcf/--recode` | kinship-pca |
| 14 | **表型与基因型样本必须一致** — 子集 bfile 先 `--keep` 表型样本，再送 GEMMA/rMVP（实测 162/162 匹配才无错） | 全部 |
| 15 | **每阶段脚本收尾必须过门禁**（`gwas_verify.py --stage <stage>`）：未过则写 `.failed` + `exit 1`，**不得**无条件写 `.done`；`.verify/<stage>.json` 是编排层硬门禁的输入 | 全部（已接入：S0 / S0.5） |
| 16 | **`verifier_path` 必须是绝对路径并写进 `run.params.json`** — 阶段脚本在 `{RUN_DIR}/scripts/`，与仓库无固定相对关系 | 全部 |
| 17 | **过滤后必须留 `.frq/.lmiss/.hwe`**（`plink --freq --missing --hardy` 一趟出）—— 门禁要靠它们证明 `-maf/-geno/-hwe` 真的生效；缺了就是 fail-closed | qc-filter |
| 19 | **表型与基因型先对齐再进 QC**：样本名对不上时不要手改、不要猜；用 `gwas-pheno-align` 产出 `keep.txt`，S0.5 用 `plink --keep` 固定分析集；对齐后**必须用户确认** | pheno-align / qc-filter |
| 18 | **GEMMA 的 `-p` 表型文件是单列值**（按 `.fam` 顺序、无表头）；给成 `FID IID value` 时 GEMMA 不报错但整份结果是 `nan`/`beta=0` —— 门禁会把这种"假成功"拦下 | gemma |

## 自然语言触发

关键词：`GWAS`、`全基因组关联分析`、`关联分析`、`曼哈顿图`、`QQ图`、`GEMMA`、`rMVP`、
`MVP`、`FarmCPU`、`MLM`、`GLM`、`plink`、`GCTA`、`亲缘关系`、`kinship`、`PCA`、
`LD衰减`、`PopLDdecay`、`LDBlockShow`、`Haploview`、`候选基因`、`显著位点`、`表型关联`、
`基因关联`、`IMF GWAS`
