# TraitPilot — 面向基因组与表型分析的科研 Agent

TraitPilot 将自然语言指令转成可审阅的分析步骤、脚本和工具调用。仓库内置 19 个技能，覆盖全基因组测序（WGS）分析、全基因组关联分析（GWAS）、表型提取和表型–基因型对齐。可通过浏览器或命令行使用，无需 Claude Code。

**版本与变更**：[VERSION](VERSION) · [CHANGELOG](CHANGELOG.md)

**文档**：[完整使用指南](docs/usage.md) · [分析环境与数据配置](environment/README.md)

## 快速开始：启动 Agent

以下命令适用于 **Linux + Bash + Python 3.10 或更高版本**。Windows 用户可在 WSL2 的 Linux 环境中按此流程尝试，原生 Windows 的完整分析流程未验证。仅启动 Agent 不需要安装 Conda 或分析工具链；对话需要能访问兼容 **Anthropic Messages API** 的模型服务，并拥有可用的 API 凭证。

**1. 克隆项目并安装依赖**

```bash
git clone https://github.com/rua111555/TraitPilot.git ~/TraitPilot
cd ~/TraitPilot
python3 -m venv "$HOME/.venvs/traitpilot"
source "$HOME/.venvs/traitpilot/bin/activate"
python -m pip install -r requirements.txt
cp config.json.example config.json
```

**2. 配置模型服务**

编辑 `config.json`，将 `api_key` 填为自己的凭证，`base_url` 和 `model` 填为服务商提供的地址和模型 ID。也可以通过 `ANTHROPIC_AUTH_TOKEN`（或 `ANTHROPIC_API_KEY`）及 `ANTHROPIC_BASE_URL` 提供凭证和地址，环境变量会覆盖配置文件中的对应值。更多选项见[配置模板](config.json.example)。

**3. 启动 Web 服务**

```bash
python run_web.py
```

命令会启动 Python 后端，并由同一个服务提供浏览器页面。**无须先单独启动后端，也无须安装 Node.js 或运行 `npm`。** 保持终端运行，在浏览器打开 <http://127.0.0.1:8787>。首次可输入「列出可用技能，并说明 GWAS 的主要步骤，不执行分析」，验证模型连接，不需要准备数据。

习惯命令行可以改用 `python run.py chat`；配置好凭证后，`python run.py list` 可检查有哪些技能被识别。`python run.py --version` 可查看版本。

## 默认功能

Web 和命令行默认开启检索（`Search` / `RepoMap`）、任务清单、项目记忆、上下文摘要、会话保存与恢复、只读工具并行、循环保护、重复执行防护、超长输出落盘、工具追踪、提示缓存和错误学习。Web 的后台任务默认支持继续对话；任务完成后会回传结果，并提示下一步。用 `/lessons` 查看当前项目积累的错误经验。

文件工具默认限制访问路径，`Write` 和 `Edit` 默认需要确认。分析数据在仓库之外时，将可信的数据目录加入 `config.json` 的 `permission_roots`（下节有示例）。安装后可以在 `config.json` 将任一功能开关设为 `false`；**升级已有项目时保留原配置和凭证，把想启用的旧 `false` 改为 `true`，或删除对应键以采用新默认值**。修改后重启 Web 或重新启动 CLI。会话、长输出和工具追踪的文件写在 `~/.skill-agent/` 下；项目记忆、索引和错误经验保存在项目内的 `.skill-agent/`。

默认用 `host` 执行分析命令，便于直接调用本机的 Conda 环境。Docker 沙箱、额外插件和备用模型需要配置相应的环境、目录或模型 ID。模型端点如果明确拒绝提示缓存字段，Agent 会去掉该字段重试；缺少 SQLite FTS5 时，检索不可用。

## 跑真实分析：安装依赖并准备数据

以下命令面向 Linux x86_64，从仓库根目录执行。先安装并初始化 Conda，确认终端能运行 `conda --version`。安装器默认逐项确认，`--list` 可查看版本与安装项：

```bash
bash environment/install.sh --list
```

