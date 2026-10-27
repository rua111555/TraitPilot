# 更新日志

版本号的**唯一载体**是仓库根的 [`VERSION`](VERSION) 文件；`skill_agent.__version__`
由它读取，`run.py --version` / `python -m skill_agent --version` 打印它。
本文件记录**每个版本为什么值得单独发布**，格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。

> ⚠️ **0.1.0 的诚实说明**：0.1.0 于 2026-10-15 写入 `skill_agent/__init__.py`，
> 但它**从未被任何代码读取** —— 没有 `--version`、没有消费者、没有 tag。
> 2026-10-26 有一次提交信息就叫 `version change`，版本号却一字未动。
> 因此下表中的 0.1.0 条目是**追溯整理**（按当时的代码与文档归纳），
> 不是当时的发布记录；0.2.0 才是**第一个真正可读的版本号**。

---

## [0.2.0] — 2026-10-27

**主题：阶段结果校验门禁 —— 让"中途坏掉"在当步就被拦下，而不是传到下游很久以后才暴露。**

用户诉求（逐字）：「在WGS管线或者表型提取的每一步结束之后，需要对结果值进行校验，
以免出现中途错误还传递到后面步骤」

### 新增：阶段结果校验门禁（Stage Result Verification）

- **`skills/wgs-expressive-pipeline/scripts/stage_verify.py`**：按 `@stage` 注册的阶段校验器。
  每个阶段在**写 `.done` 之前**必须调用它；判据分 `hard`（不过则不写 `.done`）与
  `warn`（只告警，仍写 `.done`）。报告以 JSON 落盘 `{RUN_DIR}/.verify/{stage}.json`，
  同时输出人类可读版本。校验不通过 → 写 `.failed` + `exit 1`，编排层不得推进。
- **六个阶段 skill 的收尾门禁**（fastp-qc / align-sort / markdup-bqsr /
  haplotypecaller / joint-genotyping / filtering）：定位校验器 → 样本名单兜底 →
  调用校验 → 按结果写 `.done` 或 `.failed`。
- **可声明的阈值判据**（`--min-variants` 等）：调用方显式声明即 `hard`；
  未声明则登记 `warn` 并**明说"未做该判定"** —— 既不静默通过，也不误拦健康数据。
- **表型侧出口校验 `skills/phenotype-extract/scripts/validate.py`**：
  在写 GWAS 表型文件**之前**校验（值域 / 失败率 / 全同值 / 样本数 / 重复个体 / 参照相关性）。
  hard 未过 → 不写 GWAS 文件（明细 CSV 与校验报告仍落盘，供排查）。

### 修复：本批次实测踩到的门禁缺陷（V1–V19 + F1–F7，详见 `docs/test-evidence.md` §11）

按**失效方式**归类（每条都先用真实工具复现、再修、修完双向验证）：

- **判据读的不是本体**：`samtools idxstats` 读的是索引而非 BAM（陈旧索引掩盖空 BAM，
  `read_count`/`mapped_ratio`/`mapped_rate` 三条判据同时被骗过）；`dup_rate` 用会**跨行**的
  正则解析 metrics，从库名 `lib1` 里抓走 `1`，把真值 0 报成 100%。
- **判据根本没被触发**：`combined.g.vcf.gz` 缺失时 `if` 没有 `else`，静默通过；
  「变异数异常少」只在 docstring 里、代码里没有任何带宽判据；
  表型侧 `value_range` 在 CSV 缺 `状态` 列时**整条判据从报告里消失**，末尾照样打"✅ 全部检查通过"。
- **判据根本到不了**：裸 `tabix -p vcf` 在 `set -euo pipefail` 下杀死脚本
  （GATK 已自动写索引），健康数据既不写 `.done` 也不写 `.failed`，编排层误判"已中断"；
  `: "${VERIFIER:?}"` 与 `VAR="$(cmd)"` 都会绕过写 `.failed` 的兜底；
  编排层权威模板漏了 `${SAMPLES_CSV}` 的赋值（`set -u` 下 `unbound variable`）。
- **默认路径必拦健康数据**：`pbrun fq2bam` 不传 `--read-group-sm` 时生成写死的
  `SM:sample` 常量读组，而 parabricks 是 GPU 就绪时的默认后端 → 每个样本都被拦。
- **两个产物各造各的**：PLINK 与 GAPIT 两个表型导出走独立构造路径，`--fam` 缺样本时
  一个 3 行一个 4 行，同时写出且无人发现 → 改为由**同一个 `order`** 构造，分歧结构上不可能发生。
- **权威文档说了假话**：编排层判据表把 hard 的 `mapped_ratio` 只以 warn 的"比对率 ≥90%"面貌写出；
  `usage-guide.md` 建议"用 conda 绝对路径"（违反硬规则 #5.1），并称 Parabricks 会自动配好读组（实测为假）。
- **声称可声明、实际无从声明**：`--min-variants` 设计上"由调用方显式声明"，
  但全仓没有任何一处会传它，判据**恒停在 warn** → 补上 `run.params.json` 的 `min_variants` 回落通路。
- **陈旧标记把已修好的阶段报成失败**：成功路径写 `.done` 却不清上一轮的 `.failed`，
  而四态判定优先读 `.failed`。

