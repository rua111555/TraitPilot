 SkillAgent 现状调研与标准 Agent Harness 组件增补方案

> 本文仅做调研与方案设计，不含任何实现代码。全文出现的文件名、目录名、接口名、协议名均为设计标识，不构成实现。

---

## 1. 现状概览

当前 `/home/chenyuxi/skill/agent` 是一个**自研、单 Agent、纯 stdlib 依赖（requests / PyYAML / flask）**的本机编程与生信分析智能体：它以 Anthropic Messages API 的 `tool_use` 循环为核心，复用 Claude Code 的 skill 机制（扫描 `.claude/skills`、注入 skill 清单、按需 `Skill(name)` 加载），具备 CLI（REPL / 单轮）与 Flask+SSE Web 双入口，工具集覆盖 `Skill/Bash/Read/Write/Edit/Glob/Grep/TaskOutput`，并为 WGS/GWAS 两个编排管线内建了 `Bash background=true`（nohup + `.done/.pid`）后台机制。整体约 1700 行 Python，功能自洽、可跑通，但**作为“标准 agent harness”尚缺关键治理与健壮性组件**：终止原因只有字符串白名单、无预算/成本核算、无死循环熔断、无结构化权限模型、无沙箱、历史压缩为“丢弃旧消息”、history 仅内存无持久化、无 MCP/子 agent/规划/检索/多 provider 层。

### 现有组件表

| 组件 | 文件 | 现状要点 |
|------|------|----------|
| 配置 | `skill_agent/config.py` | `Config` dataclass；凭证三源（env → `~/.claude/settings.json`）；**无预算/权限模式字段** |
| 技能注册表 | `skill_agent/skills.py` | `SkillRegistry` 扫描 `.claude/skills`，解析 frontmatter（`allowed-tools` / `disable-model-invocation`） |
| API 客户端 | `skill_agent/client.py` | 纯 `requests`；SSE 流式 + 非流式回退；指数退避重试；**仅解析 `output_tokens`，tool_use 解析失败静默 `pass`** |
| 工具层 | `skill_agent/tools.py` | 8 个工具硬编码在 `ToolSet.__init__`；正则黑名单（11 条）仅覆盖 Bash；`permission=auto|ask`；`_resolve` 无工作区边界；串行执行 |
| 主循环 | `skill_agent/agent.py` | `while` + `MAX_TURNS=60` 硬编码；`_maybe_compact` 丢弃旧消息；`stop_reason` 白名单一行判断（`tool_use/max_tokens/""`） |
| CLI | `skill_agent/cli.py` | `list`/`chat`（REPL + `-p`）；`_finish_log` 写 jsonl（收尾 `end:true`）；REPL 命令 `/help /clear /skills /active /model /permission` |
| Web 会话 | `webapp/session.py` | `WebSession`：事件队列 + 命令确认桥（线程 ↔ SSE）；`done` 事件含 `final/messages/active` |
| Web 服务 | `webapp/server.py` | Flask；`/api/chat`(SSE)/`/api/confirm`/`/api/clear`/`/api/skills`/`/api/config`；`MAX_STREAM_WAIT=3600` |
| Web 前端 | `webapp/static/index.html` | 单文件、无构建；聊天气泡、工具卡片、技能侧栏、确认弹窗、SSE 事件渲染 |

---

## 2. 与开源 harness 对照：缺失的标准组件清单

对照标准 agent harness 的 16 项组件（核心循环/终止、预算、工具注册、MCP、权限、沙箱、人机中断、事件流、hook、上下文压缩、持久化、委派、规划、检索、多 provider、cache 等），逐项核对如下：

| # | 标准组件 | 现状 | 借鉴项目 | 影响 |
|---|----------|------|----------|------|
| 1 | 核心循环 + 终止契约 | 一行 `stop_reason` 白名单；无结构化终止原因 | Anthropic Messages API 规范 / Claude Code / smolagents | 停不下来、停得不明不白；`pause_turn`/截断/拒绝分支缺失 |
| 2 | 预算与 token/成本核算 | 无；仅一个 `MAX_TURNS` 常量 | SWE-agent（cost limit）/ OpenHands（max_iterations+runtime 时长）/ smolagents（max_steps）/ Claude Code（--max-turns） | 长管线可能静默烧钱；无累计 usage |
| 3 | 死循环/无进展熔断 | 仅“声明 tool_use 但无块则 break” | OpenHands `stuck.py` StuckDetector / SWE-agent（连续错误计数）/ Claude Code 提示 | 空转直到硬上限，不可解释 |
| 4 | 上下文摘要式压缩 + 分层记忆 | `_maybe_compact` 直接**丢弃旧消息** | Claude Code auto-compact / OpenHands SummarizingCondenser / Letta-MemGPT / Mem0 | 长管线丢失阶段参数/路径/审批结论 → 致命误操作 |
| 5 | 会话持久化 + checkpoint/resume | history **仅内存**，jsonl 仅事后归档 | LangGraph（checkpointer）/ OpenHands（事件流 replay）/ Claude Code（--resume）/ Temporal（durable replay） | 进程重启即丢全部上下文 |
| 6 | 可观测性（真 trace/span） | `on_event` 是轻量事件，非 trace | OpenTelemetry 式 trace / LangSmith / Langfuse | 无法回放、无法量化误判率 |
| 7 | Evals / 评测 | 有单测无 evals | smolagents / SWE-agent eval harness | 无法量化 loop guard 误判率 |
| 8 | 子 agent / 委派 | 无 | Claude Code 子 agent / OpenHands delegator / AutoGen | 单上下文串行，无隔离 |
| 9 | 规划 / todo | 无（仅 system prompt 文字建议） | Claude Code TodoWrite / OpenHands planning | 长任务无外部化的计划态 |
| 10 | 工具注册表 + 动态发现 | `ToolSet.__init__` **硬编码 8 工具字面量** | smolagents（@tool 注册）/ OpenHands（装饰器注册）/ Claude Code（内置+MCP 注册表）/ Continue.dev | 加工具须改源码重启 |
| 11 | 检索 / repomap / RAG | 无 | Aider repomap（tree-sitter）/ 向量库 RAG | 大代码库无结构感知 |
| 12 | 模型路由 / 失败降级 | 重试后不切模型 | OpenRouter / litellm / Claude Code 模型降级 | 过载时只会重试同模型 |
| 13 | 多 provider 抽象 | `client.py` 硬编码 Anthropic | litellm / LangChain model 层 | 锁死单一后端，是所有 provider 改动的地基 |
| 14 | Prompt cache_control | **从不发送** cache_control | Anthropic prompt caching 规范 | 成本与延迟未优化 |
| 15 | 输出/结果校验 | 只校验输入，不校验输出 | smolagents（结构化报错）/ OpenHands（pydantic） | 畸形工具参数落入 executor 产生误导异常 |
| 16 | 权限模型 + 沙箱 + hook | 字符串黑名单 + 仅 Bash `ask`；无沙箱；无 hook | Claude Code（permissions/modes/hooks）/ Codex CLI / OpenHands（SecurityAnalyzer）/ E2B/gVisor/bubblewrap | Bash 直连宿主，权限门是唯一防线 |

**结论**：方案对第 1/2/3/10/16（及人机中断、事件流、hook）覆盖良好；但作为“标准 harness”仍缺上下文与记忆（#4）、持久化（#5）、委派（#8）、规划（#9）、检索（#11）这半壁能力，另有 #6/#7/#12/#13/#14/#15 六项可低成本补齐。其中应判为**架构风险**的缺失项为 #4（改 history 压缩契约与配对不变量）、#5（消息级序列化 + 恢复校验 + 幂等联动）、#8（子 agent 上下文隔离→内层预算/权限/loop 边界）、#13（重构 `client.create` 核心请求路径）。

---

## 3. 模块增补方案

### M1 RunBudget — 多维度预算与 token/成本核算

