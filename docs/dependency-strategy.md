# 依赖策略与 M5/M9 落地方案（已按真实代码核校）

> 面向：决定是否引入外部依赖、以及 M5/M9 怎么落的技术负责人。
> 结论先行：**核心保持零新依赖；每种能力放到「适配器 + 可选 extra」后面；缺依赖时 fail-soft；
> 默认关闭。** 本机实测支持该路线（docker 可用、pip 镜像可达、无用户命名空间）。

本版相对初稿的改动，全部来自对 `skill_agent/*.py` 逐文件核读与在本机实测（见 §7「订正清单」）。
最要紧的一条：**`_bash_background` 的 pid 写入是死代码**，必须先修，它同时是 M9 Tier-2 的落点。

> **状态（2026-10-14 落地）**：
> - **M5 MCP 客户端：经决策者判定「不需要 MCP 服务器」，已放弃，不实现。** 下方 §1.1、§2 保留其
>   依赖结论作为记录（若日后重启，仍以「手写 stdio、零依赖」为准）。
> - **M9 沙箱：已实现。** 新增 `skill_agent/sandbox.py`（Host/Rlimit/Docker 三档，纯 stdlib、默认 host、
>   fail-soft），`ToolSet` 新增 `sandbox=` 后端，`--sandbox` / `SKILL_AGENT_SANDBOX` 开关，
>   两个 Bash 启动点已改经后端；`_bash_background` 死代码已修复（后台现在正确写 pid 并 cd）。
>   测试 `tests/test_sandbox.py`（13 项，含 docker 网络隔离与后台判活）。

---

## 0. 现状与本机实测约束（已复核）

`agent/` 当前依赖：`requests>=2.31` + `PyYAML>=6.0` + `flask>=3.0`（`requirements.txt`，3 行）。
**无 `pyproject.toml`，无 `requirements-optional.txt`，`skill_agent/sandbox.py` 与 `mcp_client.py` 均不存在**（green-field）。

| 候选能力 | 实测 | 备注 |
|---|---|---|
| Python | **≥3.10**（实测 3.12.13 / 3.13.9） | 初稿误写 3.11 |
| `pip download mcp` | ✅ mcp 2.3.0 可下 | 但见下一行 |
| `pip install --dry-run mcp` | ❌ **~30 个包闭包** | 见 §1.1，决定性证据 |
| `node` / `npx` | ✅ v24.16.0 / 11.13.0 | 多数 MCP server 走 npx |
| **docker** | ✅ client 26.0.0 / server 24.0.5 | `docker run --rm --network none python:3.11-slim echo hi` → `hi`；uid 1005 已在 `docker(985)` 组 |
| bubblewrap / firejail / nsjail / podman | ❌ 未安装 | |
| `unshare` 用户命名空间 | ❌ | `max_user_namespaces=0`；el7 内核 3.10，`--map-root-user` 报 `Invalid argument` |
| `systemd-run --user` | ❌ | 无 D-Bus |
| stdlib `resource` | ✅ | `RLIMIT_AS/CPU` 可设；注意 `RLIMIT_NPROC` 软限**本机已是 4096**，非无限 |

**关键推论**：无特权命名空间 → bwrap/firejail 一类「无 root 轻隔离」在本机**根本不可用**，装了也没用。
可用的隔离手段只有两条：**stdlib rlimit（弱，零依赖）** 与 **docker（强，且已具备）**。

---

## 1. 通用原则（两条都要遵守）

1. **核心零新依赖**：`skill_agent/` 的核心模块（agent 循环、tools、permissions、stops、loopguard、
   validation、config）继续只用 stdlib + 现有三件。新能力一律隔离在**独立适配器模块**里。
2. **适配器契约**：
   - **惰性导入**：`import resource` / docker 调用 / `import mcp` 放在函数内部，模块顶部不 import 可选依赖；
   - **fail-soft**：依赖缺失或不可用时，返回一条清晰可见的「能力不可用」结果，**绝不抛异常、绝不崩主循环**；
   - **默认关闭**：opt-in（config / env / CLI），未开启时行为与今天**逐字节一致**；
   - **有降级层**：能就近用更低依赖的实现就不上重依赖（见 M9 三层）。

