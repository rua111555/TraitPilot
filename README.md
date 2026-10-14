# SkillAgent — 可调用 Claude Code skills 的本机 Agent

一个**完整、可用**的单 Agent 系统，沿用 Claude Code 的技能模型，让你现有 `.claude/skills`
（WGS / GWAS 两大管线 + 表型提取等 18 个技能）能在**你自己的 Agent** 中被自主调用，
而无需依赖 Claude Code。

复用当前配置的 API，**自动从 Claude Code 的同一来源取凭证**（优先级：环境变量 →
`~/.claude/settings.json` 的 `env` 块），token 不写死、无需你重复配置：

```bash
cd /home/chenyuxi/skill/agent
python -m skill_agent          # 直接可用
```

> 若要改用普通环境变量方式，可在 shell 配置里（如 `~/.bashrc`）：
> ```bash
> export ANTHROPIC_BASE_URL="http://182.92.166.143:3200"        # 你 .bashrc 第49行已有
> export ANTHROPIC_AUTH_TOKEN="<你的token>"                     # 值可从 ~/.claude/settings.json 的 env 块复制
> ```

## 快速开始

```bash
cd /home/chenyuxi/skill

# 列出发现的 skills
python agent/run.py list

# 交互对话（REPL，推荐）
python agent/run.py chat            # 可选 --permission ask / -m claude-haiku-4-5

# 单轮运行（脚本化）
python agent/run.py chat -p "用 gwas-pipeline skill 说明标准流程，先不要真正跑数据"
python agent/run.py chat -p "列出 phenotype-extract 提供哪些表型" -m claude-haiku-4-5
```

## Web 前端

```bash
cd /home/chenyuxi/skill/agent
./start_web.sh                    # 推荐：后台启动/状态检查（log=web.log；停止 ./start_web.sh stop）
# 或前台式：python run_web.py     # 浏览器打开 http://127.0.0.1:8787
```

- **单文件前端**（无构建、无 node），REST + SSE 流式：聊天气泡、实时工具调用卡片
  （Skill/Bash/Read/Write…）、技能侧栏（点技能可预填调用指令）、模型/权限切换。
- **命令审批弹窗**：`权限=危险命令询问`（默认，危险命令才弹）或 `每条命令询问`（==CLI 的
  ask）；点「允许/拒绝」回复 `/api/confirm`，未答复即断开连接会自动拒绝并终止任务。
- **联网访问**：页面能执行本机命令，默认仅绑定 `127.0.0.1`。远程使用请 SSH 端口转发：
  `ssh -L 8787:127.0.0.1:8787 <server>`，然后本机浏览器打开 `http://127.0.0.1:8787`；
  非本也可用 `--host 0.0.0.0`（局域网内可访问，注意风险）。
- 其他参数：`--port` `--model` `--permission` `--project` `--cwd`。
- 网页后端复用同一 Agent 引擎，仅入口不同：`webapp/server.py`（Flask）+ `webapp/session.py`
  （事件桥 + 命令确认桥，把线程里的 agent 运行接到 SSE）。

## 常用参数

| 参数 | 说明 |
|------|------|
| `-m / --model` | 模型 id（默认 `claude-opus-4-5`） |
| `--project <dir>` | 含 `.claude/skills` 的项目根（默认自动向上探测） |
| `--cwd <dir>` | Bash 等工具的执行目录（默认=项目根） |
| `--permission auto\|ask` | `auto`（默认）：危险命令仅告警；`ask`：每个 Bash 先询问 |
| `--restrict-paths` | 启用工作区路径边界（越界拒绝；默认关）|
| `--write-approval` | 把 Write/Edit 纳入审批门（默认关）|
| `--no-stream` | 关闭流式输出 |
| `list --json` | 以 JSON 导出技能清单（含 allowed-tools / 是否可自动调用） |

REPL 内命令：`/help` `/clear` `/skills` `/active` `/model X` `/permission ask` `/cost` `/exit`。
每轮对话自动归档到 `~/.skill-agent/sessions/<时间戳>.jsonl`（含全部工具调用，可回溯）。

### 已落地的 harness 增补（见 `docs/harness-gap-analysis.md`）