- **目标**：统一收敛现有三个隐式上限（`MAX_TURNS` 常量、SSE `MAX_STREAM_WAIT`、Bash 自身上限），补齐 token 采集与成本核算，提供一处主动终止判定点与可查询落盘。
- **借鉴**：SWE-agent per-instance cost limit（每步累加 token×单价，超限以 `cost_limit` 带原因停止并落库）；OpenHands AgentController 同时持 `max_iterations` 与会话运行时长，终止携带结构化原因；smolagents `max_steps` 构造参数 + 专用 `AgentMaxStepsError`（而非静默 break）；Claude Code `--max-turns` + 累计 usage 与 context 占用提示（区分累计花费与当前窗口占用）。
- **要做什么**：新增横切但**被动**的预算对象——`TokenBudget`（每次 run 独立实例化，绝不挂共享 Config/ToolSet）+ `CostAccountant`（落盘查询）。采集 input/output/cache_read/cache_creation token、model、step、stop_reason；`BudgetLimits`（max_input/output/total_tokens、max_cost_usd、warn_ratio、price_table）**全部可选，未设即不限制**。提供 tick_step / exceeded / snapshot / status(OK|WARN|EXCEEDED) / check_before_call / remaining_for_tool。定价纯配置，未知模型降级为只计 token 不报错。原则：只做“被动累加器 + 一处主动判定点 + 落盘查询”，不碰工具执行/权限/压缩。
- **数据流**：现状每步 `client.create` 后丢弃 out_tokens。改造后 `client.create` 返回完整 `UsageRecord`（流式从 `message_start` 合并 input/cache token，与 `message_delta.output_tokens` 合并；非流式取 `data.usage`）→ 每步 `budget.add(record)` + emit usage 事件 → 下次请求前 `check_before_call` 超限带原因终止。`answer()` 开头 `budget.start()`；Web `done`/`GET /api/usage` 暴露 snapshot；CLI 新增 `/cost`；落盘 `~/.skill-agent/usage/<session>.json`。工具层经 `remaining_for_tool` 收窄单次 Bash timeout。
- **新增文件**：`skill_agent/budget.py`、`skill_agent/pricing.py`、`skill_agent/usage_store.py`、`tests/test_budget.py`
- **修改文件**：`skill_agent/client.py`、`agent.py`、`config.py`、`tools.py`、`cli.py`、`webapp/session.py`、`server.py`、`static/index.html`、`README.md`
- **change_type**：**architectural_risk**
- **风险与缓解**：核心部分改动主循环终止契约（`_run` 返回 `(text, stop_reason)`、发请求前预判超限）与工具超时路径。高风险点：(a) `client.create` 返回签名变更——**追加 usage 而非打乱三元组位置**以降回归；(b) 流式 usage 合并对缺 cache/input token 的代理必须缺省记 0 降级，否则静默算错成本；(c) **共享 Config 陷阱**——`CFG_REF['config']` 为多会话共享，预算运行态写进 cfg 会跨 Web 会话串扰（`set_params` 已改 `cfg.model`，是既有隐患）；(d) 熔断必须发生在“发请求前或完整一轮后”，不得在 tool 执行中途硬切，否则产生半截 `tool_use` 无 `tool_result` 的非法序列。缓解：limits 全可选默认不设 → 老流程零回归（这是压在 architectural_risk 的关键）；新增文件可独立删除回退。
- **优先级**：P0｜**依赖**：`client.py` 先暴露 input/cache token；`config.py` 提供预算默认值与 env 装载。

### M2 StopController — stop_reason 分类、终止决策与终止原因透出

- **目标**：把“一行 `if` 判断 stop_reason”升级为“分类 → 决策 → 守卫”三层，并把终止原因结构化透出（termination surface）。
- **借鉴**：Anthropic Messages API 规范（`stop_reason` 权威枚举 `end_turn/tool_use/max_tokens/stop_sequence/pause_turn/refusal`；`pause_turn` “原样回传 assistant 内容续推”语义；`tool_use` 与 `tool_result` 强制配对）；Claude Code（按 stop_reason 分支收尾/续推；对终止型 stop_reason 夹带 tool_use 按协议异常处理）；smolagents（解析失败作为 error observation 回灌模型而非拿残参执行）。
- **要做什么**：新增纯函数模块 `stops.py`：`StopReason` 枚举 + `StopDecision{action ∈ continue_tools|finalize|resume_server_tool|refuse|repair_truncation|abort, reason_code, user_message, truncated, warn}`；唯一入口 `classify(stop_reason, blocks)`。规则：(1) 存在完整 tool_use（无论 stop_reason）→ continue_tools，end_turn 带工具额外 `warn=protocol_anomaly`；(2) `tool_use` 但全部 `_parse_error` → repair_truncation；(3) `max_tokens`+截断工具 → repair_truncation，`max_tokens`+纯文本 → finalize 且 `truncated=True`；(4) `pause_turn` → resume_server_tool；(5) `refusal` → refuse；(6) `end_turn/stop_sequence` 无工具 → finalize；(7) 未知 → finalize+warn。`make_truncation_result()` 为截断工具生成 `is_error` 的配对 tool_result（维持配对不变量）。
- **数据流**：client 层 `content_block_stop` 解析失败不再静默 pass，标 `blk['_parse_error']/_raw`（非流式路径同校验）。`_run` 每轮拿 blocks 先交 `classify` 得 `StopDecision`，据此分派（执行工具/收尾/续推/拒绝/修截断），替代现有 stop_reason 白名单。termination surface：notice 事件经既有 `on_event→session.event_queue→SSE→index.html` 提示条下发；`session.run` 的 done 加 `stop_reason`；`cli._finish_log` 写 `stop_reason`。history 结构不变，唯一新增是畸形工具的合成 tool_result。
- **新增文件**：`skill_agent/stops.py`、`tests/test_stops.py`
- **修改文件**：`client.py`、`agent.py`、`cli.py`、`webapp/session.py`、`static/index.html`、`README.md`
- **change_type**：**architectural_risk**
- **风险与缓解**：直接重写主循环终止契约，并触碰 history 的 tool_use/tool_result 配对不变量。最高风险=配对不变量：end_turn 带工具、max_tokens 截断工具、pause_turn 三种新分支都必须保证每个 tool_use 在下一条 user 消息有且仅有对应 tool_result，任何漏配/多配会让后续所有请求 400；需复查 `_safe_cut` 边界在合成 tool_result 后仍成立。次风险：end_turn 夹带工具选择“照常执行”会扩大执行面（当前无沙箱），建议该异常分支对 Bash **强制 ask** 并 emit warning；pause_turn 严格原样回传、不追加 user；预算计数须限定“每次成功 create 计一次”。缓解：先把 `classify()` 做成纯函数并加 `tests/test_stops.py` 覆盖六种 stop_reason×有无工具×是否截断组合；pause_turn/repair_truncation 首期只发 warning 不改控制流，分两阶段落地。
- **优先级**：P0｜**依赖**：`client.py` 暴露 tool_use 解析失败标记；`agent.py:_run` 重构为决策分派；M1 的 usage 返回以支持 truncated 判定。

### M3 LoopGuard — 死循环/重复动作 + 工具调用无进展（stuck）检测与熔断