为当前分析设置数据路径。把示例路径换成自己的路径：

```bash
export GENO_DIR=/path/to/genomics_data
export PIPELINE_DB="$GENO_DIR/pipeline.db"
export IMAGE_DIR=/path/to/phenotype_images  # 只跑 WGS / GWAS 可省略
export PATH="$HOME/.local/bin:$PATH"         # GWAS 独立程序的安装位置
```

如果希望这些路径随项目启动，也可以把 `GENO_DIR`、`PIPELINE_DB` 和 `IMAGE_DIR` 写入 `config.json` 的 `env`。以下是配置片段，请合并到现有 `config.json`，保留模型和凭证：

```json
{
  "permission_roots": ["/path/to/genomics_data", "/path/to/phenotype_images"],
  "env": {
    "GENO_DIR": "/path/to/genomics_data",
    "PIPELINE_DB": "/path/to/genomics_data/pipeline.db",
    "IMAGE_DIR": "/path/to/phenotype_images"
  }
}
```

### WGS：从 FASTQ 开始

```bash
bash environment/install.sh --only agent
conda run -n Agent samtools --version
conda run -n Agent bcftools --version
conda run -n Agent python skills/wgs-expressive-pipeline/scripts/db_query.py --action query-ref
```

`Agent` 环境包含 BWA、SAMtools、BCFtools、GATK 等工具。准备双端 FASTQ、物种匹配的参考 FASTA 与所需索引（如 `.fai`、`.dict`），若使用 BQSR 还需准备已知位点 VCF。使用 `wgs-expressive-pipeline` 前，需要提供项目数据库 `pipeline.db`，并在 `reference_genome` 和 `fastq_samples` 中登记文件路径与样本编号，并在 `pipeline_config` 中配置管线所需参数。安装器不生成数据库或输入数据；上面的查询应返回参考基因组记录。参见 [WGS 工作流的数据布局](skills/wgs-expressive-pipeline/references/workflow.md)。再用同一脚本的 `--action query-samples --samples 101` 检查样本（用你自己的编号替换 `101`）。

在 Agent 中调用 `wgs-expressive-pipeline`，给出已登记的样本编号，先检查它生成的运行参数和脚本，再确认执行。

### GWAS：从过滤后的 VCF 或 PLINK 文件开始

```bash
bash environment/install.sh --only agent  # 已装 WGS 环境可跳过
bash environment/install.sh --only rgwas
bash environment/install.sh --only bins
bash environment/install.sh --only tools
conda run -n Agent plink --version
conda run -n r-gwas Rscript -e 'library(rMVP); library(CMplot)'
command -v gemma gcta64
```

准备过滤后的 VCF，或已有的 PLINK `.bed/.bim/.fam` 文件前缀，以及含样本 ID 的表型表。有协变量时一起提供，确认样本 ID 与基因型文件一致。安装分组会准备 PLINK、R 关联与作图包，以及 GEMMA、GCTA 和 LD 分析工具。独立工具安装在 `~/.local/bin`；如果 Agent 从另一个终端启动，给那个终端也设置 `PATH`。

在 Agent 中调用 `gwas-pipeline`，给出基因型、表型和可选协变量路径，请它先检查样本对齐并生成阶段脚本。

### 表型提取：从已登记的样本图片开始

```bash
bash environment/install.sh --only pheno --cpu  # 用 GPU 时省略 --cpu
conda run -n Phenotype python skills/phenotype-extract/scripts/phenotype_extract.py
```

第二条命令列出已接入的表型。以肌内脂肪（IMF）为例：

1. 在 `pipeline.db` 的 `sample_images` 表登记样本与图片路径，确认 Agent 所在机器可以读取这些图片。
2. 单独准备训练好的 `best_model.pth`。仓库不附带该权重。确保 `models` 表存在名为 `imf_mask2former` 的记录，且 `model_path` 指向该权重的本机绝对路径。
3. 准备 Mask2Former 骨架模型 `facebook/mask2former-swin-small-ade-semantic` 的缓存。若网络可访问模型源，可在首次小样本运行时加 `--allow-download`。先用少量图片验证，再跑完整数据：