| 模块 | 内容 | 开关 |
|------|------|------|
| M6 输入校验 | executor 前按 `input_schema` 校验 required/type/enum，畸形参数结构化报错 | 恒开（宽容优先） |
| M1a 用量采集 | 累计 token/延迟，emit `usage` 事件，`/cost` + `GET /api/usage` | 恒开（纯被动） |
| M3a 进展信号 | `ToolResult.meta` 填充 exit_code/size/mtime/pid_alive 等（只记不判） | 恒开 |
| M8a 权限模型 | allow/deny/ask 规则表、工作区路径边界、写审批 | 默认关，`--restrict-paths`/`--write-approval` 或 `SKILL_AGENT_RESTRICT_PATHS`/`SKILL_AGENT_WRITE_APPROVAL` 开启 |
| M12 摘要压缩 | 命中阈值时保留最近 K 条、更早区间压成结构化前情摘要（保留阶段号/参数/路径/pid/审批结论） | 默认关，`--summary-compact` 或 `SKILL_AGENT_SUMMARY_COMPACTION=1` |
| M12 分层记忆 | core.md（稳定前缀）+ archival（SQLite FTS5 检索，注入尾部） | 默认关，`--memory` 或 `SKILL_AGENT_MEMORY=1` |
| M13 会话持久化 | 逐条消息落盘（append-only jsonl），恢复时修复悬挂 tool_use 并截到安全边界 | CLI `--persist`；Web 默认开（`SKILL_AGENT_PERSIST=0` 关）|
| M2 终止契约 | `stop_reason` 分类→决策（continue/repair_truncation/pause_turn/refuse/finalize），透出终止原因 | 恒开（更严格，含截断修复）|
| M1b 预算熔断 | 轮次/墙钟时长/输出 token 上限，发请求前熔断，到点只停调用不杀后台 | 默认不限，`--max-turns`/`--max-seconds`/`--max-output-tokens` |
| M3 死循环熔断 | 无进展循环检测（动作/观测指纹 + meta 进展信号），轮询工具白名单 | 默认关，`--loop-guard` 或 `SKILL_AGENT_LOOP_GUARD=1` |
| M4 工具注册表 | 工具以数据登记、可运行时挂载；声明式插件目录（`*.json` 规格） | `--plugin-dir DIR`（默认不加载，防扩大执行面）|
| M10 钩子 | PreToolUse（可阻断/改参）/ PostToolUse（可脱敏）/ 会话事件钩子 | 代码注入 `HookManager`；`--trace` 用示例钩子落盘 |
| #6 可观测性 | 工具调用 trace（工具名/参数摘要/耗时/结果）jsonl | `--trace` 或 `SKILL_AGENT_HOOKS=1` |
| M7 工具治理 | per-tool 看门狗超时；超长结果落盘转存（可继续 Read）；同轮只读工具并行 | `--spill` / `--parallel-tools`（默认关）|
| M9 沙箱 | Bash 执行后端三档：宿主 / stdlib rlimit / docker 强隔离；缺依赖 fail-soft 回退宿主 | 默认 `host`；`--sandbox rlimit\|docker` 或 `SKILL_AGENT_SANDBOX` |
| M11 幂等去重 | 同一次运行内相同「有副作用」工具调用（Bash/Write/Edit）只执行一次，防重复起后台/重复写 | 默认关，`--idempotency` 或 `SKILL_AGENT_IDEMPOTENCY=1`（模式 `reuse`/`deny`）|
| #9 规划/todo | `TodoWrite` 工具把计划外部化为结构化清单（单一 in_progress），长任务可检查 | 默认关，`--todo` 或 `SKILL_AGENT_TODO=1` |
| #14 prompt cache | 给稳定前缀（system + 工具定义末尾）打 `cache_control: ephemeral` | 默认关，`--prompt-cache` 或 `SKILL_AGENT_PROMPT_CACHE=1` |
| #12 模型降级 | 过载重试耗尽后按列表依次切换后备模型 | 默认无，`--fallback-models M1,M2` 或 `SKILL_AGENT_FALLBACK_MODELS=M1,M2` |
| #11 检索 | `Search`（FTS5/BM25 相关性检索，返回 文件:行号+片段）+ `RepoMap`（类/函数/方法/标题大纲）；纯 stdlib，语料=skills 文档+agent 源码 | 默认关，`--retrieval` 或 `SKILL_AGENT_RETRIEVAL=1`；`--index-rebuild` 强制重建 |

