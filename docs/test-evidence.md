# TraitPilot 功能测试证据文档（用于审查）

> 用途：逐条记录**每个测试测的是什么、怎么测的、实测结果是什么**。与 `docs/test-matrix.md`（用例清单）配套——
> matrix 说「该测什么、判据（反例）是什么」，本文档记「实际跑出来什么」。
> 维护约定：**后续每跑一条测试就往本文档追加一条，不再另行确认。**

- 生成日期：2026-10-20
- 被测代码：`/home/chenyuxi/skill/agent`（HEAD 工作区，非 git 仓库）
- 运行环境：
  - Agent 环境（跑 CLI / 探针）：`/home/chenyuxi/anaconda3/envs/Agent/bin/python`
  - base 环境（跑 pytest / Playwright）：`/home/chenyuxi/anaconda3/bin/python`
  - Web：`http://127.0.0.1:8787`
- 结论词表：**具备** / **不具备** / **部分具备**（含「不具备（已知边界）」= 代码本就如此设计，不是回归）
- 判据原则（承 matrix）：**只有在反例未出现时才算具备**；「跑完没报错」不算证据。

---

## 0. 本轮总览

**批次 1（2026-10-20 上午）**

| 批次 | 方法 | 条数 | 具备 | 不具备 / 边界 | 证据位置 |
|---|---|---|---|---|---|
| 3a 离线探针（植入 stub，不发网络） | 进程内直接调库 | 16 | 13 | 3 | `/tmp/agent_probes/logs/<ID>.txt` |
| 3a SKILL 模块探针 | 进程内直接调库 | 7 | 6 | 1 | 同上 |
| 3b CLI 端到端（真打模型） | 子进程跑 `run.py` | 8 | 7 | 1 | 本文件 §2 |
| 3c Web SSE（curl 直打 API） | HTTP | 12 | 12 | 0 | 本文件 §3 |
| 3d 昂贵档（docker / kill -9） | 外部进程 | 5 | 5 | 0 | 本文件 §4 |
| **批次 1 合计** | | **48** | **43** | **5** | |

**批次 2（2026-10-20 下午）：matrix 剩余 79 条，全部经对抗性复核**

| 批次 | 方法 | 条数 | 具备 | 部分具备 | 已知边界 | 证据位置 |
|---|---|---|---|---|---|---|
| 3a–3c 混合（stub / 真实 ToolSet / 真打模型 / 临时 web） | 8 组探针 + 8 组独立对抗复核 | 79 | 71 | 7 | 1（HOOK-13） | §8 |
| 3a–3c FEAT 模块（TodoWrite / Search / RepoMap / 索引 / 降级重试） | 4 组探针 + 4 组独立对抗复核 | 9 | 8 | 1（FEAT-12） | 0 | §9 |
| **批次 2+3 合计** | | **88** | **79** | **8** | **1** | |

**⚠️ 对「132」的订正（2026-10-20 18:20）**：早先的「三批合计 132」是用 `48 + 88 − 重复计 + 补跑 2` 这类口头算术凑的，**未对用例 ID 做双向差集**，因而**整段漏掉了 `M13-03 … M13-10` 共 8 条**（会话持久化后半段）。用 `grep -oE '...-[0-9]{2}'` 抽 matrix 全部用例 ID 与 evidence 出现过的 ID 做 `comm -23` 差集才发现。

**订正后的口径**：matrix 去重后共 **142 条真实用例**（143 个 ID 中 `SKILL-12` 只是「推荐执行顺序」里的注释标签、非用例）。现**142 条全部执行完毕**：批次 1（50）+ 批次 2（79）+ 批次 3（9）+ **M13 补跑（8）** − 批次 1 与批次 2 的 4 条重复计 = 142。

**批次 4（M13-03…M13-10 补跑，2026-10-20 傍晚）**

| 批次 | 方法 | 条数 | 具备 | 部分具备 | 证据位置 |
|---|---|---|---|---|---|
| M13 补跑（会话持久化恢复/安全/归档/删除/历史回放） | 8 组探针 + 8 组独立对抗性复核 | 8 | 6 | 2（M13-09、M13-10） | §10 |

**非「具备」总计 16 条**：14 条「部分具备」+ 2 条「不具备（已知边界）」（HOOK-13、PERM-07）。其余 126 条判「具备」，每条都附可复现的探针脚本与含数字的证据串。

**对批次 1 的更正**：
- **D4 已闭合** —— MEM-12 的浏览器端到端在批次 2 真跑了：备份 `config.json` → 加 `"memory":true,"summary_compaction":true` → `start_web.sh restart` → 发一轮 → `os.path.exists` 得 **`archival_db_exists = True`**（复核方逐字节备份并还原了 config.json）。MEM-12 结论由「部分具备」升为**具备**。
- **CORE-05 / CORE-10 已补跑**（见 §2、§3）：CORE-05 磁盘哨兵 `/tmp/core_loop_proof.txt` 内容 `HELLO_LOOP`；CORE-10 原始 SSE 里 `tool_start`/`tool_end` 各 1（`{"name":"Bash","is_error":false,"result":"$ echo SSE_OK\nSSE_OK"}`）。

**两批新发现的实际缺陷（非已知边界）**：