```bash
conda run -n Phenotype python skills/phenotype-extract/scripts/phenotype_extract.py \
  --pheno IMF --db "$PIPELINE_DB" --device cpu --limit 10
```

GPU 环境可改为 `--device cuda:0`。需要按基因型样本顺序输出时，可用 `--fam` 指定 `.fam` 文件。

安装完可用 `bash environment/check_env.sh` 检查；只装一条工作流时，其他工作流出现缺件提示是正常的。完整依赖版本、数据库与路径约定见[部署指南](environment/README.md)。

## 常用命令

```bash
cd ~/TraitPilot                         # 仓库根；如按上文使用 venv，需先激活

# 列出本机识别的技能
python run.py list

# 命令行交互对话
python run.py chat           # 可选 --permission ask

# 单轮运行（脚本化）
python run.py chat -p "用 gwas-pipeline skill 说明标准流程，先不要真正跑数据"
python run.py chat -p "列出 phenotype-extract 提供哪些表型"
```

## Web 启动与重启

在仓库根目录、装好依赖并配置凭证后，**任选一种方式**启动。两种命令启动的都是同一个 Flask 服务，网页和聊天 API 共用同一个端口：

| 使用方式 | 启动 | 停止或重启 |
| --- | --- | --- |
| 前台运行，直接看终端日志 | `python run_web.py` | `Ctrl+C` 停止；再次运行命令重启。 |
| 后台运行，日志写入 `web.log` | `./start_web.sh` | `./start_web.sh stop` 停止，`./start_web.sh restart` 重启。 |

`start_web.sh` 在找到名为 `Agent` 的 Conda 环境时会优先用它的 Python，否则用当前的 `python3`。按上面的 `venv` 流程安装时，建议直接运行 `python run_web.py`，确保使用刚装好依赖的解释器。修改 `config.json` 或 Python 代码后，需要重启服务再刷新网页；浏览器刷新不会重新加载后端代码。

- **单文件前端**（无构建、无 node），REST + SSE 流式：聊天气泡、实时工具调用卡片
  （Skill/Bash/Read/Write…）、技能侧栏（点技能可预填调用指令）、模型/权限切换。
- **快捷提问**：输入框上方 4 个预设 chip（列技能 / 说明 GWAS 流程 / 检索参数位置 / 当前会话用量），点一下即发送。
- **实时用量徽标**：顶部随流式更新本轮模型轮次与输出 token（接 M1a `usage` 事件）。
- **停止按钮**：运行中「发送」变「停止」，点按会 abort 本次流并 `POST /api/cancel`，
  协作式终止当前轮次（拒绝所有待确认命令，后台任务不被杀）。
- **后台任务**：以后台模式提交的任务启动后，用户可以继续对话；任务结束且会话空闲时，Web 服务会主动发起一轮结果汇报。
- **导出对话**：一键把消息与工具卡片导出为 Markdown 文件下载。
- **命令审批弹窗**：`权限=危险命令询问`（默认，危险命令才弹）或 `每条命令询问`（==CLI 的
  ask）；点「允许/拒绝」回复 `/api/confirm`，未答复即断开连接会自动拒绝并终止任务。
- **远程访问**：服务默认只监听 `127.0.0.1`。若 Agent 在远程服务器运行，在自己的电脑上执行
  `ssh -L 8787:127.0.0.1:8787 <server>`，再用本机浏览器打开 <http://127.0.0.1:8787>。
- 其他参数：`--port` `--model` `--permission` `--project` `--cwd` `--skills-dir` `--config`。
- 网页后端复用同一 Agent 引擎，仅入口不同：`webapp/server.py`（Flask）+ `webapp/session.py`
  （事件桥 + 命令确认桥，把线程里的 agent 运行接到 SSE）。

## 常用参数