**修复作用域本身也修了**：上面这批里**三条是已"修好"缺陷在没改到的文件里复发**
（A5→编排层模板、A7→joint-genotyping、A2→usage-guide）。根因是按"这次动过的文件"逐个改，
而缺陷按**写法**传播。故新增 `tests/test_defect_class_guards.py`：**自动发现全部 skill 文档、
按失效模式扫描**（裸 tabix / 读组自动生成主张 / 硬编码 conda 路径 / 样本名单无赋值 /
写 `.done` 不清 `.failed` / 比对率只写 warn / 权威模板自足），将来新增文件自动纳入。
它上线即找出审计自己都没报的**第四处** A2 站点。每条谓词都配**负控 + 正控**自检
（真违规必须抓到、纠正叙述不得误伤），并做过变异验证。

### 新增：版本载体（本文件 + `VERSION` + `--version`）

此前版本号是**纯装饰**（见上方 0.1.0 说明）。现在：`VERSION` 文件是唯一来源，
`__version__` 从它读取，`run.py --version` 与 REPL 横幅都会显示它。

### 测试与证据

- `pytest tests/ -q` → **516 passed**（较上一批 271 增 245：门禁模板 57 + 缺陷类防线 153 +
  探针自检 4 + 校验器 20 + 表型导出 6 + 版本载体 5）
- `tools/concurrency/verify_fix_probe.py` → **46/46**（真 samtools / bgzip / tabix / 真容器）
- `tools/concurrency/stage_verify_probe.py` → **FAILED 0**
- `tools/concurrency/skill_doc_consistency_probe.py` → **FAILED 0**
- 用例清单 `docs/test-matrix.md`（VERIFY-01…25、PROBE-01、VERSION-01）；
  逐条实测证据 `docs/test-evidence.md` §11

> **复核工具自身也被复核了**（第四批的连带发现）：`verify_fix_probe.py` 是"防判据变装饰"的
> 对策工具，审计却发现它自己就有两条恒真断言（陈旧变量 `rc == 1 or rc4 == 1`；
> 判据名只写进说明字符串、不参与断言）。实测把两个 critical 缺陷的检测代码整段删掉，
> 探针仍 `45/45 全绿`、pytest 仍 `49 passed` —— **"阶段 2 只做 markdup 也算通过"这条
> 已修的 critical 缺陷，整套证据链再也发现不了它回归**。现已改为读 `.verify/<stage>.json`
> 的**级别+真值**做断言，并加 `tests/test_probe_selfcheck.py` 静态防线
> （不许引用跨段陈旧变量、不许以 stdout 子串为唯一依据）。

---

## [0.1.0] — 2026-10-15（追溯整理）

**主题：从"依赖 Claude Code 的提示词集合"变成"自带 skills 的独立 Agent"。**

### 新增

- **技能内聚**：技能迁入 `skills/`，模型通过 `Skill` 工具按需加载 `SKILL.md` 指令再执行。
- **独立配置**：`config.json` 作为单一控制面板（凭证 / 模型 / 各可选能力开关）；
  优先级为「环境变量 > `config.json` > 代码默认值」。
- **harness 模块**：M1a 用量采集、M2 终止契约 + 预算熔断、M3 死循环熔断、
  M4 工具注册表/插件、M6 输入校验、M7 工具治理、M8a 权限模型、
  M9 沙箱后端（host/rlimit/docker）、M10 钩子与 trace、M11 幂等去重、
  M12 摘要压缩、M13 会话持久化与恢复、prompt cache、模型降级。
- **Web 前端**（Flask + SSE）：会话列表 / 归档删除 / 历史回放 / 思考折叠。
- **客户端韧性**：429/529/5xx 退避重试 + `Retry-After` 支持 + thinking 流式回调。
- **环境提取**：`environment/` 一键安装与自检脚本；表型 ML 栈独立成 `Phenotype` 环境。

### 之后的迭代（0.1.0 → 0.2.0 之间，均无版本号记录）

- 可移植化：技能改为运行时读取本机环境，去掉写死的 conda / 数据绝对路径。
- 通用化：移除专有物种描述与路径改写工具，数据路径全环境变量化。
- 改名：仓库名与产品名统一为 **TraitPilot**（详见 `docs/rename-to-traitpilot.md`）。
- 修复测试发现的 14 个缺陷（D1–D14，详见 `docs/test-fix-report.md`）。
- 修复会话并发缺陷：切走不再中断、切回看得见、界面解锁
  （详见 `docs/session-concurrency-fix.md`）。
- 对话框解放与上下文预算：弹性轮次、溢出恢复、压缩续跑
  （详见 `docs/dialog-liberation-and-context-budget.md`）。

---

## 版本号约定

`MAJOR.MINOR.PATCH`：

| 位 | 何时递增 |
|---|---|
| `MAJOR` | 破坏兼容：技能契约（如阶段产物文件名 / `run.params.json` 结构）变更，或 CLI 参数语义变更 |
| `MINOR` | 新增能力：新技能、新阶段判据、新 harness 模块、新 CLI 参数 |
| `PATCH` | 只修缺陷：判据修正、文档纠错、测试补充，不改变对外契约 |

**发版清单**：

1. 改 `VERSION`（唯一一处）；
2. 在本文件顶部加一节，写清**为什么值得单独发布**；
3. 跑全量回归：`pytest tests/ -q` + `tools/concurrency/` 下相关探针；
4. 提交并推送后，可选打 tag：`git tag -a v$(cat VERSION) -m "TraitPilot $(cat VERSION)"`。
