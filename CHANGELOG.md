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

## [0.4.0] — 2026-10-29

**主题：根因 D ——「同一个断点的两端各说各话」。七条缺陷，每一条的两端各自都自洽，
放一起才矛盾。**

第二轮对抗审计（7 条 `STILL_PRESENT`）全部属于这一根因。它的阴险之处在于：
**任何单侧阅读、任何单阶段的测试都发现不了它** —— 文档 A 是对的、代码 B 也是对的，
只有把 A 和 B 放在一起才看得出它们说的不是一件事。七条各是它的一个面：

| # | 一端 | 另一端 |
|---|---|---|
| G02 | 围栏自称"可逐字照抄" | 引用了自己从没定义的 `${LOG_DIR}`/`log_info` → 照抄即 rc=1 |
| G03 | resume 用 `[ -f ]` 判"产物好了" | 门禁的 `have()` 要**非空**（0 字节算不存在）→ 重跑永久卡死 |
| G04 | 文档说整数 `0` = "明确不做此判定" | 代码按"非正整数"判畸形 |
| G05 | 四态判定先看 `.done` | 陈旧 `.done` + 存活 pid 被报成"已完成" |
| G06 | fastp 声明"clean FASTQ 是 Stage 1 的输入" | align-sort 的契约写**原始** FASTQ → 静默消费错的那一份 |
| G07 | 校验器读 `min_variants_indel` | Stage 0 的 schema 里没有它，也没被授权自造键 |
| G08 | docstring 示范"由阶段脚本调用" | 同一段示范 `conda run`（阶段脚本已在 conda 内）→ rc=127 |

> 为什么是 0.4.0 而不是 0.3.2：按本文件「版本号约定」，本版新增了**能力**
> （新 CLI 参数 `--prev-reads-file` 之外，本批新增多条门禁判据与一个 CLI 参数
> `--witness`/`--no-witness` 体系）→ `MINOR`。且三项**破坏性**的口径变更：
> 四态判定的优先级、`min_variants` 的 `0` 语义、`fastq_dir` 的决定权。

### 修复：七条「两端各说各话」，逐条

- **G02 照抄面引用了自己没定义的名字** —— `gwas-format-convert` 的 SESSION-SAFE 围栏
  改用 `$(dirname "${LOG_FILE}")`（与 6 个兄弟 skill 逐字相同），未定义的 `log_info` 换 `echo`，
  并补 `IN_VCF_GZ`/`PREFIX` 两个运行期占位（**只修前两处照抄仍 rc=1**）。提交前自查发现
  **同类残留未铺开**：6 份 gwas 兄弟模板另有 41 处未声明占位符、`gwas-ld-block` 缺 `source env.sh`，
  已一并补上。
- **G03 resume 判存在性比门禁松** —— 三处守卫 `-f` → `-s`，并写明"内容级问题 `-s` 覆盖不到"
  的边界。防线**从校验器源码推导**门禁的"存在"是什么，门禁将来收紧它会跟着收紧。
- **G04 `0` 的语义** —— 拆开 `raw <= 0` 单分支。豁免必须写成**整数** `0`：
  `False == 0` 与 `0.0 == 0` 都为真，类型写错必须留在畸形分支。
- **G05 四态判定顺序 + 开工清陈旧 `.done`** —— 判序改为 `.failed` → **pid 存活** → `.done`
  （两个站点）；阶段脚本**开工先清自己的 `.done`**（写 `.pid` 之前），与收尾清 `.failed` 成对。
  另补产物 glob 的空目录守卫。
- **G06 交接两侧对齐** —— align-sort 补输入契约表 + 逐样本赋值围栏（`R1`/`R2` 此前全仓无赋值点）；
  fastp 的 `sample_numbers`（**该键不存在**）改为 `samples`/`sample_names` + 空名单兜底；
  params-propose 补 `fastq_dir` 决策表；`reads_anchor` 的失败文案改掉错误前提。
- **G07 `min_variants_indel` 进 schema** —— 正规通路此前**无法声明**这个键，
  只能由校验器在 warn 里"教用户怎么写"。补语义三分说明（正整数 / `0` / 畸形），
  并点明不拿 SNP 标量去判 INDEL 的理由。
- **G08 docstring 示范与自述环境冲突** —— 改为裸 `python "${VERIFIER}"`，
  并补一整段机理（`conda run` 把 PATH 收窄成 `envs/Agent/bin`，那里没有 conda 可执行文件）。

