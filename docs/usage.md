# SkillAgent 使用文档

> 本机自研、**自包含**的单 Agent harness：技能放在本仓库 [`skills/`](../skills/)，
> 配置从本目录 `config.json` 读取——**不依赖 Claude Code**。
> 自带 21 个技能（WGS / GWAS 两大管线 + 表型提取等）。入口在仓库根 `agent/`。
>
> 本文覆盖：安装与凭证、三种入口、全部命令行参数、环境变量、REPL 命令、Web 前端、
> 12 个可选 harness 能力、技能机制、端到端示例、故障排查。
> 设计与实现细节见 `docs/harness-gap-analysis.md`，依赖决策见 `docs/dependency-strategy.md`。

---

## 目录

1. [安装与凭证](#1-安装与凭证)
2. [三种入口](#2-三种入口)
3. [命令行参数总表](#3-命令行参数总表)
4. [环境变量总表](#4-环境变量总表)
5. [REPL 内命令](#5-repl-内命令)
6. [Web 前端](#6-web-前端)
7. [可选能力（harness 模块）](#7-可选能力harness-模块)
8. [技能机制](#8-技能机制)
9. [端到端示例](#9-端到端示例)
10. [故障排查](#10-故障排查)

---

## 1. 安装与凭证

### 依赖

**Agent 本体**只依赖 `requests + PyYAML + flask`，装在 conda 虚拟环境 **`Agent`** 里
（与 WGS/GWAS 管线共用同一环境）；Python ≥3.10（本机 `Agent` 环境为 3.12.13）。

```bash
cd <repo>
conda run -n Agent python -c "import requests, yaml, flask; print('ok')"   # 确认依赖在位
```

> 用哪套解释器都行（`Agent` 环境 / base / 系统 python3 均可），只要装了这三个包。本项目的
> `start_web.sh` 会优先挑 `Agent` 环境的解释器。

> ⚠️ 这三个包**只够 Agent 本体**（对话 / 读技能 / 执行 Bash）。自带 21 个技能背后还有三套 conda
> 环境（`Agent` / `r-gwas` / `Phenotype`）、若干独立二进制与数据文件。
> **一键安装**：`bash environment/install.sh`（`--list` 先看规格）；
> **完整清单**见 [`environment/README.md`](../environment/README.md)；
> **自检**：`bash environment/check_env.sh`（`--core` 只查本体）。

### 凭证与配置

Agent 从**本目录的 `config.json`** 与环境变量取配置，优先级：

1. 环境变量 `ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN`（或 `ANTHROPIC_API_KEY`）
2. `config.json`（本目录或 `--config` 指定；模板见 `config.json.example`）
3. 代码默认值

首次使用：复制模板并填写 `api_key`：

```bash
cd <repo>
cp config.json.example config.json
# 编辑 config.json：填 api_key（及可选 base_url / model）
python3 -m skill_agent
```

也可改用环境变量（优先级更高）：

```bash
export ANTHROPIC_BASE_URL="https://api.anthropic.com"
export ANTHROPIC_AUTH_TOKEN="<你的token>"
```

> `config.json` 已被 `.gitignore` 忽略，凭证不会入库。`config.json` 还是**功能总开关**：
> 把想用的能力由 `false` 改成 `true` 即开启（见下一节与 §7）。
> 其 `env` 块可集中设置技能数据路径（`GENO_DIR` / `IMAGE_DIR` / …，见 §4.1），免去逐次 `export`。
> 代理侧目前只稳定提供 `DSv4-flash` 一个模型 id（`claude-*` 常 503）。见 [参数 `-m`](#3-命令行参数总表)。

### config.json 功能开关一览

在 `config.json` 里设置这些键即可打开对应 harness 能力（省略 = 用默认，多数默认关）。
也可换成同名的 `SKILL_AGENT_*` 环境变量或 CLI 参数（CLI > 环境变量 > config.json）。

| 配置键 | 打开的能力 | 说明 |
|---|---|---|
| `retrieval` | #11 检索 | 挂载 `Search`/`RepoMap`（FTS5 索引） |
| `todo` | #9 规划 | 挂载 `TodoWrite`，计划外部化 |
| `memory` | M12 分层记忆 | `core.md` + archival 检索 |
| `summary_compaction` | M12 摘要压缩 | 长对话按结构化前情摘要压缩 |
| `persist` | M13 会话持久化 | 逐条落盘，支持 `--continue/--resume` |
| `parallel_tools` | M7 只读并行 | 同轮多个只读工具并行执行 |
| `loop_guard` | M3 死循环熔断 | 无进展循环检测（`loop_warn`/`loop_halt` 调参）|
| `idempotency` | M11 幂等去重 | 有副作用工具 run 内只执行一次（`idempotency_mode`）|
| `prompt_cache` | #14 prompt cache | 稳定前缀打 `cache_control` |
| `restrict_paths` | M8a 路径边界 | 越界读写拒绝 |
| `write_approval` | M8a 写审批 | Write/Edit 纳入审批门 |
| `trace` | #6 可观测性 | 工具调用 trace 落盘（等价 `--trace`）|
| `spill` | M7 结果落盘 | 超长工具结果转存（`spill_threshold`）|
| `sandbox` | M9 沙箱 | `host`/`rlimit`/`docker`（`sandbox_strict`/`sandbox_spec`）|
| `fallback_models` | #12 模型降级 | 过载后依次切后备模型（数组）|

调参键（可省略）：`max_tokens`、`max_turns`、`max_seconds`、`max_output_tokens`、
`spill_threshold`、`loop_warn`、`loop_halt`、`index_dir`、`retrieval_roots`、
`retrieval_excludes`、`plugin_dirs`。基础键：`base_url`、`api_key`、`model`、`permission`、
`skills_dir`、`cwd`。完整模板见 [`config.json.example`](../config.json.example)。

### 项目根与技能目录探测

Agent 需要找到「含 `skills/` 的项目根」。默认从当前目录**向上探测**；找不到则取当前目录。
可用 `--project <dir>` 显式指定，用 `--cwd <dir>` 指定工具执行目录（默认 = 项目根）。

```bash
cd <repo> && python3 run.py list      # 自动探测到 <repo>
```

---

## 2. 三种入口

### 2.1 列技能

```bash
cd <repo>
python3 run.py list              # 表格：技能 / 自动可调 / 用途
python3 run.py list --json       # JSON：description/path/model_invocable/allowed_tools
```

### 2.2 交互 REPL（推荐日常使用）

```bash
python3 run.py chat              # 或 python3 -m skill_agent（等效，无子命令默认进 REPL）
```

进入后可直接说需求（如「用 gwas-pipeline 说明标准流程」），用 `/` 命令控制会话（见 §5）。

### 2.3 单轮脚本化（不进 REPL）

```bash
python3 run.py chat -p "用 gwas-pipeline 说明标准流程，先不要真正跑数据"
python3 run.py chat -p "列出 phenotype-extract 提供哪些表型" -m DSv4-flash
```

`-p/--prompt` 跑完即退出，适合脚本/批处理。

> **入口等价**：`python3 run.py <args>` 与 `cd agent && python3 -m skill_agent <args>` 完全等价。

---

## 3. 命令行参数总表

所有参数同时可用于 `chat` 子命令与顶层（如 `... chat --retrieval -p "..."` 或 `... --retrieval chat`）。

### 3.1 基础

| 参数 | 作用 | 默认 |
|---|---|---|
| `-m, --model MODEL` | 模型 id | `SKILL_AGENT_MODEL` → `claude-opus-4-5`（代理下建议 `DSv4-flash`） |
| `--project DIR` | 项目根（含 `skills/`） | 从 cwd 向上自动探测 |
| `--skills-dir DIR` | 显式技能目录 | `<project>/skills` |
| `--config FILE` | 显式 config.json 路径 | `<project>/config.json` 或 `./config.json` |
| `--cwd DIR` | Bash 等工具的执行目录 | = 项目根 |
| `--permission {auto,ask}` | `auto`：危险命令仅告警；`ask`：每个 Bash 先询问 | `auto` |
| `--max-tokens N` | 单次响应 token 上限 | `8192` |
| `--no-stream` | 关闭流式输出 | 关（默认流式） |
| `--verbose` | 打印每次工具调用 | 关 |
| `-p, --prompt TEXT` | （仅 chat）单轮运行后退出 | 无 → 进 REPL |

### 3.2 安全与治理

| 参数 | 作用 | 默认 |
|---|---|---|
| `--restrict-paths` | **M8a** 启用工作区路径边界（越界拒绝） | 关 |
| `--write-approval` | **M8a** 把 Write/Edit 纳入审批门 | 关 |
| `--sandbox {host,rlimit,docker}` | **M9** Bash 执行后端（见 §7.7） | `host` |
| `--idempotency` | **M11** 同一次运行内有副作用工具去重（见 §7.9） | 关 |

### 3.3 预算与稳定性

| 参数 | 作用 | 默认 |
|---|---|---|
| `--max-turns N` | **M1b** 单轮最多模型轮次 | 不限（内部上限 60） |
| `--max-seconds N` | **M1b** 单轮墙钟上限（秒） | 不限 |
| `--max-output-tokens N` | **M1b** 单轮输出 token 上限 | 不限 |
| `--loop-guard` | **M3** 无进展死循环熔断 | 关 |
| `--fallback-models M1,M2` | **#12** 过载重试耗尽后依次切后备模型 | 空（不切换） |

> 预算熔断只在**发请求前**或**整轮后**判定，到点只停止后续调用、**不杀后台任务**。

### 3.4 上下文与持久化

| 参数 | 作用 | 默认 |
|---|---|---|
| `--summary-compact` | **M12** 摘要式压缩（长管线不丢阶段参数/路径/审批结论） | 关 |
| `--memory` | **M12** 分层记忆（core.md + FTS5 archival 检索） | 关 |
| `--persist` | **M13** 逐条消息落盘，支持 resume | 关 |
| `--continue` | **M13** 恢复最近一次会话 | 关 |
| `--resume SID` | **M13** 按会话 id 恢复 | 无 |

### 3.5 工具治理与扩展

| 参数 | 作用 | 默认 |
|---|---|---|
| `--plugin-dir DIR` | **M4** 加载声明式插件工具（可重复） | 不加载 |
| `--trace` | **#6** 工具调用 trace 落盘 jsonl | 关 |
| `--spill` | **M7** 超长工具结果落盘转存（可继续 Read） | 关 |
| `--parallel-tools` | **M7** 同轮只读工具并行执行 | 关 |
| `--todo` | **#9** 挂载 `TodoWrite` 计划工具 | 关 |
| `--prompt-cache` | **#14** 给稳定前缀打 `cache_control: ephemeral` | 关 |
| `--retrieval` | **#11** 挂载 `Search`/`RepoMap` 检索工具 | 关 |
| `--index-dir DIR` | **#11** 覆盖检索索引目录 | `<project>/.skill-agent/index` |
| `--index-rebuild` | **#11** 启动时强制重建检索索引 | 关 |

---

## 4. 环境变量总表

给 Web/非 CLI 场景使用；统一前缀 `SKILL_AGENT_`。CLI 参数存在时以 CLI 为准。
此外 `ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN` / `ANTHROPIC_API_KEY` / `SKILL_AGENT_CONFIG`
分别覆盖 `config.json` 的 `base_url` / `api_key` 与配置文件路径。

| 变量 | 对应 | 说明 |
|---|---|---|
| `SKILL_AGENT_MODEL` | `-m` | 模型 id |
| `ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN` / `ANTHROPIC_API_KEY` | — | 覆盖 `config.json` 的 base_url / api_key |
| `SKILL_AGENT_CONFIG` | `--config` | config.json 路径 |
| `SKILL_AGENT_PERMISSION` | `--permission` | `auto`/`ask` |
| `SKILL_AGENT_RESTRICT_PATHS` | `--restrict-paths` | 真值 `1/true/yes/on` |
| `SKILL_AGENT_WRITE_APPROVAL` | `--write-approval` | 同上 |
| `SKILL_AGENT_SUMMARY_COMPACTION` | `--summary-compact` | 同上 |
| `SKILL_AGENT_MEMORY` | `--memory` | 同上 |
| `SKILL_AGENT_PERSIST` | `--persist` | Web 默认开；`=0` 关闭 |
| `SKILL_AGENT_SESSION_ID` | `--resume` | 指定会话 id |
| `SKILL_AGENT_MAX_TURNS` / `_MAX_OUTPUT_TOKENS` / `_MAX_SECONDS` | M1b | 整数 |
| `SKILL_AGENT_LOOP_GUARD` | `--loop-guard` | 布尔 |
| `SKILL_AGENT_PLUGIN_DIRS` | `--plugin-dir` | 冒号分隔的目录列表 |
| `SKILL_AGENT_HOOKS` | `--trace` | 布尔 |
| `SKILL_AGENT_SPILL` / `_SPILL_DIR` | `--spill` | 布尔 / 目录 |
| `SKILL_AGENT_PARALLEL_TOOLS` | `--parallel-tools` | 布尔 |
| `SKILL_AGENT_SANDBOX` | `--sandbox` | `host`/`rlimit`/`docker` |
| `SKILL_AGENT_SANDBOX_SPEC` | — | JSON 对象（镜像/内存/CPU 等） |
| `SKILL_AGENT_SANDBOX_STRICT` | — | 布尔：档位不可用改为 fail-closed |
| `SKILL_AGENT_IDEMPOTENCY` / `_MODE` | `--idempotency` | 布尔 / `reuse`\|`deny` |
| `SKILL_AGENT_TODO` | `--todo` | 布尔 |
| `SKILL_AGENT_PROMPT_CACHE` | `--prompt-cache` | 布尔 |
| `SKILL_AGENT_FALLBACK_MODELS` | `--fallback-models` | 逗号分隔 |
| `SKILL_AGENT_RETRIEVAL` | `--retrieval` | 布尔 |
| `SKILL_AGENT_INDEX_DIR` / `_INDEX_ROOTS` / `_INDEX_EXCLUDES` | `--index-dir` | 目录 / 冒号分隔的根 / 追加排除 |

### 4.1 技能数据路径（与物种/项目无关）

技能与脚本用下面这组环境变量定位**数据与外部脚本**，均可用环境变量直接设置，或写进
`config.json` 的 `env` 块（启动时注入进程环境；**真实环境变量优先**，不覆盖）：

| 变量 | 含义 | 缺省 |
|---|---|---|
| `GENO_DIR` | 基因组学数据根（DB、参考基因组、FASTQ、输出） | `$HOME/genomics_data` |
| `IMAGE_DIR` | 表型图片根 | `$GENO_DIR/images` |
| `PIPELINE_DB` | SQLite 库路径 | `$GENO_DIR/pipeline.db` |
| `FAM_PATH` | 表型输出排序基准 `.fam`（可选） | 空 → 按样本号排 |
| `NIR_XLS` / `CORR_XLS` | 表型参照真值表 / 样本-个体关联表（可选） | 空 |
| `PAPER_SEARCH_DIR` | `paper-search` 脚本所在目录 | `$HOME/paper_search` |
| `AGENT_NEWS_DIR` | `agent-news` 脚本所在目录 | `$HOME/paper_search` |

> 这些变量名与物种无关——项目本身通用，不含任何特定物种的硬编码；实际用到哪套数据由你
> 通过上述变量（或 `config.json` 的 `env` 块）指定。技能里的**工具**路径则是运行时用
> `command -v` 从当前 conda 环境探测的，同样无需写死。

---

## 5. REPL 内命令

| 命令 | 作用 |
|---|---|
| `/help` | 命令列表 |
| `/exit`（或 `/quit`） | 退出 |
| `/clear` | 清空当前上下文（保留会话日志） |
| `/skills` | 列出发现的技能 |
| `/active` | 当前激活的 skill |
| `/drop <skill>` | 重置激活的 skill |
| `/model <id>` | 切换模型 |
| `/permission ask\|auto` | 切换权限模式 |
| `/cost` | 本轮用量（模型轮次 / 输出 token / 累计延迟） |

每轮对话自动归档到 `~/.skill-agent/sessions/<时间戳>.jsonl`（含全部工具调用，可回溯）。
开启 `--persist` 后另有逐条消息的 checkpoint 落盘 `~/.skill-agent/contexts/<sid>.jsonl`。

---

## 6. Web 前端

```bash
cd <repo>
./start_web.sh                       # 后台启动；日志 web.log；探活 /api/config
./start_web.sh 9000                  # 指定端口（host 仍 127.0.0.1）
./start_web.sh 0.0.0.0:8787          # 局域网可访问（谨慎）
./start_web.sh restart               # 重启（改了 config.json / 代码后必须用它重载）
./start_web.sh stop                  # 停止
# 或前台：python3 run_web.py
```

浏览器打开 `http://127.0.0.1:8787`。**单文件前端（无构建、无 node）**，REST + SSE 流式：

- 聊天气泡、实时工具调用卡片（Skill/Bash/Read/Write…）、技能侧栏（点技能预填指令）、模型/权限切换。
- **快捷提问 chip**：输入框上方 4 个预设（列技能 / 说明 GWAS 流程 / 检索参数位置 / 当前会话用量），点一下即发送。
- **实时用量徽标**：顶部随流式刷新本轮模型轮次与输出 token（接 M1a `usage` 事件）。
- **停止按钮**：运行中「发送」按钮变「停止」，点按 abort 本次流并 `POST /api/cancel`，协作式终止当前轮次
  （拒绝所有待确认命令；已起的后台任务不被杀）。
- **导出对话**：一键把消息与工具卡片导出为 Markdown 文件下载。
- **命令审批弹窗**：`权限=危险命令询问`（默认，仅危险命令弹）或 `每条命令询问`（== CLI 的 `ask`）；
  点「允许/拒绝」回复 `/api/confirm`，未答复即断开会自动拒绝并终止任务。
- **默认只绑 `127.0.0.1`**。远程使用走 SSH 端口转发：
  `ssh -L 8787:127.0.0.1:8787 <server>`，再在本机浏览器打开。
- **Web 默认开启 M13 持久化**（断线可按 sid 恢复）；`SKILL_AGENT_PERSIST=0` 可关。

`run_web.py` 直接参数：`--host` `--port` `--project` `--cwd` `-m` `--permission` `--debug`。
后端 API：`GET /api/config` `/api/usage` `/api/skills`；`POST /api/chat`(SSE) `/api/clear` `/api/confirm` `/api/cancel`。

---

## 7. 可选能力（harness 模块）

**全部默认关，不开启时行为与改造前逐字节一致**——这是刻意设计（opt-in / fail-soft）。

| 模块 | 能力 | 开启方式 |
|---|---|---|
| M1a 用量采集 | 累计 token/延迟，`usage` 事件，`/cost`，`GET /api/usage` | 恒开（纯被动） |
| M1b 预算熔断 | 轮次/时长/输出 token 上限，发请求前熔断 | `--max-turns/--max-seconds/--max-output-tokens` |
| M2 终止契约 | `stop_reason` 分类→决策（含截断修复/pause_turn） | 恒开 |
| M3 死循环熔断 | 无进展循环检测（动作/观测指纹 + 进展信号） | `--loop-guard` |
| M4 工具注册表 | 运行时挂载声明式插件工具 | `--plugin-dir DIR` |
| M6 输入校验 | executor 前按 `input_schema` 校验 required/type/enum | 恒开（宽容优先） |
| M7 工具治理 | per-tool 看门狗超时；超长结果落盘；同轮只读并行 | `--spill` / `--parallel-tools`（超时恒开） |
| M8a 权限模型 | allow/deny/ask 规则表、路径边界、写审批 | `--restrict-paths` / `--write-approval` |
| M9 沙箱 | Bash 执行后端三档 | `--sandbox host\|rlimit\|docker` |
| M10 钩子 | PreToolUse（阻断/改参）、PostToolUse（脱敏） | 代码注入 `HookManager`；`--trace` 用示例钩子 |
| M11 幂等去重 | 同一次运行内有副作用工具只执行一次 | `--idempotency`（模式 `reuse`/`deny`） |
| M12 摘要压缩 | 命中阈值时保留最近 K 条、更早压成结构化前情摘要 | `--summary-compact` |
| M12 分层记忆 | core.md（稳定前缀）+ archival（SQLite FTS5 检索） | `--memory` |
| M13 会话持久化 | 逐条消息落盘 + resume（修复悬挂 tool_use） | CLI `--persist`；Web 默认开 |
| #6 可观测性 | 工具调用 trace jsonl | `--trace` |
| #9 规划/todo | `TodoWrite` 工具，计划外部化 | `--todo` |
| #11 检索 | `Search` / `RepoMap`（FTS5） | `--retrieval` |
| #12 模型降级 | 过载后切后备模型 | `--fallback-models M1,M2` |
| #14 prompt cache | 稳定前缀打 `cache_control` | `--prompt-cache` |

### 7.1 M1b 预算熔断

```bash
python3 -m skill_agent chat --max-turns 120 --max-seconds 3600 --max-output-tokens 400000
```

到点后只停止**后续模型调用**，已起的后台任务照常运行（配合 `.done/.pid` 轮询）。

### 7.2 M3 死循环熔断

```bash
python3 -m skill_agent chat --loop-guard
```

无进展循环（动作/观测指纹 + `ToolResult.meta` 进展信号）达到阈值即熔断，轮询类工具白名单豁免。

### 7.3 M4 插件工具

目录内放 `*.json` 规格即可挂载新工具（不必改源码）：

```json
{"name":"Hello","description":"示例","input_schema":{"type":"object","properties":{"who":{"type":"string"}}},
 "path":"/abs/hello.py","executor":"make_executor"}
```

`hello.py` 里定义 `make_executor()` 返回 `callable(args)->ToolResult`。

```bash
python3 -m skill_agent chat --plugin-dir /path/to/plugins
```

> 动态工具与内置工具**同权限执行**，故默认禁用，须显式开启。

### 7.4 M7 工具治理

- per-tool 看门狗超时：默认 `Read/Write/Edit 30s`、`Glob 60s`、`Grep 90s`、`Search/RepoMap 60s`、其余 120s。
- `--spill`：超长结果落盘转存，返回可继续读取的引用/分页，而不是粗暴截断。
- `--parallel-tools`：同轮多个**只读**工具（Read/Glob/Grep/TaskOutput/Search/RepoMap）并行，副作用工具串行。

### 7.5 M8a 权限模型

```bash
python3 -m skill_agent chat --restrict-paths --write-approval
```

- `--restrict-paths`：`Read/Write/Edit/Glob/Grep/TaskOutput/RepoMap` 全经 `_resolve` 校验归属，拒绝 `..` 越界与工作区外绝对路径。
- `--write-approval`：Write/Edit 纳入审批门（默认只覆盖 Bash）。
- 规则表可经 `Config.permission_rules` 传入，例如
  `[{"tool":"Bash","match":"rm\\s+-rf","action":"deny"}]`（`match` 支持正则或 glob，`action` ∈ allow/deny/ask，按序首次命中）。

### 7.6 M9 沙箱

`Bash`（含 `background=true`）的执行后端可切换，默认 `host`（与改造前逐字节一致）：

| 档位 | 依赖 | 隔离 | 说明 |
|---|---|---|---|
| `host`（默认） | 无 | 无 | 直接 `subprocess` |
| `rlimit` | stdlib `resource` | 弱 | 限 CPU/地址空间；后台用内联 `ulimit` |
| `docker` | docker（**系统依赖**，非 pip） | 强 | `--network none`、`--memory/--cpus`、工作目录同路径挂载 |

```bash
python3 -m skill_agent chat --sandbox rlimit
SKILL_AGENT_SANDBOX=docker \
SKILL_AGENT_SANDBOX_SPEC='{"image":"python:3.11-slim","memory":"8g","cpus":"4"}' \
  python3 -m skill_agent chat
```

- **fail-soft**：档位不可用（未装 docker / 未给镜像）时**回退宿主并打一行提示**；`SKILL_AGENT_SANDBOX_STRICT=1` 改为 fail-closed。
- **docker 后台任务**采用「宿主监督 pid」：先写宿主 pid 再 `exec docker run --rm`，容器生命周期 = 该 pid。
  **须把工作目录与编排 `RUN_DIR` 以相同宿主路径挂载**，否则编排脚本的 `.done` 落在容器内、宿主不可见。

### 7.7 M11 幂等去重

```bash
python3 -m skill_agent chat --idempotency                 # reuse：复用上次结果
SKILL_AGENT_IDEMPOTENCY_MODE=deny python3 -m skill_agent chat --idempotency  # deny：拒绝重放
```

只作用**有副作用**工具（Bash/Write/Edit）；同一次运行内相同「工具+规范化参数」只执行一次，防重复起后台/重复写。
只缓存**成功**结果（失败可重试）；每轮运行开始重置。

### 7.8 M12 上下文与记忆

```bash
python3 -m skill_agent chat --summary-compact        # 摘要式压缩
python3 -m skill_agent chat --memory                 # 分层记忆
```

- **摘要压缩**：命中阈值时保留最近 K 条原样，更早区间压成结构化前情摘要，**强制保留管线关键字段**（阶段号/参数、输入输出路径、后台 `log+pid+.done`、审批结论）。压缩失败自动回退「丢旧消息」。
- **记忆**：core.md 位于 `<project>/.skill-agent/memory/core.md`（可手工编辑，注入 system 稳定前缀）；archival 事实存同目录 `archival.db`（FTS5，按最新提问检索 top-k 注入消息尾部）。

### 7.9 M13 会话持久化 / resume

```bash
python3 -m skill_agent chat --persist                          # 落盘，开始打印会话 id
python3 -m skill_agent chat --continue                         # 恢复最近一次
python3 -m skill_agent chat --resume 20261014T101530-a1b2      # 按 id 恢复
```

落盘位置 `~/.skill-agent/contexts/<sid>.jsonl`。恢复会校验：若崩溃发生在「assistant 已落盘、tool_result 未落盘」之间，
会剔除悬挂 `tool_use` 并回退到最后一个可安全续跑的边界（避免 Messages API 因配对不符 400）。

### 7.10 #9 规划/todo

```bash
python3 -m skill_agent chat --todo
```

挂载 `TodoWrite` 工具，模型可把计划外部化为结构化清单（单一 `in_progress`），长任务可检查。

### 7.11 #11 检索（Search / RepoMap）

```bash
python3 -m skill_agent chat --retrieval                # 挂载 Search/RepoMap
python3 -m skill_agent chat --retrieval --index-rebuild # 强制重建索引
```

- **`Search`**：FTS5/BM25 相关性检索，返回 `文件:起止行 + 片段`（trigram 分词，中文友好；短词/无命中走 LIKE 兜底），模型随后 `Read(offset,limit)` 精读。支持 `limit`（默认 8，≤30）、`path_glob`、`kind` 过滤。
- **`RepoMap`**：给目录/文件的结构化大纲（Python 类/函数/方法带行号、Markdown 标题），用于快速理解陌生文件构成。
- 索引落 `<project>/.skill-agent/index/code.db`，**首次用时惰性构建**，之后按 `(mtime,size)` 增量刷新。
- 语料**只取显式根**：项目自带 `skills/**`（含 references）与源码 `skill_agent/**`、`webapp/**`、`docs/*.md`；**绝不整仓遍历**，就地剪枝 + 读前按大小/后缀拒绝（`skills/phenotype-extract/model` 下 ~263MB 权重不会被 stat）。
- **零新依赖**（纯 stdlib `sqlite3`+`ast`）；FTS5 缺失或目录不可写时自停用、不阻断。两个工具只读，`--parallel-tools` 下与 Read/Grep 并行。

### 7.12 #14 / #12 成本与容错

```bash
python3 -m skill_agent chat --prompt-cache --fallback-models DSv4-flash,claude-haiku-4-5
```

`--prompt-cache` 给 system 与工具定义末尾打 `cache_control: ephemeral`（是否真正命中取决于代理）；
`--fallback-models` 在过载重试耗尽后依次切换后备模型（默认空 = 不切换）。

### 7.13 #6 trace

```bash
python3 -m skill_agent chat --trace
```

工具调用 trace（工具名/参数摘要/耗时/结果）落盘 `~/.skill-agent/traces/`，用于回放与量化误判率。

### 7.14 M13 之外：安全与健壮性默认

- 危险命令（`rm -rf`、`DROP TABLE`、`dd`、`git push -f`…）命中即告警；`--permission ask` 全部询问。
- 长命令输出自动截断；历史超长按**消息边界**压缩（不拆散 tool_use/tool_result 配对）。
- 限流/过载指数退避重试；流式失败自动回退非流式。

---

## 8. 技能机制

（Agent 内建）

- **发现**：扫描 `<项目>/skills/<name>/SKILL.md`（优先）；无 `skills/` 时回退 `<项目>/.claude/skills`（legacy 兼容）或 `~/.claude/skills`；可用 `--skills-dir` 显式指定。
- **注入**：system prompt 只放技能**清单**（name + 用途）；模型按需调用 `Skill(name)` 工具，工具结果即返回该
  skill 的完整指令（含 usage-guide 与目录内文件清单），再据此执行——编排类（`wgs-expressive-pipeline` / `gwas-pipeline`）
  会继续指示调用子 skill。
- **白名单**：激活某 skill 后，下一轮模型可见工具 = 该 skill 的 `allowed-tools`（`Skill`/`TaskOutput` 保底）。
  遵守 frontmatter 的 `disable-model-invocation`。
- **阶段审批**：两个编排 skill 要求「生成脚本 → 用户确认 → 执行」，交互模式下 Agent 会展示脚本/命令等用户答复。

### 内置工具集

`Skill` · `Bash`（支持 `background=true` → nohup + 日志，配合管线的 `.done/.pid` 机制，用 `TaskOutput` 轮询）
· `Read` · `Write` · `Edit` · `Glob` · `Grep`（优先 rg，本机无 rg 时纯 Python 回退）· `TaskOutput`。
可选挂载：`TodoWrite`（`--todo`）、`Search`/`RepoMap`（`--retrieval`）、插件工具（`--plugin-dir`）。

### 自带技能（21 个）

- **WGS**：`wgs-expressive-pipeline`（编排） + `wgs-express-server-detect` / `-params-propose` / `-fastp-qc` / `-align-sort` / `-markdup-bqsr` / `-haplotypecaller` / `-joint-genotyping` / `-filtering`
- **GWAS**：`gwas-pipeline`（编排） + `gwas-format-convert` / `-qc-filter` / `-kinship-pca` / `-gemma` / `-mvp` / `-visualization` / `-ld-block`
- **表型/其他**：`phenotype-extract`、`fan-files`、`paper-search`、`agent-news`

---

## 9. 端到端示例

### 9.1 跑 WGS 全流程（长任务可靠性组合）

```bash
cd <repo>
python3 run.py chat --persist --max-turns 200 --loop-guard \
  -p "用 wgs-expressive-pipeline 跑这批样本"
```

编排 skill 会：探测服务器 → 提议参数 → 生成各阶段脚本 → **等你确认** → 用 `Bash background=true` 起后台 +
`TaskOutput` 轮询 `.done`。中途可 `Ctrl-C` 退出，之后 `--continue` 恢复。

### 9.2 只问流程、不跑数据

```bash
python3 run.py chat -p "用 gwas-pipeline skill 说明标准流程，先不要真正跑数据"
```

### 9.3 跨文件找参数（开启检索）

```bash
python3 run.py chat --retrieval \
  -p "用 Search 找 GWAS 各阶段参数与质控阈值写在哪里，给出行号"
python3 run.py chat --retrieval -p "用 RepoMap 列出 agent/skill_agent 的结构"
```

### 9.4 危险命令需逐条确认

```bash
python3 run.py chat --permission ask --restrict-paths --write-approval
```

### 9.5 强隔离沙箱 + 成本优化

```bash
SKILL_AGENT_SANDBOX_SPEC='{"image":"python:3.11-slim","memory":"8g","cpus":"4"}' \
  python3 run.py chat --sandbox docker --prompt-cache \
  --fallback-models DSv4-flash -p "在容器内跑 gwas-format-convert"
```

### 9.6 恢复上一次会话

```bash
python3 run.py chat --continue
python3 run.py chat --resume 20261014T101530-a1b2
```

---

## 10. 故障排查

| 现象 | 原因 / 处理 |
|---|---|
| `缺少 API 凭证` | `config.json` 无 `api_key` 且环境变量 `ANTHROPIC_AUTH_TOKEN` 未设（复制 `config.json.example` 填写） |
| 模型 503 / 无响应 | 代理侧 `claude-*` 常不可用；用 `-m DSv4-flash`，或用 `--fallback-models` 兜底 |
| `python3 -m skill_agent` 报 `No module named` | 需在 `agent/` 目录下运行，用 `run.py` 或 `cd agent` |
| 找不到技能 | `--project <含 skills/ 的根>`，或确认从项目内运行；`--skills-dir` 可显式指定 |
| `Search`/`RepoMap` 报「索引不可用」 | 目录不可写或本机缺 FTS5；`--index-dir` 换目录，或确认 `import sqlite3` FTS5 可用 |
| 检索结果陈旧 | 索引按 `(mtime,size)` 增量；`--index-rebuild` 强制重建 |
| docker 沙箱里编排 `.done` 找不到 | 工作目录与 `RUN_DIR` 必须以**相同宿主路径**挂载进容器 |
| Web 打不开 | 默认只绑 `127.0.0.1`；远程用 SSH 转发；查 `web.log` |
| **前端显示的模型/技能与 `config.json` 不一致，或无法对话** | 多半是**旧的后台进程**还在跑（改过配置/代码但没重启）。`./start_web.sh restart` 重载，再 `Ctrl-Shift-R` 硬刷新浏览器 |
| 审查 trace | `~/.skill-agent/traces/`（`--trace`）；会话日志 `~/.skill-agent/sessions/` |
| **部署到新机器/换机后跑不通** | 先跑 `bash environment/check_env.sh`，按「核心缺失/可选缺失」逐项对照 [`environment/README.md`](../environment/README.md)（conda 环境、独立二进制、GLIBCXX、bash 版本等） |

---

_文档随代码同仓维护于 `agent/docs/usage.md`；设计与模块状态见 `agent/docs/harness-gap-analysis.md`。_
