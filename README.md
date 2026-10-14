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
| `--no-stream` | 关闭流式输出 |
| `list --json` | 以 JSON 导出技能清单（含 allowed-tools / 是否可自动调用） |

REPL 内命令：`/help` `/clear` `/skills` `/active` `/model X` `/permission ask` `/exit`。
每轮对话自动归档到 `~/.skill-agent/sessions/<时间戳>.jsonl`（含全部工具调用，可回溯）。

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