### 扩展工具（M4）

在目录放一个 `*.json` 规格即可挂载新工具（不必改源码）：

```json
{"name":"Hello","description":"示例","input_schema":{"type":"object","properties":{"who":{"type":"string"}}},
 "path":"/abs/hello.py","executor":"make_executor"}
```

`hello.py` 里定义 `make_executor()` 返回 `callable(args)->ToolResult`。用
`--plugin-dir <dir>` 加载（默认禁用；动态工具与内置工具同权限执行，故须显式开启）。

### 会话恢复（M13）

```bash
python3 -m skill_agent chat --persist                 # 落盘，开始时会打印会话 id
python3 -m skill_agent chat --continue                # 恢复最近一次会话
python3 -m skill_agent chat --resume 20261014T101530-a1b2   # 按 id 恢复
```

落盘位置 `~/.skill-agent/contexts/<sid>.jsonl`。恢复会做校验：若崩溃发生在
「assistant 已落盘、tool_result 未落盘」之间，会剔除悬挂的 tool_use 并回退到最后一个
「可安全续跑」的 assistant 边界（避免 Messages API 因配对不符 400）；崩溃导致的未完成
轮次会被丢弃并在恢复时提示条数。

### 记忆（M12）

core.md 位于 `<project>/.skill-agent/memory/core.md`，可手工编辑，会被注入 system prompt
的稳定前缀（利于 prompt caching）；archival 事实存于同目录 `archival.db`，按最新提问检索
top-k 注入消息尾部。

M8a 规则表可通过 `Config.permission_rules` 传入，例如 `[{"tool":"Bash","match":"rm\\s+-rf","action":"deny"}]`（`match` 支持正则或 glob，`action` ∈ allow/deny/ask，按序首次命中）。

### 沙箱执行后端（M9）

`Bash`（含 `background=true`）的执行后端可切换，默认 `host`（与改造前逐字节一致）：

| 档位 | 依赖 | 隔离 | 说明 |
|---|---|---|---|
| `host`（默认） | 无 | 无 | 直接 `subprocess` |
| `rlimit` | stdlib `resource` | 弱 | `preexec_fn` 限 CPU/地址空间；后台用内联 `ulimit` |
| `docker` | docker（**系统依赖**，非 pip） | 强 | `--network none`、`--memory/--cpus`、工作目录同路径挂载 |

```bash
python3 -m skill_agent chat --sandbox rlimit
SKILL_AGENT_SANDBOX=docker \
SKILL_AGENT_SANDBOX_SPEC='{"image":"python:3.11-slim","memory":"8g","cpus":"4"}' \
  python3 -m skill_agent chat
```

- **fail-soft**：所选档位不可用（未装 docker / 未给镜像）时**回退宿主并打一行提示**，绝不阻断任务；
  `SKILL_AGENT_SANDBOX_STRICT=1` 可改为 fail-closed（抛错）。
- **docker 后台任务**采用「宿主监督 pid」：后台脚本先写宿主 pid 再 `exec docker run --rm`，
  容器生命周期 = 该 pid 生命周期，故 `TaskOutput` 判活无需改动。**须把工作目录与编排 `RUN_DIR`
  以相同宿主路径挂载**，否则编排脚本的 `.done` 落在容器内、宿主不可见（自动推进/`--resume` 依赖它）。
- 只拦截 Bash 的两个进程启动点，不改动 `ToolSet.execute` 的权限/钩子治理顺序。

### 检索（#11）

`Search` / `RepoMap` 两个工具，让模型在「这个流程/参数/函数在哪里」这类跨文件问题上先检索再 `Read` 精读：

```bash
python3 -m skill_agent chat --retrieval
python3 -m skill_agent chat --retrieval --index-rebuild     # 强制重建索引
SKILL_AGENT_RETRIEVAL=1 python3 -m skill_agent chat          # 环境变量方式
```

- 索引落在 `<project>/.skill-agent/index/code.db`（FTS5 + `tokenize='trigram'`，中文友好），默认**首次用时惰性构建**，
  之后按 `(mtime,size)` 增量刷新——`Write/Edit` 后再 `Search` 只重切改动的文件。