| 参数 | 说明 |
|------|------|
| `-m / --model` | 模型 id（默认 `claude-opus-4-5`） |
| `--project <dir>` | 项目根（含 `skills/`，默认自动向上探测） |
| `--skills-dir <dir>` | 显式技能目录（默认 `<project>/skills`） |
| `--config <file>` | 显式 `config.json` 路径（默认 `<project>/config.json` 或 `./config.json`） |
| `--cwd <dir>` | Bash 等工具的执行目录（默认=项目根） |
| `--permission auto\|ask` | `auto`（默认）：危险命令仅告警；`ask`：每个 Bash 先询问 |
| `--restrict-paths` | 启用工作区路径边界（越界拒绝；默认开）|
| `--write-approval` | 把 Write/Edit 纳入审批门（默认开）|
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
| M8a 权限模型 | allow/deny/ask 规则表、工作区路径边界、写审批 | 默认开；`restrict_paths` / `write_approval` 可分别关闭 |
| M12 摘要压缩 | 命中阈值时保留最近 K 条、更早区间压成结构化前情摘要（保留阶段号/参数/路径/pid/审批结论） | 默认开；`summary_compaction: false` 可关闭 |
| M12 分层记忆 | core.md（稳定前缀）+ archival（SQLite FTS5 检索，注入尾部） | 默认开；`memory: false` 可关闭 |
| M13 会话持久化 | 逐条消息落盘（append-only jsonl），恢复时修复悬挂 tool_use 并截到安全边界 | CLI 和 Web 默认开；`persist: false` 可关闭（`SKILL_AGENT_PERSIST=0` 关）|
| M2 终止契约 | `stop_reason` 分类→决策（continue/repair_truncation/pause_turn/refuse/finalize），透出终止原因 | 恒开（更严格，含截断修复）|
| M1b 预算熔断 | 轮次/墙钟时长/输出 token 上限，发请求前熔断，到点只停调用不杀后台 | 默认不限，`--max-turns`/`--max-seconds`/`--max-output-tokens` |
| M3 死循环熔断 | 无进展循环检测（动作/观测指纹 + meta 进展信号），轮询工具白名单 | 默认开；`loop_guard: false` 可关闭 |
| M4 工具注册表 | 工具以数据登记、可运行时挂载；声明式插件目录（`*.json` 规格） | `--plugin-dir DIR`（默认不加载，防扩大执行面）|
| M10 钩子 | PreToolUse（可阻断/改参）/ PostToolUse（可脱敏）/ 会话事件钩子 | 代码注入 `HookManager`；`--trace` 用示例钩子落盘 |
| #6 可观测性 | 工具调用 trace（工具名/参数摘要/耗时/结果）jsonl | `--trace` 或 `SKILL_AGENT_HOOKS=1` |
| M7 工具治理 | per-tool 看门狗超时；超长结果落盘转存（可继续 Read）；同轮只读工具并行 | `--spill` / `--parallel-tools`（默认开）|
| M9 沙箱 | Bash 执行后端三档：宿主 / stdlib rlimit / docker 强隔离；缺依赖 fail-soft 回退宿主 | 默认 `host`；`--sandbox rlimit\|docker` 或 `SKILL_AGENT_SANDBOX` |
| M11 幂等去重 | 同一次运行内相同「有副作用」工具调用（Bash/Write/Edit）只执行一次，防重复起后台/重复写 | 默认开；`idempotency: false` 可关闭|
| #9 规划/todo | `TodoWrite` 工具把计划外部化为结构化清单（单一 in_progress），长任务可检查 | 默认开；`todo: false` 可关闭 |
| #14 prompt cache | 给稳定前缀（system + 工具定义末尾）打 `cache_control: ephemeral` | 默认开；`prompt_cache: false` 可关闭 |
| #12 模型降级 | 过载重试耗尽后按列表依次切换后备模型 | 默认无，`--fallback-models M1,M2` 或 `SKILL_AGENT_FALLBACK_MODELS=M1,M2` |
| #11 检索 | `Search`（FTS5/BM25 相关性检索，返回 文件:行号+片段）+ `RepoMap`（类/函数/方法/标题大纲）；纯 stdlib，语料=`skills/` 文档 + `skill_agent/`、`webapp/` 源码 | 默认开；`retrieval: false` 可关闭；`--index-rebuild` 强制重建 |

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
- 语料**只取显式根**：项目自带的 `skills/**`（含 references 的 .md/.py/.sh）与源码 `skill_agent/**`、`webapp/**`、`docs/*.md`。
  **绝不整仓遍历**：就地剪枝 `__pycache__/.git/.skill-agent/saved…`，读前按大小（>2MB）与后缀（`.pth/.png/.tfevents…`）拒绝，
  故 `skills/phenotype-extract/model` 下的模型权重（~263MB）不会被 stat。