**只挂载到既有缝，不改治理顺序**：`ToolSet.execute`（`tools.py:131`）里
`hooks → permission → 权限门 → tool.executor(args)`（`:171`）是**载荷契约**，M5/M9 都不得插进这段。
唯一允许的落点：
- M5 → `ToolSet.register_tool(tool, source="mcp:<server>")`（`tools.py:123` → `registry.py:30`）；
- M9 → 只替换 Bash 的**两个**外部进程启动点（`tools.py:255` 前台、`tools.py:342-345` 后台）。

---

## 2. M5 — MCP 客户端：**手写最小 stdio 客户端，零新依赖**

### 2.1 为什么不上 SDK（决定性）
`pip install --dry-run --ignore-installed mcp` 在本机解析出 **30 个包**：
`httpx2-2.13.1`（**与已装的 httpx 0.28.1 是不同包**）、`mcp-types-2.3.0`、`opentelemetry-api-1.45.1`、
`starlette-1.8.0`、`uvicorn-0.54.0`、`sse-starlette-3.5.0`、`PyJWT[crypto]-2.15.1`、`cryptography-50.0.2`、
`pydantic-2.14.1`、`jsonschema`、`anyio`……**已装的 pydantic/httpx/anyio 并不能满足该闭包**。
MCP stdio 传输就是「换行分隔的 JSON-RPC 2.0」，用 stdlib（`subprocess`+`json`）约 200 行可实现。
**决策：手写 stdio；保留 `McpTransport` 缝，日后确实要 HTTP/SSE 再以 extra 引入 SDK。**

### 2.2 新增 `skill_agent/mcp_client.py`（仅 stdlib）
- `class McpConnection(server_cfg, timeout=60)`：
  - `Popen([command,*args], stdin=PIPE, stdout=PIPE, stderr=<logfile>)`——**stderr 必须重定向到文件，
    不能 PIPE**（话痨 server 会写满管道死锁）。
  - 守护读线程：`readline → json.loads`，响应按 `id` 路由到 `pending[id]=(Event, slot)`，忽略通知；
    容忍非换行帧（坏行跳过记日志，不杀连接）。
  - `_rpc(method, params, timeout)`：加锁写 `json.dumps+"\n"`，`Event.wait(timeout)`——**这是 M5 自带的
    超时**（见 §7：M7 per-tool 看门狗**不继承**给非内置工具，必须自兜）。
  - `initialize()`（`protocolVersion="2024-11-05"`）→ `notifications/initialized` → `list_tools()` →
    `call_tool(name,args)`：`result.content[]` 文本块 → `ToolResult`，`isError` → `is_error=True`。
  - EOF/崩溃 → `_dead`，所有 pending 失败返回「MCP 连接已断开」。
  - `close()`：关 stdin → 宽限 → `terminate()` → `kill()`。
- `class McpManager(servers)`：逐 server **fail-soft** 连接；`tools()` 为每个远端工具生成一个 `Tool`，
  名字**命名空间化** `mcp__<server>__<remote>`（`register` 按 name 存储，不命名空间会**静默顶掉内置工具**），
  `input_schema = 远端 inputSchema or {"type":"object"}`；`mount(tools)` → `register_tool(t, source=f"mcp:{server}")`。
- `mount_mcp_servers(tools, cfg)` / `load_mcp_config(path)` / `get_manager(cfg)`（`id(cfg)` 缓存 + `atexit` 清理）。

### 2.3 配置与接线
- `config.py`：新增 `mcp_servers: list|None = None`；在 `Config.load` 的唯一 env 读取处（`:120` `plugin_dirs` 旁）
  读 `SKILL_AGENT_MCP_CONFIG`(JSON 文件路径) / `SKILL_AGENT_MCP_SERVERS`(JSON 串)。
  **不要用 `_env_list`**——它按 `:` 切分，会毁掉 JSON 与绝对路径。
