---
name: agent-news
description: 每5天抓取「智能体开发」一线资讯（主流框架 GitHub Releases / OpenAI·Anthropic·Hugging Face 官方新闻 / arXiv LLM-agent 最新提交），增量去重落盘中文速递，并对结果生成配套中文解读（每个仓库是干什么的、每篇论文做了什么）。当用户要「智能体/agent 开发资讯」「今日/最近 agent 动态」「最近框架/模型/协议有啥新东西」「解读/总结这些 agent 资讯」时激活。服务于智能体相关论文写作的背景跟进。
---

# agent-news：智能体开发资讯 · 速递 + 解读

## 目标与适用场景
用户在做**智能体相关论文**，需要跟进主流智能体框架、官方新闻、LLM-agent 学术动态，以完善论文背景/相关工作/趋势章节。每次产出**两层结果**：
1. **速递**：纯脚本抓取的中文结构速递 `agent_news/reports/YYYY-MM-DD.md`；
2. **解读**：Claude 对速递生成的配套解读 `agent_news/reports/YYYY-MM-DD.解读.md`，满足：
   - 每个 **GitHub 仓库**：总结「这个框架是干什么的」+ 本次发版重点；
   - 每篇 **arXiv 论文**：总结「这篇论文做了什么」；
   - 按与用户论文的相关度排序。

数据源（`sources.json` 可增删改）：15 个主流框架/工具的 **GitHub Releases**（走 `api.github.com` REST API —— 本机 github.com 网页被墙但 API 可达）；OpenAI 新闻 RSS；Hugging Face Blog（`hf-mirror.com` 镜像兜底）；Anthropic /news（HTML 解析）；**arXiv 学术速递**（`all:"LLM agent" OR all:"multi-agent system"`）。

## 工具与资源
- 脚本 `agent_news.py`（纯 Python 标准库，零依赖）。**脚本不在本仓库**，定位顺序：
  `$AGENT_NEWS_DIR/agent_news.py` → 当前工作目录 → 与本 SKILL.md 同目录；找不到即报错提示放置方式（不回退写死的个人路径）。
- 信源配置：与脚本同级 `sources.json`。
- 产物目录 `${AGENT_NEWS_DIR:-$HOME/paper_search}/agent_news/`（亦可用脚本参数指定）：
  - `reports/YYYY-MM-DD.md` — 速递（cron 每 5 天 08:17 自动生成）；
  - `reports/YYYY-MM-DD.解读.md` — **配套解读（Claude 生成，skill 触发）**；
  - `index.md` — 入口：最新一篇 + 近 30 天研究主题热度 + 历史清单 + 信源状态；
  - `state.json` — 跨次去重（`seen`）+ 统计（勿手改）。

## 工作流

### 1. 理解需求
- **要看资讯**：说「最近的 agent 资讯 / 今日 agent 动态」。先看最新 `reports/*.md`；**如果最新速递较旧（5 天 cron 尚未跑到）或用户要求，先跑脚本补新**再解读。
- **要解读**：说「解读 / 总结这些资讯」。读取（或先生成）最新 `reports/YYYY-MM-DD.md`，按第 3 节生成 `解读.md`。

### 2. 运行脚本（需要时）
```bash
NEWS_DIR="${AGENT_NEWS_DIR:-$HOME/paper_search}"
python3 "$NEWS_DIR/agent_news.py" --dry-run     # 先预览命中
python3 "$NEWS_DIR/agent_news.py"                # 正式生成速递 + 更新索引
python3 "$NEWS_DIR/agent_news.py" --since 2026-01-01   # 年度模式：年初至今总览
```
- 默认回溯窗口 **5 天**（`--days 5`），与 5 天 cron 配套；首次运行回溯 10 天基线。每个 GitHub 仓库拉最近 20 个 release，单次运行约 16 次 API 调用，远低于匿名 60 次/时配额。
- **年度模式（`--since YYYY-MM-DD`）**：分页拉全年度 Releases（per_page=100）+ arXiv 按自然月抽样 25 篇打趋势 + 官方 feed 全量，生成 `reports/<年份>-annual.md`；**不动 `state.json`**（不污染日更去重）。产出后用第 3 节写 `<年份>-annual.解读.md`。
- 重复运行不会重复上屏（`state.json` 增量去重）；当日已生成且无新增时保留原档。
- 任意信源失败不致命，报告标注「⚠️ 不可达信源」；GitHub 配额耗尽时自动跳过后面的仓库。

### 3. 生成解读（**默认执行**，除非用户只要速递）
对最新一轮（或用户指定日期的）`reports/YYYY-MM-DD.md`，撰写 `reports/YYYY-MM-DD.解读.md`：
- **arXiv 论文**：逐篇一句「做了什么」——研究目标 / 方法概览 / 关键发现；
- **框架发布（GitHub）**：先复述一句「这个框架是干什么的」（速递里已有「说明：」行可借用），再挑**本次最重要变动**（新能力/协议/大版本/新模型接入）；
- **标注相关度**：对用户论文主题（智能体）给出 ★ 高/中/低；
- 收尾给出「**对这篇论文最有启发的事**」Top 3。

解读是**小文件任务**，主 agent 直接读 `reports/*.md` 撰写，无需子代理。模板：

```markdown
# <日期> 智能体资讯解读

> 与你的智能体论文相关的逐条解读；相关度：★★★ 高 / ★★ 中 / ★ 低

## 一、论文速览（—）
| 相关度 | 论文标题 | 这篇论文做了什么 | 链接 |
|---|---|---|---|
| ★★ | ... | ... | ... |

## 二、框架与工具发布解读（—）
### <框架名>（— 条）
- **是干什么的**：...
- **本次重点**：最值得关注的变化
...

## 三、官方新闻值得关注
...

## 四、对本篇论文最有启发的事（Top 3）
1. ...
```

### 4. 向用户汇报
- 汇报：本轮新增条数、解读文件路径、相关度最高的是哪几篇/哪个发布；
- 若用户马上要写某章节，可直接基于解读帮其归并素材。

## 注意事项
- `state.json` 是去重命脉，勿手改；想重置全部重抓可删除后重跑（会重新回溯窗口）。
- **网络**：本机 GitHub 网页 / huggingface.co / DeepMind 被墙 → 不新增此类信源；新增前先 `curl -s -o /dev/null -w "%{http_code}"` 实测。**Claude 会话内跑脚本可能受沙箱限制**：`api.github.com` 可能 403 致 GitHub 抓不到，但 OpenAI/Anthropic/HF/arXiv 正常；**完整抓取靠 cron（真实网络）或用户在终端执行**。会话内主要做「读已有速递 + 生成解读」。
- 报告里的 `#tag` 是关键词启发式打标（词边界），供快速浏览；论文引用/深挖务必回到原文。
- cron 每 5 天 08:17：`crontab -l` 查看、`crontab -e` 调整或停用。想改回每天或改成固定星期几，见 README。