**G01 保持不修**（用户裁定）：GWAS 管线整条没有阶段门禁。这是**已知边界**，
只维持 `docs/test-evidence.md` §11.6 的披露 —— 不假装它被解决了。

### 测试与证据

- **32 条变异，全部指名命中**（G02 **7**、G03 1、G04 4、G05 7、G06 7、G07 2、G08 2）：
  每条修复都验证了"还原它会让**具名**的 pytest nodeid 变红"——`rc != 0` 不算数
  （收集期错误 rc=4 会伪装成命中）。每次都按 md5 校验还原。
- **新增 6 条防线**，全部**按写法扫全仓**（不写死清单，从机器可读源推导目标集）：
  `test_bash_fences_do_not_reference_undefined_names`、
  `test_resume_guards_are_at_least_as_strict_as_the_gate`、
  `test_every_verifier_params_key_is_declared_in_the_schema`、
  `test_four_state_verdict_checks_pid_alive_before_done`、
  `test_stage_template_clears_stale_done_at_start`、
  `test_product_globs_guard_against_empty_directory`、
  `test_producer_output_form_appears_in_consumer_input_contract`、
  `test_stage1_input_claim_declares_selection_rule`、
  `test_nested_conda_run_demos_have_stage_context`。
  每条都带**判别力自检**（坏样本必红、好样本不误伤、并断言自己"推导出了东西"以防空转）。
- 全量回归 **939 passed**。逐条证据见 `docs/test-evidence.md` §11.12。

> **⚠️ 提交前的自查推翻了自己：G02 的防线第一版**自己**就是「纸面具备」。**
> 一份外部设计工作流对刚造好的防线提了质疑，逐条实测后**三条全部成立**：
> - 判据 (B)"调用 `log_*` 而全文档无定义"**判别力实测为零** —— 它不按名匹配（文档里
>   有**任意** `log_*()` 定义就整体放行），且正则的 `^` 要求 token 在**行首**，而全仓
>   **41 处真实调用里 33 处是缩进的**（全在失败分支 —— 正是它声称看住的地方）。
>   把 `wgs-express-filtering` 的整段定义删掉，谓词**仍返回 `[]`**。
>   更坏的是：判别力自检的坏样本恰好写成无缩进形式，两次变异注入的也是无缩进调用 ——
>   **自检与变异一起掩盖了它**。
> - 注释里写的"宽版 137 处命中"**任何口径都重算不出来**（实测 253 次出现 / 97 对 / 213 行）。
>   **写进代码注释的数字也是断言**，后人会照引。
> - 同类残留**没有铺开**：新判据 (D) 上线即抓出 **6 个 gwas 兄弟模板共 41 处**未声明占位符，
>   另发现 `gwas-ld-block` 调了 `skills/_shared/env.sh` 的 `sk_ensure_ld_path` 却没 source。
>
> 改法：判据收紧为**按名匹配 + 允许缩进**，新增 (C)/(D) 两条，**每条都配"真实形态"的正控**
> （缩进的失败分支调用、文档已定义**别的** `log_*`、不带 shebang 的片段示例），
> 并断言自己推导出了东西。变异 2 → **7**，全部指名命中。
> 设计方的另一条建议（强制 `log_*` 定义早于使用）复核后**不采纳**：会让 41 处正常的分块写法
> 全部误报 —— 那正是本条防线当初否掉"宽版"的同一个理由。

> **⚠️ 本轮最值得记的一条教训：防线是三次收紧才有效的，而每次收紧都抓出了真东西。**
> - 第一版按"断言单元"判 → 一条变异**溜过去**（把规则挪到下一句、同文件别处仍留关键词）；
>   收紧到"同句"后，立刻抓出一处**真残留**（fastp 的一句无条件断言，原先被下方解释块挡住）。
> - `.py` 侧的"单元"**不能用纯括号平衡** —— 多行 f-string 拼接的每一行各自都平衡，
>   不补「字符串拼接链」展开的话，**修好的代码反倒报红**。
> - 判据一度用 `_exec_code_lines()`（去注释）之外的原文扫描，把**说明文字**当成了实现。
>
> **另一条：本仓有一个跟踪中的符号链接指向仓外的部署副本**（`db_query.py`）。
> 一次变异**穿透**它写进了仓外文件，而 `git status` 全干净、仓内文件也没变 ——
> 直到全量回归报红才发现。已新增 `_repo_py_sources()` 统一跳过符号链接，
> 并只删掉自己注入的那一段（**不整文件覆盖** —— 副本有自己的既有漂移）。

---

## [0.3.1] — 2026-10-28