| 缺陷 | 位置 | 现象 | 影响 |
|---|---|---|---|
| **D1** CLI 无子命令时崩溃 | [cli.py:246](skill_agent/cli.py#L246) | `/…/python run.py`（不带 `chat`/`list`）→ `AttributeError: 'Namespace' object has no attribute 'prompt'` | 顶级 `sub = add_subparsers(required=False)` 允许无子命令走到底，`args.prompt` 未定义。顶部用法字符串写的是 `python agent/run.py ...`，易踩 |
| **D2** 幂等键把 `description` 算进去 | [idempotency.py:53-58](skill_agent/idempotency.py#L53-L58) | 两条**命令相同、description 不同**的 Bash 调用被判为不同键 → 都执行（matrix GUARD-09 的预期是「只有 1 行」，实测 2 行） | 键 = 工具名 + **全部参数**的规范化 JSON（含 `description`），[tests/test_idempotency.py](tests/test_idempotency.py) 把这个行为固化为通过用例。但模型只要改写 description 就绕过去重。**部分具备** |
| **D3** MEM-08 只抽到关键词、抽不到值 | [agent.py:479-491](skill_agent/agent.py#L479-L491) | `re.findall(r"\b(task_id\|pid)\s*[=:]\s*\S+")` 只返回捕获组 → archival 里存的是 `'pid'` / `'task_id'` 字符串，**不含** `12345` / `T7` | 路径与 `.done` 标记抽对，pid/task_id 的**值**丢失 |
| **D5** LoopGuard 轮询白名单是**纯子串匹配**，副作用死循环被漏放 | [guard.py](skill_agent/guard.py)（`is_polling`） | `echo hello.log`、`mv x.log y`、`rm -f stale.pid` 全被判为「轮询」；`rm -f stale.pid` 连按 15 次 `level` 恒为 `warn`、**永不 HALT** | matrix GUARD-02 字面判据（查 `ls -la`）能过，但「白名单不过宽」的结论被证伪：含 `.log/.done/.pid` 子串的**危险**重复命令不会被熔断 |
| **D6** 结构化 deny 无视 allow 与规则顺序 | [permissions.py:122](skill_agent/permissions.py#L122) | `is_denied` 遍历**全部**规则只看 `action=="deny"`；`execute` 又先调它 → `rules=[allow ^echo, deny ^echo]` 时 `needs_confirm=(False,'')` 但 `is_error=True` 被拒 | 与模块自称「按序首次命中」矛盾；allow 规则形同虚设 |
| **D7** config.json 的 `permission_rules` / `extra_roots` 被静默丢弃 | `PermissionModel.from_config` + [config.py](skill_agent/config.py) `_apply_file_overrides` | 读回 `None`；`from_config` 后 `rules=[] extra_roots=[]` | 规则与额外根**只能代码传入**，配置文件里写了没用（同 CLAUDE 记忆里已登记的「纸面键」） |
| **D8** M6 校验对「数字传成字符串」过度拦截 | [validation.py](skill_agent/validation.py) | `validate_args(BASH, {"timeout":"5"})` → `期望类型 integer，实际为 string` 报错；docstring 自称「避免过严…如把数字传成字符串」**未兑现** | 模型常把数字写成字符串，会被误拒 |
| **D9** archival 检索要求 ≥3 字连续子串重叠 | [memory.py](skill_agent/memory.py) `search` | 「继续执行阶段 S3 那个任务」→ `[]`；「阶段 S3」/「rMVP」/「grm」→ 命中；「S3 任务」/「输出文件在哪」→ `[]` | 模糊自然语言 query 普遍召回不到；注入路径本身工作 |
| **D10** CLI 检索的 `--index-dir` 指到项目外时语料为空、建出空库 | [retrieval.py:388-405](skill_agent/retrieval.py#L388-L405) `_corpus_roots` | `--index-dir /tmp/idx_probe` 上溯不到语义 project → `[]` → 打印「0 文件 / 0 建立」、DB 内 `files=0 chunks=0`（机制真、只是选了个没语料的路径；改到项目内得「101 文件 / 101 建立」） | 用户按字面把索引目录指到项目外会静默得到空索引 |
| **D11** spill 大文件的「Read 分段续读」未实测 | [tools.py](skill_agent/tools.py) spill | 落盘文件名真、内容完整（38905B 未截断）、`meta["spilled"]` 真存在；但续读只用 `limit=5` 的小文件验过，从未对真正 >30k 的落盘文件跑 offset+limit | 能力标注「部分具备」，只差这一条实测 |
| **D12** env 与 config.json 的优先级**方向反了** | [config.py](skill_agent/config.py) `Config.load` 先赋 env 值，随后第 304 行 `_apply_file_overrides(cfg, file_cfg)` 用 config.json 覆盖 | config.json 同名键 `false` + env `1` 时，`todo`/`retrieval`/`prompt_cache`/`parallel_tools` **4/4 仍是 false**；`fallback_models`/`index_dir` 也是 file 赢 → **6/6 env 未能压过 config.json**（env-only 时 4/4 生效，机制本身可用，只是方向反了） | 与文档承诺的「环境变量 > config.json > 代码默认」相反。**pytest 全绿掩盖了它**：`test_config_json.py::test_env_overrides_config_file` 只测 `api_key`（走另一条真优先级路径），toggle/list 键的优先级从未被测 |
| **D13** `SessionStore.delete()` 不持 `append` 的锁、且不处理别名 → 「删除」宣称的语义不成立 | [session_store.py:209](skill_agent/session_store.py#L209) `delete`（全程无 `with self._lock`，`append` 有） | ①**竞态**：一个线程持续 `append`，主线程 `delete`，实测 **4090/5000** 次 `delete` 返回 `True` 后 0.5ms 内文件已被 writer 重建；②**符号链接**：`<sid>.jsonl` 为指向 root 外真实文件的 symlink 时，`delete→True` 但只删链接，**被指向文件原样留在磁盘**（`outside.exists=True`、内容完整） | `delete` 返回 `True` 不等于「磁盘上不再存在该内容」。代码无重命名/回收站（这点是真 unlink），但非原子 + 不 resolve 别名，故文档不宜用「永久删除」。**部分具备**（探针只测了唯一标准路径的串行删除，故仍判具备；此项为复核方对抗场景发现，主测方已独立复现） |
| **D14** `load_view` 的 `resume_count` 在「block 级悬挂」输入下退化为 `len(display)`，前端据此画不出分隔条 | [session_store.py:333-344](skill_agent/session_store.py#L333-L344) `_boundary_index` 兜底 `return len(display)`；[index.html:590/593-597](webapp/static/index.html#L590-L597) | 当某条**已完成**的 assistant 消息内含「无对应 `tool_result` 的 `tool_use` 块」（并行工具批次被中断/部分回答的残留）时，`_analyze` 清洗后的 strict 前缀与 display 消息不再逐字相等 → 按序对齐失败 → `resume_count == len(display)`。实测：golden 场景 `display=4 resume_count=2`（正确）；block 级悬挂场景 `display=6 **resume_count=6**`（应为 4），Playwright 断言「『尚未完成』分隔条」**FAIL**（notice 仍在、分隔条缺失） | 同一份前端在两种真实输入下行为相反：分隔条对 `resume_count` 的依赖在边界输入下塌陷。**部分具备**（golden path 正确、接口确走 `load_view`，故不判「不具备」） |

---

## 1. 离线探针（3a）

方法：`/tmp/agent_probes/run_offline.py`、`run_skill.py`，用 `@run(tid)` 装饰器逐条捕获 verdict + 证据串 + stdout，落在 `/tmp/agent_probes/logs/<ID>.txt`。
**这些探针不发网络**：`CORE-*` 用假 `requests.post`/假 `iter_lines` 灌 SSE 帧；`PERM-*`/`HOOK-*` 用临时目录 + 哨兵文件；`M13-*` 用临时 SessionStore。

| # | 测什么 | 怎么测 | 实测结果（证据） | 结论 |
|---|---|---|---|---|
| CORE-02 | 流式增量是**逐块**回调，不是攒完一次性给 | 假响应体吐 3 帧 `content_block_delta`（"你"/"好"），断言 `on_delta` 调用次数 ≥2 且拼接 == "你好" | `on_delta 回调 2 次 ['你','好']`；`blocks[0].text='你好'`；`stop=end_turn tok=2` | **具备** |
| CORE-03 | `--no-stream` 是**真非流式**（body 里 `stream:false`，且不调 on_delta） | 抓 `requests.post` 的 payload，断言 `stream is False`、`on_delta` 未被调用 | `payload.stream=False`；`on_delta 被调用=[]`；`blocks=[{'type':'text','text':'hi'}]` | **具备** |
| CORE-04 | thinking 与正文**分流**到不同回调、不同 block | 灌 `thinking_delta` + `text_delta` 交替帧，断言两个回调各拿各的、block 类型不乱 | `on_delta=['你好'] on_thinking=['想一下'] blk0.type=thinking blk1.text='你好'` | **具备** |
| CORE-11 | 取消是**协作式**：置 `cancel_event` 后一次 client 调用都不发 | 置 event 后调 `answer()`，数 `client.create` 调用次数 | `answer='（任务已取消）' client.create 调用=0` | **具备** |
| STOP-12 | 模型声明 `stop_reason=tool_use` 但**没有可执行块**时不空转 | stub client 每次都返回 `tool_use` 却无 tool 块，断言只调 1 次且尽快收尾 | `client.create 调用=1（期望 1）answer='x' 耗时=0.00s` | **具备** |
| PERM-06 | 开 `restrict_paths` 后**越界 Write 真不落盘** | 临时根 `/tmp/xxx`，Write 目标 `/tmp/probe_escape_xyz.txt`，查 `is_error` + **磁盘真值** | `is_error=True`；`磁盘文件存在=False`；content=`[异常] Write：ToolError: 路径越出允许的工作区边界：/tmp/probe_escap…` | **具备** |
| PERM-07 | 路径边界是否也管住 Bash | 同上环境跑 `Bash: cat /etc/hostname`，看输出里是否含宿主机真实主机名 | `is_error=False`；输出含 `/etc/hostname` 真值(`seedlab1`)=**True** | **不具备（已知边界）**：`restrict_paths` 只护 Read/Write/Glob/Grep，Bash 一行绕过 |
| PERM-10 | `permission=auto` 下危险命令的行为 | 跑 `touch`（普通）与 `rm -rf`（危险），比 `is_error` | 危险命令 `is_error=False`（=仅告警、仍执行）；普通命令 `is_error=False` | **具备**（auto 意即只告警；要阻断需 `ask`） |
| M13-02 | 日志三件套：meta 只写一次、非法对象静默丢弃、`enabled=False` 降级 | 同 sid 连续 `append_meta` 两次（第二次内容不同）；`append` 一个含 `set` 的非法消息；对 `enabled=False` 的 store 读写 | `meta 只写一次=True`（首行仍 `"model": "m"`，`CHANGED` 不出现）；`非法对象被静默丢弃=True(行数 2)`；`disabled 降级=True` | **具备** |
| HOOK-10 | 参数校验发生在钩子**之前**（畸形参数不该惊动钩子） | 注册 `pre_tool_use` 返回 `'BLOCK'`，然后调 `Bash({})`（缺 `command`） | 命中 `'[输入校验失败] 参数: 缺少必填字段 \`command\`'` → 钩子未收到调用 | **具备** |
| HOOK-11 | trace 钩子真落盘且带 `duration_ms` / `meta` | `build_trace_hooks('SID', dir)`，手调 pre/post，读 `SID.jsonl` | `落盘=True 行数=2 post.duration_ms=0`；`meta={'exit_code': 0}` | **具备** |
| HOOK-12 | trace 写盘失败**不炸主流程** | 把 trace 目录设成 `/proc/nonexistent/ro`（必失败），调 pre/post | 未抛异常（`OSError` 被 `_write` 吞掉） | **具备** |
| SBOX-07 | rlimit 后台脚本带 CPU 时间上限前缀 | 生成后台包装脚本，检查首行是否以 `ulimit -t` 开头 | 脚本=`'ulimit -v 4194304; ulimit -t 1; cd "/tmp" && echo $$ > "/tmp/pid"\nexec bash -c "cmd"\n'`；`含 ulimit -t 1: True` | **具备**（首跑因断言写死「必须在首位」误判为不具备，核对全文后更正：`ulimit -t 1` 确实存在，只是前面还垫了 `ulimit -v`） |
| MEM-09 | core 记忆有长度上限，不会被灌爆 | `set_core('A'*5000)` 后读回长度 | `读出 len=4000`，上限 `MAX_CORE_CHARS=4000` | **具备** |
| MEM-08 | 阶段事实抽取进 archival | 调 `_remember_phase_facts("阶段 S3 输出 /p/grm.grm，后台 task_id=T7 pid=12345，完成标记 /p/x.done")`，看 `all_items()` | 抽到 `/p/grm.grm`(kind=path)、`/p/x.done`(kind=marker)、**`'task_id'`**、**`'pid'`**（kind=task） | **部分具备** → **D3**：pid/task_id 只抽到**键名**，值 `12345`/`T7` 丢失（`re.findall` 只返回捕获组） |
| USAGE-04 | 畸形 token 数被钳位，不污染统计 | 依次 `_record_usage(-5)`、`('x')`、`(3)`，看 usage_bar | `usage_bar={'steps':3,'output_tokens':3,'latency_ms':1}`（-5 与 'x' 各计 0） | **具备** |
| SKILL-04 | 畸形 YAML 安全降级；非法 name 回退目录名 | 临时 skills 下放 `name: bad name!!` 与 `: : bad yaml :` 两个 SKILL.md | `keys=['broken','gooddir']`（回退目录名，未出现 `bad name!!`）；`broken.frontmatter={}`；`broken.description='技能 broken'` | **具备** |
| SKILL-05 | 只扫顶层；裸 `.md` 算技能；无 SKILL.md 的目录跳过 | 造 orchestrator/sub 嵌套、noskel/笔记、loose.md | `names=['loose','orchestrator']`；无 `nested`（不递归）、无 `noskel` | **具备** |
| SKILL-06 | `Skill(name)` 返回**正文全文**，不是一句「已加载」 | `ts.execute("Skill", {"skill":"gwas-format-convert"})`，拿源文件末行比对是否出现在返回里 | `is_error=False len=3800`；源正文末行**出现于返回=是**；含「Skill 目录文件清单」 | **具备** |
| SKILL-07 | 未知技能名 / 空名 / 缺参数三种失败各给**不同**提示 | 分别调 `{skill:"does-not-exist"}`、`{skill:""}`、`{}` | 三条全 `is_error=True`；首行分别 `没有名为 \`does-not-exist\` 的 skill。可用：…` / `可用 skill：…` / `[输入校验失败] 参数: 缺少必填字段 \`skill\`` | **具备** |
| SKILL-09 | 只有显式 `disable-model-invocation: false` 才不自调 | 读三个技能的 `model_invocable` | `{'gwas-pipeline': True, 'wgs-expressive-pipeline': True, 'gwas-gemma': False}` | **具备** |
| SKILL-10 | `instructions()` 超长截断并追加标记 | 造 50000 字正文，读回长度与结尾 | `MAX_INSTRUCTIONS=40000 返回len=40011 结尾='AAAAA\n…[内容超长已截断]'` | **具备** |
| MEM-12 | `config.json` 的 `memory`/`summary_compaction` 能覆盖到配置，且端到端真生效 | ① 映射层：`_apply_file_overrides(cfg, {"memory":True,"summary_compaction":True})`；② **端到端**（批次 2 补跑）：备份 config.json → 加两个键 → `start_web.sh restart` → 发一轮 → `os.path.exists` | ① 默认 `False/False` → 覆盖后 `True/True`；非法值 `"notabool"` 被忽略。② `archival_db_exists = True`、`memory_dir_exists = True`，SSE 事件仍为 thinking/text/usage/done | **具备**（批次 2 已闭合此前标注的 D4 端到端缺口；复核方逐字节备份并还原了 config.json） |

---

## 2. CLI 端到端（3b，真打模型 DSv4-flash）

| # | 测什么 | 怎么测（原样命令） | 实测结果 | 结论 |
|---|---|---|---|---|
| CORE-01 | 单轮问答基线：无工具、正常收尾 | `run.py chat -p "只回复两个字：收到" --no-stream --max-turns 2` | stdout=`收到`；`exit=0`；stderr 只有 `[检索] 已启用`、`[persist] 会话 id=20261020T144014-cc28`；日志末行 `{"t":"2026-10-20 14:40:16","end":true,"stop_reason":"end_turn","truncated":false}` | **具备** |
| CORE-05 | 多轮 tool_use 循环：工具真执行、结果回灌、续推收尾 | `run.py chat -p "必须用 Bash 执行 echo HELLO_LOOP \| tee /tmp/core_loop_proof.txt 并原样告诉我" --no-stream --max-turns 4`，然后 `cat` 哨兵文件 | `exit=0`；stdout 里模型复述了命令与输出 `HELLO_LOOP`；**磁盘哨兵 `/tmp/core_loop_proof.txt` 内容 = `HELLO_LOOP`** | **具备** |
| STOP-04 | refusal 分支（pytest 侧） | `pytest tests/test_stops_loop.py::test_refusal_returns_message_no_tools -q` | `1 passed in 0.11s` | **具备**（CLI 真触发拒答那条未执行，注明） |
| STOP-07 | 墙钟熔断**用真实时钟**验证 | `run.py chat -p '用 Bash sleep 30 反复执行直到我说停' --max-seconds 3 --no-stream`，外层 `time` | stdout=`（预算熔断：已达时长上限 3s（实际 5s）。已停止继续调用；后台任务不受影响。）`；`real 0m5.485s`（**远小于 30s**）；`exit=0` | **具备** |
| STOP-10 | 终止原因落盘供审计 | `run.py chat -p '说一句你好就结束' --no-stream --max-tokens 20`，看最新日志末行 | 末行 `{"t":"2026-10-20 14:41:16","end":true,"stop_reason":"max_tokens","truncated":true}` → **`stop_reason` 真是 `max_tokens` 而非 `end_turn`，`truncated=true`** | **具备** |
| GUARD-09 | 幂等端到端：同命令第二次不落副作用 | `run.py chat -p "用 Bash 工具把 hi 追加进 /tmp/idem_demo.txt，然后再用完全一样的命令，再追加一次 hi" --idempotency --permission auto` + `wc -l` | stderr 有 `[幂等] 已启用（模式=reuse，仅作用有副作用工具）`；但 `wc -l /tmp/idem_demo.txt = 2`（**预期 1**）。查日志两条 tool_use 为 `{"command":"echo hi >> /tmp/idem_demo.txt","description":"追加 hi 到 /tmp/idem_demo.txt（第一次）"}` 与 `…（第二次，同一命令）` —— **command 相同、description 不同** | **部分具备** → **D2**：进程内复跑证明「参数完全一致（含 description）」时确实只落 1 行（第 2 次 meta 带 `idempotent_replay: True`）；模型只要改写 description 就绕过去重 |
| USAGE-05 | `/cost` 输出本轮用量 | `printf '/cost\n/exit\n' \| run.py chat` | `本轮用量：模型 DSv4-flash · 模型轮次 0 · 输出 token 0 · 累计延迟 0 ms`（0 是因为还没提问）；`exit=0` | **具备** |
| （新）| 裸 `run.py`（不带子命令）的健壮性 | `run.py </dev/null` | `AttributeError: 'Namespace' object has no attribute 'prompt'`（[cli.py:246](skill_agent/cli.py#L246)） | **不具备** → **D1** |
| STOP-10b | 上一行日志是否为 `/cost` 会话同名文件 | `ls -t ~/.skill-agent/sessions/*.jsonl \| head -1` | 文件名如 `20261020T144115.jsonl`（**不含** `-cc28` 之类的后缀段；`[persist]` 打印的 id 形如 `20261020T144014-cc28`，两者不是同一字符串——注意别拿打印 id 直接拼文件名） | 记录备注（不判定能力，属测试方法坑） |

**会话 id 备注**：`[persist] 会话 id=20261020T144014-cc28` 中的 `-cc28` 是**展示用短哈希**，磁盘文件名为 `20261020T144014.jsonl`。matrix 里若按打印 id 拼路径会找不到文件。

---

## 3. Web SSE 直打 API（3c）

服务：`bash start_web.sh restart` → `http://127.0.0.1:8787`（解释器 Agent 环境）。全部用 curl，不看前端。

| # | 测什么 | 怎么测 | 实测结果 | 结论 |
|---|---|---|---|---|
| WEB-01 | SSE 流合法、事件类型齐全、帧格式正确 | `curl -N -X POST /api/chat -d '{"session":"t_sse","message":"只回复两个字：收到"}'`；带工具版再打一次 | 纯文本轮：`event: text / usage / done` 各 1。首帧 `event: text` + `data: {"type":"text","delta":"收到"}`（帧格式 `event:\ndata:\n\n` 正确）。**带工具轮**：`text×23, thinking×7, tool_start×1, tool_end×1, usage×2, done×1` —— 六类事件全部出现 | **具备** |
| CORE-10 | Web 端 `/api/chat` 工具事件真下发（不是前端自造工具卡） | `curl -N -X POST /api/chat -d '{"session":"probe","message":"用 Bash 执行 echo SSE_OK 并告诉我结果"}'`，抓原始字节流 | `tool_start` 1 条：`{"type":"tool_start","id":"chatcmpl-tool-8e33f881…","name":"Bash","input":"{\"command\": \"echo SSE_OK\", \"description\": \"执行 echo SSE_OK\"}"}`；`tool_end` 1 条：`{…,"is_error":false,"result":"$ echo SSE_OK\nSSE_OK"}`；另有 text×4 / thinking×4 / usage×2 / done×1 | **具备** |
| WEB-02 | 确认桥：`approve=false` 后工具被拒且本轮正常收尾 | `--permission=ask` 发 `用 Bash 运行 echo hi`；抓 `confirm` 事件的 `uid`，`POST /api/confirm {approve:false}` | confirm 前事件含 `confirm`+`tool_start`；回 `{"ok":true}` 后事件收齐 `done`；`tool_end` 内容 = `{"name":"Bash","is_error":true,"result":"用户拒绝执行该操作。"}` —— **被拒且没挂死** | **具备** |
| WEB-03 | `/api/cancel` 真终止后端本轮（不只断前端） | 发 `sleep 20` 任务，7s 后 `POST /api/cancel`，等 `CURL_DONE` 落盘并计时 | `{"ok":true}`；取消后约 17s 流关闭（本次含一次模型调用延迟，未跑满 20s sleep 的完整等待）；事件含 `tool_end`+`done` | **部分具备（见备注）**：后端确实在 turn 边界返回并关闭流，但「从取消到关闭」的时延未做严格上界断言，故不给满分判据 |
| WEB-04 | 同会话并发重入返回 409 | 先起一个长请求，3s 后同 sid 再 POST | 第二个返回 `HTTP 409` + `{"error":"该会话正在运行中，请稍候"}`；`cancel` 后再试 → `HTTP 200`（可重入） | **具备** |
| WEB-05 | `GET /api/session/<sid>` 是只读展示视图，未完成轮不清空 | 手写一份「只问未答」的 jsonl，读接口 | `{"ok":true,"resume_count":0,"discarded":1,"reason":"no_completed_assistant","messages":[{"role":"user","blocks":[{"type":"text","text":"数据库里有什么？"}]}]}` —— 用户原话**在** messages 里；不存在的 sid → `HTTP 404` | **具备** |
| WEB-06 | 删除是**真删盘**不是前端隐藏 | 写 `t_del.jsonl` → `POST /api/session/delete` → `test -f` | `{"ok":true}`；`GONE_ON_DISK`（文件真消失） | **具备** |
| WEB-07 | 归档走 `os.replace` 移目录，可读、可还原 | 写 `t_arc.jsonl` → archive:true → 查两处路径 → 读接口 → archive:false | `{"ok":true}`；`NOT_IN_ROOT` / `IN_ARCHIVE`；读接口仍返回内容；还原后 `RESTORED_ROOT` | **具备** |
| WEB-08 | 路径穿越读/删/归档一律拒绝 | `GET /api/session/..%2f..%2fetc%2fpasswd`；delete/archive 传 `../../etc/passwd`、`../evil` | `read=404`；delete → `{"ok":false}`；archive → `{"ok":false}`；`pytest -k 'path_traversal or reject_bad_sid'` → `2 passed` | **具备** |
| WEB-09 | 历史会话只读：切到历史后流式写入被拦 | Playwright：选中历史会话 → 探 `viewingHistory` / `.sess-banner` → 调 `textDelta('不应出现XYZ')` → 比 `#chat` 文本 | `.sess-banner` **存在**；调用 `textDelta` 后 `#chat` 文本**不含**「不应出现XYZ」；无 JS 错误。**但 `viewingHistory` 读出来是 `False`** | **部分具备**：防串写这一**行为**成立（由 `activeViewId !== SID` 一类判据拦下），但 matrix 预期的 `viewingHistory===true` 这个**可观察量不成立**——变量在 [index.html:460](webapp/static/index.html#L460) 被显式置 `false` 且再未置 `true`，`textDelta` 的 `if(viewingHistory) return` 实际是死守卫（真正拦住写入的是别处）。**matrix 的 WEB-09 预期栏需修正** |
| WEB-10 | 长内容真折叠 E2E | `bash start_web.sh restart` → `python tests/ui_fold_e2e.py` | 34 条断言全 `{PASS}`，`FAILED: 0`；末几条：`⑧ 导出不丢全文 {'inner':669,'full':669}`、`⑨ 流式期间就折上 {'fold':True,'h':94,'sh':792}`、`⑨ 中断收尾后折叠与全文都还在`、`⑨ 不重复渲染` | **具备** |
| WEB-11 | 中断/导出不丢被折内容 | 同上脚本第 ⑧/⑨ 组 | `innerText` 长度 == `textContent` 长度（669/669），末行 `line 39` 在内；中断后 `clientHeight=94 < scrollHeight=792` | **具备** |
| WEB-12 | 畸形/越界输入不 500、不崩服务 | 空消息、`{bad json`、未知 sid 的 confirm/cancel/usage、缺 session 的 archive/delete、`/api/config` | `empty=400 ct=application/json`；`malformed=400`；`confirm/cancel/usage` 未知 sid → `404 {"ok":false,"error":"session not found"}`；`archive/delete {}` → `400 {"缺少 session"}`；`config=200`。**全程无 500** | **具备**（备注：usage 是 GET，误用 POST 会得 405——非缺陷） |

---

## 4. 昂贵档（3d：docker 隔离 / kill -9 崩溃一致性）

| # | 测什么 | 怎么测 | 实测结果 | 结论 |
|---|---|---|---|---|
| SBOX-09 | docker 内存上限真传；越界真被 OOM kill | ① 打印 `DockerBackend({'memory':'64m'}).fg(...)` 的 argv；② `docker run --memory 64m python:3.11-slim` 里分配 500MB | ① argv=`['docker','run','--rm','--name','traitpilot-fg','--network','none','--memory','64m','-v','/tmp:/tmp','-w','/tmp','python:3.11-slim','/bin/bash','-c','x']`（含连续 `--memory','64m'`）；② `exit=137`，无 `alloc ok` | **具备** |
| SBOX-10 | 容器工作目录以宿主同路径 bind-mount 并设 workdir | ① 打印 argv；② `docker run -v /tmp/wd:/tmp/wd -w /tmp/wd … 'pwd > made.txt'` 后读宿主文件 | ① argv 含 `-v','/tmp/wd:/tmp/wd'` 与 `-w','/tmp/wd'`；② 宿主 `/tmp/wd/made.txt` 内容 = `/tmp/wd` | **具备** |
| SBOX-12 | 固定容器名 `traitpilot-fg` 的并发撞名（已知局限） | 起一个持名容器，1s 后同名再起 | 第二个 `exit=125`，stderr=`Conflict. The container name "/traitpilot-fg" is already in use by container "bf7d12a0…"` | **具备（即「局限如实存在」）**；前台并发跑两个 docker 任务会撞名失败 |
| M13-01 | 逐条 append-only：强杀进程后已发生的消息仍在盘上 | `SKILL_AGENT_SESSION_ID=m13kill run.py chat --persist -p '用 Bash 执行 sleep 20…'`，5s 后 `kill -9`，读文件 | 文件 `~/.skill-agent/contexts/m13kill.jsonl` 存在，**3 行**：① `{"_meta":true,"model":"DSv4-flash",…}` ② `{"role":"user",…}` ③ `{"role":"assistant","content":[thinking, tool_use Bash sleep 20]}` —— 第一条 user **与** 已产生的 assistant/tool_use 都在强杀前落了盘 | **具备** |
| M13-01b | 落盘路径 | 同上看实际路径 | 写在 `~/.skill-agent/contexts/m13kill.jsonl`（`SKILL_AGENT_SESSION_ID` 经 `cfg.session_id` 生效），**不是** `sessions/` | 记录备注（matrix 里两种路径都提过，实测落在 `contexts/`） |

---

## 5. 未执行 / 未完成的项（如实登记）

| 项 | 为什么没跑 | 影响 |
|---|---|---|
| MEM-12 端到端（浏览器发一轮 → 出现 `archival.db`） | ~~本轮只做了映射层验证~~ **批次 2 已补跑**：`archival_db_exists=True` | D4 已闭合 |
| STOP-04 的 CLI 真触发拒答那条 | 需构造能稳定触发 refusal 的 prompt，成本高、不确定 | pytest 侧已绿；CLI 侧 `stop_reason=refusal` 落盘未验证 |
| WEB-03 取消时延上界 | 本次取消时延含模型往返，未做严格断言 | 只判「后端确实在 turn 边界返回」 |
| DOCS/长跑类（matrix 里 OTHER/LONG 档剩余条目） | 本轮未覆盖 | 见 matrix 缺口表 |
| 手动 UI 项（WEB-09/WEB-11 的浏览器人工步骤） | 用 Playwright 探针替代 | 结论基于脚本化证据 |

---

## 6. 与 matrix 的差异（需回写 matrix 的结论）

| matrix 条目 | matrix 原预期 | 实测 | 处置 |
|---|---|---|---|
| GUARD-09 | 「文件只有 1 行 hi」 | description 不同 → 2 行 | matrix 预期加限定：「**参数逐字相同**时才只落一次」；补 D2 |
| SBOX-07 | 需 `ulimit -t` 前缀 | 确有 `ulimit -t 1`，但前置了 `ulimit -v` | matrix 判据改为「脚本**含** `ulimit -t N`」（不要求居首） |
| WEB-09 | 「`viewingHistory===true`」 | `viewingHistory` 恒为 `false`；防串写靠别的判据 | matrix 预期改为「历史内容不被新 delta 覆盖」（去掉对 `viewingHistory` 取值的断言） |
| MEM-08 | 抽到 pid/.done 事实 | `.done`/路径抽对；pid/task_id 只抽到**键名** | matrix 预期拆成两层，补 D3 |
| MEM-12 | 「config 生效为 `cfg.memory_enabled=True`」 | 映射层已证；端到端未跑 | matrix 标注「端到端待补」 |
| CORE-01 日志路径 | 打印 id `2026…-cc28` vs 文件名 `2026….jsonl` | 两者不同串 | matrix 加备注：文件名 = 打印 id 去掉 `-xxxx` 后缀 |

---

## 7. 自动化回归基线（与本文档同批复核）

| 套件 | 命令 | 结果 |
|---|---|---|
| pytest 全量 | `/home/chenyuxi/anaconda3/bin/python -m pytest tests/ -q` | （见 matrix「本次实测结果」节：196 passed） |
| Web E2E ×4 | `start_web.sh restart` 后串跑 `tests/ui_e2e.py`、`ui_fold_e2e.py`、`ui_history_e2e.py`、`ui_title_e2e.py` | 全 `FAILED: 0` |
| 折叠 E2E（本轮复跑） | `python tests/ui_fold_e2e.py` | 34 断言全 PASS，`FAILED: 0` |
| 历史 E2E（本轮复跑） | `python tests/ui_history_e2e.py` | 全 PASS，`FAILED: 0` |
| 路径穿越单测 | `pytest tests/test_session_store.py -k 'path_traversal or reject_bad_sid'` | `2 passed` |
| refusal 单测 | `pytest tests/test_stops_loop.py::test_refusal_returns_message_no_tools` | `1 passed` |

> 注意：E2E 全绿**不等于**所有能力都具备——上面 §5 的未执行项与 §0 的 D1–D4 都不在自动化覆盖范围内。

---

## 附：证据文件位置

- 离线探针原始日志：`/tmp/agent_probes/logs/<ID>.txt`
- 离线探针汇总：`/tmp/agent_probes/results.json`、`/tmp/agent_probes/results_skill.json`
- Web SSE 原始流：`/tmp/t_sse.sse`、`/tmp/t_sse2.sse`、`/tmp/t_cfm.sse`、`/tmp/t_can.sse`
- CLI 输出：`/tmp/core01.err`、`/tmp/stop10.err`、`/tmp/idem.err`、`/tmp/m13kill.err`、`/tmp/m13kill.out`

---

## 8. 批次 2：matrix 剩余 79 条（3a–3c，含对抗性复核）

方法：16 个 agent 分 8 组，每组**先写探针真跑**、再由一个独立 agent **对抗性复核**（读探针判其是否真在测那件事、断言是否恒真；自己重跑；对判「具备」的设法构造反例、对判「不具备」的设法证其具备）。复核方降级了 8 条、确认了 1 条死代码。
探针与原始日志：`/tmp/agent_probes/r2/<ID>.py`、`<ID>.log`；复核后汇总：`/tmp/agent_probes/r2/final.json`。
每组「结论词」是**复核后**的判定（证据串里注明是复核确认还是降级）。

### 8.0 本批非「具备」清单

| # | 能力点 | 复核后结论 | 降级/维持的理由（复核方原话摘要） |
|---|---|---|---|
| FEAT-08 | CLI 端检索开关端到端：--index-rebuild 真落盘 code.db 并打印计数；config.json retrieval:true 也生效 | 部分具备 | 降级。CLI 开关与 code.db 落盘为真（重跑逐字一致），但原证据选的 `--index-dir /tmp/idx_probe` 上溯不到语义 project → 语料为 0，`files=0 chunks=0`。改到项目内 `.skill-agent/index_revprobe` 重跑得「101 文件 / 101 建立」、DB 内 `files=101 chunks=1769` → 机制真、原判据选的路径只证明「打印计数 + 建空库」。 |
| GUARD-02 | 轮询类工具（TaskOutput / 读 *.log/*.done/*.pid）的重复等待永不硬停，只降级到 WARN | 部分具备 | 降级。「轮询重复永不硬停」已复现（TaskOutput×10 恒 warn）；「普通命令能硬停」也真（`ls -la`×6→halt）。但「白名单不过宽」被证伪：`is_polling` 是**纯子串匹配**——`echo unrelated.logstring`、`echo hello.log`、`mv x.log y`、`rm stale.pid` 全判为轮询；`rm -f stale.pid` 连按 15 次 `level` 恒为 warn 永不 HALT → 含 `.log/.done/.pid` 子串的**副作用**死循环会被漏放。 |
| GUARD-11 | 超长结果 spill 落盘：写入独立文件、返回「已转存：<路径>」引用、落盘内容是完整输出（非截断版），且随后能用 Read 继续分段读取；落盘失败时静默降级保留原结果 | 部分具备 | 降级（范围缩窄）。核心均真：content 含「转存」、`meta["spilled"]` 文件真实存在（530B）、seq 8000 尾行完整落盘（38905B 未截断）、不可写目录静默降级。但「随后能用 Read 分段读取」只以小文件 `limit=5` 验过一次，从未对真正 >30k 的落盘文件跑 offset+limit 分页。 |
| HOOK-13 | 会话事件（session_start / session_end / pre_compact）端到端真被 emit（非仅测试里手动触发） | 不具备（已知边界） | 维持**不具备（已知边界）**。三重独立复现：全仓 `grep -rn "emit("` 排除 def 后**调用点为 0**（唯一命中 `webapp/session.py:153 agent.on_event = hooks.emit` 是 WebSession 往 SSE 队列塞事件，非 HookManager 会话钩子）；真打模型 `--trace` 产出的 jsonl `event_kinds == ["pre_tool_use","post_tool_use"]`；离线确定性探针（注册了 session_start/end 各 1 个钩子）跑完一轮工具周期仍只有 pre/post 两行。会话事件是死代码。 |
| MEM-07 | archival 记忆按 query 检索 top-k 并注入（渲染为「## 相关历史记忆」块） | 部分具备 | 降级。原样 query「继续执行阶段 S3 那个任务」→ `search()=[]` 无注入；另测「阶段 S3」/「rMVP」/「grm」→ 命中，但「S3 任务」/「输出文件在哪」→ 均 `[]`。即命中要求 query 与记忆文本有 **≥3 字连续子串重叠**，模糊自然语言 query 普遍落空。注入路径本身工作。 |
| PERM-04 | 宽容优先：合法输入与无法识别的 schema 关键字不应被误杀（防过严） | 部分具备 | 降级。三条已列输入（缺可选字段/未知类型/空 schema）确实不误杀，但 matrix 反例栏点名的「把数字传成字符串」被实测误杀：`validate_args(BASH, {"timeout":"5"})` → 「期望类型 integer，实际为 string」；`validation.py` docstring 自称「避免过严…如把数字传成字符串」**未兑现**。 |
| PERM-08 | 结构化 deny 规则（正则 / glob）命中即拒绝，命令不进入 executor | 部分具备 | 降级。deny 正则/glob 命中即拒、磁盘哨兵未变（真值）；但 `permissions.py:122 is_denied` 遍历**全部**规则只看 `action=="deny"`，**无视 allow 与顺序**，而 `execute` 又先调 `is_denied` → `rules=[allow ^echo, deny ^echo]` 时 allow 形同虚设，与模块自称「按序首次命中」矛盾。 |
| PERM-12 | 默认全关零行为变化 + 开关接线（config.json/env/CLI）与 permission_rules 只认代码传入的缺口 | 部分具备 | 降级。默认 `enabled=False`、config.json 的 `restrict_paths`/`write_approval` 生效、CLI banner 正确；但 `permission_rules`/`extra_roots`/`permission_roots` 读回 **None**，`PermissionModel.from_config` 后 `rules=[] extra_roots=[]` → config.json 里的规则被**静默丢弃**（规则/根只能代码传入）。另 CLI 参数必须写在子命令**前**（`list --restrict-paths` → rc=2）。 |

### 8.1 明细（按模块）

| # | 能力点 | 怎么测（探针要点） | 实测证据（复核后） | 结论 |
|---|---|---|---|---|
| CORE-06 | 工具参数被 max_tokens 截断 → REPAIR_TRUNCATION 合成配对 tool_result 续推 | 重跑探针（进程内假 client，第一轮 _parse_error tool_use+max_tokens，第二轮 end_turn）+ 读 client.py/agent.py/stops.py 源码验证触发链真实 | 复核确认（亲自重跑 /tmp/agent_probes/r2/CORE-06.py，输出一致）：answer='重试完成'; history_roles=['user','assistant','user','assistant']; unpaired=[]; executor_calls=0; next block 精确为 {"type":"tool_result","tool_use_id":"t1","is_error":true}。交叉核对 client.py:207-216 content_block_stop 的 json.loads 失败确实置 _parse_error（_parse_error 是真实代码产物，非凭空构造），agent.py REPAIR_TRUNCATION 分支对 blocks 里每个 tool_use 合成 make_truncation_result。断言非恒真：若合成的是文本 user 或漏配对，unpaired 会非空。 | 具备 |
| CORE-07 | pause_turn 原样续推，不制造连续两条 user（协议不变量） | 重跑探针（假 client 第1轮 text+'pause_turn'，第2轮 text+'end_turn'）+ 源码确认合并逻辑 | 复核确认（重跑复现）：answer='part2'; calls=2; history_roles=['user','assistant']; consecutive_same_role=[]; user_message_count=1; assistant_text_blocks=['part1','part2'] 合并进同一条 assistant。反例可辨：若 pause_turn 后 append 空 user，roles 会出现连续 user——探针的 consec/user 计数断言能抓到。源码 agent.py:176-185 用 history[-1].role=='assistant' 判定合并。 | 具备 |
| CORE-08 | 工具循环轮次上限熔断（防死循环，且不超跑） | 重跑探针（LoopClient 每轮永返 tool_use，cfg.max_turns=4，数 create 次数） | 复核确认（重跑复现）：answer='（达到最大工具轮次，已停止）'; create_calls=4 恰等于 max_turns=4; has_usage_event=True。源码 agent.py:146 for step in range(budget['max_turns']) + 末行 return 兜底文案，计数点正确（熔断在进入下一轮循环即结束，非多跑一次）。反向：calls 若>4 即无关；探针断言 calls==4 精确。 | 具备 |
| CORE-09 | 畸形/越界工具参数在 executor 之前被 schema 校验拦截 | 重跑探针（真实 ToolSet.execute('Bash', 畸形 args) 检查哨兵文件是否落盘 + 合法参数对照执行） | 复核确认（重跑复现，四反例+一对照全对）：wrong_type_integer(string 冒充)/bool_as_integer(True 冒充)/missing_required: is_error=True 且 sentinel_written=False; non_dict_args: '参数必须是 JSON 对象'; valid_control: is_error=False sentinel_written=True（证明 executor 通路未被误杀）。用真实 ToolSet 的 Bash 工具+touch 哨兵文件——即 matrix 要求的『磁盘真值』而非仅错误串。 | 具备 |
| CORE-12 | 过载/限流降级：429/529/5xx 退避重试，重试耗尽后切后备模型 | 重跑探针（stub _request/_attempt，覆盖退避重试、耗尽抛错、401 不重试、_http_error 分类、fallback 切换、无后备对照） | 复核确认（重跑复现）：_retryable_status {429/529/500/502/503:True},{400/401/404/422:False}; 429 连失2次第3次成功 calls=3 stop=end_turn; 一直429 max_attempts=3 后抛且 retryable=True; 401 calls=1 立即抛 retryable=False; 真实 _http_error(429).retryable=True/(401).retryable=False; fallback seen=['A','B'] final_model=B; 无 fallback 时抛 APIError=True。全程打桩 _request/_attempt 不发网络。 | 具备 |
| STOP-01 | stop_reason × blocks 的分类决策正确（6 类动作） | 重跑探针（直接调纯函数 classify(stop_reason,blocks) 逐条比对）+ 读 stops.py:60-108 确认 6 类动作分支 | 复核确认（重跑复现，10 case+配对全 OK）：end_turn+tool_use→continue_tools warn='protocol_anomaly'; max_tokens+parse_error→repair_truncation truncated=True; mixed usable+broken→continue_tools; max_tokens+text→finalize truncated=True; pause_turn→resume_server_tool; refusal→refuse; end_turn+text/空 stop→finalize; 'something_new'→finalize warn='unknown_stop_reason:something_new'; make_truncation_result 配对 True。逐 case 断言 action+warn+truncated，非恒真（每 case 期望值独立）。 | 具备 |
| STOP-02 | 截断修复：全部 tool_use 解析失败 → 合成 is_error 的配对 tool_result 并续推（维持配对不变量） | 重跑探针（假 client 截断轮+续推，检查 history 配对/executor 计数/notice） | 复核确认（重跑复现）：answer='重试完成'; executor_calls=0; next_role=user; 首块 {'type':'tool_result','tool_use_id':'t1','is_error':true}; notice 含『截断』。与 CORE-06 同机制但独立脚本、独立断言（额外查 notice 文案与 executor 计数）。matrix 判据『history 出现 is_error=true 配对 tool_result』被满足。 | 具备 |
| STOP-03 | pause_turn 续推：原样回传 assistant，不追加 user（不制造连续两条同 role） | 重跑探针（pause_turn 续推，断言无连续同 role + assistant 条数==1 + 文本合并） | 复核确认（重跑复现）：answer='part2'; calls=2; history_roles=['user','assistant']; consecutive_same_role=[]; user_message_count=1; assistant_msg_count=1; assistant_text_blocks=['part1','part2']。比 CORE-07 多断言 assistant_msg_count==1，直接证 pause_turn 未新起消息。源码 RESUME_SERVER_TOOL 分支只 continue、不 append user。 | 具备 |
| STOP-05 | 轮次熔断：max_turns 到点只停止调用，恰好 N 次不多跑，且不杀后台 | 重跑探针（真实 ToolSet+Bash background=sleep 300，max_turns=3，轮询等 pid 落盘后 os.kill(pid,0)+/proc 校验存活） | 复核确认（亲自重跑复现，pids 每次不同但结论稳定）：create_calls=3==max_turns=3; 3 个 pid 文件落盘; pids=[118423,118429,118433] 全部 os.kill(pid,0) 成功且 /proc/<pid> 存在。证明轮次熔断不杀后台。探针 finally 自清 SIGKILL，重跑后 `ps | grep 'sleep 300'` 为空（未留残留）。 | 具备 |
| STOP-06 | 输出 token 熔断：累计 output_tokens ≥ 上限时在**下一次请求前**熔断 | 重跑探针（max_output_tokens=25，LoopClient 每轮返 10 token+tool_use，数 create/executor 次数+notice 文案） | 复核确认（重跑复现）：create_calls=3（每轮10 token，累计30≥25 后下一轮前熔断，熔断点在 _budget_gate 顶部而非工具中途）; executor_calls=3 证明前 3 轮工具都完整跑完; answer 含『预算熔断』+『上限』; 熔断 notice 含『后台任务…仍在运行』。**更正策录一条小失真**：notices 实为 2 条（含 1 条『预算提醒：已用 20/25 接近上限』），原 evidence 只列了熔断那条——不影响本条判定。 | 具备 |
| STOP-08 | 预算预警只提醒一次（warn_ratio 命中后置位 warned，不刷屏） | 重跑探针（多轮 stub 循环，统计『预算提醒』类 notice 条数须恰为 1） | 复核确认（重跑复现）：create_calls=10（多轮）; warn_notice_count=1 恰为一条; warn_notices=['预算提醒：输出 token 已用 10/100（接近上限）']。max_output_tokens=100/warn_ratio=0.1，第一轮末即命中并置位 warned，之后不再刷屏——断言『恰 1 条』严格（≥2 即失败）。源码 agent.py:249-258 b['warned'] 门控。 | 具备 |
| STOP-09 | 默认不限预算 = 与改造前行为一致（不误熔断） | 重跑探针（cfg 全 None + 单轮 stub 返回 text+end_turn，检查 answer/notice/_init_budget 回落值） | 复核确认（重跑复现）：answer='done'; create_calls=1; notices=[]; _init_budget()=60/0/0（max_turns 回落内置 60，output/seconds=0=不限）。单轮正常收尾、无误熔断、无多余 notice。对照 STOP-06 证明 0 是『不限』语义。 | 具备 |
| STOP-11 | Web SSE 透出终止契约：done 事件带 stop_reason，熔断/提醒以 notice 事件推送 | 重跑探针（SKILL_AGENT_MAX_OUTPUT_TOKENS=1 自启临时 web，urlopen /api/chat 解析原始 SSE 的 notice 与 done 事件） | 复核确认（亲自重跑，自启临时 web 端口8793+TEMP 配置，真实打模型 DSv4-flash）：SSE 含 'event: notice' → 预算熔断文案; done_count=1; done.stop_reason='tool_use'（非空）; done.usage={model:DSv4-flash,steps:1,output_tokens:83,latency_ms:2645}（对象非空）。matrix 判据『done 带 stop_reason + usage、存在 notice 事件』被满足。**确认 matrix 失真预期**：客户端流中 'event: __end__' 未出现（server._sse_stream 把内部 {'type':'__end__'} 当哨兵 consume 后 break，不 yield），原探针注释正确。判定不降级——真实具备证据是 notice+done.stop_reason+usage，与 __end__ 无关。 | 具备 |
| PERM-01 | 畸形参数＝明显类型错误（timeout 传字符串）在 executor 之前被前置拦截，返回结构化报错 | 重跑原探针 + 读 tools.py:152-156 断言顺序 | 复核确认。重跑 `/home/chenyuxi/anaconda3/envs/Agent/bin/python -I /tmp/agent_probes/r2/PERM-01.py`：is_error=True，content 首行 `[输入校验失败] 参数.timeout: 期望类型 integer，实际为 string`，has_executor_marker=False、has_valueerror=False。读 tools.py:154 确认 validate_args 在 executor 前无条件调用，故为前置拦截非事后兜底。 | 具备 |
| PERM-02 | 必填字段缺失（Bash 无 command）触发结构化报错，而非落进 executor 兜底 | 重跑原探针 | 复核确认。重跑 PERM-02.py：is_error=True，content=`[输入校验失败] 参数: 缺少必填字段 `command``，has_executor_fallback=False（不含 `Bash 需要 command 参数`）。validate_args 的 required 循环在 executor 前拦截。 | 具备 |
| PERM-03 | enum 越界值被拒（Search 的 kind 只允许 py_func/window 等） | 重跑原探针 + 自建 /tmp/xv_perm03c.py 走真实 execute 路径 | 复核确认并加强。原探针仅对 schema dict 调 validate_args（`ts.execute` 那行被 `if False` 关掉）。我用启用检索（CodeIndex）的 ToolSet 直测真实工具：`/tmp/xv_perm03c.py` 得 execute('Search',kind='bogus') is_error=True 前置校验串，kind='py_func' is_error=False 落 executor；真实 enum=['py_func','py_class','py_module','md_section','sh_block','window']。 | 具备 |
| PERM-04 | 宽容优先：合法输入与无法识别的 schema 关键字不应被误杀（防过严） | 重跑原探针 + 自建 /tmp/xv_perm04b.py 补测 numeric-as-string 及浮点传 integer | 降级（复核发现断言覆盖不足）。原三条断言复现为 None（optional_missing/unknown_type/empty_schema，all_none=True）。但矩阵该行反例栏明确把「把数字传成字符串」列为‘合法宽松输入不得误杀’，实测 `/tmp/xv_perm04b.py`：validate_args(BASH,{timeout:'5'}) 返回 `期望类型 integer，实际为 string` 报错，ts.execute('Bash',{'timeout':'5'}) is_error=True；validation.py docstring 自称‘避免过严…如把数字传成字符串’却未兑现。故『无一误杀』的乐观结论不成立→部分具备（对三个已列输入宽容，对文档声明的宽松输入过度拦截）。 | 部分具备 |
| PERM-05 | 工作区路径边界：Read 工作区外绝对路径被拒 | 重跑原探针 + 自建 /tmp/xv_perm05b.py 补 symlink/../ 遍历/前缀兄弟攻击 | 复核确认。重跑 PERM-05.py：越界读 is_error=True，content 含『路径越出允许的工作区边界：/etc/hostname』与『允许根：/tmp/...』，LEAK_hostname=False（/etc/hostname 真值 seedlab1 未出现），工作区内读 is_error=False。我另测 `/tmp/xv_perm05b.py`：workspace 内指向 /etc/hostname 的 symlink、`../etc/hostname`、以及 root=/tmp/ab 读 /tmp/abc_secret 前缀兄弟目录，三种越界均 is_error=True、leak=False（_resolve 先 resolve 再 check_path，符号链接/遍历被消解）。 | 具备 |
| PERM-08 | 结构化 deny 规则（正则 / glob）命中即拒绝，命令不进入 executor | 重跑 PERM-08/08b + 自建 /tmp/xv_perm08c.py 走 execute 验 allow-first | 复核确认（同意部分具备）。重跑 PERM-08.py：deny 正则命中 `命令被权限策略拒绝：命中 deny 规则：tool=Bash, match='rm\\s+-rf'`，磁盘 keep.txt 仍='safe'（确未执行）；glob `*.env` 拒；`echo ok` is_error=False。缺口核实：permissions.py:122 `is_denied` 遍历全部规则只看 action=='deny'，无视 allow 与顺序；execute 在 permissions 前先调 is_denied。重跑 `/tmp/xv_perm08c.py` 得 rules=[allow ^echo, deny ^echo] 时 needs_confirm=(False,'') 但 is_error=True 且 is_denied 命中——与模块自称‘按序首次命中’矛盾。 | 部分具备 |
| PERM-09 | ask 规则命中 + 无 TTY 时拒绝执行（判定不落成真执行） | 重跑原探针 + 读 tools.py 无 TTY 分支 | 复核确认。重跑 PERM-09.py：stdin.isatty=False；execute('Bash','curl http://127.0.0.1:1') is_error=True，content=`交互确认不可用（无 TTY），操作已拒绝——请用 --permission auto 或进入交互模式。`，无 curl 连接输出/错误；needs_confirm=(True, 命中 ask 规则)。读 tools.py:167-187 确认无 handler 且非 TTY 时在 `tool.executor(args)` 之前直接 return，命令结构上不可能执行。 | 具备 |
| PERM-11 | 写操作审批：write_approval 开启后 Write/Edit 纳入审批门，无 TTY/无 handler 时拒绝且不改盘 | 重跑原探针（三态开关对比 + 磁盘真值） | 复核确认三态。重跑 PERM-11.py：write_approval=True 无 handler → Edit is_error=True content 无 TTY 拒绝串、disk 仍 'orig'、disk_unchanged=True；Write y.txt 拒且 y.txt 不存在；默认 write_approval=False → Write z.txt is_error=False 且落盘 'hi'；confirm_handler 返回 True → Write w.txt 成功落盘 'yes'。开关真能双向切换行为。 | 具备 |
| PERM-12 | 默认全关零行为变化 + 开关接线（config.json/env/CLI）与 permission_rules 只认代码传入的缺口 | 重跑原探针 + 自建 /tmp/xv_perm12b.py 验 from_config 丢弃 + env 变量 | 复核确认（同意部分具备）。重跑 PERM-12.py：默认 PermissionModel().enabled=False，默认读 /etc/hostname is_error=False；config.json restrict_paths/write_approval=True 生效，但 permission_rules/extra_roots/permission_roots 读回 None；CLI `--restrict-paths --write-approval list` rc=0 且 stderr banner『路径边界=True 写审批=True 规则=0』，反序 `list --restrict-paths` rc=2 unrecognized arguments。我另测 `/tmp/xv_perm12b.py`：PermissionModel.from_config 后 rules=[]、extra_roots=[]、is_denied('Bash','curl x')=None——config.json 的规则被静默丢弃；env SKILL_AGENT_RESTRICT_PATHS/WRITE_APPROVAL 生效、无任何规则 env。规则/根只能代码传入的缺口成立。 | 部分具备 |
| GUARD-01 | 连续「同动作+无进展」达到 halt 阈值 → 真熔断该轮（返回熔断文案、停止继续调用） | 进程内 Agent+LoopGuard 三配置(默认3/6、matrix 2/4、1/1)，重跑脚本比对 calls/文本/事件/跳过块 | 复核确认。复跑 GUARD-01/01b/01c 全部 PASS 且数值一致：calls=6/4/1 < max_turns=40；final_text='（已熔断：检测到无进展循环，已停止。）'；_guard_halted=True；熔断事件≥1；01c skipped_blocks_returned=1 且 tool_exec_count=1（第2块真被跳过）。另跑 matrix 点名测试 test_loopguard_halts_and_returns 通过。唯一小瑕：证据写 'client_calls=6 < max_turns=40' 尚可，但两条 loop_guard 事件/轮由实现（warn 与 halt 各发一次）决定，非断言。 | 具备 |
| GUARD-02 | 轮询类工具（TaskOutput / 读 *.log/*.done/*.pid）的重复等待永不硬停，只降级到 WARN | 复跑 GUARD-02.py 得原结果；自写 ADV-polling-breadth.py / ADV-polling-loop.py 构造反例 | 降级。'轮询重复永不硬停'已复现（TaskOutput×10 last=warn，无 halt）；'普通命令能硬停'也真（ls -la×6→halt）。但证据里'白名单不过宽'被证伪：is_polling 是纯子串匹配，我另测 echo unrelated.logstring→True、echo hello.log→True、mv x.log y→True、rm stale.pid→True；且 rm -f stale.pid 连按15次→level 恒为 warn、永不 HALT(rm_never_halt=true)。即含 .log/.done/.pid 子串的任何 Bash 命令都被豁免硬停，副作用死循环 rm 会被漏放。matrix 判据字面只查 'ls -la' 一类，故用例'通过'，但证据结论过度声明。 | 部分具备 |
| GUARD-03 | 进展信号（meta 中 exit_code 由失败转成功 / 日志行数增长 / mtime 前进）出现时重复计数回落到 0 | 进程内 LoopGuard 逐信号驱动，读 _repeat 与 level；对照 pytest 通过 | 复核确认。复跑三信号均 _repeat=0/level=ok（exit_code 1→0、lines_total 10→25、mtime 1000→2000），反面无进展3次 _repeat=3 不被过早复位。matrix 点名 test_progress_resets_counter 通过。注：三信号是各起独立 LoopGuard 分别驱动的，非一次连续序列，但足以支撑判据。 | 具备 |
| GUARD-04 | 未到 halt 但到 warn 阈值时，纠偏提示被并入本轮 tool_result（严禁另起一条 user 消息） | 进程内 Agent(warn=2,halt=99) 跑 _RepeatClient，扫 history 各 tool_result content 与 role 序列 | 复核确认。复跑：history 末条 tool_result content 含'疑似重复：`Bash` 已连续 6 次…'；hint_in_standalone_user_msg=False；roles 严格 user/assistant 交替、无连续 user（history_len=13）。matrix 点名 test_warn_injected_into_tool_result 通过。提示真被并入同轮 tool_result 而非另起 user 消息。 | 具备 |
| GUARD-05 | 默认关闭时 LoopGuard 完全不干预（无计数副作用、Agent 上 loopguard 为 None） | 进程内裸 LoopGuard + 默认 Agent 端到端，扫 blob 关键词 | 复核确认。复跑：裸 LoopGuard() 20 次同动作 level 恒 ok、_repeat=0；默认 Agent.loopguard is None=True；默认 Agent 一轮重复动作 blob 不含'疑似重复/轮询无新进展/熔断'。matrix 两条点名测试通过。默认零泄漏成立。 | 具备 |
| GUARD-06 | 幂等 reuse 模式：同一次运行内相同 (工具名+规范化参数) 的副作用调用只真正执行一次，第二次复用上次结果 | 进程内 ToolSet(idempotency=reuse) 连跑两次同 Bash，查文件行数/meta/键 | 复核确认。复跑：第二次 r2.meta['idempotent_replay']=True、content 含'复用'、文件只写 1 行（真去重非仅打日志）、guard_size=1；键顺序无关 True、值变则键变 True。matrix 两条点名测试通过。 | 具备 |
| GUARD-07 | 幂等 deny 模式：命中相同副作用调用时直接拒绝重放（返回 is_error，模型可改用其它参数） | 进程内 ToolSet(idempotency=deny) 连跑两次同 Bash，判 is_error 与文件行数 | 复核确认。复跑：deny 模式 r2.is_error=True、content 含'幂等'+'拒绝重放'、底层文件仍只 1 行（executor 真未再执行）；r1 正常。matrix 点名 test_deny_blocks_replay 通过。 | 具备 |
| GUARD-08 | 只读工具（Read/Grep/Glob/TaskOutput）不参与去重；失败（is_error）的调用不被缓存、允许重试 | 进程内 ToolSet 分别验证只读不去重、失败不缓存、eligible 白名单 | 复核确认。复跑：Read 第二次 replay=None（只读不去重）；Bash exit 3 两次均 is_error 且 fail2_replay=None（失败不缓存可重试）；eligible={Bash/Write/Edit:True, Read/Grep/Glob/TaskOutput:False}。matrix 三条点名测试通过。 | 具备 |
| GUARD-10 | per-tool 看门狗：慢工具被超时杀死并返回可见超时报错（非静默挂死）；超时参数被夹取到合法区间 | 复跑 GUARD-10.py + 自写 ADV-clamp.py（RecSandbox 记录 sandbox.fg 收到的 timeout） | 复核确认并补强。复跑：sleep(2) 工具以 1s 看门狗包装→slow_elapsed=1.0s、is_error=True、content 含'超时'；Read/Grep executor 均为包装闭包；default_timeout Read=30/Bash=120；Bash timeout=999999 实测 elapsed=0.0s 无错。补证：证据自认'上界 3600 无法直接观测'——我用记录型 sandbox 拦 fg() 实测夹取 0→1、999999→3600、7200→3600，行为一致，判据满足。三条点名测试通过。 | 具备 |
| GUARD-11 | 超长结果 spill 落盘：写入独立文件、返回「已转存：<路径>」引用、落盘内容是完整输出（非截断版），且随后能用 Read 继续分段读取；落盘失败时静默降级保留原结果 | 复跑 GUARD-11.py；核对 Read 分页参数与落盘体量对比 | 降级（范围缩窄）。核心均真：复跑 content 含'转存'、meta['spilled'] 文件存在(spilled_size=530)、对 spilled 路径 Read not error、seq 8000 落盘尾行 8000 完整(38905 字节>30k 未截断)、默认无 spill_dir 不含'转存'、/proc/nonexistent/x 静默降级无异常。三条点名测试通过。但'随后能用 Read 继续分段读取'只以 limit=5（约750字节小文件，阈值 100）验了一次，从未对真正的>30k 落盘文件跑 offset+limit 分页；实现上 Read limit 上限 2000 行，大文件续读未实测。该子句证据不足。 | 部分具备 |
| GUARD-12 | 同轮多个只读工具（Read/Grep/Glob/TaskOutput）真并行执行、结果顺序仍与原 blocks 对齐；副作用工具（Bash/Write/Edit）永不进并行分支 | 进程内 Agent._execute_tools 直喂3块计时对比；对照 pytest 通过 | 复核确认。复跑：parallel=True 3×sleep(0.3) Read→0.3s(<0.6) 且顺序 a/b/c 对齐；默认串行 0.6s；parallel=True 下 3×Bash 仍 0.9s 串行；_is_readonly Read/Grep=True、Bash/Write=False。另读 agent.py:_execute_tools 确认副作用工具硬进 serial 分支。三条点名测试通过。 | 具备 |
| HOOK-01 | 插件目录挂载：把 *.json 工具规格加载为运行时工具，source=plugin:<路径>，且与内置工具同权可执行 | 现场重跑原作者探针 + 自写 ToolSet.execute 端到端探针；子进程真跑 CLI list 抓 stderr。 | 复核确认。我用 python -I 重跑 /tmp/agent_probes/r2/HOOK-01.py：loaded==['EchoPlugin']、source='plugin:/tmp/hook01_g599xr62/entry/mytool.json'、executor({}).content=='plugin-ok'；CLI `python -m skill_agent --project ... --plugin-dir <dir> list` exit=0 且 stderr 真有一行 '[插件] 已加载扩展工具：EchoPlugin'。[补充加固] 我自写 REV_HOOK01_toolset.py 直接走 ToolSet 门面：schemas_for(None) 含 'Plugged'、ts.execute('Plugged',{}) content=='plugin-ok' is_error=False —— 证明插件工具不是只进了 registry，而是真能经 ToolSet 执行（矩阵'与内置同权可执行'）。矩阵 CLI 串亦能复现。 | 具备 |
| HOOK-02 | 插件白名单 allow 过滤：只加载 allow 中的工具名，其余无论规格是否合法都跳过 | 重跑原作者探针；人读 registry.py 白名单分支 + config/cli 接线。 | 复核确认。重跑 HOOK-02.py：allow=['A'] → loaded==['A']、'A' in names=True、'B' in names=False（B 规格完全合法仍被挡）、len(registry)==1；allow=['NOEXIST'] → loaded==[] 且不抛（静默跳过）。[反例尝试] 我核对 registry.py:96 `if not name or (allow is not None and name not in allow): continue`，白名单在 register 之前断言，语义真区分；并核对 config.py:116/206 与 cli.py:206 确认 plugin_allow 真接到 load_plugin_dir。未找到可推翻构造。 | 具备 |
| HOOK-03 | 畸形/缺失插件规格不拖垮进程：坏 JSON、缺 module/path、目录不存在一律静默跳过返回 [] | 重跑原作者探针 + 读 registry.py:82-109 独立 try 结构。 | 复核确认。重跑 HOOK-03.py：同目录混放坏 JSON('{ not json')、缺 path 的 nosource.json、合法 good.json → crashed=False、loaded==['GoodTool']（坏插件不拖累合法插件）、'NoSrc' in names=False；不存在目录 load_plugin_dir 返回 [] 不抛 FileNotFoundError。核对 registry.py 逐 spec 独立 try/except + 目录不存在 early return，与行为一致。 | 具备 |
| HOOK-04 | PreToolUse 阻断：钩子返回 str 视为阻断理由，工具不执行，返回 is_error 的 ToolResult | 重跑原作者哨兵探针 + 人读 tools.py execute 分支。 | 复核确认。重跑 HOOK-04.py：pre 钩子返回 '禁止执行' → sentinel_exists=False（side-effect 真未发生）、is_error=True、content=='禁止执行'。对照 tools.py:158-161：blocked is not None 时 `return blocked if isinstance(blocked,ToolResult) else ToolResult(str(blocked),True)`，真阻断且不会执行 executor。哨兵证据坐实非假阻断。 | 具备 |
| HOOK-05 | PreToolUse 改参：钩子返回 None 时可原地修改 args，改后的参数才进 executor | 重跑原作者双哨兵探针。 | 复核确认。重跑 HOOK-05.py：钩子把 args['command'] 从 'echo orig; touch orig' 改成 'echo injected; touch inj' → inj_sentinel=True、orig_sentinel=False、is_error=False。两哨兵区分证明 executor 收到的是改后参数而非副本。与 tools.py:159 传同一 args dict 一致。 | 具备 |
| HOOK-06 | PreToolUse 短路：钩子返回 ToolResult 时直接作为结果，authorized 门与 executor 均不执行 | 重跑原作者探针。 | 复核确认。重跑 HOOK-06.py：pre 返回 ToolResult('cached!',False) → executor 哨兵未出现（真未跑）、final content=='cached!'（非 repr 包裹）、is_error=False。tools.py:161 isinstance 分支直接 return，符合。 | 具备 |
| HOOK-07 | PostToolUse 脱敏/改写：返回 ToolResult 时替换原结果内容 | 重跑原作者探针（关键点：返回新对象验证采纳而非别名）。 | 复核确认。重跑 HOOK-07.py：post 钩子返回**新** ToolResult（非原地改），执行 'echo secret' 后 r.content=='$ echo ***\n***'、'secret' 不在、'***' 在 —— 返回值确被采用。tools.py:202-205 `if isinstance(replaced,ToolResult): result=replaced` 符合。 | 具备 |
| HOOK-08 | 钩子异常隔离：任一钩子抛异常被吞并记入 last_errors，绝不拖垮工具执行/主循环 | 重跑原作者探针 + 自写 post 异常/多钩子续跑探针。 | 复核确认。重跑 HOOK-08.py：抛 RuntimeError('hook 崩了') 的 pre 钩子 → 工具仍成功(content 含 'ok')、h.last_errors==['pre_tool_use[boom]: hook 崩了']、异常未冒泡；未知事件 register 抛 ValueError('未知事件 bogus')。[补充加固] 自写 REV_HOOK08_post.py 验证 post 钩子抛异常同样被吞(post 钩子异常→last_errors=['post_tool_use[boom_post]: post崩了']，工具结果仍 ok)，且对多钩子用 continue 而非 break（抛异常后第二个钩子仍执行，out=='阻断2'）。 | 具备 |
| HOOK-09 | 非阻断钩子：blocking=False 的 pre 钩子返回非 None 也不终止工具 | 重跑原作者探针（含 blocking=True 对照组）。 | 复核确认。重跑 HOOK-09.py：blocking=False 钩子返回 '只是观测' → sentinel_exists=True（命令真执行）、is_error=False；同返回值在 blocking=True 下 r2.is_error=True。blocking 语义真区分。与 hooks.py:80-82 `if h.blocking: return out` 一致。 | 具备 |
| HOOK-13 | 会话事件（session_start / session_end / pre_compact）端到端真被 emit（非仅测试里手动触发） | 全仓 grep emit 调用点 + 重跑真打模型的 --trace 子进程探针 + 重跑离线确定性探针。 | 复核确认（维持原判，且我自己独立复现了端到端）。1) 全仓 `grep -rn "emit(" skill_agent/ webapp/ --include=*.py` 排除 def emit 后调用点为 0；`grep -rn '\.emit'` 全仓仅命中 webapp/session.py:153 的 `agent.on_event = hooks.emit`（这是 WebSession.Hooks.emit 往 SSE 队列塞 agent 事件对象，**不是** HookManager.emit 的会话钩子）与 tests/test_hooks.py 的手工 h.emit(...)。2) 重跑 HOOK-13.py 真打模型（exit=0）→ traces/20261020T160843-3d7e.jsonl 仅 event_kinds==['pre_tool_use','post_tool_use']，has_session_start/end 均 False。3) 重跑 HOOK-13_offline.py 确定性复现：注册了 session_start/end 各 1 个 trace 钩子，跑完一轮工具周期后仍只有 pre/post 两行，session_start_present_after_cycle=False。会话事件确为死代码。 | 不具备（已知边界） |
| FEAT-01 | TodoWrite 默认不挂载（未开启时工具集与改造前一致） | 进程内构造 ToolSet（默认 vs enable_todo=True），读 registry_tools.names() 与 schemas_for(None) 工具名列表 | 复核确认（亲跑复现，确定性无网络）。重跑 FEAT-01.py：默认 registry_tools.names()=['Bash','Edit','Glob','Grep','Read','Skill','TaskOutput','Write']，TodoWrite 不在 registry 也不在 schemas_for(None)；enable_todo=True 时两处均 True。挂载点 tools.py:126-127（if enable_todo：register(_tool_todo())）与默认关一致。 | 具备 |
| FEAT-08 | CLI 端检索开关端到端：--index-rebuild 真落盘 code.db 并打印计数；config.json retrieval:true 也生效 | subprocess 跑 CLI（Agent env python -m skill_agent），带/不带 --index-rebuild，校 stderr 行 + 文件存在/大小；再 sqlite3 查 files/chunks 计数；另用项目内 index-dir 反证可生成有效索引 | 降级（原判具备）。我亲跑复现得到与原日志逐字相同的输出：case A rc=0、'[检索] 索引重建完成：0 文件 / 0 建立 / 0 移除 → /tmp/idx_probe/code.db'、code.db 存在 size=49152；case B '已启用'。断言本身非恒真（有 exists/size/行文本三条件），但我进一步查 sqlite：/tmp/idx_probe/code.db 与 /tmp/idx_probe_b/code.db 的 files=0、chunks=0——索引里一条语料都没建。原因：retrieval._corpus_roots()（retrieval.py:388-405）在 include_dirs 为空时把 root 上溯找 project，而 idx_root=/tmp/idx_probe 上溯得到不存在的语义 project → 返回 [] → 0 文件。我反证其可具备：把 index-dir 设为项目内 .skill-agent/index_revprobe 重跑，得 '101 文件 / 101 建立'、DB 内 files=101/chunks=1769（清理后未留痕）。因此 CLI 开关与 disk 落盘确为真（构建在可写目录/可枚举语料时真实发生），但原证据选的 --index-dir 恰是语料为空的路径，只能证明『打印计数 + 建出 sqlite schema 文件』，不足以支撑 matrix FEAT-08 反例所排斥的『纸面开关』整体结论，且 case B『已启用』分支从不触发构建、同样落在空索引。判部分具备：开关接线与落盘具备，『真落盘有效索引』在给定探针路径下不成立。 | 部分具备 |
| FEAT-09 | #14 cache_control：默认不打；开启后在 system 末块 + 最后一个工具定义打 ephemeral，且不改消息内容 | 重跑原作者 _payload 结构探针 + 人读 client.py 打标逻辑与 config→client 接线。 | 复核确认。重跑 FEAT-09.py(3 工具)：默认关 → system=='SYS'(str 原样)、tools_cc==[False,False,False]、messages 未改；开启 → system==[{'type':'text','text':'SYS','cache_control':{'type':'ephemeral'}}]、tools_cc==[False,False,True]（只在末个工具打标）、messages 仍==[{'role':'user','content':'hi'}]；system 为 list 时末块打标首块无；on_differs=True。逆向核对 client.py:99-116 _apply_cache_control 与探针行为逐条吻合，且 agent.py:70-73 把 cfg.prompt_cache 真传进 AnthropicClient、cli.py @ 143/config.py 接线到位。反例（对每工具都打标/改 messages/开关无效）均不成立。 | 具备 |
| SBOX-01 | 默认档位 host 且行为与改造前逐字节一致（未选档时不改变语义，默认关闭新后端） | 重跑 /tmp/agent_probes/r2/SBOX-01.py; 另核 sandbox.py:79-83 HostBackend.fg 字面量等于断言 expected; 默认档未选即 config.py:205 sandbox='host'。 | 复核确认(亲自复现): argv='echo hi'(str), kw={'shell':True,'executable':'/bin/bash','cwd':'/tmp','timeout':7} 与 HostBackend.fg 源码逐字段一致; kw_exact_match=True, has_preexec_fn=False; 默认 ToolSet.sandbox=HostBackend, Bash pwd is_error=False exit=0。第 1 点直击 matrix 反例(额外 preexec_fn / argv 变 list)——两者均未发生。 | 具备 |
| SBOX-02 | 后端工厂 build_backend 按档位选后端；未知档位 fail-soft 回退 HostBackend 并返回警告文案（不抛异常） | 重跑 SBOX-02.py; 对照 sandbox.py:259-261 _fallback 分支。注意该探针用匿名配置对象而非真解析 config.json,但 build_backend 是判据所指单元,可接受。 | 复核确认(亲自复现): host->(HostBackend,None); bwrap->(HostBackend, "M9：未知沙箱档位 'bwrap'，已回退宿主执行。"), unknown_is_host=True, 两子串断言均 True, 未抛异常; rlimit->RlimitBackend。第 1 点否掉了 matrix 反例(抛异常/返回 None/非 HostBackend)。 | 具备 |
| SBOX-03 | 选 docker 档但未配置镜像（或本机无 docker）时 fail-soft 回退 host 并给一行提示 | 重跑 SBOX-03.py; which docker=/bin/docker, docker --version 26.0.0; 对照 sandbox.py:180-191 probe 中 image 检查在 shutil.which 之前。 | 复核确认(亲自复现): docker 无镜像->(HostBackend, is_host=True, not_docker_backend=True), warn 含 '未配置容器镜像' 与 '已回退宿主执行'; probe(None)=(False,'未配置容器镜像（sandbox_spec.image）')。本机 docker 26.0.0 存在, image 仍先于 which('docker') 被查——顺序正确, 正对 matrix 点名的顺序陷阱。 | 具备 |
| SBOX-04 | sandbox_strict 时档位不可用改 fail-closed（抛 SandboxUnavailable 而非回退） | 重跑 SBOX-04.py; 审 sandbox.py:252-267 确认 ok 时直接 return backend,None, strict 不介入。 | 复核确认(亲自复现): docker+strict=True -> 抛 SandboxUnavailable(verdict_probe=CLOSED), 未返回 HostBackend; bwrap+strict 同样抛。matrix 反例(strict 仍回退不抛)未发生。另读源码确认 strict 仅在 _fallback 内生效(sandbox.py:254-257), 可用档位不会误抛。 | 具备 |
| SBOX-05 | host 后台任务写宿主可见的活 pid 文件并 cd 进 self.cwd（本次修复核心：command.sh 落的是真脚本而非原始 command） | 重跑 SBOX-05.py; 核 tools.py:356-367 _bash_background 确实把 sandbox.bg 返回的 script 写入 command.sh 并 Popen(launch_argv, cwd=self.cwd)。 | 复核确认(亲自复现): 后台 sleep 4 -> task_id=9f87baff5c, pid 文件存在值=111407, os.kill(111407,0) 不抛, TaskOutput.pid_alive 运行中=True 结束后=False; where.txt 内容==self.cwd; command.sh 首行='cd "/tmp/tmped91qasy" && echo $$ > ".../pid"' 含 'echo $$ >'(真脚本非原始 command)。正对 matrix 反例(pid 空/pid_alive 恒 None/command.sh 是原始 command)。 | 具备 |
| SBOX-06 | rlimit 前台限地址空间（RLIMIT_AS / preexec_fn 真下发） | 重跑 SBOX-06.py; 核 sandbox.py:107-131 _preexec 惰性 import resource 并 _set(RLIMIT_AS,...), fg 真下发 preexec_fn。 | 复核确认(亲自复现): RlimitBackend({'as':1}).fg kwargs 含 preexec_fn(type=method); as=256MiB 分配 1GiB -> MemoryError + '退出码 1' + is_error=True; 对照 as=4GiB 同命令 is_error=False。matrix 反例(256MiB 下仍成功打印 ok)未发生。附注: 探针某行 limited_ok_absent=False 是假阴性——因输出回显了命令文本里的字面 'ok', 真正判据是 MemoryError/退出码 1, 不影响结论。 | 具备 |
| SBOX-08 | docker 强隔离-断网（--network none 真传进 docker run，容器内连外网失败） | 重跑 SBOX-08.py; 额外手工: docker run --rm --network none ... (Errno 101) vs docker run --rm ... (CONNECTED, exit=0) 对照。核 tools.py:279 前台经 sandbox.fg, 路径真实。 | 复核确认(亲自复现)+补关键对照: probe=(True,''); argv idx5 连续 ['--network','none']; ToolSet 走 DockerBackend 容器内连 1.1.1.1:80 -> is_error=True exit=1, 完整 traceback 为 OSError [Errno 101] Network is unreachable。**我另跑了缺失的对照**——同镜像同命令去掉 --network none -> 打印 CONNECTED exit=0, 证明不是镜像本身无网, 而是 '--network none' 生效。matrix 反例(容器内能连外网)被否。 | 具备 |
| SBOX-11 | docker 后台用宿主监督 pid（先 echo $$ 再 exec docker run --rm），TaskOutput 判活正确 | 重跑 SBOX-11.py; 核 sandbox.py:222-231 DockerBackend.bg 先 echo $$ 再 exec docker run, 且 tools.py:701-715 TaskOutput 用宿主 os.kill 判活, 故 exec 后 pid 不失效。 | 复核确认(亲自复现): probe=(True,''); 生成的 bg 脚本='cd "/tmp" && echo $$ > "/tmp/pid1"\nexec docker run --rm --name traitpilot-t1 --network none ...' 含 'echo $$ >' 与 'exec docker run --rm'; 后台 sleep 5 -> pid 文件值=113505, os.kill0 不抛, pid_alive 运行中=True 结束后=False(宿主监督 pid 生命周期正确)。正对 matrix 反例(pid 文件不存在/pid_alive 恒 None)。 | 具备 |
| MEM-01 | 默认关闭：未开启 summary_compaction 时压缩行为与改造前一致（丢旧消息，无摘要） | 重跑MEM-01.py（结果逐字一致），另写/tmp/review_probe.py用Agent(Cfg(),R(),T())零注入复现真默认，核对config默认值与门控源码 | 复核确认。原probe用build_agent(compact=False)机制上等价：Agent.__init__本就设summary_compressor=None/summarize=None（agent.py:85-86），我改用「零注入」真默认路径复跑：history=50→new_len=40、_compacted=True、summarize从未被调用、history[0]无「【前情摘要」；config.py:181 summary_compaction默认False。与agent.py:426门控(需compressor且summarize非None)一致。 | 具备 |
| MEM-02 | 开启后保留最近 K=12 条，更早区间压成一条结构化摘要并入最近窗口首条 user；关键字段不丢 | 重跑MEM-02.py；另写/tmp/review_probe2.py用100条多轮工具历史复测合法性与悬空tool_use | 复核确认并加严。原probe 50条含tool_use/result配对→new_len=10、POST_VALID=True、首条user含「【前情摘要」、summarize_called=1、末条保留、无连续同role。我构造更狠的100条(25轮真实tool_use/tool_result)+末条为完整assistant text：new_len=12、valid=ok、dangling_tool_use=False、prefix=True。COMPACT_KEEP_RECENT=12与len≤13一致。 | 具备 |
| MEM-03 | 结构化摘要模板强制保留阶段号/参数/路径/pid/审批结论等不变量字段 | 重跑MEM-03.py，核对render_prompt→SUMMARY_TEMPLATE.format(history=body)链路 | 复核确认。传summarize的prompt(916字)同时含「阶段号/后台任务/审批结论/log_path」四词，ALL=True。非恒真：这些词来自SUMMARY_TEMPLATE(compaction.py:14-27)强制字段，模板被清空即失败。 | 具备 |
| MEM-04 | 摘要失败/无回调时回退为丢弃旧消息，绝不阻塞主循环 | 重跑MEM-04.py，核对agent.py降级分支源码 | 复核确认。A(summarize抛异常)、B(返回None)两路降级：history仍50→40、_valid_history=True、首条无「【前情摘要」(走旧丢弃逻辑)；经_run主循环stub客户端answer()=RUN_OK calls=1无崩溃。契约'绝不阻塞主循环'成立(agent.py:452-455 try/except→None; 430回退分支)。 | 具备 |
| MEM-05 | 压缩后边界写入持久化日志（_prefix），resume 时作为本轮起点覆盖，避免重复压缩 | 重跑MEM-05.py，核对session_store._read_raw的_prefix覆盖逻辑与agent._persist_prefix | 复核确认。真落盘50行→压缩后52行且含"_prefix"行；store.load('s1')返回2条、首条含「【前情摘要」、早期q0/q2被覆盖；restore进新Agent后再_maybe_compact→RECOMPACT_CALLS=0、len=2。session_store.py:140-144的_prefix分支确实以[prefix]覆盖raw。 | 具备 |
| MEM-06 | core.md 内容被注入 system prompt 稳定前缀（而非消息尾部） | 重跑MEM-06.py，核对_system_prompt与session.py装配 | 复核确认。set_core后answer('你好')：CORE文本出现在system、core.md真落盘、SECTION_HEADER('## 长期记忆')在system、CORE_IN_MESSAGES=False。agent.py:57-59把core追加为稳定前缀、_run:156取memory.get_core()。缺口(note)：probe直接set ag.memory绕过了memory_enabled→MemoryStore的配置装配(session.py:34/webapp),但注入层本身已证实。 | 具备 |
| MEM-07 | archival 记忆按 query 检索 top-k 并注入（渲染为「## 相关历史记忆」块） | 重跑MEM-07.py；另写/tmp/review_probe.py对7种query形态测search()命中 | 复核同意原判并加证。清单原样query『继续执行阶段 S3 那个任务』→search()=[]无注入(MATRIX_QUERY_RECALL=False)；我另测『阶段 S3』→命中、『rMVP』→命中、『grm』→命中，但『S3 任务』『输出文件在哪』→均[]。即命中要求query含≥3字连续子串且与记忆文本有重叠；fuzzy自然语言query普遍落空。注入路径(render_context/尾部块)本身工作。 | 部分具备 |
| MEM-10 | archival 搜索对畸形/空/纯符号输入不抛异常，返回空列表（LIKE 兜底 2 字中文） | 重跑MEM-10.py；另写/tmp/review_probe2.py fuzz 24输入+核对sanitizer正则 | 复核确认并加严。我以24种输入(含None/''/空白/制表/***/a" OR )(/'; DROP TABLE mem;--/emoji/1万字符/中文)打search()，0异常、恒返回list。另证FTS语法错分支实际不可达：re.findall(r"[\w一-龥]+",q)已过滤引号/括号，故畸形FTS语句进不去MATCH(如'a" OR )('→terms=[]直接LIKE)。小quirk：'%'/'_'是LIKE通配符会返回1命中，但判据只要求不抛异常/纯符号空列表，不构成降级。 | 具备 |
| MEM-11 | 分层记忆整体禁用时静默无副作用；FTS5/trigram 不可用的环境自动降级（不崩溃） | 重跑MEM-11.py，核对memory.py:36-46与142-159降级分支 | 复核确认。A(enabled=False)在尚不存在目录上：get_core=''/search=[]/all_items=[]、目录与core.md/archival.db均未创建(对比启用档均创建)；B猴补sqlite3.connect使建FTS虚表抛错→_init_db自动enabled=False且后续操作不崩、EXIT=0。环境FTS5=True/TRIGRAM=True。B为模拟(本机无法真缺FTS5)，但代码路径明确。 | 具备 |
| MEM-12 | config 生效为 `cfg.memory_enabled=True` | 逐字节备份 config.json；json 加 memory/summary_compaction；bash start_web.sh restart；urllib 发一轮；os.path.exists/getsize 查 archival.db；finally 还原并校验字节一致+重启；另 sqlite3 查 mem 行数与目录还原 | 复核确认（我从干净态亲跑复现）。原探针的 archival_exists_before=False 有歧义——脚本未清理上次的 archival.db，可能是旧文件（该 db 在 16:18 已因他处会话 >config 缺 memory 键< 但 webapp 却建于 16:18，存疑）。我消除疑点：先把 .skill-agent/memory 整体移走→确认缺失→重跑 MEM-12.py：restart_rc=0、api_up=True、SSE 仅 thinking/text/usage/done、然后 memory_dir_exists=True、archival_db_exists=True、archival_db_size=28672，finally config_restored_identical=True 且 config.json md5=4be2a012198d79be9ec9c1f50200c061 与备份一致。故 Web 端确实按 config.json 的 memory:true 装配 MemoryStore 并真落盘 archival.db（webapp/session.py:175-183）。边界（写入判据但属其实质更强主张）：archival.db 系 MemoryStore.__init__ 建表即产生，我查 mem 表 rows=0，本轮消息（pid=12345 等）经 _remember_phase_facts 未写入，故证据只覆盖『开关生效+文件出现』，未覆盖『记忆真在写』——matrix 判据本身也只要求文件出现，故不改判，但提示该 db 出现不等于有记忆内容。收尾：memory 目录已还原为原 backup，config 字节一致。 | 具备 |
| SKILL-01 | 技能发现：扫描 <project>/skills 下每个含 SKILL.md 的子目录，注册进 SkillRegistry | 以 -I 重跑 /tmp/agent_probes/r2/SKILL-01.py 并逐行核对输出；独立确认 fs 差集两侧均为空 | 复核确认复现：python -m skill_agent list --json exit=0；json键18 vs 文件系统含SKILL.md子目录18；missing=[] phantom=[]；field-mismatch=[]（18键均恰为{description,path,model_invocable,allowed_tools}）；非--json末行'共 18 个 skill。'。探针用真实文件系统集合做双向差集，非恒真断言，判定成立。 | 具备 |
| SKILL-02 | 不回退 .claude/skills：只读项目自带 skills/，外部/用户级技能不得混入 | 以 -I 重跑 SKILL-02.py；核对 legacy 排除与 real 命中的正向对照 | 复核确认复现：临时proj植入 .claude/skills/legacy-one + 真项目 skills/real；names=['real']，legacy found=False，real found=True；skills_dir覆盖后 names=['custom-skill','real']，legacy仍False。探针含正向对照（real技能确被发现）防'只看空集'假证，判定成立。 | 具备 |
| SKILL-03 | 技能目录可被 --skills-dir / config.json skills_dir 覆盖，且优先级正确（CLI > config > 默认 <project>/skills） | 以 -I 重跑 SKILL-03.py，核对四层输出与 CLI 子进程 rc=0 | 复核确认复现：默认 names=['own']；skills_dir=custom→含custom-skill=True；config.json skills_dir解析为绝对/tmp/...→names=['from-config','own']；子进程CLI --skills-dir 覆盖 config → has from-cli=True 且 has from-config=False（CLI胜）。三层优先级各由独立路径真跑，判定成立。 | 具备 |
| SKILL-08 | allowed-tools 生效：激活技能后当轮工具集按其 allowed-tools 收窄（Skill/TaskOutput 保底） | 以 -I 重跑 SKILL-08.py + 自写 /tmp/rev_skill08.py 覆盖未绑定/多技能/幽灵技能/首步调用激活四态 | 复核确认复现（并加严验证）：parsed allowed_tools=['Read']；schemas_for(None) 8工具；schemas_for({Read})=['Skill','Read','TaskOutput']（Write/Edit/Bash剔除，保底在）；hitless→['Skill','TaskOutput']。我另写 /tmp/rev_skill08.py 做真·端到端：FakeClient 首步返回 Skill 调用，step1工具集=全部8个、step2工具集=['Skill','Read','TaskOutput']，白名单真实收窄；已注册但无allowed-tools的skill→_allowed_set()=None（不限制，正确）。边界备注（不降级）：未注册的技能名（ghost）→_allowed_set()=set()→schemas_for(set())→回退全集（略微放宽），但实际激活路径只加入 registry 中存在的技能，正常流程不触发。 | 具备 |
| SKILL-11 | Web 端点 /api/skills 暴露技能清单（前端技能抽屉数据源） | 以 -I 重跑 SKILL-11.py；独立 curl -w %{http_code} 与 ps 校验服务进程与源码一致性 | 复核确认复现：urllib GET http://127.0.0.1:8787/api/skills → HTTP 200；len=18；keys=['allowed_tools','description','model_invocable','name','path']；gwas-pipeline model_invocable=True allowed_tools=['Read','Write','Bash','Grep','TaskOutput']；rows missing fields=[]。已确认8787端口服务进程(pid 92960, run_web.py)存在，且源码 webapp/server.py:199 api_skills 未被改动（时间戳早于探针），非陈旧进程假证。 | 具备 |
| USAGE-01 | 单轮内多步调用累计 token/steps/延迟（steps、output_tokens、latency_ms、model） | 以 -I 重跑 USAGE-01.py，逐项核对（含 ==7 的反向断言） | 复核确认复现：usage_bar={'test-model',steps=2,output_tokens=18,latency_ms=0}；steps==2 True；output_tokens==18 True 且==7 False（排除退化为末次值）；model 正确；latency_ms 为 int>=0。stub FakeClient 两次调用(11+7)，断言具区分度。 | 具备 |
| USAGE-02 | 新一轮用户请求重置用量（不跨请求/跨会话串扰） | 以 -I 重跑 USAGE-02.py，核对重置值与两 Agent 隔离 | 复核确认复现：round1后steps=2，round2后steps=2（非4）；cfg对象无_usage/usage属性（未污染共享Config）；同cfg两Agent各自steps=2互不影响（ag2先为0）。隔离性由真实对象检查而非口头断言，判定成立。 | 具备 |
| USAGE-03 | 每次模型调用 emit usage 事件，字段齐全（step/output_tokens/total_output_tokens/latency_ms） | 以 -I 重跑 USAGE-03.py，核对字段集合与累计值 | 复核确认复现：usage事件数=2；事件契约为 {type,step,output_tokens,total_output_tokens,latency_ms} 全齐；step序列[1,2]；末条 total_output_tokens==18。逐事件字段集合校验，非仅计数。 | 具备 |
| USAGE-06 | GET /api/usage 真跑一轮后返回该会话累计（E2E） | urllib 直打 /api/chat 读 SSE，解析 done/usage 事件与 GET /api/usage 比对四字段；用 Agent env python -I 复现 | 复核确认（我亲跑复现）。重跑 /tmp/agent_probes/r2/USAGE-06.py：新会话 u06_ff6fae72 发一轮后 done.usage={model:'DSv4-flash',steps:1,output_tokens:44,latency_ms:3515}，GET /api/usage 200 返回同四字段逐字一致，SSE event:usage 计 1 条 total_output_tokens=44。原报告 output_tokens=70 是首跑值，接口行为一致。端点实现 webapp/server.py:189-196 读 s.agent.usage_bar，与 session.py:96 done 事件同一来源（agent.usage_bar，agent.py:106 快照 _usage），故 done 与端点本就同源、非各自造假——判据要求的『不等于 0 / 不互相矛盾』成立。 | 具备 |
| USAGE-07 | 未知会话 GET /api/usage 明确 404 而非假装成功 | urllib 请求未知会话，HTTPError 捕获 status/body，断言 404 且 ok is False 且 error 非空 | 复核确认（亲跑复现）。重跑 USAGE-07.py：GET /api/usage?session=does-not-exist-xyz-123 → 404，body={"error":"session not found","ok":false}。与 webapp/server.py:193-195 分支一致。边界说明：该判据只覆盖『未注册会话』这一种未知；对已存在的会话恒返回 200（设计如此），不构成反例。 | 具备 |
| USAGE-08 | 流式 usage 事件到达前端并驱动顶部徽标 | Playwright chromium headless 真开页面发一轮，抓 /api/chat SSE 统计 usage 事件数，轮询读 #usageBadge 文本/title 并 regex 解析 | 复核确认（亲跑复现，但须换解释器）。探针用 playwright，Agent env 无 playwright（ModuleNotFoundError），原作者实际是用 /home/chenyuxi/anaconda3/bin/python 跑的；我用该解释器复现：badge_before='tokens 0' → badge_after='tokens 45 · 3.8s · 1 轮'，title='模型 DSv4-flash · 输出 token 45 · 累计延迟 3791ms · 模型轮次 1'，page.on('response') 抓到 1 条 event:usage(output_tokens=45)。断言非恒真：正则 tokens (\d+) 解析真值、徽标真由 0 变 45。缺口提示：解释器路径未写进证据串（属方法记录问题，非结果问题）。 | 具备 |
| USAGE-09 | 配置优先级：环境变量 > config.json > 代码默认 | 以 -I 重跑 USAGE-09.py，三态逐一核对 | 复核确认复现：env设 ANTHROPIC_BASE_URL=env-wins/SKILL_AGENT_MODEL=env-model→'http://env-wins:1 env-model'（env>file）；env清空→'http://file-url:9 file-model'（file>default）；无file无env→'https://api.anthropic.com claude-opus-4-5'（=DEFAULT_MODEL）。三态用独立 tempfile 隔离，判定成立。 | 具备 |
| USAGE-10 | config.json 作为单一控制面板：所有可选能力开关都能被读到并生效 | 以 -I 重跑 USAGE-10.py 并核对 25 行 OK | 复核确认复现：25键全OK，FAILURES=none。含 retrieval/todo/parallel_tools/prompt_cache/loop_guard/idempotency/restrict_paths/write_approval/summary_compaction/persist=True；memory→memory_enabled=True；spill→spill_dir非空；trace→hooks_enabled=True；sandbox='rlimit'；sandbox_spec={'memory':'4g'}；max_turns=120；spill_threshold=12345；idempotency_mode='deny'；index_dir/retrieval_roots/fallback_models 均生效。逐键比对 Config 实测属性。 | 具备 |
| USAGE-11 | 非法/缺失配置处理：缺文件、坏 JSON、错误类型都不抛且回落默认 | 以 -I 重跑 USAGE-11.py，覆盖缺文件/坏JSON/四类错误类型/数字串 | 复核确认复现：缺文件→load_config_file返回{}不抛；坏JSON '{not valid json'→{}不抛；retrieval:'yes'→False、max_turns:'abc'→None、sandbox:'nonsense'→'host'、fallback_models:5→空；数字串'42'→42（合法强转仍在）。备注（不降级）：fallback_models 实测为 None（falsy）而非字面 []，判据 `not cfg.fallback_models` 仍成立，语义为空。 | 具备 |
| USAGE-12 | config.json.example 与 config 逻辑一致（无未映射/纸面键）+ env 块注入进程环境 | 以 -I 重跑 USAGE-12.py，核对键集 diff 与 env 注入/优先序/空串三态 | 复核确认复现：example非注释键34个，Config字段46，未映射键=[]；env块 GENO_DIR=/tmp/tgeno、PIPELINE_DB=/tmp/t.db 真注入os.environ；已存在 GENO_DIR=/already 不被覆盖；空串值(EMPTY_X)被忽略仍为None。注入与优先序由真实 os.environ 检查，判定成立。 | 具备 |

---

## 9. 批次 3：FEAT 模块补跑 9 条（含对抗性复核）

方法：4 组探针（TodoWrite / Search+RepoMap / 索引安全+配置优先级 / 模型降级重试），每组再由独立 agent 对抗性复核：读探针判其是否真在测那件事、**自己重跑**、给判「具备」的构造反例、给判「不具备」的设法证其具备。复核方**降级 1 条**（FEAT-12）。
探针：`/tmp/agent_probes/r3/<ID>.py`；复核后汇总：`/tmp/agent_probes/r3/final.json`。

**本批非「具备」**：

| # | 能力点 | 复核后结论 | 理由 |
|---|---|---|---|
| **FEAT-12** | 配置接线：CLI 参数 > 环境变量 > config.json | **部分具备** | 见下 **D12**：CLI 确能压过文件（`cli.py:141-150` 在 `Config.load` 后覆盖）、env-only 时 4/4 生效；但 **config.json 与 env 冲突时 6/6 是 config.json 赢** → 方向反了。pytest 全绿**掩盖**了这点：矩阵点名的 `test_config_json.py::test_env_overrides_config_file` 只测 `api_key`（走 CLI>env>file 的真优先级），toggle/list 键从未被覆盖测到 |

### 9.1 明细

| # | 能力点 | 怎么测（探针要点） | 实测证据（复核后） | 结论 |
|---|---|---|---|---|
| FEAT-02 | TodoWrite 开启后可写入结构化清单，返回渲染文本 + meta 进度 | 独立以 /home/chenyuxi/anaconda3/envs/Agent/bin/python -I 重跑原探针，直连 ToolSet+Agent(stub client) 只读 ToolResult.content/meta/is_error 与 agent.history；并自加 5 组边界输入（空/全完成/缺status/缺键/非字符串）构造反例；源码核对 todo.py 与 tools.py。 | 复核复现：以 -I 重跑 /tmp/agent_probes/r3/FEAT-02.py，输出与结论完全一致——default_mounted=False；enabled_mounted=True；合法写入 content='计划进度：0/2 已完成\n[~] 跑 QC\n[ ] 比对'、meta.todo={'completed':0,'in_progress':1,'pending':1,'total':2}、is_error=False；第二条3项 r2_first_line='计划进度：1/3 已完成'、r2_meta={'completed':1,'in_progress':1,'pending':1,'total':3}；端到端 history 唯一 tool_result content 同渲染文本且 is_error=False。探针断言非恒真：逐行 contains/startswith 与 meta 逐键真值，且有默认不挂载对照。另加边界反例（探针未含，我亲跑）：空数组→is_error=False content='（无待办项）' meta 全零；全 completed→'2/2 已完成' 且逐行 [x]；缺 status→默认 pending；缺 todos 键/ todos 非字符串→参数层 is_error=True meta={}（拒绝）。均无『只回显 JSON/无渲染/无 meta/抛异常』反例。源码核对 todo.py render/progress 与 tools.py:566-589 一致。无缺口。 | 具备 |
| FEAT-03 | TodoWrite 失败模式：非法 JSON / 缺 content / 非法 status / 多条 in_progress | 独立以 /home/chenyuxi/anaconda3/envs/Agent/bin/python -I 重跑原探针，逐类畸形 todos 走 ts.execute 观察 is_error/content/meta，并在 Agent(stub) 端到端核对 history 中坏调用 tool_result 无计划渲染文本；另加缺键/非字符串反例；源码核对 todo.py 与 tools.py。 | 复核复现：以 -I 重跑 /tmp/agent_probes/r3/FEAT-03.py，8 类畸形输入全部 is_error=True、meta={}、leaked_plan=False，报错文本与 matrix 判据逐字吻合：bad_json→'输入校验失败：todos 不是合法 JSON：Expecting value: line 1 column 2 (char 1)'；missing_content/blank_content→'第 1 项缺少 content。'；bad_status→"第 1 项 status 非法：'weird'（应为 pending, in_progress, completed）。"；two_inprogress→'同时最多只能有一条 in_progress（计划态须唯一）。'；not_array→'todos 必须是数组。'；item_not_obj→'第 1 项不是对象。'；empty→'todos 不能为空。'。端到端 history_toolresult_count=2：tr0.is_error=True 且 bad_result_has_plan_text=False（坏调用未污染计划态），tr1.is_error=False、good_result_has_plan_text=True。断言非恒真（逐类取真值、并交叉核对 history 块字段）。另加反例：缺 todos 键/todos 非字符串→参数层 is_error=True meta={}。源码 todo.py:36-57 校验与 tools.py:571-572 错误回填一致。未发现『默默接受写 history/只 print 警告 is_error=False/返回成功含两条 in_progress』反例。无缺口。 | 具备 |
| FEAT-04 | Search 返回 BM25 命中的「文件:起止行 + 片段」，行号真实存在 | 真跑探针全量复现 + 多query交叉核对行号真实性 + 反例(无意义query/空区间)检验 + pytest | 复核确认，亲自复现。重跑 /tmp/agent_probes/r3/FEAT-04.py：12/12 PASS。真实数据：CodeIndex(skill_agent).search('cache_control',limit=3) n=3，hit[0] path=/home/chenyuxi/skill/agent/skill_agent/client.py start=99 end=116，文件322行，lines[99-116]含cache_control=True，第99行='def _apply_cache_control(self, payload: dict) -> None:'，snippet_len=400。工具层 ts.execute('Search',{'query':'cache_control','limit':3}) is_error=False hits=3，content正则命中 client.py:99-116 且回读该区间含关键字(tool_ref_real=True)。对抗性补测：10个query×≤5 hits共35条命中，报告区间全部非空；无意义query 'zzzqqqxyznotexist'→0 hits（无空结果假成功）。缺口说明：部分query(如'_apply')命中区间内不含整词字面量(命中在symbol字段，配置为 config.py _apply_file_overrides._bool)，但所报区间为真实函数区间而非伪造行号；矩阵该行核心判据(path+真实起止行+回读含关键字)成立。探针索引写入仓库 .skill-agent/ 索引产物(非源码)。pytest tests/test_retrieval.py 21 passed。 | 具备 |
| FEAT-05 | Search 失败模式：空 query / 超短中文 / 畸形符号不崩，缺 query 走结构化报错 | 真跑探针全量复现(含corrupt db构造) + 工具层执行 + 19个畸形query对抗补测 | 复核确认，亲自复现。重跑 /tmp/agent_probes/r3/FEAT-05.py：10/10 PASS。search('')→0、search('!!!')→0、search('" ) OR )(')→0 均不抛；search('')==[]；'中文'(2字)走LIKE兜底 n=1 命中 z.py；enabled=False 时 search=[]/outline=[]/stats.enabled=False；corrupt code.db(b'not a database at all')→ensure()={'indexed':0,...,'enabled':False} 不抛且二次ensure仍disabled。工具层：Search{}→is_error=True 含 '[输入校验失败] 参数: 缺少必填字段 query'；query=' '→'Search 需要 query 参数。'；limit='abc'→'[输入校验失败] 参数.limit: 期望类型 integer'。对抗性补测：19个畸形/FTS特殊query('NEAR(','"unclosed','a AND','*','-','(',')','{','^','OR','AND','NOT','foo*','\x00','%'等)全部不抛。缺口说明：退化1字符/纯符号输入(* - ( % \x00)会经LIKE兜底返回命中，非本行要求，不影响判定。 | 具备 |
| FEAT-06 | RepoMap 输出结构化大纲（kind+行号），且受路径边界与只读约束 | 真跑探针全量复现 + 边界正反对照 + 相对路径穿越对抗补测 + _is_readonly正反检查 | 复核确认，亲自复现。重跑 /tmp/agent_probes/r3/FEAT-06.py：11/11 PASS。RepoMap(mod.py)→is_error=False，meta['symbols']=1，content行 '1 function run_pipeline'(kind+行号)，回读第1行='def run_pipeline():' 含符号名(line_real=True)；idx.outline() Symbol(name=run_pipeline,kind=function,start=1)。路径边界：PermissionModel(restrict_paths=True,roots=[proj.resolve()]) 下 RepoMap('/etc')→is_error=True '路径越出允许的工作区边界：/etc'。对抗性补测(同一边界模型)：'/etc/passwd'、'../x'、'../../etc'、'/tmp/.../agent/../../etc' 全部 is_error=True(已归一化越界路径，边界非形同虚设)；越界内 proj/agent/m.py→is_error=False。不存在路径→'路径不存在'报错。_is_readonly('RepoMap')=True、('Search')=True、('Write')=False。反例(无行号只给名/越界仍可读/_is_readonly未含)均未出现。 | 具备 |
| FEAT-07 | 索引安全：绝不整仓遍历（排除目录/超大文件/二进制后缀），增量刷新 | 重跑 stdlib CodeIndex API 探针（ensure/search/stats + sqlite3 真值回读）两份脚本；独立跑矩阵指定 5 条 pytest；读 retrieval.py 核验常量与剪枝/增量/schema 逻辑；另写反例探针验大小写后缀、2MB 边界、同尺寸同 mtime 改动、大文件分块截断。 | 复核确认具备（本人重跑复现）。用 /home/chenyuxi/anaconda3/envs/Agent/bin/python -I 重跑 /tmp/agent_probes/r3/FEAT-07.py 与 FEAT-07-real.py，输出与原日志逐项一致：受控语料 indexed=3 files=3 且 indexed_paths 真值仅 [agent/mod.py, agent/second.md, skills/x/SKILL.md]；7 项泄漏探针（__pycache__/.git/saved/.pth/.bin/.log/huge>2MB）全 false，可读文本 .pth/.bin/.log 仍因后缀被拒；ensure2 indexed=0 skipped=3（增量非全量）；改文件 indexed=1 且新 marker 可检索；删 second.md removed=1 files 3→2 且旧 marker search=false；max_files=3 时 10 候选→files=3；schema 置 999 后重建 enabled=true files=2。真仓探针复现 seen_candidates=202、files=101、db_bytes=5025792(~5.0MB，非 GB)、bad_ext_count=0、bad_dir_count=0、二次 ensure indexed=0 skipped=101。/home/chenyuxi/anaconda3/bin/python -m pytest（Agent env 无 pytest）跑矩阵指定 5 条 retrieval 用例，独立通过。源码核对：retrieval.py:415-452 _iter_files 在 os.walk 内就地剪枝 _EXCLUDE_DIRS 后才 stat、_should_index 用 p.suffix.lower()（大小写不敏感）匹配 _EXCLUDE_SUFFIXES、max_file_bytes=2_000_000；473-520 _refresh 按 (mtime,size) 跳过。缺口标注：①真仓语料本身不含权重/大目录，其 seen/db 断言非对抗性（生成语料才真正覆盖排除项）；②本人构造正交反例发现两处既有边界（均与 FEAT-07 声明判据不冲突故不降级）：(a) 同尺寸+同 mtime 的改文件会被跳过（增量仅凭 mtime/size，属脏数据隐患）；(b) >6000 字符的单体 .py 模块内容在 chunk_python 的 MAX_CHUNK_CHARS 截断下丢失、末尾 marker 不可检索（chunk 完整性问题，非索引收录问题）。 | 具备 |
| FEAT-10 | #12 模型降级：主模型 429 耗尽重试后按列表切后备，生效模型被记录 | 复跑原脚本（/home/chenyuxi/anaconda3/envs/Agent/bin/python -I，exit 0），逐行核对 /tmp/agent_probes/r3/FEAT-10.py 与源码 skill_agent/client.py:50-84、118-136；另起独立真桩并改输入构造反例（重复后备、400 非重试错误降级、401 降级）亲自复现。 | 复核确认，无降级。原脚本非恒真：断言 seen==['primary','backup'] 且 c.model=='backup' 且 blocks 含文本，来源为真桩 HTTP 服务器的实际请求 model 字段，排除恒真与桩内自证。复跑结果与报告一致：C1 seen=['primary','backup'] c.model=backup texts=['ok']；C2（SSE 流式）seen=['primary','backup'] c.model=backup texts=['streamok']；C3 无后备 raised=APIError seen=['primary']（未空转）；C4 fallback=['A','B']（含主模型）seen=['A','B']，A 仅一次。源码 client.py:67 models=[self.model]+[m for m in fallback if m!=self.model] 证实主模型自去重；:74-75 仅在 mi>0 时回写 self.model。反例探针（/tmp/adv_r3.py）证实：fallback=['B','B'] 去重后 seen=['A','B']、B 仅命中一次（无重复打同一模型）；主模型 400/401 时也会降级到 backup（seen=['primary','backup'], c.model=backup）——矩阵三反例（降级未接线/ c.model 仍 primary / 重复请求）均不出现。注：原脚本含 resp2/do_stream_post 等未用死代码，不影响结论。无未复现缺口。 | 具备 |
| FEAT-11 | #12 重试语义：429/529/5xx 才退避重试，鉴权/参数错误立即抛出，Retry-After 被尊重 | 复跑原脚本（python -I，exit 0）；对照源码 client.py:118-136/257-260/286-314；额外独立探针 /tmp/adv_r3b.py 用无 Retry-After 头 vs Retry-After:3 对比相邻请求实时间隔，并叠加 529 场景，验证速率为头真实驱动而非巧合。 | 复核确认，无降级。Part A 直调 _http_error/_retry_after_seconds：429/529/500/502/503→retryable=True，400/401/404/422→False；Retry-After:12→12.0，无头→None。Part B 真桩复跑（流式+非流式各 5 场景全 OK）：B1 429x2→200 calls=3 gaps≈1.003（两条路径）证按 Retry-After 等待；B2 401 calls=1 retryable=False 立即抛；B3 400 calls=1 立即抛；B4 500→重试成功 calls=2；B5 max_attempts=3 耗尽 calls=3 抛 APIError。反例探针证实退避确由头驱动：无 Retry-After 时 gap≈1.996（指数退避 1.5+jitter≈2.0），Retry-After:3 时 gap≈3.003，并非固定小延迟猛刷；529→200 calls=2 成功（529 确被重试）。矩阵三反例（401 被重试/5xx 一次放弃/无视 Retry-After）均不出现。无未复现缺口。 | 具备 |
| FEAT-12 | 配置接线：CLI 参数 > 环境变量 > config.json | 重跑三份 Config 探针（config.json 读入/类型容错、CLI 解析、config.json+env 冲突矩阵、env-only/file-only 对照）；跑矩阵指定 4 条 test_config_json pytest（9 passed，含 test_env_overrides_config_file）并核对其实测对象仅 api_key；读 config.py Config.load/_apply_file_overrides 与 cli.py:141-150 定位优先级实现。 | 复核确认「部分具备」，降级成立（本人重跑复现优先级反转）。重跑 FEAT-12.py/-priority.py/-supplement.py 输出一致。正向确认真具备：config.json 8 键全读入；非法类型不抛(wrongtype_raised=null)且逐键回落默认；CLI --fallback-models A,B→['A','B']、--todo/--retrieval/--prompt-cache→True（cli.py:141-150 在 Config.load 后覆盖，CLI 确能压过）。反例亦确凿复现：config.json 同名键=false 且 env=1 时，todo/retrieval/prompt_cache/parallel_tools 4/4 仍 false；fallback_models file=['filem'] 对 env='e1,e2' 时 file 赢；index_dir file='/tmp/file' 对 env='/tmp/env' 时 file 赢——共 6/6 env 未压过 config.json。env-only 时 4/4 生效，故 env 机制本身可用，仅覆盖方向反了。源码根因确认：config.py Config.load 先赋 _env_bool/_env_list 值，随后第 304 行 _apply_file_overrides(cfg, file_cfg) 用 config.json 覆盖（_bool/_list 存在即覆盖）。矩阵 FEAT-12 行(183)明确要求「环境变量覆盖 config.json」且反例判据为「环境变量未能压过 config.json（优先级反了）」，据此降级合理。重要补证：矩阵点名的 test_config_json.py::test_env_overrides_config_file 只测 api_key（api_key 走 CLI>env>file 的真优先级），故 pytest 全绿却掩盖了 toggle/list 键的优先级反转——探针抓到了测试套件漏掉的反向行为。 | 部分具备 |

### 9.2 复核方在探针之外另发现的能力边界（不改变上述判定，但值得记）

| 发现 | 位置 | 说明 |
|---|---|---|
| 增量刷新只凭 `(mtime, size)` 判「未变」 | [retrieval.py:473-520](skill_agent/retrieval.py#L473-L520) | 同尺寸 + 同 mtime 的改动文件会被**跳过**（脏数据隐患）。不违反 FEAT-07 的声明判据，故未降级 |
| 单体大 `.py` 的分块截断 | `chunk_python` 的 `MAX_CHUNK_CHARS` | >6000 字符的单文件模块，末尾内容在分块时被截断、末尾 marker **不可检索**（分块完整性问题，非收录问题） |
| Search 命中可落在 `symbol` 字段 | [retrieval.py](skill_agent/retrieval.py) | 如查 `_apply` 命中的是 `config.py _apply_file_overrides._bool`，**所报区间内不含整词字面量**——但区间是真实函数区间、非伪造行号。matrix FEAT-04 的核心判据（path+真实起止行+回读含关键字）仍成立 |
| 1 字符/纯符号 query 会经 LIKE 兜底命中 | [memory.py](skill_agent/memory.py) / `retrieval.py` | `*` `-` `(` `%` `\x00` 会返回命中。matrix FEAT-05 只要求「不崩」，故不影响判定 |
| FEAT-10 的**反例探针**发现 | `client.py:67-75` | 主模型遇 **400/401 也会降级**到 backup（`seen=['primary','backup']`）。矩阵三条反例未包含此场景；记为此模块的语义边界 |

### 9.3 D12 的独立复现（主测方自跑，与工作流复核方相互独立）

工作流复核方对 FEAT-12 的降级依据出自其自己的探针；为免「复核方与探针作者同源」的循环论证，主测方另写一份**最小复现** `/tmp/verify_d12.py`（不读任何探针产出、只读源码路径），在 Agent 环境单独跑：

```python
# /tmp/verify_d12.py 要点
(d/"config.json").write_text(json.dumps({"todo": False, "prompt_cache": False,
                                          "parallel_tools": False, "retrieval": False}))
os.environ["SKILL_AGENT_TODO"] = "1"; os.environ["SKILL_AGENT_PROMPT_CACHE"] = "1"
os.environ["SKILL_AGENT_PARALLEL_TOOLS"] = "1"; os.environ["SKILL_AGENT_RETRIEVAL"] = "1"
cfg = Config.load(project=d)
print({k: getattr(cfg, k) for k in ("todo","prompt_cache","parallel_tools","retrieval")})
```

| 项 | 内容 |
|---|---|
| 测试内容 | config.json 4 个布尔键显式置 `false`，同环境 4 个 env 变量置 `1`，看 `Config.load` 后谁赢 |
| 命令 | `/home/chenyuxi/anaconda3/envs/Agent/bin/python -I /tmp/verify_d12.py` |
| 实测输出 | `env=1 + file=false → {'todo': False, 'prompt_cache': False, 'parallel_tools': False, 'retrieval': False}` |
| 预期（文档承诺 env > file） | 4 键全 `True` |
| 反例判据 | 「env 未能压过 config.json（优先级反了）」——**4/4 命中** |
| 结论 | **D12 成立**，FEAT-12 降级为「部分具备」由主测方独立复现确认，非复核方单方结论 |


---

## 10. 批次 4：M13-03 … M13-10 补跑 8 条（会话持久化恢复 / 安全 / 归档 / 删除 / 历史回放）

> **背景**：本批是补漏。早先「三批 132 条全跑完」的结论未做用例 ID 双向差集，静默漏掉了 M13 后半段 8 条（见 §0 订正）。本批方法同批次 2/3：**每条一个探针 agent 真跑 + 一个独立对抗性复核 agent 重跑并设法证伪**；复核方对 **2 条**（M13-09、M13-10）给出了降级，主测方已各自独立复现。
> 探针与复核脚本：`/tmp/m13gap/<ID>.py`、`/tmp/m13gap/<ID>_adv.py`，日志同目录 `<ID>.log`。

### 10.0 本批结论

| ID | 用例 | 判定 | 依据（一句话） |
|---|---|---|---|
| M13-03 | 恢复校验：剔除孤立 `tool_use`、截断到安全边界 | **具备** | 场景 1 `roles=['user','assistant','user','assistant']`、末条含 `final` 且无 `tool_use`、`discarded=1`、孤立 tool_use 集合为空；复核方另造 8 场景（崩溃中未回、跨 id result、混合已答+悬挂…）8/8 未命中反例 |
| M13-04 | 丢弃未完成轮次如实提示条数 | **具备** | 三路真跑均为非零：库 `resume_into restored=2 discarded=1`；CLI stderr `[resume] …（丢弃未完成轮次 1）` 与 `[persist]` 并存；Web `done.resume_discarded=1` |
| M13-05 | `load_view`（展示全量）与 `load`（续跑截断）分家 | **具备** | A（只 user）：`load==[]` vs `load_view` 保留原话；B：`load=2` vs `load_view roles=[u,a,u,a] resume_count=2`；且 `load_view.messages` 与磁盘 jsonl **逐字节相等**；base 环境 5 个 load_view 单测 passed |
| M13-06 | 路径穿越防御：`../` / 非法 sid 全拒 | **具备** | 5/5 恶意 sid 的 load 全 `[]`、exists/archive/delete 全 `False`、哨兵 `secret.jsonl` 未动；**正控**证明不校验必命中；HTTP 层 `../secret`/`..%2fsecret` 全 404 且响应体不含 SECRET |
| M13-07 | 崩溃恢复：容忍半截/损坏尾行 | **具备** | `load_exception=None`、`roles=['user','assistant']`、好行保留；截断点 sweep 150/150 半截 offset 无异常；复核方发现的 `RecursionError` 路径经证明**不可达**（writer 自己先抛） |
| M13-08 | 归档 = `os.replace` 真移动，可还原 | **具备** | 归档文件存在且为**字节级真移动**（`_meta` 行 + 原字节，根目录仅剩 `archive/`）；归档后默认列表不含、`?archived=1` 含；归档后 `load()` 回退读出 2 条；`unarchive` 回主列表 |
| M13-09 | 删除 = 真 unlink，归档件也删 | **部分具备 ↘** | 唯一标准路径的串行删除确为真 unlink（无回收站、root+archive 两处都删）。但复核方发现 **D13**：`delete` 不持 append 的锁（竞态：4090/5000 次 `delete→True` 后文件被 writer 重建）+ 不 resolve 别名（symlink 只删链接，目标文件留存）。**主测方独立复现**：`delete→True, link.is_symlink=False, outside.exists=True` |
| M13-10 | Web 历史回放走 `load_view`，前端据 `resume_count` 画分隔条 | **部分具备 ↘** | 接口确走 `load_view`（`messages=4` vs `load=2`，`resume_count/discarded/reason` 齐全），golden 场景 Playwright 18/18 PASS。但复核方发现 **D14**：block 级悬挂使 `resume_count` 退化为 `len(display)`，分隔条画不出（Playwright FAIL）。**主测方独立复现**：golden `resume_count=2` vs block 级悬挂 `resume_count=6==display` |

**本批非「具备」2 条 → 各对应一个新缺陷 D13、D14（已登记 §0 主表）。**

### 10.1 主测方对两条降级的独立复现（与工作流复核方相互独立）

为避免「复核方与探针作者同源」的循环论证，主测方另写最小复现（不读复核脚本、只读源码路径），在 Agent 环境单独跑：

**D13 —— `M13-09` 的删除语义**（`/tmp/verify_m13_09.py`、`/tmp/verify_m13_09c.py`）

| 项 | 内容 |
|---|---|
| 测什么 | ① `<sid>.jsonl` 为指向 root 外真实文件的 symlink 时 `delete()` 的后果；② 一个线程持续 `append`、主线程 `delete` 是否原子 |
| 命令 | `/home/chenyuxi/anaconda3/envs/Agent/bin/python -I /tmp/verify_m13_09.py`（及 `..._09c.py`） |
| 实测（别名） | `delete->True  link.is_symlink=False  outside.exists=True  outside内容='SECRET_CONTENT_XYZ'` —— **返回 True 但目标文件内容原样留在磁盘** |
| 实测（竞态） | `race: 4090/5000 次 delete 返回 True 后 0.5ms 内文件被重建; 样本大小=[1064, 532, 532]` |
| 源码核对 | `inspect.getsource(SessionStore.delete)` 不含 `with self._lock`；`append` 含 → **确为不持锁** |
| 反例判据 | 「delete 返回 True 但文件仍在磁盘」——**命中**（别名与竞态两条路径） |
| 结论 | **D13 成立**，M13-09 降「部分具备」由主测方独立确认。（注：主测方的第一版竞态脚本用单 writer、无 `sleep` 让出，得 0/3000；加 `time.sleep(0)` 让出 GIL + 采样延迟后稳定复现 —— 记录此自纠正，说明该竞态**非必然**、取决于调度，但确实存在） |

**D14 —— `M13-10` 的 `resume_count`**（`/tmp/verify_m13_10.py`）

| 项 | 内容 |
|---|---|
| 测什么 | 对比「悬挂只在最末轮」（golden）与「已完成 assistant 内混有未答 `tool_use` 块」（block 级悬挂）两种输入下的 `load_view.resume_count` |
| 命令 | `/home/chenyuxi/anaconda3/envs/Agent/bin/python -I /tmp/verify_m13_10.py` |
| 实测（golden） | `display=4 resume_count=2 reason=incomplete_turn`（正确） |
| 实测（block 级悬挂） | `display=6 resume_count=6 discarded=1 reason=incomplete_turn`，`strict(load)=4` → **`resume_count` 应等于严格可续跑前缀 4，实为 6** |
| 反例判据 | 「`resume_count == len(display)` → 前端 `cut` 落在末尾 → 画不出分隔条」——**命中** |
| 结论 | **D14 成立**，M13-10 降「部分具备」由主测方独立确认 |

### 10.2 复核方在判据之外另发现的能力边界（不改变判定，登记备查）

| 用例 | 发现 | 说明 |
|---|---|---|
| M13-03 | 带 `thinking+text` 的悬挂 assistant 恢复后出现连续两条 assistant | Messages API 允许并会合并同角色轮，不触发 400，未违反判据 |
| M13-03 | 同一条 assistant 内「已答 t0 + 悬挂 t1」会把整个已完成工具轮一并丢弃 | 偏保守的数据丢失，但结果为 API 安全 |
| M13-05 | `load_view` 会丢弃 `content==[]` 的空 assistant 条 | 源码注释明示的设计（不渲染空气泡），空 assistant 不携带「工具调了但中断」信息 |
| M13-06 | `_valid` 只做 sid 字符白名单，**不校验解析后真实路径是否在 root 内** | root 内已存在 symlink（如 `legalname.jsonl -> root外`）时 `load_view` 会泄内容。前置条件是「攻击者已能写入 root」，超出用例声明范围，登记为范围外局限 |
| M13-07 | `_read_raw` 只捕 `JSONDecodeError`，不防 `RecursionError` | 深嵌套近合法尾行（`'['*N`，N≥9997）会冒泡并使整段 load 失败；但 writer 的 `json.dumps` 在同一深度**先**抛 RecursionError，故 store 写不出这样的行，**真实崩溃下不可达**。建议 `except (json.JSONDecodeError, RecursionError, ValueError)` |
| M13-08 | 归档后再对同 sid `append` 会 split-brain；`unarchive` 用归档文件覆盖根文件会丢新增 append | 并发边界，非用例判据 |
| M13-08 | `unarchive` 不剥离归档时插入的 `_meta` 首行 | 根文件字节不再等于归档前原文（数据无损，仅多一行注解） |
| M13-04 | SSE 的 `resume_discarded` 只挂在 `done` 事件 | 首轮以 `error` 结束则拿不到丢弃提示；另 WebSession 常驻缓存使同 sid 二次探测须换新 sid |

---

## 11. 阶段结果校验门禁（2026-10-26）

**背景（用户诉求逐字）**：「在WGS管线或者表型提取的每一步结束之后，需要对结果值进行校验，
以免出现中途错误还传递到后面步骤」

**改动**：新增两个校验器 + 六个阶段 skill 的收尾门禁 + 编排层四态判定 + 表型侧出口校验。

**跑法**（`samtools`/`bcftools` 需在 PATH —— 校验器用 `command -v` 探测）：
```bash
cd /home/chenyuxi/skill/agent
export PATH=/home/chenyuxi/anaconda3/envs/Agent/bin:$PATH
/home/chenyuxi/anaconda3/bin/python -m pytest tests/ -q          # 516 passed（含第四批 167 条）
/home/chenyuxi/anaconda3/bin/python -I tools/concurrency/stage_verify_probe.py   # 30 PASS / 0 FAILED
/home/chenyuxi/anaconda3/bin/python tools/concurrency/verify_fix_probe.py            # 46 项 / 46 通过
/home/chenyuxi/anaconda3/bin/python tools/concurrency/skill_doc_consistency_probe.py # FAILED 0
```
> 计数随批次演进：首批 269 → 第二批（A1/A2/A7）271 → 第三批（A3/A4/A5/A6 + 版本载体）349
> → 第四批（F1–F7 + 版本载体）516；探针 33 → 45 → 46。
> 表中各行只记**该行自己**的实测结果，不重复记总数。

| 用例 | 怎么测 | 实测证据 | 结论 |
|---|---|---|---|
| VERIFY-01 | `grep -l stage_verify.py skills/wgs-express-*/SKILL.md \| wc -l` | `6`；每阶段同时含 `.failed` 与 `exit 1` | 具备 |
| VERIFY-02 | `pytest tests/test_stage_verify.py::test_stage1_empty_bam_is_caught` | 1 passed。空 BAM 触发 `read_count` hard 失败；**同一测试内断言** `samtools quickcheck` 对它返回 0（证明对照有区分度） | 具备 |
| VERIFY-03 | `pytest ...::test_stage3_real_gvcf_passes ::test_stage3_fake_gvcf_is_caught` | 2 passed。真 GVCF（头部 `##ALT=<ID=NON_REF`）过；普通 VCF 被 `gvcf_marker` 拦 | 具备 |
| VERIFY-04 | `pytest ...::test_stage4_sample_count_mismatch_is_caught` | 1 passed。VCF 有 SAMPLE_A 而期望 A,B → `samples` hard 失败 | 具备 |
| VERIFY-05 | `pytest ...::test_stage5_all_filtered_indel_is_caught` | 1 passed。SNP 90% PASS 而 INDEL 全 LowQual → `indel.pass_rate` hard 失败 | 具备 |
| VERIFY-06 | `pytest ...::test_titv_ratio_parsing_matches_bcftools_format` | 1 passed。造 ts:tv=2:1，解析出 Ti/Tv=2.00（真值） | 具备 |
| VERIFY-07 | `pytest ...-k 'discrimination or unknown_stage or internal_error'` | 3 passed。好数据 rc=0 / 坏数据 rc=1；未知阶段与内部异常都判失败 | 具备 |
| VERIFY-08 | 生成真实形态的阶段脚本 → 故意让一个样本产出空 BAM → 执行 → `ls run/.done/ run/.failed/` | 脚本 rc=1；`.done` **空**；`.failed/01_bwa_sort.failed` 存在且含校验报告。**查磁盘真值，未只看退出码** | 具备 |
| VERIFY-09 | 承 VERIFY-08：修好数据重跑 | 重跑后 `.done` 出现、`.failed` 被清；编排层判定输出 `✅ 01_bwa_sort 已完成` | 具备 |
| VERIFY-10 | `python -I tools/concurrency/stage_verify_probe.py \| grep G10e` | G10e-1…G10e-5 全 PASS | 具备 |
| VERIFY-11 | `grep -c verifier_path skills/wgs-express-params-propose/SKILL.md` | ≥1，且文档说明必须是绝对路径、缺失时 `${VERIFIER:?}` 早失败 | 具备 |
| VERIFY-12 | `pytest tests/test_phenotype_validate.py -q` | 24 passed（含 8 条阴性对照：越界/负值/全同值/全 0/失败率超限/样本数不足/重复个体/NaN 均 blocked） | 具备 |
| VERIFY-13 | `pytest ...-k 'imf or single_class'` | 3 passed（实测；此前误写 4）。纯黑图+高阈值 → `compute_imf` 抛 `PhenotypeError`；跨模块同一类 identity 成立 | 具备 |
| VERIFY-14 | `python -I tools/concurrency/stage_verify_probe.py \| grep G10g-3` | PASS：明细 CSV 落盘位置早于 `report.blocked` 判定 | 具备 |
| VERIFY-15 | `pytest tests/test_stage_verify.py -q -k 'bai or index'` | 5 passed。四种合法索引命名（`foo.bam.bai`/`foo.bai`/`foo.bam.csi`/`foo.csi`）全被识别；markdup 索引 warn、BQSR 索引 hard；真缺 BQSR 索引仍 rc=1。另在 `verify_fix_probe.py` H/I 段以真 samtools 复现（6 项，回退旧实现即失败） | 具备 |
| VERIFY-16 | `pytest tests/test_stage_verify.py -q -k 'variant_floor or tiny_variant'` | 3 passed。显式 `--min-variants` 时位数不足即 hard 拦截；未声明时登记 warn 明说"未做该判定"且不拦截；高于下限时通过。探针 J 段复现 | 具备 |
| VERIFY-17 | `pytest tests/test_stage_verify.py -q -k 'stale_index'` | 2 passed。真 samtools 造"200 reads + 索引 → 覆写成 0 reads 空 BAM（沿用旧索引）、并 `os.utime` 回拨索引 mtime" → 门禁 rc=1，`index_stale`/`read_count` 两条 hard 失败；`samtools index` 重建后 rc=0。测试内断言前提（`view -c`=0、`idxstats` rc=0、stderr 含 "older than"）以防场景失真。探针 K 段 4 项（含前提自检与健康 BAM 对照） | 具备 |
| VERIFY-18 | `python -I tools/concurrency/verify_fix_probe.py \| grep -A4 '^L\.'` | L 段 3 项全 OK。读组 `@RG ID:r0.1 LB:lib1 PL:bar SM:sample PU:r0.1`（真机 `pbrun fq2bam` 在 clara-parabricks:4.3.2-1 容器内的原样输出）→ `readgroup` hard 拦、rc=1；改 `SM:s1` → rc=0。align-sort 模板含 `--read-group-sm "${sample_name}"` | 具备 |
| VERIFY-19 | `python -I tools/concurrency/verify_fix_probe.py \| grep -A6 '^M\.'` | M 段 5 项全 OK。真 bgzip+tabix 产物上裸 `tabix -p vcf` rc=1（`the index file exists`）、`tabix -f` rc=0；端到端把裸 tabix 放进 `set -euo pipefail` 脚本 → 其后的 `touch` 未执行（门禁行够不到，实测复现）；两个阶段 skill 已无比做性质的裸 tabix | 具备 |
| VERIFY-20 | `pytest tests/test_stage_verify.py -q -k dup_rate` | 6 passed。真 GATK MarkDuplicates metrics（含 `LB lib1`）：解析得 `0.0`（真值），而同测试内断言**朴素正则会得 `1.0`** → 区分度成立；0.375 / 0.05 亦正确；非 metrics 文件返回 `(None, 原因)` 不抛异常。探针 N 段 5 项 | 具备 |
| VERIFY-21 | `pytest tests/test_phenotype_validate.py -q` | 30 passed（含 6 条 A4 专项）。fam 缺 s4 时两导出同为 `s1..s4`（旧实现 PLINK 3 / GAPIT 4）；fam 外样本保留并返回 `not_in_fam=['s4']`；fam 行序生效（`s3,s1,s2` 非字典序）。探针 P 段 3 项 | 具备 |
| VERIFY-22 | `pytest tests/test_stage_gate_template.py -q` | 57 passed。**5 种畸形配置 × 7 份模板**：rc≠0 且 `.failed` 存在含 `reason=`，且未走到校验调用；阳性对照下健康配置 rc=0 并**走到**校验器。抽真实 shell 段落在**真 bash** 里跑。回退 `\|\| true` → 3 条失败（已验区分度） | 具备 |
| VERIFY-23 | `pytest tests/test_stage_verify.py -q -k 'combined or genomicsdb'` | 4 passed。缺 combined → hard 拦 rc=1（旧实现 rc=0 且报告 0 次提到 combined）；0 字节 → 报"存在但为空"（与缺失区分）；有 GenomicsDB 工作区 → warn 且 rc=0；两者都在 → 放行。探针 O 段 4 项 | 具备 |
| VERIFY-24 | `pytest tests/test_stage_verify.py -q -k min_variants` | 7 passed。`run.params.json` 写 `min_variants=1000` 时 2 位点队列被 hard 拦 rc=1、报告写明下限来源；CLI 紧值覆盖文件松值；键不存在仍 warn 不拦；六种畸形值（`"1M"/0/-5/3.5/true/null`）一律 hard 拦住；Stage 1 不受影响。**变异验证**：拆掉回落分支 → 7 条失败。见 §11.5 F6 | 具备 |
| PROBE-01 | `pytest tests/test_probe_selfcheck.py -q` | 4 passed（AST 静态检查：断言不得引用跨段陈旧结果变量、不得以 stdout 子串为唯一依据、结构化助手真被调用、被审计点名的 C/D 两段仍在）。**变异验证**：删 `bqsr.coverage` 登记 → 修复前探针 45/45 全绿、修复后 `[BAD]`；删 `reads_conservation_lost` 检测 → 修复前全绿（且打印自相矛盾的"rc=1（期望 1）；无 reads_conservation_lost"）、修复后抓住。见 §11.5 F7 | 具备 |
| VERIFY-25 | `pytest tests/test_defect_class_guards.py -q` | 153 passed。**按写法扫全部 skill 文档**（自动发现，含 references/usage-guide）：裸 tabix / 读组自动生成主张 / 硬编码 conda 路径 / `${SAMPLES_CSV}` 无赋值 / 写 `.done` 不清陈旧 `.failed` / 比对率只写 warn / §2.2 模板自足。另含 `test_guards_actually_discriminate`：每条谓词配**负控**（真违规必须抓到）与**正控**（纠正叙述不得误伤），并做过变异验证（改宽→报"误伤"，改恒假→报"漏报"）。见 §11.5 F1/F2/F3/F5 | 具备 |
| VERSION-01 | `pytest tests/test_version.py -q` | 5 passed。`VERSION` 文件为 `0.2.0`（干净 `X.Y.Z` + 单行）；`__version__` 与文件逐字相等；`run.py --version` rc=0 输出 `TraitPilot 0.2.0`（**清空 `ANTHROPIC_*` 后仍成功**，证明不依赖凭证）；`python -m skill_agent --version` 同值；拷走 `skill_agent/` 且无 `VERSION` 时回退 `0.0.0+unknown` 不抛异常 | 具备 |

**独立 CLI 复核（Phenotype 环境实跑）**：
```
conda run -n Phenotype python scripts/validate.py --csv bad.csv --col IMF_预测_% --range 0 100
→ [FAIL] not_constant: 20 个有效值，std=0，唯一值 1 个；**所有样本同值**，模型很可能没真正推理
→ ❌ 拦截：1 项硬性检查未通过   退出码 1
conda run -n Phenotype python scripts/validate.py --csv good.csv --col IMF_预测_% --range 0 100 --min-nir-r 0.6
→ ✅ 全部检查通过（Ti/Tv 类比：r=1.000）   退出码 0
```

### 11.1 本轮开发中**实测踩到**的缺陷（均已修复并加判据）

这些是本改动自身引入/发现的缺陷，登记备查——它们的共同点是**都靠真实工具输出或真实文件才暴露**，
纯 mock 测试会忠实复现作者的错误假设从而放过：

| # | 缺陷 | 症状 | 修法与判据 |
|---|---|---|---|
| V1 | GVCF 标记判据找错位置 | 在头部找字面量 `<NON_REF>`（带尖括号），而头部写的是 `##ALT=<ID=NON_REF,...>`（ID 值不带尖括号）→ **真 GVCF 被判成假** | 改为正则 `^##ALT=<ID=NON_REF`；G10a + `test_gvcf_marker_checks_header_declaration_not_literal` |
| V2 | Ti/Tv 取错列 | `bcftools stats` 的 `TSTV` 行第 3/4 列是 ts/tv **计数**，第 5 列才是比值；误取第 4 列 → tv=20 被报成 `Ti/Tv=20.00` | 正则改为抓 5 个字段；`test_titv_ratio_parsing_matches_bcftools_format` |
| V3 | 工具缺失时 fail-**open** | bcftools 不在 PATH 时，内容是乱码的 VCF 判**通过**（检查标为 SKIP，而 SKIP 不阻止放行）→ 任何没装工具的机器上门禁静默失效 | 新增 `Report.need_tool()`：缺工具登记为 hard 失败并单列 `missing_tools`（区分环境问题 vs 数据问题）；G10a-4 + `test_missing_tool_fails_closed_not_silently_passed` |
| V4 | reads 守恒链断开 | Stage 2 读 `prev["read_totals"]`，但**无任何地方写过该键** → 守恒检查永远静默跳过（声称存在、从不生效） | `Report` 携带 `read_totals` 并写入 JSON，各阶段填充；G10a-5 + 4 条守恒测试（含"报告缺失须告警而非静默"） |
| V5 | 装饰性参数 | `--ref` / `--expected-reads` 在 argparse 里有，代码里从不使用 → 传了以为多了层检查，实际没有 | 删除两参数；G10a-6 |
| V6 | 空壳检查体 | 一处注释写「# 5) 比对率」但函数体是 `pass` → 比对率检查根本不存在，注释让人以为有 | 实装为 `mapped_ratio`（hard，全 unmapped 拦截）+ `mapped_rate`（warn，<90% 告警）；G10a-7 + 2 条测试 |
| V7 | 编辑残留的重复行 | 连续两行完全相同的 `except ...: rep.add(...)` → 语法合法、`py_compile` 通过，但第二个分支不可达 | 删除重复行；G10a-8 静态检查连续重复可执行行 |
| V8 | 陈旧 `.failed` 未清 | 坏数据→写 `.failed`；修好重跑通过→写 `.done` 但**遗留 `.failed`**；四态判定优先读 `.failed` → **已修好的阶段被误报"校验未通过"** | 六个阶段模板在写 `.done` 后 `rm -f` 同阶段 `.failed`；G10c-2 + VERIFY-09 端到端 |
| V9 | `FAILED` 用而未定义 | haplotypecaller 模板用 `FAILED=$((FAILED+1))` 3 处却从未 `FAILED=0` → `set -u` 下**直接崩**（unbound variable） | 三个并发模板均加 `FAILED=0` 初始化；G10d-2 |
| V10 | 索引命名只认一种 | 校验器查 `bam + ".bai"`，而 GATK/Picard 的 `--CREATE_INDEX true` 把索引写成 `foo.bai`（`.bam` 整个换成 `.bai`；`samtools index foo.bam` 才写 `foo.bam.bai`）→ **真机 GATK 产出的 BQSR BAM 稳定被判"缺索引"**。实测：ApplyBQSR 产出 `S1.sorted.markdup.BQSR.bai`，旧校验器报缺 `...BQSR.bam.bai`、rc=1 —— 健康数据被拦 | 新增 `bam_index()`：四种合法命名（`foo.bam.bai`/`foo.bai`/`foo.bam.csi`/`foo.csi`）全查 —— 实测 samtools 与 GATK 四种都认（`idxstats` 不走慢路径、`HaplotypeCaller` rc=0）；`test_all_four_index_namings_are_accepted` + `test_gatk_style_bai_is_recognized`（回退旧实现即失败，已验证区分度） |
| V11 | 索引判级一刀切 hard | markdup BAM 的索引被判 hard，但 `gatk MarkDuplicates` 的 `CREATE_INDEX` **默认 false**、文档里的标准命令本就不产出；且下游 BaseRecalibrator/ApplyBQSR 读该 BAM **不需要**索引（实测 rc=0）→ 又一道拦健康数据的门槛 | `check_bam(..., index_level=)`：BQSR 索引 hard（实测 HaplotypeCaller 无索引 rc=2）、markdup 索引 warn；`test_bqsr_index_is_hard_but_markdup_index_is_warn` |
| V13 | **docstring 承诺的失效没实现** | 模块 docstring 把"变异数异常少"列为要防的头号失效，但代码里**没有任何带宽判据** —— `min_variants=1` 只拦"完全为空"。实测：2 样本队列的联合 VCF 仅 2 条记录（全基因组真实应有数百万）→ `✅ 全部检查通过`、rc=0。文档承诺的保护不存在于代码中 | 新增 `variant_floor()`：`--min-variants` 显式声明即 hard 拦截；未声明则登记 **warn** 明说"未做该判定"（不静默通过、也不误拦）。接入 Stage 3/4/5 的 5 个 VCF 检查点；`test_tiny_variant_count_is_caught_when_floor_declared` 等 3 条 + 探针 J 段 |
| V14 | 装饰性重复判据 | `verify_joint` 另登记 `joint.nonempty`，与 `check_vcf` 已登记的 `joint.variants` 判**同一个 `n`、同一阈值、同一补救措施** —— 零额外检测力，却让读报告的人以为有"两道独立位点守卫" | 删除 `joint.nonempty`，改为真正缺的那条（位点数下限，见 V13）；无测试引用该名字 |
| V12 | 两套索引参数混用 | `--CREATE_INDEX` 是 Picard 风格（MarkDuplicates 认、默认 false）；walker 认的是 `--create-output-bam-index` / `--create-output-variant-index`（默认 true）。对 ApplyBQSR 传 `--CREATE_INDEX` 报 `not a recognized option` → 想关索引没关掉、想开索引没开成 | 文档记录差异（编排层错误预防 #24 + markdup skill #8-#10）；实测 4.3.0.0 各 walker 默认值 |
| V15 | **门禁被陈旧索引整体绕过**（第二批，critical） | `samtools idxstats` 读的是**索引**而非 BAM 本体。BAM 被重写而索引没跟着重做时（重跑、`--resume` 跳过 index 步骤、覆写产物），idxstats **照旧值作答**：实测 200 reads 的健康 BAM 覆写成 0 reads 的空 BAM（沿用旧 `.bai`），`samtools view -c` 说 0、idxstats 说 200 → 门禁 `[PASS] read_count: mapped=200`、`✅ 全部检查通过`、rc=0。**`read_count`/`mapped_ratio`/`mapped_rate` 三条判据同时被骗过**，而这正是本模块 docstring 自认的头号防范目标（空 BAM） | 新增 `{label}.index_stale`(hard)：采信 samtools 自己往 stderr 打的 `[W::hts_idx_load3] The index file is older than the data file`（不另造一套 mtime 比较，避免与 samtools 判定标准不一致）；索引陈旧时**回退全量 `samtools view -c`** 计数；mapped/unmapped 无从取得时登记 `mapped_ratio`(warn) 明说"**未做**该判定"而非静默丢弃。`test_stage1_stale_index_does_not_mask_empty_bam` + `test_stage1_stale_index_fixed_by_reindexing_passes`（含前提自检）+ 探针 K 段 4 项；编排层错误预防 #25 |
| V16 | **默认后端下拦下每一个样本**（第二批，critical） | `pbrun fq2bam` 不传 `--read-group-sm` 时自动生成**写死常量**读组：`@RG ID:r0.1 LB:lib1 PL:bar SM:sample PU:r0.1`（真机容器内跑出的原样）。GPU 就绪时 parabricks 是**文档规定的默认后端**（错误预防 #9/#10），于是 Stage 1 的 `readgroup`(hard) 对**每一个样本**都失败；该 SM 还会继承进 GVCF 样本列，使 Stage 3 的 `samples`/`identity`(hard) 连锁失败；队列样本同名则 Stage 4 `CombineGVCFs` 拒绝合并 | align-sort 的 fq2bam 模板补 `--read-group-sm "${sample_name}" --read-group-lb --read-group-pl ILLUMINA`，并加说明块与错误预防 #27（**不能反过来删判据**：删了常量读组就变成"放过串样本"）。探针 L 段 3 项：`SM:sample` 被拦 rc=1、`SM:s1` 放行 rc=0、模板含该参数 |
| V17 | **脚本死在门禁之前**（第二批，major） | Stage 3/Stage 5 的 skill 文档各写着一条"补索引"的裸 `tabix -p vcf`。而 GATK walker 的 `--create-output-variant-index` **默认 true**，索引早已存在 → 裸 tabix **rc=1**（`the index file exists. Please use '-f' to overwrite.`），配合阶段脚本头部的 `set -euo pipefail` 当场退出。**健康的过滤结果既不写 `.done` 也不写 `.failed`**（磁盘上两者皆无），编排层按四态判定归为"已中断"、引用户去查进程 —— 而真实原因是一行文档里的多余命令 | 两个 skill 删除全部裸 `${TABIX} -p vcf` 与 `TABIX` 变量，改为说明"GATK 自动写索引，勿手工补"；确需重建时用 `tabix -f`。清单/验收项同步改写（否则 LLM 照清单又把裸 tabix 加回来）。探针 M 段 5 项含**端到端**：裸 tabix 之后的门禁行确实未执行；编排层错误预防 #26 |
| V18 | **重复率被正则跨行抓错**（第三批，minor 但恒发） | `dup_rate` 用 `PERCENT_DUPLICATION[^\d]*([\d.]+)` 解析，而 `[^\d]*` **跨行**匹配，从下一行 `LB  lib1` 的库名里抓走了 `1`。真 GATK 产物实测：`PERCENT_DUPLICATION` 真值 `0`，报告写 **`重复率 100.0%；异常偏高，查文库/比对`**。后果不是漏拦而是**恒误报**——每个健康样本都带一条 warn，报告失真到无人再看 warn；且原实现把解析包在 `except: pass` 里，"指标文件在、重复率却没被检查"这件事在报告里完全看不出来（正是"纸面具备"）。修法与判据见 §11.4 A3 |
| V19 | **两个 GWAS 导出无单一事实来源**（第三批，major） | PLINK 走 `pred` 字典（去重 + fam 过滤），GAPIT 走 `df.dropna()`（全部行）——两条独立构造路径。`--fam` 缺一个样本时实测 **PLINK 3 行 / GAPIT 4 行**，两个文件同时写出、rc=0。下游用哪份取决于跑 PLINK/GEMMA 还是 GAPIT；门禁的 `sample_uniqueness` 查的是"同一 Taxa 多行"，**结构上查不到跨文件分歧**。修法与判据见 §11.4 A4 |

### 11.3 第二批（2026-10-27）：A1/A2/A7 三条

用户点名的三条，均**先用真实工具独立复现**再修，修完双向验证（坏数据拦得住、健康数据不误伤）。

| 代号 | 一句话 | 复现方式 | 现状 |
|---|---|---|---|
| A1 | 陈旧索引掩盖空 BAM（见 V15） | 真 samtools 造 200 reads BAM + 索引 → `samtools sort` 覆写为仅头部 → 索引未重做 | 已修 + 2 条 pytest + 探针 K 段 |
| A2 | `pbrun fq2bam` 默认常量读组 `SM:sample`（见 V16） | docker 起 clara-parabricks:4.3.2-1 真跑 `pbrun fq2bam`，把产出的 BAM 喂给门禁 | 已修（文档）+ 探针 L 段；`--read-group-sm` 的真机对照 rc 由 1 变 0 |
| A7 | 裸 `tabix` 在 `set -e` 下杀死脚本、够不到门禁（见 V17） | `tabix -p vcf` 对已索引文件 rc=1；再把该行放进 `set -euo pipefail` 脚本验后续行不执行 | 已修（文档）+ 探针 M 段 |

**回归基线**：`271 passed`（较首批 269 增 2 条 A1 测试）；`verify_fix_probe.py` 33/33；`stage_verify_probe.py` FAILED 0。

**这批的共同点**：三条都不是"判据写错了"，而是**判据根本没被触发或根本到不了**——
A1 是判据被输入骗过、A2 是默认路径必拦健康数据、A7 是门禁执行不到。
纯 mock 测试对三者全部无效（它们忠实复现作者的错误假设），只有真工具、真文件、真容器才暴露。

### 11.4 第三批（2026-10-27）：A3/A4/A5/A6 四条

同一套方法：**先复现、再修、修完双向验证**（坏数据拦得住 + 健康数据不误伤），
并且每一条都做了**区分度验证**——把修法回退掉，确认对应的测试真的会红。

| 代号 | 一句话 | 复现方式（实测） | 修法 | 判据落地 |
|---|---|---|---|---|
| A3 | **重复率解析取到库名里的数字**：`PERCENT_DUPLICATION[^\d]*([\d.]+)` 的 `[^\d]*` 会**跨行**吃掉 `ESTIMATED_LIBRARY_SIZE` 与库名，从 `LB  lib1` 里抓走那个 `1`。真值 0.0 被报成 **100.0%**，于是每个健康样本都背上一条"重复率异常偏高"的 warn（见 V18） | 用真 GATK 生成一份 MarkDuplicates metrics（内含 `LB  lib1`），`PERCENT_DUPLICATION` 真值 `0`；旧正则返回 `'1'` | 新增纯函数 `parse_percent_duplication()`：按**制表符切列**、用表头定位列号、只取同宽数据行——不再用会跨行的正则。取不到时返回 `(None, 原因)`，**不抛异常也不静默**；原先的 `except: pass` 改为显式登记一条 warn，写明"**未做重复率判定**" | `tests/test_stage_verify.py` 6 条（含一条**前提自检**：断言该 fixture 确实能骗过朴素正则，否则测试自身失去区分度）；探针 N 段 5 项 |
| A4 | **两个 GWAS 导出可能静默分歧**：PLINK 用 `pred` 字典（按键去重、再按 `--fam` 过滤），GAPIT 用 `df.dropna()`（保留全部行）。`--fam` 里少一个样本时，PLINK 3 行 / GAPIT 4 行，**两个文件都被当权威产物写出、rc=0**，没有任何机制会发现。`sample_uniqueness` 查的是"同一 Taxa 多行"，查不到这个（见 V19） | 构造 fam 缺 s4 的场景跑导出：旧实现 PLINK 3 行、GAPIT 4 行 | 抽出 `build_gwas_exports()` 返回**唯一**的 `(pred, order, not_in_fam)`，两处导出都由它构造——分歧在构造上不可能发生（不是"加一条检查去发现分歧"）。同时纠正语义：`--fam` 按 SKILL.md 契约只是**排序基准**，不是成员过滤器，故 fam 外的样本**保留**并追加在末尾 + 打警告说明 GEMMA `-p` 按行序对齐的代价（早期把它们静默丢掉）。函数尾部带不变量断言，防将来有人只改一处导出又引入分歧 | `tests/test_phenotype_validate.py` 6 条（现共 30）；探针 P 段 3 项 |
| A5 | **`${VERIFIER:?}` 抢先杀掉脚本，写 `.failed` 的兜底永远到不了**：5 种畸形配置形态里 4 种磁盘上什么都不写 → 编排层四态判定归为"已中断"，引用户去查进程（真实原因是配置读不到）。**且第一轮修法不够**：换成显式判空守卫后仍有 2 种失败——`VAR="$(cmd)"` 是**赋值**语句，`set -e` 下命令替换失败即退出整个脚本，`if [ -z ... ]` 根本执行不到 | 从 7 份 SKILL.md 里**抽出真实 shell 段落**放进真 bash 跑 5 种形态 × 7 文件 | ① 去掉 `: "${VERIFIER:?}"`，改显式判空 + 判存在两段式守卫；② 两处命令替换加 `|| true` 使其在 `set -e` 下安全；③ 兜底 `.failed` 里写可操作的排查三项（run.params.json 在不在 / verifier_path 是否非空绝对路径 / 文件是否还在） | `tests/test_stage_gate_template.py`（新文件，57 条）：5 形态 × 7 文件的核心参数化 + 阳性对照（健康配置必须**走到**校验器，证明不是"永远写 `.failed`"）+ 三条回归防线（`${VERIFIER:?}` 不得回来、赋值必须带 `|| true`、7 份守卫形态一致） |
| A6 | **`combined.g.vcf.gz` 缺失时静默通过**：`if have(combined):` **没有 else**。实测只放 `co.vcf.gz`、不造 combined → 报告 4 条 PASS + 1 条 warn、`grep -c combined` = **0**、rc=0。而它是 Stage 4 SKILL.md 输出表与错误预防 #6 都列明的契约产物 | 只造 `co.vcf.gz`，跑 `verify_joint`，数报告里提到 combined 的行数 | 补 `else` 分支，并**区分三种情形**：文件缺失（hard，且说明 resume 的存在性检查会因它不在而重跑合并）、0 字节（hard，报"存在但为空"而非"缺失"）、有 GenomicsDB 工作区（warn，明说该路径合法地用 GenomicsDBImport 取代了 CombineGVCFs，故**未做**该判定）。第三种是为了不误伤合法路径——判据必须是"要么拦、要么明说没判"，不能既不说也不拦 | `tests/test_stage_verify.py` 4 条 + 探针 O 段 4 项；另修复 2 条既有测试与探针 J 段的 fixture（它们只造联合 VCF 不造 combined —— **fixture 不完整，不是判据放宽**） |

**回归基线（第三批）**：`344 passed`（较第二批 271 增 73 条：A4 六条 + A5 五十七条 + A3/A6 十条）；`verify_fix_probe.py` **45/45**（较第二批 33 增 12：N 段 5 / O 段 4 / P 段 3）；`stage_verify_probe.py` FAILED 0；`skill_doc_consistency_probe.py` FAILED 0。

**区分度验证**（把修法回退掉，确认测试真会红）：
- 回退 A5 的 `|| true` → 3 条失败（`bad_json` / `no_run_params` / `test_assignments_are_set_e_safe`）
- 回退 A4 的统一构造 → 2 条失败
- 两者恢复 → 全绿

**这批的共同点**：四条里三条的本质是**"判据读的不是本体"**——A3 读的是正则而非表格列、
A6 读的是"文件在不在"却漏了不在的情形；A4 则是**两个产物各造各的**，没有单一事实来源。
A5 是唯一一条**修法本身也踩了坑**的：第一版修法（判空守卫）看起来对，实测 5 形态里仍有 2 种漏网，
根因在 shell 的控制流（赋值语句同样受 `set -e` 管辖）——所以它的测试必须抽真实 shell 段落在真 bash 里跑，
文本断言（"文件里有 `if` 就通过"）恰恰是"纸面具备"的来源。

### 11.5 第四批（2026-10-27）：F1–F6（含审计对我自己复核工具的发现）

前四批的共同手法是"造坏输入，看门禁拦不拦"。这一批多了一层：审计的六个透镜里有**一份专门看"我的修复工具链本身"**，
它抓到的两条都不是产品缺陷，而是**我用来证明"缺陷已修"的探针自己不成立**。这正是本项目反复出现的
"纸面具备"形态换了个宿主——从产品挪到了证据链上。所以这一批的记录方式也变了：每条都要求
**把被测代码真删掉，确认判据会红**（而不只是"测过、是绿的"）。

| 代号 | 一句话 | 复现方式（实测） | 修法 | 判据落地 |
|---|---|---|---|---|
| F1 | **权威模板不自足**：编排层 §2.2 是"完整模板见编排层 SKILL.md"指定的那份，却直接用了 `${SAMPLES_CSV}` 而全仓库只有它一处出现该名字（六个阶段 skill 各自都有那一段，唯独权威版漏了）。照抄即 `SAMPLES_CSV: unbound variable`（`set -u`）→ 磁盘上 `.done`/`.failed` **都不存在**，编排层归为"已中断"并引用户查进程。补上后第二半又暴露：成功路径只写 `date > "${DONE_FILE}"`，没有 `rm -f` 陈旧 `.failed`（错误预防 #19 明文要求）→ 重跑通过后两个标记并存，四态判定优先读 `.failed`，**已修好的阶段被报成"校验未通过"**。同一批还发现 `RUN_DIR`/`DONE_FILE` 在本块内也无定义，"完整可抄"名不副实 | 逐行摘录 §2.2 到临时脚本（只补 `log_error`/`log_done`，其余一字不改）在真 bash 跑：`line 13: SAMPLES_CSV: unbound variable`、RC=1、`.done`/`.failed` 两目录都不存在；补定义后再跑，§2.2 的健康路径 rc=0 但 `.done` 与预置的陈旧 `.failed` 并存 | ① 把六个阶段 skill 已有的"样本名单兜底 + 判空写 `.failed`"块补进 §2.2；② 成功路径补 `rm -f "${RUN_DIR}/.failed/01_bwa_sort.failed"` 并加说明块；③ 块首补 `RUN_DIR`/`DONE_FILE` 的定义与确认（用 `[ -z ]` 判空，**不是** `${RUN_DIR:?}` —— 那正是 A5 的坑） | `tests/test_defect_class_guards.py`（新文件，153 条，按**写法**扫全部 skill 文档而非按"我改过的文件"）+ `test_orchestrator_template_is_self_contained` 要求 §2.2 用到的变量本块内都有定义 |
| F2 | **A7 的第三处复发**：joint-genotyping 残留 `TABIX="$(resolve_tool tabix)"` 与清单第 5 项"输出 VCF 已 tabix 索引"。GATK `--create-output-variant-index` 默认 true → 裸 tabix rc=1 → `set -e` 下当场退出，够不到门禁。这一处**比前两处更危险**：变量就摆在手边，LLM 逐条过清单时极易照抄。当时只修了 HC 与 filtering 两个 skill | 用真 bgzip+tabix 造出已带 `.tbi` 的 VCF，把 `tabix -p vcf` 放进 `set -euo pipefail` 脚本：rc=1，其后的门禁行未执行 | 删掉 `TABIX` 变量与清单第 5 项的旧写法，改为与 HC/filtering 一致的"GATK 自动写索引，勿手工补；确需重建用 `tabix -f`" | `test_defect_class_guards.py::test_no_bare_tabix_without_force`（扫全仓，允许"告诫读者不要这么写"的行）+ 探针 M 段新增第三个 skill 的文档侧检查（45→46 项） |
| F3 | **hard 判据在文档里只以 warn 面貌出现**：代码有 `mapped_ratio`(hard，全 unmapped 即拦) 与 `mapped_rate`(warn，≥90%) 两条，而编排层判据表与 warn 说明只写"比对率 ≥90%" → 读者以为"比对率低只是收到一行告警"，实际整个阶段被判失败；反向排查时按 warn 的定位又找不到这条硬失败 | 读编排层 SKILL.md 第 196/202-206 行与 workflow.md 第 156 行，`mapped_ratio` 零出现 | Stage 1 硬性列补上 `mapped_ratio` 并明确"这是 hard，与下面 warn 级的 `mapped_rate` 是两条不同判据"；warn 列表改为明确写 `mapped_rate`；并补一句"反向错误（把 hard 漏出硬性列）同样有害" | `test_defect_class_guards.py::test_mapped_ratio_not_documented_as_warn_only`（提到比对率 warn 却没提 hard 的就拦住） |
| F4 | **hard 判据未进文档 + 缺前提时整条判据静默消失**：`sample_uniqueness`(hard) 未出现在表型侧判据表；`value_range` 在原实现里，当 CSV 缺 `状态` 列时整个判定块被跳过，报告里**连一行都没有**，末尾照样打"✅ 全部检查通过"——实测喂两列 CSV（含 `1000000` 这种百分比不可能值）rc=0，报告读起来像"值域已把关" | 造只有 `Taxa,IMF_预测_%` 两列的 CSV（正是 CLI 文档里"在 GWAS 入口重新校验已存在的表型文件"的用法），填入 1000000，跑 `validate.py`：rc=0 且报告无 `value_range` 行 | ① 判据表补 `sample_uniqueness`(hard) 与 `value_range` 的 hard¹ 脚注；② 重构 value_range 块：前提不满足时**明说为什么没判**（缺 `状态` 列 → 明说未做值域判定），而不是整条消失 | `tests/test_phenotype_validate.py`（30 条，含缺 `状态` 列的场景）；`test_defect_class_guards.py` 扫表型 SKILL.md |
| F5 | **usage-guide 与硬规则直接冲突**：`Use conda absolute paths for all tools` 违反硬规则 #5.1（必须 `command -v` 运行时探测），照抄生成的脚本换机即失效；同文件还写着"Parabricks generates the Read Group automatically"（实测为假，见 V16） | 读文档 + 与硬规则 #5.1、V16 的实测结论对照 | 改为 `command -v` 探测 + `resolve_tool()` 示例，并注明旧写法为何被纠正；fq2bam 片段补 `--read-group-sm/-lb/-pl` | `test_defect_class_guards.py::test_no_hardcoded_conda_paths` 与 `::test_no_parabricks_readgroup_auto_claim` —— **两条正则都要区分"错误主张"与"纠正叙述"**（见下） |
| F6 | **`--min-variants` 无从声明**：设计文档说"由调用方显式声明"，但全仓没有任何一处会传它——阶段脚本由 LLM 现场生成、`run.params.json` 无该字段、编排层无此参数。于是这条判据**恒停在 warn**，"声称可声明、实际无从声明"（V20/VERIFY-24） | 全仓 grep `min_variants`：只命中文档叙述与校验器自身，无写入方 | 校验器新增 `_resolve_min_variants()`（CLI > `run.params.json` > 未声明）；`STAGES_NEEDING_VARIANT_FLOOR` 限定只在该阶段拦（否则 Stage 1 也被卡）；params-propose schema 与说明补该字段 | `tests/test_stage_verify.py` 7 条：7 种取值路径 + 6 种畸形值一律 hard。**变异验证**：拆掉回落分支 → 7 条失败 |
| F7 | **我的复核探针自己不成立**（审计发现，非产品缺陷）：C 段断言写 `rc == 1 or rc4 == 1`，而 `rc` 是**上一段的旧值、恒为 1** → `or` 左边永远成立；D 段把 `reads_conservation_lost` 只写进**说明字符串**、不参与断言（该场景 rc 被另一条判据拉成 1，于是断言恒真）。实测：把两个 critical 缺陷的检测代码**整段删掉**，探针仍打印 `45/45 全绿`，同一变异下 pytest 49 passed —— **"阶段 2 只做 markdup 也算通过"这条已修的 critical 缺陷，整套证据链再也发现不了它回归** | 按审计给的复现脚本删检测块：C 段仍 `[OK]`、D 段输出自相矛盾地打印"rc=1（期望 1）；无 reads_conservation_lost" | 探针新增 `verdicts()` / `hard_failed()` / `hard_failed_suffix()`：读 `.verify/<stage>.json` 的**级别+真值**做断言（子串无法区分"hard 拦住"/"warn 放行"/"恰好出现在说明里"）；新增静态自检文件 | `tests/test_probe_selfcheck.py`（4 条，AST 静态检查：陈旧结果变量、子串**唯一**依据、助手真被调用、C/D 两段仍在）。**变异验证**：删 `bqsr.coverage` 登记 → 修复前全绿 / 修复后 `[BAD]`；删 `reads_conservation_lost` 检测 → 修复前全绿 / 修复后抓住。自检自身也做变异（改宽谓词→报"误伤"、改恒假→报"漏报"） |

**回归基线（第四批）**：`516 passed`（较第三批 349 增 167：`test_defect_class_guards.py` 153 + `test_probe_selfcheck.py` 4 + `test_stage_verify.py` 增 10）；`verify_fix_probe.py` **46/46**；`stage_verify_probe.py` FAILED 0；`skill_doc_consistency_probe.py` FAILED 0。

**区分度验证（这一批的硬要求）**：
- 拆掉 `min_variants` 的 `run.params.json` 回落 → 7 条失败
- 删掉 `bqsr.coverage` 登记 → 探针报 `[BAD]`（修复前此处全绿）
- 删掉 `reads_conservation_lost` 检测 → 探针报 `[BAD]`，且 `rc=1` 仍为真 —— 正是"rc 对但判据没了"这一形态
- 删掉索引命名判据 → H 段 4 条 `[BAD]`
- 把 `test_defect_class_guards.py` 的谓词改宽/改恒假 → 自检分别报"误伤"/"漏报"

**这批的共同点（与前几批不同之处）**：前四批的缺陷都在产品里，修复的作用域是"文件"；
这一批暴露的是**作用域本身错了**——六条里三条（F1/F2/F5）是已"修好"缺陷在没改到的文件里复发，
根因是我按"这次动过的文件"逐个改，而缺陷按**写法**传播。所以 F1 的产出不是又一份文档补丁，
而是 `test_defect_class_guards.py`：**自动发现全部 skill 文档、按失效模式扫描**，将来新增的文件
自动纳入，不需要有人记得"这里也要改"。它上线即找出审计自己都没报的**第四处** A2 站点
（align-sort 的"与 CPU 路径差异"对照表）。
F7 是同一逻辑推到证据链上：**复核工具的可信度必须自己也被复核**——否则"已验证"三个字
和它要防的"纸面具备"是同一种东西。

**一个操作性教训（F5 的连带发现）**：这类"按写法扫描"的正则会把**纠正叙述本身**当成违规——
`usage-guide.md` 里"旧版本这里写的是「Use conda absolute paths」—— 那句话是错的"是**正确**的文本，
却被判失败。故每条谓词都必须配**正控**（纠正措辞不得被误伤）与**负控**（真违规必须被抓到），
这正是 `test_guards_actually_discriminate` 存在的原因：没有它，一条写松或写死的正则会在整个仓库上
"全绿"而什么也没防住。

### 11.2 复核方另发现的能力边界（不改变判定，登记备查）

| 项 | 说明 |
|---|---|
| `samtools quickcheck` 不验排序 | 实测：乱序 BAM 也返回 0；且 samtools **拒绝**给乱序 BAM 建索引（`[E::hts_idx_push] Unsorted positions`），故"乱序且带索引"在现实中不存在。排序判据改为验 `@HD SO:coordinate` 标签（缺标签即拦），见 `test_stage1_coordinate_header_is_checked` |
| 比对率判据有样本量门槛 | total < 100 时不判比对率（小样本下比率无统计意义），只判 mapped > 0 |
| `.recal.table` 缺失降为 warn | parabricks 的 `pbrun bqsr` 一步完成时不产出该文件，属预期；GATK 路径下缺失才说明 BQSR 被跳过。故不作 hard |
| Stage 2 全无产出判 hard | 经查 Stage 3 消费 `{sample}.sorted.markdup.BQSR.bam`，而 fq2bam 只产出 `{sample}.sorted.bam` → 无论何后端 Stage 2 都必须产出 BQSR BAM，故 `bqsr_output` 保持 hard 正确 |
| 重复率检查依赖未固定的文件名 | `MARKDUP_METRICS` 变量在 markdup skill 里被引用但**从未定义**文件名，故重复率检查做成"候选名命中才查"的可选判据（warn） |