- **零新依赖**：纯 stdlib（`sqlite3` + `ast`）；不引入 tree-sitter / ripgrep / 向量库。FTS5 缺失或目录不可写时自停用、不阻断。
- 只读工具，`--parallel-tools` 开启时与 Read/Grep 一同并行执行。

## 架构

```
agent/                        # 本目录即仓库根，也是「项目根」（含 skills/）
├── run.py                  # CLI 入口（等效 python -m skill_agent）
├── run_web.py              # Web 入口：python3 run_web.py → http://127.0.0.1:8787
├── config.json.example     # 配置模板（复制为 config.json 填 api_key）
├── skills/                 # 自带技能库（19 个；模型权重 .pth 已 gitignore）
├── requirements.txt        # Agent 本体的 Python 依赖：requests / PyYAML / Flask
├── environment/            # 完整部署环境：README.md（清单/指引）+ check_env.sh（自检）+ Agent.yml/r-gwas.yml
├── skill_agent/
│   ├── config.py           # 配置：环境变量 → config.json → 默认值（不依赖 Claude Code）
│   ├── skills.py           # SkillRegistry：扫描 skills/、解析 SKILL.md frontmatter
│   ├── tools.py            # 工具执行器 + 权限门 + 后台任务（Bash background=true）
│   ├── sandbox.py          # M9：Bash 执行后端（host/rlimit/docker），默认 host
│   ├── idempotency.py      # M11：工具幂等去重（有副作用工具、run 级）
│   ├── todo.py             # #9：计划外部化（TodoWrite）
│   ├── retrieval.py        # #11：FTS5 检索（Search/RepoMap）
│   ├── client.py           # Anthropic Messages API 客户端（SSE 流式 + 重试回退）
│   ├── agent.py            # agentic 循环：tool_use → 执行 → 续推；context 压缩；事件/取消钩子
│   └── cli.py              # list / chat / 会话日志
└── webapp/
    ├── server.py           # Flask：/api/chat(SSE) /confirm /cancel /usage /skills /models /config + 静态页
    ├── session.py          # WebSession：事件桥 + 命令确认桥（线程 ↔ SSE）
    └── static/index.html   # 单文件前端（无构建、纯浏览器 JS，含轻量 Markdown 渲染/停止/导出/用量徽标）
```

### 技能机制

- **发现**：扫描 `<项目>/skills/<name>/SKILL.md`（唯一来源；显式 `--skills-dir` 可覆盖）。
  **不再回退 `.claude/skills`**——只读本仓库自带的技能，避免混入外部技能。
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
默认还提供 `TodoWrite`、`Search` / `RepoMap`；额外插件用 `--plugin-dir` 或 `plugin_dirs` 配置。

## 安全与健壮性

- 危险命令（`rm -rf`、`DROP TABLE`、`dd`、`git push -f`…）命中即告警；`--permission ask` 全部询问。
- 长命令输出自动截断；历史超长按**消息边界**压缩（不拆散 tool_use/tool_result 配对）。
- 限流/过载指数退避重试；流式失败自动回退非流式。
- 模型默认 `claude-opus-4-5`，可用 `claude-haiku-4-5`/`claude-sonnet-4-5` 等（按你的 API 端点而定）。

## 说明

- 分析脚本在运行 TraitPilot 的机器上执行；模型推理请求发送到 `base_url` 所配置的服务。
- 需要更多工具（如 WebSearch）时可按 `tools.py` 中 `Tool` 的写法扩展。