**主题：v0.3.0 的修复动作**自己**把「怎么跑」的配方弄失效了 —— 补齐并加一道
能自动发现这类脱节的防线。**

> 为什么是 0.3.1 而不是并进 0.3.0：0.3.0 已提交、打 tag、推送到远端，
> 改动已对外可见，不该改写已发布的记录。且按本文件「版本号约定」，
> 这一版是**文档纠错 + 测试补充、不改变对外契约** → `PATCH`。

### 修复：仓库自己的「怎么跑」配方不自足

v0.3.0 把 `tests/test_stage_verify.py` 从「缺工具静默 skip（rc=0）」改成
「缺工具收集期报错（rc=2）」——**方向正确**，但 `docs/test-matrix.md` 第 1 步
那条配方没有 `export PATH`，于是当场从"勉强算绿"变成**照着敲就报错**：

```
cd /home/chenyuxi/skill/agent && /home/chenyuxi/anaconda3/bin/python -m pytest tests/ -q
→ ERROR tests/test_stage_verify.py - RuntimeError: ...（退出码 2）
```

不是配方的文字写错了，而是**一处修复改变了另一处的有效性，而两边没有任何机制
把它们连起来**（本项目的 11.5 号失效模式的第七次换宿主 —— 前六次见
`docs/test-evidence.md` §11.5/§11.6/§11.7）。本机默认 PATH（含登录 shell）上
`samtools`/`bcftools`/`bgzip`/`tabix` **四个都没有**，实测确认。

- 修 `docs/test-matrix.md` 第 1 步：补 `export PATH`，并写明它**不是可选项**、
  给出 `SKILL_AGENT_ALLOW_SKIP_WGS_TOOLS=1` 这条显式跳过出口（会在
  warnings summary 里留告警，不无声跳过）。