- **目标**：新增纯内存、线程内自持、可单测的熔断器，职责“观测 + 判定 + 兜底终止”，不改工具执行器与权限路径。
- **借鉴**：主借鉴 OpenHands `controller/stuck.py` 的 StuckDetector（已核对真实存在）——滑动窗口模式匹配：scenario1 连续“同 action + 同 observation”逐字重复；scenario2 连续同 action 且 observation 均为 ErrorObservation；scenario3 monologue（连续 ≥3 条相同 agent MessageAction 且其间无 Observation）；scenario4 最近 6 步 (A1,O1)(A2,O2)(A1,O1)… 交替；`_eq_no_pid` 做忽略 pid/命令 id 的等价比较；命中即终止并暴露独立 STUCK 状态。次级借鉴 SWE-agent 的“连续同类错误/超时计数 → 达阈值 exit”三段式（计数器 + 升级 + 熔断）。
- **纠偏说明**：原设计归给 OpenHands 的“先干预、后熔断两段式”与归给 SWE-agent 的“动作指纹去重/重复告警注入”**均不实**——OpenHands 命中即 `AgentStuckInLoopError` 直接终止、无纠偏注入；SWE-agent 无动作指纹机制。已按真实机制改写；AutoGPT“连续相同命令计数中止”缺乏可靠开源对应，删除。
- **要做什么**：指纹采用三元组（动作指纹, 观测指纹, 状态指纹）。动作指纹 = 排序后 `(tool_name, 规范化 input)`；观测指纹 = 对 tool_result 内容归一化后取 hash（剥离行号/时间戳/进度/pid/耗时）；状态指纹 = 本步是否产生真实副作用（文件 mtime/size 变化、后台新进程、日志新增行、退出码改善、Skill 首次激活）。分级 ok/warn/escalate/halt；progress-aware 判据以“观测摘要变化”而非“调用签名相同”为主，`TaskOutput/*.log/.done/.pid` 进 polling 白名单（只降级记录不熔断）。阈值来自 Config（loop_guard 默认开、warn 3、halt 6、window 12）。与 `MAX_TURNS` 互补。
- **数据流**：`_execute_tools` 两阶段：执行前 check（已 halt 则跳过执行、产 is_error 的“已熔断”tool_result），执行后 `record(name,args,tr,tr.meta)`；warn 时把告警前缀**拼进该条 tool_result.content**（随 history 回传模型自纠；禁用独立追加 user 消息）。`_execute_tools` 回传 `should_halt`；`_run` 在追加 tool_result 前检查，为真则 break 并返回 guard 专属终止文案。`ToolResult` 增可选 meta 字段承载进展信号。事件 `{type:loop_guard, level, name, count}` 经既有 SSE 通道下发。
- **新增文件**：`skill_agent/loopguard.py`、`tests/test_loopguard.py`
- **修改文件**：`agent.py`、`tools.py`、`config.py`、`cli.py`、`webapp/session.py`、`static/index.html`、`README.md`
- **change_type**：**architectural_risk**（对抗性纠正后判定）
- **风险与缓解**：新增 `_run` 主循环**第四条终止路径**并在循环中途 return；向 history 注入合成 user 消息——若作为“紧接其后的独立 user 消息”会**制造连续两条 user**、违反 Anthropic 角色交替约束致后续请求 400，必须并入既有 tool_result 消息；改动被所有工具共用的 `ToolResult` dataclass 数据流（仅加带默认值字段不破坏位置调用，此子风险确为小增量）。诚实备注：单看“重复动作检测”本可降为 medium；合并“无进展检测”后需新增文件系统 stat/日志字节行数 IO 数据流并注入 history，且默认开启+误判为破坏性终止，故整体报 architectural_risk；advisory-only 版（仅告警+注入+事件，复用 MAX_TURNS 作唯一硬停）可降至 medium。**编排层最大回归面**：gwas/wgs 管线大量合法重复“Bash background → TaskOutput 轮询 → 检查 .done/.pid”，若只比 (name,input) 极易误杀；必须 progress-aware + polling 白名单 + 保守阈值。建议实现顺序：先只加 meta 信号做纯观测（只记不判、可回退），观察误判率后再开 WARN/STUCK。
- **优先级**：P1｜**依赖**：`agent.py:_run` 与 `_execute_tools`；`tools.py:ToolResult.meta`；`config.py:Config` 阈值；上层编排 skill 的后台轮询模式。

### M4 ToolRegistry — 可插拔工具注册表与动态发现

- **目标**：把 `ToolSet.__init__` 里写死的 8 工具字面量列表替换为运行时 `ToolRegistry`，使工具集不再在构造时冻结，编排 skill 需要新阶段工具时可运行时挂载。
- **借鉴**：smolagents（`Tool` 子类 + `@tool` 自动注册到工具箱）；OpenHands（Action/Observation 装饰器注册 + 工具按名增删）；Claude Code（内置工具与 MCP 工具的注册表 + 自动挂载）；Continue.dev（tool provider 抽象，可动态刷新）。
- **要做什么**：提供 register/unregister/replace/get/names/list_schemas 接口，工具以数据（name/description/input_schema/executor/always/来源标记）登记而非绑定为 `ToolSet` 方法。支持内置供体与外部供体（磁盘插件目录 `*.py`、MCP server）。`schemas_for(allowed)` 改为“静态白名单过滤 + 按需向供体查询动态工具”。
- **数据流**：`ToolSet` 持 `ToolRegistry` 实例；启动时内置工具经 `registry.register` 批量登记；`execute(name,args)` 改为查 registry 获取 executor；`schemas_for` 先取内置再合并动态供体。agent 与 webapp 经同一 registry，不感知来源。
- **新增文件**：`skill_agent/registry.py`
- **修改文件**：`tools.py`、`config.py`、`README.md`
- **change_type**：**architectural_risk**
- **风险与缓解**：改动工具层核心构造与查找路径（`ToolSet` 初始化、`schemas_for`、`execute` 三处契约）；动态工具引入“工具来源不可信”，外部 executor 与内置同权限执行，须与 M8 权限门、M9 沙箱联动，否则注册即等于扩大执行面。缓解：先只做内置工具的注册表化（行为与现网完全一致），动态供体作为后续增量并默认关闭；可回退到硬编码列表。
- **优先级**：P1｜**依赖**：M8 权限模型（外部工具须受同一权限策略约束）。

### M5 MCP 客户端适配层

- **目标**：连接外部 MCP server，动态枚举并挂载远端工具。
- **借鉴**：MCP 规范（stdio/SSE/HTTP 传输；`tools/list` 动态枚举 + `tools/call` 转发；生命周期与鉴权）；OpenHands（MCP tool adapter 把远端工具适配为本地 Action）；Claude Code（MCP server 配置管理与工具自动挂载）；Continue.dev（MCP provider）。
- **要做什么**：新增 `mcp_client.py`：连接管理（三传输）、initialize 握手、`tools/list` 枚举、`tools/call` 转发、结果映射为本地 `ToolResult`、生命周期与鉴权。远端工具经 M4 registry 挂载，`input_schema` 采用远端声明。可选提供把本机工具以 MCP server 形式对外暴露的最小服务端骨架。
- **数据流**：按配置连接 MCP server → `tools/list` → 远端工具登记进 registry（来源=`mcp:<server>`）→ 模型可见可调用 → `execute` 命中则经 `mcp_client.call` 转发 → 结果回填 tool_result。全程经 M4/M6/M7/M8 治理。
- **新增文件**：`skill_agent/mcp_client.py`
- **修改文件**：`config.py`、`tools.py`、`requirements.txt`、`README.md`
- **change_type**：medium
- **风险与缓解**：新增外部进程/网络依赖（新增 mcp SDK，**违反当前“仅 requests/PyYAML/flask”约束**，须评审是否接受或手写最小客户端）；远端结果不可信、可能含提示注入，须经 M6 校验与结果治理。缓解：默认不配置即不启用，行为零回归；独立文件可整体回退。
- **优先级**：P2｜**依赖**：M4（挂载点）、M6、M8。

### M6 ToolInput 输入校验 — JSON Schema 强校验