- `cli.py`：`_common_opts` 加 `--mcp-config PATH`（单值，仿 `--resume`）；override 块仿 `--plugin-dir`；
  插件加载后调用 `mount_mcp_servers(tools, cfg)`。
- `webapp/session.py`（`:119-123` `ToolSet(...)` 之后）也要挂载——**webapp 没有 argparse**，
  只写 CLI 会让网页端静默失效，靠 Config/env 承载。

### 2.4 继承关系（核读结论，别照抄初稿）
`register_tool` 挂载后：**M6 校验、M8a deny/ask、M10 钩子、M7 落盘转存** 均**自动继承**（`execute` 对
每个工具一视同仁）。**但 M7 per-tool 看门狗超时不继承**——`_attach_timeouts`（`tools.py:324-331`）
跳过 `name=="Bash"` 且跳过 `source!="builtin"` 的工具，故 MCP executor 必须**自兜超时**。
另：危险命令扫描硬编码只在 `name=="Bash"`（`tools.py:149`），远端工具要约束只能靠 M8a 通配规则或 M10 钩子。

---

## 3. M9 — 沙箱：**三层运行时适配器，默认 Tier 0**

新增 `skill_agent/sandbox.py`（仅 stdlib），抽象三档，仅拦截 Bash 的两个启动点：

| 档 | 依赖 | 隔离强度 | 内容 | 本机 |
|---|---|---|---|---|
| **Tier 0 宿主** | 无 | 无 | 现状逐字节复现 | ✅（默认） |
| **Tier 1 rlimit** | **stdlib `resource`** | 弱 | `preexec_fn` 限 CPU/AS/NOFILE；清 env | ✅ |
| **Tier 2 docker** | docker（已具备） | 强 | `--network none`、只读/挂载、`--memory/--cpus`、`--user` | ✅ |

- 接口：`SandboxBackend.name / available() / fg(command,cwd,timeout) / bg(command,cwd,task_id,pidf,log)`。
  `HostBackend.fg` 必须返回与 `tools.py:255-258` **完全相同**的 `(argv, kwargs)`
  （`shell=True, executable="/bin/bash", cwd=..., timeout=...`）。
- `ToolSet.__init__` 加 `sandbox=None`（惰性 import，默认 `HostBackend()`）。**`execute()` 不动。**
  `_attach_timeouts` 本就跳过 Bash，故沙箱包 Bash executor **无重复包装冲突**。
- `build_backend(cfg)`：探测 `shutil.which("docker")` + `docker info`，永不抛；默认 fail-open 回宿主并打一行提示
  （`--sandbox-strict` 可改 fail-closed）。
- **Tier 1** 零依赖、只加资源上限，建议作为「低成本默认增强」候选，但**会改变语义**（超 AS 的命令会失败），
  故仍 opt-in，阈值宽松（如 AS=4G、CPU=不限、NPROC 保持现状 4096）。

### 3.1 Tier-2 后台 `.pid` 风险与解法（唯一真风险点）
**背景**：`TaskOutput`（`tools.py:563-572`）靠读日志同目录的 `pid` 文件 + `os.kill(int(pid),0)` 判存活；
编排 skill 自己也用宿主 Bash `kill -0 $(cat RUN_DIR/.pid/...)` 判「运行中」。二者都是**宿主 PID 命名空间 + 宿主 FS** 检查。

**解法（选定）：宿主监督进程 pid + 前台 `exec docker run --rm`。**
`_bash_background` 的 `command.sh` 写成：`cd "<cwd>" && echo $$ > "<pidf>"` 然后
`exec docker run --rm --name skillagent-<task_id> <flags> -v <cwd>:<cwd> <mounts> <image> /bin/bash -c <json(command)>`。
写下的 `$$` 是**宿主监督 shell 的 pid**，`exec` 把它换成前台 docker 客户端，其生命周期 = 容器生命周期；
于是 **TaskOutput 无需改动**即可正确判活。拒绝 `--pid=host`（等于丢弃隔离）。
（备选 `docker run -d` + `docker inspect` 能扛重启，但要动 TaskOutput，且**仍不解决 skill 自己那套宿主 `kill -0`**。）