- 新增失效模式防线 `_pred_pytest_recipe_missing_tool_env` + 参数化用例
  `test_run_recipes_declare_where_the_wgs_tools_come_from`：扫
  `docs/test-matrix.md` / `docs/test-evidence.md` 的 ```bash 围栏，**跑整目录
  （`tests/`）或点名 WGS 证据文件的配方必须说清工具从哪来**。
  点名单个**非 WGS** 文件不在射程内 —— 它不加载那个文件，本就不需要工具环境。

**只扫面向读者的「怎么跑」文档，不扫 dated 事件报告**
（`test-fix-report` / `session-concurrency-fix` / `rename-to-traitpilot` /
`harness-gap-analysis`）：那些是"某次运行发生过什么"的记录，其中的期望值
（如"期望 196 passed"）本就属于当时，**改它们等于篡改记录**。
这条界限是刻意的，已写进代码注释 —— 否则下一个维护者会以为那是漏判。

### 测试与证据

- `pytest tests/ -q` → **577 passed**（较 0.3.0 的 575 增 2：两条参数化配方护栏）
- `tools/concurrency/verify_fix_probe.py` → **64/64**
- `tools/concurrency/stage_verify_probe.py` → **FAILED 0**
- `tools/concurrency/skill_doc_consistency_probe.py` → **FAILED 0**
- 用例清单 `docs/test-matrix.md` 新增 **VERIFY-27**；逐条实测证据 `docs/test-evidence.md` §11.8

**双向 + 变异实测**（详见 §11.8）：
- 照抄修好后的配方（干净 shell，`env -i`）→ **577 passed**；
  同一条去掉 `export PATH` → 收集期 `RuntimeError`、退出码 2。
- **变异验证**：把 `docs/test-matrix.md` 那条配方回退成旧文本 → 护栏**变红并指名该块**；
  复原 → 全绿。
- 护栏判别力：负控 2 条、正控 3 条（显式跳过声明 / 点名单个非 WGS 文件 /
  pytest 只在注释里）。其中"点名单个非 WGS 文件"这条正控是刻意留的 ——
  过宽的正则会把它误判成缺口，从而把一整类合法配方全拦下，防线只能被关掉。
- 本轮写第一版时把 3 条正控错放进负控槽位，被 `test_guards_actually_discriminate`
  当场抓住（报"漏报"）—— **判别力自检这次抓的是我自己的手误**，正是它存在的理由。

> **这一版的教训**：前三次（11.5/11.6/11.7）都在修"修复没铺开"和"台账记漏了"，
> 这一次是同一个根因的第三种表现 —— **修复改变了别处文档的有效性**。
> 共同的自救办法仍是同一条：**别靠记忆，做一个能自动发现脱节的测量。**
> 补记见 `docs/test-evidence.md` §11.8。

---

## [0.3.0] — 2026-10-28

**主题：把门禁的"假绿"逐个拆掉 —— 判据读派生数据、判据只跑过 PASS、判据根本没被测试触碰，
三种形态在同一个版本里各修一遍。**

> 为什么是 0.3.0 而不是 0.2.1：本版**新加了 4 条 hard 判据**
> （`body_readable` / `genotypes` / `quickcheck` 的失败分支 / `index_drift`），
> 它们能**拦下以前放行的产物** —— 对使用者是可观察的行为变化，不只是一批修修补补。
> 另有一处语义收紧：表型侧 `fail_rate` 在缺 `状态` 列时由**假 PASS** 改为**明说未判定**。

### 修复：判据读**派生数据**而非本体（两个方向都错）

- **BAM 的 reads 数不再采信索引**（`read_count` 曾整条读 `samtools idxstats`）：
  - **陈旧/外来索引 → 放过坏数据**：200 reads 的健康 BAM 覆写成 0 reads 而索引未重建，
    idxstats 照旧值作答 → `[PASS] read_count: mapped=200`、rc=0。
    而 samtools 的 `older than` 警告**只在索引明确更旧时**出现（实测同秒覆写、
    `cp -p` 保留 mtime、外来索引**全都不警告**），故不能靠 stderr 判。
  - **索引漏计 → 拦下好数据**：Parabricks `pbrun applybqsr` 的 `.bai` 在中小输入上
    **系统性漏计**（真容器实测 10/200/300 reads → 索引报 0；1000→971；53296→53215），
    而 GPU 就绪时 parabricks 是文档规定的**默认后端** → 健康 BQSR BAM 被判"空 BAM"硬拦。
  - 修法：**本体计数为准**（`samtools view -c`，顺带就是"中段损坏"的判据），
    索引只作旁证。代价是健康路径多读一遍 BAM —— 刻意取舍，「放过坏数据」与
    「拦下好数据」都比"慢一点"贵得多，且下游 HaplotypeCaller 本就要全量读它。
  - 新增 `{label}.index_drift`(warn)：索引与本体分歧时**报出来**（漏计索引该被当坏产物
    暴露），但判 warn 不判 hard —— BAM 完好、下游可用，只需重建索引。
- **VCF 索引判据从"文件在不在"改为"真的用一次"**：原 `ok = have(vcf+".tbi") or have(vcf+".csi")`
  只判存在且非空，而文案却写"下游无法按区读取" —— 声称保证按区可读性，实际只保证文件在。
  实测四种坏索引全部 `[PASS]` rc=0：外来 `.tbi`（按区读 0 行、全量 3000 行）、纯文本 `.tbi`
  （rc=255）、截断的真 `.tbi`、**陈旧索引**（按区读 0 行、rc=0、stderr 全空）。
  下游 GATK `-L`、`bcftools -r`、`tabix` 会**静默**拿到空结果。
  修法：取全量扫描已知存在的首条记录坐标做一次按区读取，要求 rc=0 且 ≥1 行；
  拿不到坐标时登记"**未做可用性探测**"而非假装判过。
- **表型侧 `fail_rate` 的假 PASS**：缺 `状态` 列时 `n_fail` 取 0、`rate` 取 0.0，
  判据以 **hard 级 `[PASS] fail_rate: 0/3 个样本失败（0.0%）`** 出现在报告里 ——
  它**什么也没断言**，而读报告的人只看到一个绿色的"失败率 0%"。
  触发路径不是边角：`{trait}_phenotype_gwas_gapit.csv` 就是 `Taxa,<trait>` 两列、没有状态列，
  而它正是下游 GWAS 实际消费的文件之一。改为与 `value_range` 一致的处置：**明说未判定**。

### 修复：判据**根本没被测试触碰**（新增覆盖面测量手段）

审计证明"某判据没被测试覆盖"的办法是**强制它永远判 FAIL，看有没有人发现**。
本版把它反过来做成**可自动测量的指标**：给 `Report.add` 打桩记录
`(源码行号, level, passed)`，跑完整套测试后统计**哪些 hard 判据从未登记过 `passed=False`**。

修复前的结果：**hard 判据 42 种，4 种从未见过 FAIL** ——
`body_readable`（中段损坏的 BAM）/ `genotypes`（全 `./.` 或头部缺 `FORMAT/GT`）/
`quickcheck`（缺 BGZF EOF 块）/ `read_totals_recorded`（无失败分支，设计使然）。
这比"判据名有没有被提到"严一层：判据**跑了**，只是只在健康数据上跑过 ——
于是它接错参数、判反条件、把真值写成常量 `True`，整套回归**一个红灯都不会有**。

补 9 条阴性对照后 **4 → 1**。每条都先**自证 fixture 的前提**再断言：
如"中段损坏"必须先证 `quickcheck rc=0 且 view -c rc≠0`，否则测的不是那一层
（否则会"因错误的原因通过"）。9 个变异体逐一验证，**9/9 被捕获且都由指名的那条测试杀掉**。

新增的两条互补判据实测分工（都补了测试）：
| 损坏形态 | `quickcheck` | `view -c` | 拦截者 |
|---|---|---|---|
| 中段损坏（长度/头/EOF 不变） | rc=0（看不见） | rc=1 | `body_readable` |
| 缺 BGZF EOF 块 | **rc=16** | rc=0（照读） | `quickcheck` |

单靠任何一条都抓不全 —— 这正是"两条判据各司其职"的实测证据。

### 修复：WGS 门禁用例**静默跳过**（退出码与"全部通过"无法区分）

`tests/test_stage_verify.py` 原为整文件 `pytestmark = pytest.mark.skipif(...)`。
实测（造只含 python/bash 的 PATH）：`453 passed, 49 skipped`、**退出码 0** ——
在没装 samtools/bcftools 的机器或 CI 上，"整套门禁判据从未运行"看起来与"全部通过"完全一样。

- 改为模块级守卫 `raise RuntimeError`（**缺工具是失败、不是跳过**），消息含四个工具的
  探测结果与当前 PATH、两条可操作修法；确要跳过须**显式**设 `SKILL_AGENT_ALLOW_SKIP_WGS_TOOLS=1`。
- **四个工具都要判**：只判 samtools/bcftools 时，`bgzip`/`tabix` 缺失会让 **6 条**用例
  （combined GVCF / GVCF 身份 / INDEL 下限）**单独静默跳过**，而模块级守卫照样绿 ——
  缺口从大门挪到了侧门。
- 显式跳过路径**也要出声**：用 `warnings.warn` 而**不是** `print(..., file=sys.stderr)`。
  实测不带 `-s` 时 pytest **捕获**收集期 stderr（print 完全不出现），
  而 warnings summary 在默认输出里**必定**打印。
- 立场与产品侧一致：`stage_verify.py` 自己的 `need_tool()` 是 fail-closed 的
  （缺工具登记为 hard 失败），证据侧缺工具也必须拦。

### 文档：把"门禁管到哪"说清楚

`skills/phenotype-extract/SKILL.md` 原写"同一个门禁也可事后对**任意明细 CSV** 跑，
供 GWAS 入口复用"，而下游真正消费的两个 `_gwas_*` 产物**跑不了**：
plink.txt 无表头（`[err] 列 'IMF' 不在 CSV 中` rc=1，即"根本无法复核"而非"值不合格"）；
gapit.csv 无 `状态` 列（`value_range`/`fail_rate` 失去前提）。现收紧为"**明细 CSV**"，
并新增警告块说明两个导出**本身不过门禁**（是投影、不是被校验对象）。

### 测试与证据

- `pytest tests/ -q` → **575 passed**（较 v0.2.0 的 516 增 59）
- `tools/concurrency/verify_fix_probe.py` → **64/64**
- `tools/concurrency/stage_verify_probe.py` → **FAILED 0**
- `tools/concurrency/skill_doc_consistency_probe.py` → **FAILED 0**
- 用例清单 `docs/test-matrix.md`（新增 VERIFY-25 静默跳过 / VERIFY-26 覆盖面测量）；
  逐条实测证据 `docs/test-evidence.md` §11.7

> **这一版的教训值得单独记**：§11.6 的台账声称"33 条审计发现逐条处置"，
> 复核时发现**只覆盖 24 条** —— 记账时按"我记得改过哪些"写，漏掉的正是
> 改动分散在多轮、部分落在更早批次里的那些。更糟的是其中一条被标成"已由上一批覆盖"，
> 而**重新测量后那个判断是错的**。补救办法不是"下次仔细点"，而是**做一个能自动发现遗漏的
> 测量**：正如 v0.2.0 用 `test_defect_class_guards.py` 按失效模式扫全部文档、
> 本版用打桩测判据覆盖面 —— **台账的完整性也必须被测量，而不是被声明。**
> 补记见 `docs/test-evidence.md` §11.7。

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