- **目标**：`execute` 拿到 args 后、调用 executor 前按 `input_schema` 做 required/type/enum 校验，不合法直接返回结构化 schema 报错，让模型自我纠正。
- **借鉴**：smolagents（用 JSON schema 校验工具输入并对模型结构化报错）；OpenHands（pydantic 强校验 Action 参数）；Claude Code（tool input 校验与结构化报错）。
- **要做什么**：手写轻量校验器或引入 jsonschema（二选一）。避免现有 `Bash int(args.get('timeout'))` 遇字符串抛异常被包成泛化 `[异常]` 这类误导。
- **数据流**：`execute(name,args) → validation.validate(schema,args)` → 通过则 `tool.executor(args)`，否则返回 is_error 的 schema 报错 tool_result。纯前置校验，不改执行器与主循环。
- **新增文件**：`skill_agent/validation.py`、`tests/test_validation.py`
- **修改文件**：`tools.py`、`README.md`
- **change_type**：**small_increment**
- **风险与缓解**：纯前置、无副作用、可独立开关/回退。唯一注意：校验过严可能拒绝模型合法的宽松输入（如数字字符串），需与既有 executor 容错（各种 `args.get` 默认值）兼容——建议对可选字段宽容、仅对 required 与明显类型错误报错。**直接复用已存在的 `input_schema`**。
- **优先级**：P0

### M7 工具执行治理 — per-tool 超时 + 结果大小治理 + 并行调用 + 运行中取消

- **目标**：统一工具执行治理。
- **借鉴**：Claude Code（每工具统一超时且可中断；超长输出落盘并给可再 Read 的路径；同一 assistant turn 内多个 tool_use 并发执行）；OpenHands（Action 执行超时 + 取消；Observation 截断 + 分页；Action 批量并发）；Anthropic Messages API（原生支持一个 turn 内多个 tool_use）；smolagents（工具级 timeout）。
- **要做什么**：(a) per-tool 超时抽象（Read 超大文件/Glob 大盘递归/TaskOutput 巨日志），接受 `budget.remaining_for_tool` 收窄；(b) 结果大小治理——超限不再只做头部截断，而是落盘转存 + 返回可继续读取的引用/分页句柄，按工具类型差异化预算；(c) 并行调用——同一轮多个 tool_use 用线程池并发，无副作用工具（Read/Grep/Glob）并发、有副作用/阻塞工具（Bash 前台、Write/Edit 同文件）串行或加锁；(d) 运行中取消粒度——长工具执行期间经 `cancel_event` 联动、缩短点停止延迟。
- **数据流**：`_execute_tools(blocks, budget)` 分组（可并发 vs 需串行）→ 线程池提交可并发者 → 每个工具执行前经 `remaining_for_tool` 收窄 timeout → 超限结果落盘并返回引用 → 汇总 tool_result（顺序与原 blocks 对齐）→ 回填。事件流不变；新增结果截断落盘的引用事件。
- **新增文件**：无
- **修改文件**：`agent.py`、`tools.py`、`config.py`、`webapp/server.py`、`README.md`
- **change_type**：**architectural_risk**
- **风险与缓解**：并行化改动 `_execute_tools` 核心执行模型（串行→并发），线程安全面显著扩大——`ToolSet._bg_tasks`、`_active_skills`（在工具执行中改 `self._active_skills`）、confirm 桥、同文件 Write/Edit 竞态都需加锁或串行保护，否则引入难复现的并发 bug。超时与结果治理为 medium；取消粒度受限于“墙钟无法抢占正在运行的进程”，只有配合超时收窄才真正生效。缓解：并行仅对白名单只读工具开启（默认串行、可开关），副作用工具强制串行；落盘转存默认开启但保留旧截断兜底。
- **优先级**：P1｜**依赖**：M1（remaining_for_tool）、M3（ToolResult.meta）、M4（工具元数据声明超时策略）。

### M8 PermissionModel — 结构化 allow/deny/ask + 权限模式 + 审批粒度 + 路径白名单边界

- **目标**：把字符串正则黑名单（11 条 `DANGEROUS_PATTERNS`）+ 仅覆盖 Bash 的 `permission=auto|ask` 升级为结构化权限策略。
- **借鉴**：Claude Code（settings.json permissions rules：工具/命令前缀/路径白名单；permission modes default/acceptEdits/plan/bypassPermissions；PreToolUse 审批）；Codex CLI（approval modes + sandbox policy）；OpenHands（SecurityAnalyzer 逐 action 风险判定 + ConfirmationMode）；Aider（per-file/per-command confirm）。
- **要做什么**：(a) allow/deny/ask 规则表，按“工具 + 参数模式”声明，支持命令前缀/正则/路径 glob，**覆盖全部工具（尤其 Write/Edit 目前无任何审批）**；(b) 权限模式 default/acceptEdits/plan/bypassPermissions；(c) 审批粒度细化（只问写操作/只问网络/只问首次某命令）+ “总是允许/记住选择”规则持久化；(d) 参数级审批预演——Web 弹窗展示 diff/影响面/将创建文件列表，CLI 无 TTY 行为明确化；(e) 路径白名单/文件系统边界——`_resolve` 增加工作区根归属校验，拒绝 `..` 越出与工作区外绝对路径（Read/Write/Edit/Glob/Grep/TaskOutput 全经此），把 cwd 升级为真实边界。
- **数据流**：`tools.execute` 前置 `PermissionModel.decide(name,args)` → allow 直接执行 / deny 返回 is_error / ask 走 CLI `input()` 或 Web confirm 桥（带预演）→ 判定与规则来源记入审计事件。`_resolve` 先校验路径归属再返回，越界抛 ToolError。规则与模式来自 Config/settings，会话可覆盖。
- **新增文件**：`skill_agent/permissions.py`、`tests/test_permissions.py`
- **修改文件**：`tools.py`、`config.py`、`cli.py`、`webapp/server.py`、`static/index.html`、`README.md`
- **change_type**：**architectural_risk**
- **风险与缓解**：权限/安全边界变更，触及工具执行与文件路径两条核心路径，直接决定“模型能做什么”；黑名单无法枚举变体（`rm -fr`、`/bin/rm`、base64 混淆、`$IFS` 绕过），改为结构化规则后若匹配逻辑有洞，反而产生**“看似安全实则绕过”的虚假安全感**。路径边界是行为破坏性变更——工作区探测错误可能拒绝合法访问（如 skill 需读 `~/.claude` 或全局数据目录），须白名单化系统必要路径。当前无沙箱（Bash 直连宿主），权限门是唯一防线，故此项应为 **P0**。缓解：未知命令默认 ask 而非 allow；路径边界支持显式 `extra_roots`；规则表可回退原黑名单。
- **优先级**：P0｜**依赖**：`tools.py` 的 execute/_resolve/权限门；`config.py` 权限字段与设置加载。

### M9 Sandbox runtime — 沙箱隔离适配层

- **目标**：为 Bash（含后台任务与模型生成脚本）提供可选执行后端抽象。
- **借鉴**：OpenHands（docker runtime 执行容器）；E2B（firecracker microVM）；gVisor / firejail / bubblewrap / NSJail / Docker+seccomp（只读 rootfs、命名空间、seccomp、rlimit）。
- **要做什么**：`sandbox.py` 抽象本地宿主（现状）、本地容器（Docker/bwrap/firejail）、远程沙箱（E2B/远端），提供工作目录挂载策略、只读 rootfs、CPU/内存/磁盘/进程数 rlimit、网络开关。默认关闭，启用后 Bash/后台脚本经沙箱执行；无法启动时按策略 fail-open 或 fail-closed。
- **数据流**：Bash executor 组装命令 → `SandboxRuntime.wrap(command, cwd, limits)` → 隔离环境执行 → 回读 stdout/stderr 与退出码 → 与现状同构返回 ToolResult。后台任务同理。策略来自 Config。
- **新增文件**：`skill_agent/sandbox.py`
- **修改文件**：`tools.py`、`config.py`、`README.md`
- **change_type**：**architectural_risk**
- **风险与缓解**：引入新运行时依赖与执行后端，改变 Bash/后台任务执行语义（挂载、网络、PID 可见性、`.done/.pid` 路径映射），与编排管线后台轮询**强耦合**——沙箱内后台进程 `.pid` 在宿主不可见会破坏 TaskOutput 存活判断，须专门设计 PID/日志映射。环境差异大（是否装 docker/bwrap）需优雅降级。缓解：默认关闭、纯可选适配层；编排 skill 后台路径保留宿主直连；新增文件可整体删除。
- **优先级**：P2｜**依赖**：M8、M1。

