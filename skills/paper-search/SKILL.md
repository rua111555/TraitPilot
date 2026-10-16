---
name: paper-search
description: 按关键词搜索指定年限的顶刊顶会论文，去重下载 PDF，并用中文对每篇生成 Markdown 解读报告。当用户给出关键词与年限、要求检索/搜索/下载并解读「顶刊/顶会/顶级期刊/顶级会议」论文时激活（如"搜索2020-2024年NeurIPS上关于大模型的工作并解读"）。
---

# paper-search：关键词 → 顶刊顶会论文 搜索/去重下载/中文解读

## 目标与适用场景
用户给出**大致关键词**（可为中文）与**年限范围**，要求从顶刊顶会中检索、下载并逐篇解读，且**不重复下载同一篇论文**。默认按 `field=ai`（AI/计算机）的白名单 venue 过滤，可用参数覆盖。报告默认**中文**，`--max-total` 默认 **15 篇**。

## 工具与资源
- 脚本 `paper_search.py`（纯 Python 标准库，零依赖）。脚本可随仓库放在本 skill 目录，或独立目录；定位顺序：
  1. `$PAPER_SEARCH_DIR/paper_search.py`（推荐：部署时设该环境变量指向脚本所在目录）
  2. 当前工作目录下的 `paper_search.py`
  3. 同目录旁路：`$(dirname <本 SKILL.md>)/paper_search.py`（若把脚本随 skill 一起放置）
  找不到时明确报错并提示放置方式，**不回退到任何写死的个人绝对路径**。
- 配套 `curate_run.py`：给定种子标题 → 按 DOI/arXiv 稳健下载（供"抓取特定论文全集"场景）。
- venue 白名单：与脚本同级的 `venues.json`。字段 `ai` 已包含 NeurIPS/ICML/ICLR/CVPR/ICCV/ECCV/AAAI/ACL/EMNLP/NAACL/KDD/SIGMOD/VLDB/OSDI/SOSP/USENIX Security/IEEE S&P/CCS/WWW/CHI/AISTATS/ICRA/IROS/COLING 与 Nature/Nature MI/Nature Communications/Nature Computational Science/Science Advances/TPAMI/JMLR/TMLR/IJCV/JAIR/TACL 等。
- 产物目录：`${PAPER_SEARCH_DIR:-$HOME/paper_search}/papers`（未设则用 `$HOME/paper_search`，可用 `--outdir` 覆盖）。
- 数据源：OpenAlex（主）→ Crossref（降级）；OA PDF 用浏览器 UA 下载并含 PMC 备用源。Semantic Scholar 建议不用（匿名易限流）。

## 可靠性保证（必读）
脚本在**下载后**对每个 PDF 做**内容级校验**（`verify_download`），不再只信文件名：
1. 校验内容是合法 PDF（`%PDF` 头 + 大小上限）；
2. 提取 PDF 首页文本，与候选标题做 token 命中率比对（默认 ≥50%）；
3. 候选是 arXiv 时，校验 PDF 内嵌的 arXiv 编号与候选一致；
4. 链中有多个源（best PDF → OA url → PMC）时，**逐个源校验、通过的才落盘**；全不通过则删除并标记 `failed`，绝不会出现「正确文件名 + 错误内容」的坏档。
> 系统无 `pdftotext` 时内容校验自动跳过（标记 `*tool`），此时仅靠 PDF 头判断。请尽量保证 pdftotext 可用。

## 工作流

### 1. 理解需求
提取：关键词（1 个或多个）、年限（如 `2020-2024`）、总篇数（默认 15，用户可指定）、领域/额外 venue、语言（默认中文）。

### 2. 扩展关键词（Claude 执行）
将每个粗略词扩展为 **2–4 个精确检索串**（英文为主，含同义与近邻概念）：
- "大语言模型" → `large language model`、`foundation model`、`LLM`、`instruction tuning`
- "扩散模型" → `diffusion model`、`score-based generative model`、`denoising diffusion`
用户明确不要扩展时直接使用原词。扩展后的词作为多个 `--keyword` 传入，脚本会跨关键词去重。

### 3. 搜索 + 去重 + 下载（脚本执行）
先 `--dry-run` 预览命中清单，确认后正式下载（`$SCRIPT` = 上节定位到的 `paper_search.py`）：
```bash
PAPER_DIR="${PAPER_SEARCH_DIR:-$HOME/paper_search}"
python3 "$SCRIPT" \
  --keyword "large language model" --keyword "LLM" --keyword "foundation model" \
  --years 2023-2024 \
  --max-total 15 \
  --field ai \
  --outdir "$PAPER_DIR/papers" \
  --dry-run
```
正式下载（去掉 `--dry-run`）后读取 `$PAPER_DIR/papers/meta.json` 与 `library.json`：
- `meta.counts`：候选 / 新下载 / 重复 / 无PDF / 失败。
- `meta.downloads`：本次新下载清单（含 `slug`、`pdf_path`、`status="downloaded"`）。
- `meta.duplicates` / `meta.no_pdf` / `meta.failed`：分别处理；`failed` 中带「内容校验失败」的说明文件内容与标题不符，可放心忽略该坏档（脚本已删除）。