**残余语义**：即便按上面挂载，容器内脚本写的 `RUN_DIR/.pid/<stage>.pid` 仍是容器 pid、宿主不可见。
但**编排的完成真值是 `.done` 文件存在**（`gwas-pipeline/SKILL.md:83-88`、`wgs-expressive-pipeline/SKILL.md:83-85`
「完成状态以 .done 文件为准」）。**只要把 `RUN_DIR` 以相同宿主路径 bind-mount**，`.done` 就落在宿主，
自动推进 / `--resume` 照常；仅 skill 自带的「运行中」显示会退化——这是可接受的已知代价。
需挂载：宿主 cwd、`RUN_DIR`（`run.params.json` 的 `run_dir`，如 `$GENO_DIR/temp/runs/...`）及工具所在目录（工具用 `command -v` 运行时探测）。

---

## 4. ⚠️ 必须先修的既有缺陷：`_bash_background` 的 pid 写入是死代码

`tools.py:341` 构造了 `script = f'cd "{cwd}" && echo $$ > "{pidf}"\nexec bash -c <cmd>'`，
**但 `script` 从未被使用**：`:339` 写进 `command.sh` 的是**原始 command**，`:342-345` 的 `Popen` 直接跑
`command.sh`（且**没传 `cwd=`**）。后果（今天就在发生）：
1. 后台进程**从不写 pid 文件** → `TaskOutput` 的 `if pidf.is_file()` 恒 False → `pid_alive` **恒为 `None`**；
2. 后台任务跑在**harness 进程 cwd**，`cd "{self.cwd}"` 从未生效。

**这不是 M9 的锅，是现状缺陷。** 而 §3.1 的 Tier-2 设计**恰恰依赖 `echo $$` 真的执行**。
故必须把它作为 **Stage 0 前置修复**：让 `command.sh` 写入 `script`（而非原始 command），
并让 `Popen`（或 `sandbox.bg`）带上正确 cwd。修完 Tier 0 行为才与「本应如此」一致。

---

## 5. 依赖声明与 ADR

- **`requirements.txt` 不动**（仍 3 行）；**不加 `pyproject.toml`**。
- 新增 `requirements-optional.txt`：
  ```
  # 可选：仅当需要 MCP 的 HTTP/SSE 传输时（stdio 传输为手写实现，无需此依赖）。
  # 注意：安装 mcp 会拉入约 30 个包（httpx2/mcp-types/opentelemetry-api/starlette/
  # uvicorn/sse-starlette/pyjwt[crypto]/cryptography/pydantic 等），且并非已装依赖的可满足子集。
  # 默认不安装。
  # mcp>=2.3
  ```
- **docker 是系统依赖**，非 pip 依赖：README 说明 + `sandbox.py` 启动探测时明确提示，不写进 requirements。
- **ADR**：核心 harness 不得在模块顶层 import 任何可选依赖；可选 import 只允许出现在
  `mcp_client.py` / `sandbox.py` 内且惰性执行。
- **把它变成机制而非口号**：新增 `tests/test_no_optional_imports.py`，AST 扫描 `skill_agent/*.py`
  顶层 `Import/ImportFrom` 是否命中 denylist（`mcp`、`docker` 及一切超出 `{stdlib, requests, yaml, flask}` 的包），命中即 fail。

---

## 6. 决策矩阵与执行顺序

| 问题 | 建议 |
|---|---|
| M5 用 SDK 还是手写？ | **手写 stdio（零依赖）**；HTTP/SSE 需要时再以 extra 引入 SDK |
| 现在装 `mcp` 吗？ | **不装**。~30 包闭包，核心不需要 |
| M9 用 docker 还是 bwrap？ | **docker**（bwrap/firejail/nsjail 本机不可用）；另加 stdlib rlimit 轻档 |
| 默认开沙箱吗？ | **不开**。默认 Tier 0；Tier 1 建议尽快开；Tier 2 按需 |
| 会破坏编排管线吗？ | Tier 0/1 **不会**；Tier 2 **在**宿主监督 pid + 同路径挂载下也不会，否则后台轮询退化 |
| 远端工具超时谁管？ | **M5 自兜**（`Event.wait`），M7 看门狗不继承 |
| 远端工具会顶掉内置吗？ | 不会，用命名空间 `mcp__<server>__<tool>` |