### M10 ToolHook — Pre/Post 工具钩子系统

- **目标**：在工具执行前后提供可注册钩子，使审计/脱敏/合规扩展不必散落进各 executor。
- **借鉴**：Claude Code（hooks：PreToolUse/PostToolUse/UserPromptSubmit，可拦截/改写输入输出）；OpenHands（callback/事件总线）。
- **要做什么**：PreToolUse（审计、策略拦截、输入改写）、PostToolUse（结果后处理、脱敏、写审计日志）、会话级 UserPromptSubmit 类钩子。可声明为可阻断或仅观测。
- **数据流**：execute 前 → PreToolUse 链（任一阻断则返回 is_error）→ 执行 → PostToolUse 链（可改写 content）→ 回填。钩子在 ToolSet/Registry 初始化时注册，随会话生命周期。
- **新增文件**：`skill_agent/hooks.py`
- **修改文件**：`tools.py`、`config.py`、`README.md`
- **change_type**：medium
- **风险与缓解**：新增独立钩子层、纯插入点、不改主循环；风险在钩子执行顺序/异常隔离——某钩子抛异常不应拖垮工具执行，须 try 包裹并记日志；可阻断钩子与权限门职责需划清（钩子偏策略扩展，权限门是内建安全兜底）。缓解：默认为空钩子链（行为零变、可回退），钩子异常永不外泄到主循环。
- **优先级**：P2｜**依赖**：M4、M8。

### M11 Idempotency — 工具幂等与去重

- **目标**：为工具调用引入幂等/去重护栏，避免重复 nohup 起多份后台任务、重复写文件。
- **借鉴**：OpenHands（Action 去重与重复检测）；Claude Code（工具重复调用防护）；Temporal/durable-execution 式幂等键（task_id+幂等键缓存结果）。
- **要做什么**：对相同 `(name, 规范化 args)` 计算幂等键；有副作用工具（Bash、Write/Edit）同一次 run 内命中相同幂等键时可复用上次结果或拒绝重放；幂等键落盘（可选）以支持崩溃后安全 resume/replay。与 M3 互补：M3 判“是否卡死并熔断”，M11 判“是否已执行过、可安全跳过”。
- **数据流**：execute 前算幂等键 → 查缓存（内存/落盘）→ 命中且有副作用则复用或按策略拒绝 → 未命中执行并记录结果+幂等键。崩溃恢复时据落盘幂等键跳过已完成副作用步骤。
- **新增文件**：`skill_agent/idempotency.py`
- **修改文件**：`tools.py`、`config.py`、`README.md`
- **change_type**：medium
- **风险与缓解**：风险在误判——合法“同一命令重跑获取新状态”（如重复 cat 变化的日志）不应被去重，须区分只读/有副作用且仅对有副作用且参数完全一致者去重；落盘幂等键引入崩溃恢复语义，须与现有“history 仅内存、jsonl 仅事后归档”的现实对齐，否则形成半吊子 resume。缓解：默认仅内存去重、有副作用默认拒绝重放可关闭；独立文件可回退。
- **优先级**：P2｜**依赖**：M3、M4。

### M12（补充）上下文摘要式压缩 + 分层长期记忆/检索

- **目标**：把 `_maybe_compact`/`_safe_cut` 的“丢弃旧消息”升级为“摘要式压缩 + 分层记忆”，解决长管线丢失阶段参数/路径/审批结论的致命问题。
- **借鉴**：Claude Code auto-compact（阈值触发摘要 + 保留最近窗口 verbatim）；OpenHands SummarizingCondenser（旧事件交模型摘要、以“单条压缩事件”回灌并滚动折叠）；Letta/MemGPT 分层记忆（core memory 常驻 + archival 归档 + recall 检索）；Mem0（从轨迹抽取结构化长期记忆事实并按需召回）。**全部用 stdlib 实现（sqlite3 FTS5 做检索），不引入向量库等新依赖。**
- **要做什么**：(1) `SummaryCompressor`——命中阈值时保留最近 K 条消息原样，对更早区间取合法边界（复用 `_safe_cut` 保证不拆配对），用低成本 summary 模型（非流式、低 max_tokens）生成结构化摘要并与上一版滚动摘要折叠，作为单条 user 消息前置；摘要模板**强制保留管线关键不变量字段**：阶段号、阶段参数、输入/输出路径、后台任务 `log_path+pid+.done`、审批结论、未决 TODO、错误根因。(2) `MemoryStore`——core memory（会话级 KV，常驻 system，模型可显式编辑）+ archival memory（项目级 SQLite+FTS5，Mem0 式结构化记忆事实，每轮对新输入做“FTS5 关键词 + 结构化字段精确命中”双通道检索，注入 top-k）。(3) 暴露 Memory 工具（set_core/get_core/search/append）主动固化阶段参数与审批结论。(4) 补真实 token usage 累积以驱动阈值。压缩失败回退现有“丢旧消息”作为逃生通道。
- **数据流**：`_run` 每轮开始 → `CompactionManager.maybe_compact(history, usage_acc)`（触发 = 累积真实 token 超预算 / 消息数超阈值 / 字节超阈值）→ 保留最近 K 条 verbatim，更早区间经 `_safe_cut` 取合法边界 → `SummaryCompressor` 生成结构化摘要（失败回退丢旧消息）→ 与旧摘要折叠为单条“前情摘要”user 消息 → `MemoryStore.remember` 抽取写入 archival + core → `_system_prompt` 注入 core memory 块（稳定，作可缓存前缀）+ recall 命中放消息尾部 → Memory 工具显式固化 → CLI `/memory`、Web `/api/memory` 查看编辑。
- **新增文件**：`skill_agent/compaction.py`、`skill_agent/memory.py`、`skill_agent/tokens.py`
- **修改文件**：`agent.py`、`client.py`、`config.py`、`tools.py`、`cli.py`、`webapp/session.py`、`server.py`、`static/index.html`、`README.md`
- **change_type**：**architectural_risk**（其中“摘要式压缩”主体为 medium-high，“分层记忆/检索”为 architectural）
- **风险与缓解**：**[高-摘要丢不变量]** 摘要模型可能遗漏/改写管线关键字段（阶段参数、路径、审批结论、log/pid/.done）→ 后续阶段用错参数或重复执行（gwas 7 阶段/wgs 6 阶段致命点）。缓解：结构化模板 + 关键字段白名单强制抽取 + 最近窗口 verbatim 不参与摘要 + 回灌前确定性校验，缺字段则补齐或拒绝压缩。**[高-破坏配对]** 摘要替换必须落在 `_safe_cut` 合法边界且不得插在 assistant(tool_use) 与其 tool_result 之间。缓解：复用边界逻辑 + 边界断言 + 单测。**[中-摘要调用不可靠]** 网络超时/限流反噬主循环。缓解：低级/同模型低 max_tokens、失败即回退。**[中-prompt cache 失效]** 每轮向 system 注入会破坏前缀缓存。缓解：core memory 稳定作可缓存前缀，recall 放消息尾部。**[中-检索质量]** 无向量库、纯 FTS5 对语义相近漏召。缓解：结构化字段 + FTS 关键词 + KV 精确命中双通道。**[中-持久化并发]** threaded Flask 多会话共享项目级 archival。缓解：WAL、会话隔离、单写者、原子写 tmp+rename。**[中-记忆污染]** 自动抽取可能写错事实被永久召回。缓解：来源/时间/TTL、可查看可删除、容量上限与淘汰。**[小增量]** config 字段、client 补 usage、`/memory` 命令、README。**[注意]** 会话崩溃一致性仍弱（history 内存、memory 已落盘不同步）；本模块不解决 checkpoint/resume，属另一缺失项，不应被误认为一并修复。
- **优先级**：核心（#4/#5 为长管线可用性关键）｜**依赖**：M1 usage、`_safe_cut`、client summary 调用能力。

### M13（补充）会话持久化与 checkpoint/resume（SessionStore + 消息级 checkpoint + 恢复校验 + 幂等联动）