- 语料**只取显式根**：`.claude/skills/**`（含 references 的 .md/.py/.sh）与 `agent/**/*.py` + `agent/docs/*.md`。
  **绝不整仓遍历**：就地剪枝 `__pycache__/.git/.skill-agent/saved…`，读前按大小（>2MB）与后缀（`.pth/.png/.tfevents…`）拒绝，
  故仓库里的 ML 权重（本机 ~24.5GB）不会被 stat。
- **零新依赖**：纯 stdlib（`sqlite3` + `ast`）；不引入 tree-sitter / ripgrep / 向量库。FTS5 缺失或目录不可写时自停用、不阻断。
- 只读工具，`--parallel-tools` 开启时与 Read/Grep 一同并行执行。

## 架构

```
agent/
├── run.py                  # CLI 入口（等效 python -m skill_agent）
├── run_web.py              # Web 入口：python run_web.py → http://127.0.0.1:8787
├── requirements.txt        # requests + PyYAML + flask（本机已预装）
├── skill_agent/
│   ├── config.py           # 凭证：环境变量 → ~/.claude/settings.json 的 env（与 Claude Code 同源）
│   ├── skills.py           # SkillRegistry：扫描 .claude/skills、解析 SKILL.md frontmatter
│   ├── tools.py            # 工具执行器 + 权限门 + 后台任务（Bash background=true）
│   ├── sandbox.py          # M9：Bash 执行后端（host/rlimit/docker），纯 stdlib、默认关
│   ├── idempotency.py      # M11：工具幂等去重（有副作用工具、run 级、默认关）
│   ├── todo.py             # #9：计划外部化（TodoWrite，默认关）
│   ├── retrieval.py        # #11：FTS5 检索（Search/RepoMap），纯 stdlib、默认关
│   ├── client.py           # Anthropic Messages API 客户端（SSE 流式 + 重试回退）
│   ├── agent.py            # agentic 循环：tool_use → 执行 → 续推；context 压缩；事件/取消钩子
│   └── cli.py              # list / chat / 会话日志
└── webapp/
    ├── server.py           # Flask：/api/chat(SSE) /confirm /skills /models /config + 静态页
    ├── session.py          # WebSession：事件桥 + 命令确认桥（线程 ↔ SSE）
    └── static/index.html   # 单文件前端（无构建、纯浏览器 JS，含轻量 Markdown 渲染）
```

### 与 Claude Code 相同的技能机制

- **发现**：扫描 `<项目>/.claude/skills/<name>/SKILL.md`（兼容 `~/.claude/skills` 下裸 `.md`）。
- **注入**：system prompt 只放技能**清单**（name + 用途）；模型按需调用 `Skill(name)` 工具，
  工具结果即返回该 skill 的完整指令（含 usage-guide 与目录内文件清单），再据此执行——
  编排类（`wgs-expressive-pipeline` / `gwas-pipeline`）会继续指示调用子 skill。
- **白名单**：激活某 skill 后，下一轮模型可见工具 = 该 skill 的 `allowed-tools`（`Skill`/
  `TaskOutput` 保底）。遵守 frontmatter 的 `disable-model-invocation`（自动可调仅 ✓ 者）。
- **阶段审批**：两个编排 skill 要求“生成脚本 → 用户确认 → 执行”，交互模式下 Agent 会把
  脚本/命令展示出来等用户答复。

### 工具集

`Skill` · `Bash`（支持 `background=true` → nohup + 日志，配合管线的 `.done/.pid` 机制）
· `Read` · `Write` · `Edit` · `Glob` · `Grep`（优先 rg）· `TaskOutput`（读后台日志）。

## 安全与健壮性

- 危险命令（`rm -rf`、`DROP TABLE`、`dd`、`git push -f`…）命中即告警；`--permission ask` 全部询问。
- 长命令输出自动截断；历史超长按**消息边界**压缩（不拆散 tool_use/tool_result 配对）。
- 限流/过载指数退避重试；流式失败自动回退非流式。
- 模型默认 `claude-opus-4-5`，可用 `claude-haiku-4-5`/`claude-sonnet-4-5` 等。

## 说明

- 本 Agent 不强依赖网络外服务，工具全部在本机执行；表型提取等需要 `LD_LIBRARY_PATH` 的环境
  在 skill 指令中已写明，模型会按其执行。
- 需要更多工具（如 WebSearch）时可按 `tools.py` 中 `Tool` 的写法扩展。