**Stage 0（前置修复）**：修 `_bash_background` 死代码（`tools.py:339-345`），后台任务正确 `cd` 并写真 pid。
测试：仿 `test_meta.py`——后台 `echo $$` → `TaskOutput` 报 `pid_alive` True→False。
门禁：前台 Bash 行为不变；`test_registry.py` 工具目录断言保持绿。

**Stage 1（M5）**：`tests/test_mcp_client.py`——临时目录写桩 stdio JSON-RPC server；
断言：工具以 `source` 前缀 `mcp:` 挂载、出现在 `schemas_for`、`execute` 转发返回 `ToolResult`、
M6 校验生效（缺参 → `is_error`）、缺命令 fail-soft 不抛、慢 server 触发自兜超时。**默认工具集不加 MCP 工具。**

**Stage 2（M9 Tier 1）**：`tests/test_sandbox.py`——Tier 0 输出与 meta 逐字节一致；
Tier 1 超 AS 命令失败（OOM）；无 docker 时 `build_backend` 警告并回 Host。

**Stage 3（M9 Tier 2）**：无 docker 则 skip。断言：前台 `--network none` 断网；
**后台长 `sleep` 经 `TaskOutput` 报 `pid_alive=True` 后转 False**（这条就是 §3.1 的回归证明）；
挂载 cwd 内写 `$RUN_DIR/.done/x.done` 宿主可见；默认工具目录不变。

**Stage 4**：纯 stdlib 收尾——M11 幂等 + #9 todo + #14 prompt cache（与依赖无关）。

---

## 7. 订正清单（初稿中被核读推翻/收窄的说法）

1. **推翻：「Docker Tier-2 会因容器内 `.pid` 宿主不可见而破坏编排管线。」** 现状根本没有沙箱
   （`sandbox.py` 不存在）。更要紧的是 **pid 文件今天就没写成功**（§4 死代码），`pid_alive` 现状恒为 `None`。
   准确表述：*「未来的 Docker Tier-2 **会**破坏宿主 TaskOutput 判活，除非先设计宿主可见的 PID/日志映射；
   且当前的 pid 写入本身就是 no-op，应作为前置修复。」*
2. **收窄：「bwrap/firejail/nsjail 不可用，rlimit 是唯一无依赖隔离。」** 前半句成立；后半句误导——
   **docker 同样可用且是推荐强后端**。准确表述：*可用隔离只有两条——stdlib rlimit（弱）与 docker（强，已具备）。*
3. **收窄：「MCP 工具无额外接线即继承 M6/M8a/M10/M7。」** M6/M8a/M10/M7 落盘**确实继承**；
   **M7 per-tool 看门狗超时不继承**（`_attach_timeouts` 跳过非内置），须自兜；且危险扫描只认 `Bash`。
4. **收窄：「M9 Tier-2 实现前需先做 `.pid/.done` 映射设计。」** 确认是前置，但本方案用
   **宿主监督 pid（前台 `exec docker run --rm`）** 解决，TaskOutput 无需改动；编排以 `.done` 为准，
   只要 `RUN_DIR` 同路径挂载即不退化。
5. **错误订正**：初稿「mcp 几乎免费（pydantic/httpx/anyio 已装）」**错误**——dry-run 解析 30 包并需要
   `httpx2`（≠ 已装 httpx）、`mcp-types`、`opentelemetry-api`、`starlette/uvicorn/sse-starlette`、`pyjwt[crypto]`。
   这把「手写 stdio」的结论**进一步夯实**。另：Python 是 **3.13.9**，非 3.11。