- **目标**：为纯内存的 `Agent.history` 增加消息级持久化与崩溃恢复，补齐“后台长管线进程重启即丢全部上下文”的可靠性缺口。
- **借鉴**：LangGraph（checkpointer/thread 状态快照，按 thread_id 存盘、恢复读最近 checkpoint）；OpenHands（append-only 会话事件流 + replay 重建状态）；Claude Code（`--resume/--continue`，jsonl 落盘于 `~/.claude`，`--continue` 取最近会话）；Temporal（durable execution 重放语义：已记录结果的 activity 不重跑，用幂等 key 保证 exactly-once）。
- **要做什么**：(1) 每会话一个持久化目录，同时维护“append-only 事件流”（逐条消息/tool_result 追加，可 replay）与“周期快照”（每轮 tool 执行后原子写完整 messages 快照）。(2) 恢复校验：加载时校验每条 assistant 的 tool_use 是否都有配对 tool_result，发现悬挂轮次时**重写该 assistant 消息剔除孤立 tool_use 块**（保留 text/thinking 顺序与 signature）或标记该轮待重试。(3) 与幂等键联动：tool_result 以 `tool_use_id` 为键落盘，resume 后已存在结果直接复用、不重跑。(4) 与后台任务联动：`task_id→日志/pid` 映射与 `.done/.pid` 一并落盘，resume 时从盘上重建 `_bg_tasks` 并通过 pid 存活 + 标记文件判断真实进度。会话标识沿用 jsonl timestamp 或显式 sid，支撑 CLI `--resume/--continue` 与 Web 断线按 sid 重建。
- **数据流**：正常路径——user 消息 → `append_event(user)` → 循环：`client.create` 返回 assistant（含 tool_use(id)）→ 落盘 `event=assistant` → 写快照 → `_execute_tools` 逐 tool_use 先按 `tool_use_id` 查是否已有结果（命中复用）→ 否则执行并落盘 `event=tool_result` → 追加 user(tool_result) → 落盘 → 再写快照 → 下一轮。后台路径——`task_id→{log,pid,cmd}` 连 tool_use_id 一起写盘；TaskOutput 每轮读盘 + pid/`.done` 确认。崩溃路径——进程在“assistant(tool_use) 已落盘、tool_result 未落盘”之间死亡 → `resume(sid)`：读最近快照 → 追加重放尾部事件 → 校验配对 → 对悬挂 assistant 剔除无 result 的 tool_use 块。
- **新增/修改文件**：新增 `skill_agent/session_store.py` 等（会话存储 + 恢复）；修改 `agent.py`（主循环消息 schema 序列化契约）、`cli.py`（`--resume/--continue`）、`webapp/session.py`、`server.py`、`config.py`、`README.md`。
- **change_type**：**architectural_risk**
- **风险与缓解**：需改 `agent.py` 主循环与消息 schema 序列化契约，且恢复语义与既有压缩逻辑（M12）相互耦合；悬挂 tool_use 剔除若处理不当会破坏 thinking signature 顺序致 400。缓解：恢复校验先做纯函数并单测；快照+事件流双层，任一可独立验证；与幂等键联动保证 exactly-once。
- **优先级**：核心｜**依赖**：M12（压缩契约）、M11（幂等键）。

### 补齐项（低成本小增量：#6/#7/#9/#12/#14/#15）

- **#9 规划/todo**（借鉴 Claude Code TodoWrite）：新增一个 todo 工具，把计划外部化为可检查状态。**small_increment**。
- **#14 prompt cache_control**（借鉴 Anthropic prompt caching 规范）：在稳定前缀（system、工具定义）发 `cache_control`。**small_increment**。
- **#6 可观测性**（借鉴 OpenTelemetry 式 trace）：把 `on_event` 扩展为结构化 span/trace 落盘。**medium**。
- **#12 模型路由/降级**（借鉴 litellm/OpenRouter）：过载反复重试后切换后备模型。**small_increment**。
- **#7 evals**（借鉴 SWE-agent eval harness）：**应在 M3 LoopGuard 开启默认判定前落地以量误判率**。**medium**。
- **#15 输出校验**：对模型返回的 tool_use 参数做与 M6 一致的结构校验后再落 executor。**small_increment**。
- **#11 repomap/RAG**：原评估按 tree-sitter/向量库路线（需新依赖）而後置；**已改为纯 stdlib FTS5 路线并落地**
  （`retrieval.py` 的 Search/RepoMap，见 §8）。**#8 子 agent/委派** 体量大、需接受多 agent 运行时依赖，
  **与当前「仅 requests/PyYAML/flask」约束冲突，须先评审**，仍後置。其中 #8 为 **architectural_risk**
  （新增子 agent 上下文隔离→内层预算/权限/loop 边界）。

---

## 4. 数据流：增补前 vs 增补后

### 4.1 增补前（现状端到端）

**入口归一** → `Agent.answer(text, on_delta)`

- **CLI**：`_repl`/`_oneshot` → 构造 `Config` → `Agent.answer`（无预算/权限模式字段）。
- **Web**：`POST /api/chat` → `session.set_params`（可能改共享 `cfg.model`）→ 线程 `run()` → `Agent.answer`；`done` 事件仅含 `final/messages/active`；SSE 无 usage/notice 事件。

**主循环每轮**：`history.append(user)` → `while step in range(MAX_TURNS=60)`：检查 `cancel_event` → `_maybe_compact`（**丢弃**旧消息）→ `tools.schemas_for(allowed)`（静态过滤硬编码 8 工具）→ `_system_prompt` → `client.create`（**仅取 out_tokens，input/cache token 丢失；tool_use 解析失败静默 pass**）→ 追加 assistant → 若有 tool_use 且 `stop_reason ∈ {tool_use,max_tokens,""}` → `_execute_tools`（**串行**，仅 Bash 权限门，无超时上限、无配对数校验）→ 追加 tool_result → continue；否则取 text 返回。

**终止**：`cancel_event` → `MAX_TURNS` 硬上限；终止原因不结构化，CLI jsonl 仅写 `end:true`。

**编排 skill 路径**：`Skill(...)` 加载 → 生成脚本 → 交互模式等用户答复 → `Bash background=true`（nohup + `.done/.pid`）→ 周期性 `TaskOutput` 轮询。

### 4.2 增补后（★ = 新增环节，覆盖 CLI / Web / 编排三路）

**入口归一** → `Agent.answer(text, overrides)`

- **CLI**：`_repl/_oneshot` → 构造 `Config(含新预算/权限字段)` → `Agent.answer`。★ REPL 新增 `/cost` `/budget`；`_finish_log` 收尾行写最终 `stop_reason` 与 usage 汇总。
- **Web**：`POST /api/chat` → 从请求体读 `max_turns/max_seconds/permission` → `session.set_params` → 线程 `run()` → `Agent.answer`。★ `done` 事件扩展 `stop_reason/truncated/usage`；★ 新增 `GET /api/usage`；★ SSE 透传 `usage/notice/loop_guard/progress_warning/budget` 事件。

**初始化**：★ `budget.start()`（monotonic 起点、清零 steps/usage）；★ `loopguard.reset()`；★ `progress` 复位；`history.append(user)`。

**主循环每轮** `while True`：
1. ★ guard 预检：`budget.tick_step()` → `exceeded()`（max_steps/wall_clock）；`loopguard` 检查上轮 verdict 是否 STUCK；`cancel_event` 检查。命中进终止收尾。
2. `_maybe_compact`（★ M12 升级为摘要式压缩 + 分层记忆；不变时仍按字节/条数 + `_safe_cut` 配对边界）。
3. `tools = ToolSet.schemas_for(allowed)`；★ 由 `ToolRegistry` 提供，可含动态发现/MCP 工具。
4. `client.create(system, tools, messages)`。★ client 侧：`_stream` 从 `message_start` 读 input/cache token 与 `message_delta.output_tokens` 合并为完整 `UsageRecord`；tool_use 解析失败标 `_parse_error/_raw`。返回 `(blocks, stop_reason, usage)`。
5. ★ `budget.add(usage)` → emit usage 事件；★ `budget.check_before_call()` 若 EXCEEDED 带原因终止。
6. `history.append(assistant)`。
7. ★ `StopController.classify(stop_reason, blocks, truncated)` → `StopDecision`。分派：continue_tools → 执行工具；repair_truncation → 为截断 tool_use 合成 is_error tool_result 续一轮；pause_turn → 原样回传续推（不追加 user）；refuse/finalize → 收尾。