**去重保证**：脚本用 idkey（规范化 DOI，无 DOI 用规范化标题）+ PDF sha256 双重去重；同一论文跨关键词、跨运行都不会重复下载，已下载记录在 `library.json`。这正是"不能下载到同一篇论文"的保障。

### 4. 解读（关键：用子代理并行，控制主上下文）
> ⚠️ **本步骤必须用子代理完成逐篇阅读与写作，主 agent 严禁自己逐篇 Read PDF**（一次 15 篇的 PDF 会撑爆上下文）。主 agent 只做**派发、核对、收尾**。

对 `meta.downloads` 中每篇**新下载**的论文：

1. **派发子代理**（用 Agent tool，`run_in_background: true` 并行）。建议**一批 ≤4 个并发**，其余排队；每个子代理的任务：
   - 只负责**1 篇**论文：用 Read 工具读取该 PDF（`pdf_path`；多页按需分段），按下方「报告模板」写中文报告。
   - 报告写入**固定路径**：`<outdir>/<关键词>/<slug>.md`（与 PDF 同名配对）。
   - 元数据（venue/year/DOI/作者/被引）可在发送前由主 agent 从 `library.json` 对应条目预填进指令，让子代理不必重复查。
   - 明确要求：**忠于 PDF 实际内容**，禁止编造；PDF 未明确处标注「PDF 中未明确」；输出层级标题；若自身读 PDF 失败要报告，不要硬写。
2. **收尾校验**（主 agent）：
   - 子代理全部完成后，检查每篇 `<slug>.md` 是否已生成且非空、标题与 PDF 配对正确；
   - 失败/超时的子代理：主 agent 重新派发一次；
   - 3 次仍失败的：在 index.md 标注「解读待补」，并如实告知用户该篇未产出报告。
3. `no_pdf` / `failed` 论文**不写全文报告**，只在 index.md 中标注「无开放获取 PDF」/「下载失败（原因）」留档。

**报告模板（中文 Markdown）**：
```markdown
# <论文标题>

| 字段 | 内容 |
|---|---|
| **会议/期刊** | <venue> |
| **年份** | <year> |
| **作者** | <前5位…等> |
| **机构** | <第一作者/通讯机构，如 PDF 开头可获取> |
| **DOI** | <doi> |
| **被引** | <cited_by>（检索时） |
| **关键词** | <3–6 个> |

## 一句话总结
…

## 研究背景与动机
…

## 核心方法与思想
…

## 主要创新点
- …

## 实验设计与结果
…

## 局限与讨论
…

## 可借鉴之处 / 对本课题的启发
…

## 复现要点
<是否开源/代码/数据链接、复现关键细节>
```

### 5. 生成 `<outdir>/index.md` 索引
按关键词分组汇总本次全部论文（含无PDF/失败项的留档）：
```markdown
# 论文解读索引
> 关键词：… ｜ 年限：… ｜ 共 N 篇（新下载 X，去重跳过 Y，无PDF Z）

## <关键词1>
| # | 论文 | 会议/期刊 | 年份 | 被引 | 状态 | 解读 |
|---|---|---|---|---|---|---|
| 1 | <标题> | NeurIPS | 2023 | 1234 | ✅ | [解读](<keyword1>/<slug>.md) |
…
```
状态标记：`✅ 已下载并解读`、`📄 无开放获取 PDF（仅记录）`、`❌ 下载失败（原因）`、`↩️ 与历史重复（跳过）`。

### 6. 向用户汇报
总结：原始关键词 → 扩展词 → 候选命中 → 新下载 → 解读数量，输出路径，无PDF/失败/重复清单，以及下一步建议（如换年限、加关键词、加 venue）。

## 注意事项
- 解读以**真实读取 PDF**为准，且由**子代理**完成；主 agent 只读 `library.json`/`meta.json` 元数据与子代理报告结果。
- OpenAlex 匿名搜索可能 429，脚本已自动重试退避，且逐关键词自动降级到 Crossref 并在 stderr 提示；仍失败时向用户说明，可建议稍后再试或显式 `--source crossref`。
- 一些高被引顶会论文在 OpenAlex 里仅有「arXiv 预印本」记录（会议版本无独立记录），严格白名单会漏掉它们；如需纳入，给脚本加 `--allow-arxiv`，脚本会把「仅 arXiv 记录」的候选标为 `arXiv(预印本)`。
- 论文应落在 `<outdir>/<关键词>/`（不是 `misc/`）；若出现 misc 说明关键词未正确传递，检查扩展词。
- 默认 `--field ai`；其他领域用 `--field`（需先在 venues.json 配置对应字段）或 `--venues "Nature;Science;Cell"` 追加。
- **若发现已下载 PDF 内容与标题不符**（老坏档）：删除该 PDF 与对应 library 条目后重跑下载（新脚本会自动校验）；报告缺失时按第 4 节补派子代理。