**★ 工具执行 `_execute_tools`（改造）**：对每个 tool_use：
1. ★ `PermissionModel.check(name,args)`：结构化 allow/deny/ask + 路径边界（`_resolve` 收窄工作区）+ 权限模式；命中 deny/ask → CLI `input()` 或 Web confirm 桥（弹窗含 diff/将创建文件预演）。
2. ★ Sandbox runtime（可选）：Bash 经适配层隔离执行，否则退回宿主。
3. ★ ToolInput validation：按 schema 校验 required/type/enum，不合法直接返回结构化 schema 报错（不落 executor）。
4. ★ PreToolUse Hook → ★ idempotency 检查 → 执行（★ `remaining_for_tool` 收窄 timeout）→ ★ PostToolUse Hook。
5. ★ 并行：同轮多 tool_use 经线程池并发（有副作用者串行/加锁）。
6. ★ `ToolResult.meta` 填充进展信号（path+mtime+size / returncode / 日志字节行数 / pid 存活）。
7. 回填 tool_result（★ loopguard warn 则注入重复动作告警；★ 结果超限落盘并给可再 Read 引用）；收集 meta。
8. ★ `loopguard.record(...)` / `progress.observe(step)` → verdict。WARN 把纠偏消息并入**同一轮** tool_result（严禁连续两条 user）；STUCK 置终止标志。

**终止判定顺序（★）**：`cancel_event` → loopguard/progress 熔断 → budget 超限 → StopController finalize → `MAX_TURNS`。收尾统一走 `_finalize(text, decision/guard, budget)`：生成最终文本 + ★ emit notice + ★ 返回 `(text, stop_reason)`。

**产出**：CLI 按 `stop_reason` 打印「完成/撞上限/被截断/被拒/死循环熔断」，写会话 jsonl（含 stop_reason 与 usage），★ usage 汇总另存 `~/.skill-agent/usage/<session>.json`；Web `done` → 气泡，usage/notice/progress 事件 → 状态区与提示条，SSE `__end__` 结束。

**编排 skill 特有路径（gwas-7 阶段 / wgs-6 阶段）**：生成脚本 → ★ PermissionModel 审批（参数级 + 预演）→ `Bash background=true`（nohup + `.done/.pid`）→ 周期性 `TaskOutput` 轮询日志。★ loopguard 必须 **progress-aware**：TaskOutput/Read(`*.log/.done/.pid`) 视为 polling 白名单，仅当“同动作 + 同观测（无新增行）”才计数升级，日志增长即回落，避免误杀逐阶段自动推进。★ budget 到点只停止轮询、**绝不杀后台进程**，收尾文本提示后台任务仍在运行。★ 生成的脚本/命令在沙箱（若启用）中执行。

---

## 5. 需要新增/修改的文件列表

| 路径 | 动作 | 所属模块 | change_type |
|------|------|----------|-------------|
| `skill_agent/budget.py` | add | M1 预算与核算 | architectural_risk |
| `skill_agent/pricing.py` | add | M1 | small_increment |
| `skill_agent/usage_store.py` | add | M1 | small_increment |
| `skill_agent/stops.py` | add | M2 停止原因与终止决策 | architectural_risk |
| `skill_agent/loopguard.py` | add | M3 死循环/无进展熔断 | architectural_risk |
| `skill_agent/registry.py` | add | M4 工具注册表 | architectural_risk |
| `skill_agent/mcp_client.py` | add | M5 MCP 客户端适配层 | medium |
| `skill_agent/validation.py` | add | M6 输入校验 | small_increment |
| `skill_agent/permissions.py` | add | M8 权限模型与路径边界 | architectural_risk |
| `skill_agent/sandbox.py` | add | M9 沙箱隔离 | architectural_risk |
| `skill_agent/hooks.py` | add | M10 工具钩子 | medium |
| `skill_agent/idempotency.py` | add | M11 幂等与去重 | medium |
| `skill_agent/compaction.py` | add | M12 摘要式压缩 | architectural_risk |
| `skill_agent/memory.py` | add | M12 分层记忆/检索 | architectural_risk |
| `skill_agent/tokens.py` | add | M12 token 累积 | small_increment |
| `skill_agent/session_store.py`（及恢复模块） | add | M13 会话持久化/resume | architectural_risk |
| `tests/test_budget.py` | add | M1 | small_increment |
| `tests/test_stops.py` | add | M2 | small_increment |
| `tests/test_loopguard.py` | add | M3 | small_increment |
| `tests/test_validation.py` | add | M6 | small_increment |
| `tests/test_permissions.py` | add | M8 | small_increment |
| `skill_agent/agent.py` | modify | M1/M2/M3/M7/M10/M12/M13（主循环与工具执行编排） | architectural_risk |
| `skill_agent/client.py` | modify | M1（usage 提取）/M2（解析失败标记）/M12 | medium |
| `skill_agent/config.py` | modify | M1/M3/M7/M8 等（新增可选配置字段） | small_increment |
| `skill_agent/tools.py` | modify | M3/M4/M6/M7/M8/M10/M11（meta/执行治理/权限/幂等） | architectural_risk |
| `skill_agent/cli.py` | modify | M1/M2/M3/M7/M8（`/cost` `/budget`、终止提示、日志、`--resume`） | small_increment |
| `webapp/server.py` | modify | M1/M2/M7（`/api/usage`、done 字段、SSE 时限） | small_increment |
| `webapp/session.py` | modify | M1/M2/M3（budget 桥、done 字段、guard 复位） | small_increment |
| `webapp/static/index.html` | modify | M1/M2/M3（usage/notice/progress 事件渲染） | small_increment |
| `requirements.txt` | modify | M5（MCP SDK，待评审） | medium |
| `README.md` | modify | 全部（能力与配置说明） | small_increment |

---

## 6. 风险分级

### 【小增量】低风险、可独立回退，建议先做

| 项 | 说明 |
|----|------|
| M6 输入校验 | 纯前置校验、可开关 |
| M1 中的 usage 采集 / Config 可选字段 / CLI `/cost` / Web `/api/usage` / 前端展示 / 落盘 | 纯新增、不动既有路径 |
| M2 的 termination surface（done/notice 字段扩展） | 纯事件透出 |
| M3 的 `ToolResult.meta` 字段与纯观测（只记不判） | 加带默认值字段、不破坏位置调用 |
| M8 的规则表本身 / M10 空钩子链 / M11 内存去重 | 新增文件 |
| M12 的 config 字段、client 补 usage、`/memory`；#9/#12/#14/#15 补齐项 | 低成本并行 |

> **共性回退策略**：均为默认不改变现有行为的开关式新增，删除新增文件即回退；默认关闭/默认不设时行为与现状完全一致。

### 【架构风险】触及核心循环契约 / 状态模型 / 安全边界 / 并发，需评审 + 分阶段 + 回归测试

| 项 | 风险 | 缓解 | 回退 |
|----|------|------|------|
| M1 核心 | 改主循环终止契约（`_run` 返回 `(text,stop_reason)`、发请求前预判）与工具超时路径 | limits 全可选默认不设 → 零回归；熔断只在发请求前或完整一轮后；client **追加** usage 而非打乱三元组 | 删 budget 模块 + 还原 create 返回 |
| M2 | 重写 `_run` 终止契约并触碰配对不变量 | 先纯函数 + `tests/test_stops.py` 覆盖六种组合；暂停/修截断首期只发 warning；end_turn 夹带工具对 Bash 强制 ask | 还原 `_run` 与 client 两处标注点 |
| M3 | 新增第四条终止路径 + 向 history 注入合成 user（须并入 tool_result，**严禁连续两条 user**）+ 改 `ToolResult` 数据流 | progress-aware + polling 白名单 + 保守阈值；先纯观测后开熔断；默认可一键关闭 | `no_progress_enabled=false` 回到纯 MAX_TURNS |
| M4 | 改工具层构造/查找路径，动态工具扩大执行面 | 先只内置注册表化，动态供体默认关闭 | 还原硬编码列表 |
| M7（并行） | 串行→并发，线程安全面（`_bg_tasks`、`_active_skills`、confirm 桥、同文件写） | 并行仅对白名单只读工具、默认串行、可开关 | 改回串行 for |
| M8 | 安全边界变更，路径边界是行为破坏性变更 | 未知默认 ask；`extra_roots` 白名单系统必要路径；唯一防线故 P0 | 可回退原黑名单 |
| M9 | 新执行后端与编排后台机制（`.pid/.done` 可见性）强耦合 | 默认关闭、纯可选适配层、保留宿主直连 | 删除适配层 |
| M12 | 改核心消息装配不变量 + 主循环引入辅助模型调用 + 持久化 + 检索注入 | 结构化摘要模板 + 关键字段白名单 + 最近窗口 verbatim；失败回退丢旧消息；core 稳定作可缓存前缀、recall 放尾部；WAL/单写者/原子写 | 关压缩回退、删抓取层 |
| M13 | 改主循环消息 schema 序列化契约，恢复语义与压缩耦合 | 恢复校验纯函数 + 单测；快照+事件流双层；与幂等键联动 exactly-once | 保留内存模式 |
| #8 子 agent | 新增上下文隔离 → 内层预算/权限/loop 边界 | 后置、需评审新依赖 | — |

### 跨模块不变量（所有改动须共同守住）

1. `tool_use/tool_result` **严格配对**，任何注入/合成/截断都不得破坏；`_safe_cut` 边界假设需复查。
2. **不制造连续两条同 role 消息**（Anthropic 角色交替约束）。
3. 预算运行态挂**每次 run 的局部对象**，绝不写共享 Config（Web 多会话共享 `CFG_REF['config']`）。
4. 熔断/预算终止**只在发请求前或完整一轮后**发生。
5. 计价/未知模型**降级为只计 token**，绝不崩溃 agent。

---

## 7. 建议的落地顺序（P0 → P1 → P2）

**【阶段 0 — 纯前置、低风险、零回归（1 个迭代）】**
- M6 ToolInput 校验（small_increment）：立竿见影减少模型参数错误导致的误导性异常。
- M8 PermissionModel 第一部分：`_resolve` 路径边界 + Write/Edit 纳入权限门 + 结构化 allow/deny/ask 规则表（安全是唯一防线，优先级最高；权限模式/预演/持久化后置）。
- M1 RunBudget 第一部分：client 补齐 usage 提取 + `TokenBudget` 累加器 + Config 可选字段（默认不设 → 零回归）+ usage 落盘 + CLI `/cost`。**不触碰终止控制流。**

**【阶段 1 — 核心循环契约（1-2 个迭代）】**
- M2 StopController：先纯函数 + `tests/test_stops.py`，再接入 `_run`；pause_turn/repair_truncation 首期只发 warning。
- M1 RunBudget 第二部分：接入 `while True` + tick/exceeded + `check_before_call` 提前终止 + `remaining_for_tool` 收窄 Bash timeout + done 事件 stop_reason + termination surface。
- M8 第二部分：权限模式、审批粒度、Web 预演弹窗。
- M13 会话持久化与 resume 与 M12 摘要压缩**宜在本阶段与 client 改造一并规划**（#4/#5 是长管线可用性关键；#13 provider 层是所有 provider 改动的地基，宜在做 M1/M2 的 client 改造前先行评估）。
- 完成后主循环拥有结构化 stop_reason 与预算熔断。

**【阶段 2 — 健壮性与工具层（1-2 个迭代）】**
- M3 LoopGuard：先只加 `ToolResult.meta` 进展信号 + 纯观测（只记不判，可回退），观察误判率后再开 WARN/STUCK；polling 白名单必做。
- **#7 evals 应在 M3 开启默认判定前落地以量误判率。**
- M4 ToolRegistry：先只做内置工具注册表化，动态供体默认关闭。
- M7 工具执行治理：先做 per-tool 超时 + 结果落盘转存（medium），并行调用作为可开关增量（默认串行）。
- M10 ToolHook：空钩子链先落地。
- #9/#14/#6/#12 低成本小增量可并行补齐。

**【阶段 3 — 扩展与隔离（按需，P2）】**
- M5 MCP 客户端（需先决策依赖策略，冲突评估）。
- M9 Sandbox runtime（需先设计 `.pid`/日志映射，避免破坏编排后台机制）。
- M11 Idempotency（内存版先行，落盘 resume 待 M13 crash-recovery 整体规划）。
- M4 动态工具发现、M7 并行、M8 规则持久化等增强项。
- #11 repomap/RAG、#8 子 agent/委派：体量大、需接受新增依赖（tree-sitter/向量库/多 agent 运行时），**须先评审**后置。

> **每个阶段结束跑对应 `tests/test_*.py`；新逻辑集中于独立文件，回退只需还原 `agent._run` 与 `client` 的两处标注点。**

---

## 8. 落地状态（截至 2026-10-14）

**已完成**：M1、M2、M3、M4、M6、M7、M8a、M9、M10、M11、M12、M13，以及补齐项 #9（TodoWrite）、
#12（模型降级，可配置后备列表）、#14（prompt cache_control）、#6（trace 可观测性）、
#11（检索——见下）。
全量 `python3 -m pytest tests/ -q` = **167 项通过**。

- **M9 沙箱**：落地为 `sandbox.py` 三档（host/rlimit/docker），仅拦截 Bash 两个启动点；
  Tier-2 后台 `.pid` 风险用「宿主监督 pid（`echo $$` → `exec docker run --rm`）」解决，
  `TaskOutput` 判活无需改动；同批修复了 `_bash_background` 的 pid 死代码缺陷。
- **M5 MCP 客户端：经决策者判定「不需要 MCP 服务器」，**放弃**，不实现**（依赖结论仍留档于
  `docs/dependency-strategy.md`）。
- **M8 第二部分**（权限模式 default/acceptEdits/plan/bypassPermissions、规则持久化「总是允许」、
  Web 预演弹窗）**未做**——M8a 已覆盖规则表/路径边界/写审批主体；此部分为增强项，本机单用户
  使用下优先级低，可作后续增量。
- **#11 检索：已落地**（原按「tree-sitter + 向量库」评估为需新依赖而後置；本机核实 SQLite 已编译
  FTS5，据此改走**纯 stdlib** 路线）。实现为 `retrieval.py`：`Search`（FTS5/BM25 相关性检索，返回
  文件:行号+片段）+ `RepoMap`（Python 类/函数/方法、Markdown 标题大纲）两个只读工具；
  分块器为纯函数（Python 用 `ast`、Markdown 按标题、Shell 按块）。索引落 `<project>/.skill-agent/index/code.db`，
  惰性构建 + 按 `(mtime,size)` 增量刷新。**安全核心：只从显式语料根遍历、就地剪枝 + 读前按大小/后缀拒绝，
  不整仓扫描**（仓库 ~24.5GB ML 权重永不 stat）。默认关，`--retrieval` 开启，`--index-rebuild` 强制重建；
  FTS5 缺失/目录不可写自停用。测试 `tests/test_retrieval.py` 21 项。
- **#8 子 agent/委派**：仍后置，需评审多 agent 运行时依赖，未做。

**结论**：13 个编号模块中 12 个已完成，M5 经决策放弃；harness 主体增补收官。