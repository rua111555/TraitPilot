# 对话框解放与上下文压缩（① 根因 / ② 上限与压缩设计）

用户的原始提问（逐字）：

> 还有两个小问题，①为什么我的skill在运行的过程中，我的对话框没有解放出来，我skill中应该有写在运行过程中，对话框需要解放，因为生信分析领域，整个分析的时间可能会很长②你能查到目前支持的最大上下文是多少吗？我不想太固定最大轮次，而是每次快达到上下文上限的时候，进行上下文的压缩，然后继续这个对话框

本文记录：根因（每条都带**实测反例**）→ 每一条判据的**内容与结果** → 被我判废/重写的判据 → 设计。
上一轮（会话并发缺陷）的记录在 `docs/session-concurrency-fix.md`，本文是它的续篇。

---

## 0. 结论速览

### ② 先回答数字

| 问题 | 答案 | 依据 |
|---|---|---|
| 最大上下文 | **286,720 tokens** | 中继返回的 400 报错原文：`max_model_len=max_total_tokens=286720` |
| 从哪拿到的 | **只能从中继自己的报错拿** | `/v1/models` 不含上下文字段；`/api/status`、`/api/pricing` 都没有 |
| 当前最大轮次 | `max_turns=100`（config.json）；代码兜底 `MAX_TURNS=60` | `agent.py:20`、`_init_budget()` |

复现命令（不打印任何密钥）：

```bash
# 主动超限，读中继自己报出的上限
/home/chenyuxi/anaconda3/envs/Agent/bin/python -I -c "
import sys,requests; sys.path.insert(0,'/home/chenyuxi/skill/agent')
from skill_agent.config import Config; cfg=Config.load()
r=requests.post(cfg.base_url.rstrip('/')+'/v1/messages',
  headers={'x-api-key':cfg.api_key,'anthropic-version':'2023-06-01','content-type':'application/json'},
  json={'model':cfg.model,'max_tokens':16,'messages':[{'role':'user','content':'x'*14800000}]},
  timeout=(30,300)); print(r.status_code); print(r.json()['error']['message'])"
```

### ① 为什么对话框「没有解放」——**五条独立成因**，逐条实测

用户说「我 skill 中应该有写在运行过程中，对话框需要解放」。**文档确实写了**，
但文档写的那条路走不通，而且文档指向的工具根本不存在。

| # | 成因 | 性质 | 实测反例 |
|---|---|---|---|
| A1 | skill 文档教模型写 `Bash(run_in_background: true)`，工具 schema 只认 `background`，**没有别名**，`validation.py` 对未知键**静默放过** | 潜在 | 按文档写：阻塞 **6.01s**；按 schema 写：**0.00s** |
| A2 | 文档里的 `nohup ... &` **不重定向输出** → 子进程持有 `capture_output=True` 的管道，父进程等到 EOF | 潜在（已修正计数：9 次真后台启动中 0 次漏重定向） | 未重定向：等子进程 **6.15s**；已重定向：**0.04s** |
| A3 | skill 让模型用 **`CronCreate`** 定时查 `.done` —— **该工具在本仓库不存在** | **活跃** | 工具集 = `Bash/Edit/Glob/Grep/Read/Skill/TaskOutput/Write`，无任何 `Cron*` |
| A4 | 前台命令超出 `timeout` 时报**假的**「命令超时」，而**分离的子进程仍在跑**（无追踪） | 潜在，后果最重 | 返回 `is_error=True 命令超时（>3s）`，实测 `sleep 25` 进程 **4→5**，仍在跑 |
| A5 | 正在跑的会话按发送键 = **取消本轮**（非静默吞掉，但语义隐晦） | 设计取舍 | `/api/cancel` 被真实触发，状态 → `cancelling`；按钮 title 为「强制中断本轮任务」 |

**A5 的诚实修正**：我最初把它写成「消息被静默吞掉」——**错了**。`send()` 里确实有
一句 `if(!text||isRunning(sid)) return;`（`index.html:1143`，无任何提示），但
**用户的按键到不了那一行**：`onPrimary(){ if(state.busy) stopRun(); else send(); }`
（`index.html:1415`）会先走 `stopRun()`。所以真实语义是「按发送=取消」，
而不是「按了没反应」。这一条由并行验证代理独立复现并纠正了我（见 §5）。

**结论：用户感知到的「对话框没解放」不是单一 bug，而是「文档指的路走不通」+「等待语义隐晦」。**
A3 是真正的活跃缺陷：模型被要求用一个不存在的工具去等长任务，于是它只能在同一轮里
`sleep` 轮询 —— 那一轮永远不结束，对话框（这一轮的流）自然一直是「运行中」。

---

## 1. ① 的判据探针（后端，`tools/concurrency/blocking_probe.py`）

实现无关：真实 `ToolSet` 路径 + 真实 `subprocess.run(capture_output=True)`。
每条判据以**反例**形式给出。

```bash
cd /home/chenyuxi/skill/agent
/home/chenyuxi/anaconda3/envs/Agent/bin/python -I tools/concurrency/blocking_probe.py
```

| 编号 | 判据内容（反例形式） | 结果 |
|---|---|---|
| B1a | 按 skill 文档的写法 `run_in_background=true` —— **确实阻塞满整个命令时长** | PASS：6.01s（命令 6s） |
| B1b | **[对照]** 按 schema 的写法 `background=true` —— 立即返回 | PASS：0.00s，返回 `已在后台启动（task_id=…）` |
| B1c | Bash 的 schema 里有 `background`、**没有** `run_in_background` | PASS：`['background','command','description','timeout']` |
| B2a | 文档里的未重定向 `nohup sleep 6 &` —— **确实阻塞** | PASS：等子进程 6.15s |
| B2b | **[对照]** 重定向后的同一命令 —— 立即返回 | PASS：等子进程 0.04s（**150× 区分度**） |
| B3a | **[缺陷确认]** 工具集里**没有** `Cron*` —— 文档指向的能力不存在 | PASS：无任何 Cron* |
| B3b | skill 文档确实指示了 `CronCreate`（文档与实现不一致） | PASS：两处（见下表） |
| B4a | 模型实际用过 `background` | PASS：45 个会话中 3 次 |
| B4b | 模型**从未**按文档传 `run_in_background` | PASS：0 次（→ A1 是**潜在**坑） |
| B4c | **负控**：重定向分类器本身正确（正确写法必须判为「有重定向」） | PASS：5 例全对 |
| B4d | 模型用 nohup 时基本都做了重定向 | PASS：9 次真后台启动，0 次漏重定向 |

**结果：11/11 通过。**

### 1.1 A3 的确切位置（文档与实现不一致的证据）

| 文件:行 | 内容 |
|---|---|
| `skills/wgs-expressive-pipeline/SKILL.md:92` | `1. 脚本启动后，调用 **CronCreate** 定时检查 .done 标记` |
| `skills/gwas-pipeline/SKILL.md:90` | `长任务…超过 ~10 分钟 → 用 CronCreate 每 30 分钟查一次 .done` |

同一个 skill 里还写着「**关键规则：后台进程完成后，编排层必须主动检查进度并推进，
不能被动等待用户询问**」—— 而它给出的唯一机制是不存在的工具。

### 1.2 A1 / A2 的文档位置

| 文件:行 | 写法 | 判定 |
|---|---|---|
| `skills/wgs-expressive-pipeline/SKILL.md:6` | `background execution (nohup + run_in_background)` | 错误键名 |
| `skills/wgs-expressive-pipeline/SKILL.md:76` | `# Bash(run_in_background: true) — Harness 跟踪，对话框不阻塞` | 错误键名 |
| `skills/wgs-expressive-pipeline/references/workflow.md:80` | `` **后台执行** — `nohup` + `run_in_background` 双层保护 `` | 错误键名 |
| `skills/gwas-pipeline/SKILL.md:81` | `# Bash(run_in_background: true) — Harness 跟踪，对话框不阻塞` | 错误键名 |
| `skills/wgs-expressive-pipeline/SKILL.md:78` | `nohup conda run -n Agent bash "${SCRIPT_PATH}" &` | **无重定向** |
| `skills/gwas-qc-filter/SKILL.md:57,64` | `nohup plink --file cohort --missing &` 等 | **无重定向** |
| `skills/gwas-pipeline/SKILL.md:83` | `nohup bash "${SCRIPT_PATH}" > /dev/null 2>&1 &` | ✅ 正确 |
| `skill_agent/agent.py:40`（system prompt） | `Bash background=true（nohup + .done/.pid）启动，随后周期性检查日志，不要阻塞干等` | ✅ 正确 |

**即：system prompt 是对的，4 处 skill 文档是错的，1 处 skill 文档在对的写法上又漏了重定向。**
模型一直靠 system prompt 走对了（B4b：0 次错键名），所以这是**潜在**坑而非当前主因 ——
但只要模型某次严格照 skill 抄，就会踩中。

---

## 2. ① 的界面判据探针

### 2.1 输入区与多会话（`tools/concurrency/ui_composer_probe.py`）

用户真正的诉求是「**一个管线在跑时，我还能在别的会话里正常干活**」。

```bash
./start_web.sh restart
/home/chenyuxi/anaconda3/bin/python tools/concurrency/ui_composer_probe.py
```

| 编号 | 判据内容（反例形式） | 结果 |
|---|---|---|
| D1 | 运行中 `#input` **可编辑**（未被 disable） | PASS：`disabled=False`，写读一致 |
| D2 | 运行中按发送键**不静默吞掉**（要么发出去、要么有明确提示） | PASS：真实触发 `/api/cancel`，状态→`cancelling` |
| D2b | 若该键在运行中变成「取消」，提示必须**明确到文字**（不靠颜色/图标猜） | PASS：title=`强制中断本轮任务` |
| D5a | 切到空闲会话后按钮**恢复为「发送」**（不是仍显示取消） | PASS：`state.busy=False`，title=`发送`，停止键隐藏 |
| D5b | **[核心]** 别的会话在跑时，本会话的消息**真的发出去了** | PASS：聊天区出现该消息，输入框已清空 |
| D4 | 全程无 JS 运行时错误 | PASS：`pageerror=[]` |
| 负控 | 摘掉发送处理器后，判据确实判为「静默吞掉」 | PASS：`markInChat=False, cancelled=False` → 判为吞掉 |
| 正控 | **正常**按钮下，同一判据判为「没吞掉」（证明判据两个方向都能动） | PASS：`markInChat=True` |

**结果：8/8 通过。**

> D5b 是这一节最重要的判据：它才是用户那句「应该保留原本会话的通信，跳转到其他会话中」
> 的直接验证。结论是**这一块已经好了**（上一轮重构的成果）—— 用户感到的「没解放」
> 来自 A3 那种「一轮永不结束」，而不是来自切会话。

### 2.1.1 被我判废并重写的判据（诚实记录）

这一节我连续写废了**两版**判据，都伪装成 PASS：

| 版本 | 问的问题 | 为什么废 |
|---|---|---|
| v1 | 发送前后比对状态条文字 | 状态条含 `fmtElapsed` 的**秒表**（每秒刷新）→ 恒真 |
| v2 | 抠掉秒表后再比对 | 仍然恒真 —— 因为 `onPrimary()` 在忙碌时真的会走 `stopRun()`，状态条**真的**会变成「正在取消…」 |
| v3（采用） | **不是**问「有没有反应」，而是问「这个反应**是不是发送**」 | 有区分度 |

v1 的秒表还有一个更隐蔽的坑：我用正则 strip 它，写成
`t.replace(/\\d+\\s*s\\b/g,'')` —— Python 三引号把 `\\b` 变成了**退格转义**，
正则静默失效。改用「按行过滤」后立刻生效。**这一条值得单独记：探针里的正则，
在 Python 三引号里必须双写或干脆不用。**

### 2.2 确认框（三条独立路径，`tools/concurrency/ui_confirm_probe.py` + `ui_confirm_lifecycle_probe.py`）

`permission=auto` 下确认不常出现，但一旦出现，后果是「管线停 5 分钟然后被自动拒绝」。

| 编号 | 判据内容（反例形式） | 结果 |
|---|---|---|
| C1a | **非当前查看**会话的确认框会弹出 | **缺陷**：`#overlay.show=False`，用户什么都看不到 |
| C1b | 用户能感知到「后台会话在等确认」（提示/左栏标记/状态条任一） | **缺陷**：`notice=False 左栏标记=False 状态条=''` |
| C2 | 答复提交给**发问的**那个会话 | **缺陷**：弹窗未出现 → 无法作答 |
| C2b | **[对照]** 查看中的会话，答复归属正确 | PASS：提交 `cfprobe_viewed` ✅ |
| C3 | 确认框显示时界面不被**全屏遮挡** | **缺陷**：`position=fixed`、**1400×900 = 整个视口**，挡住 `sessionList/chat/input` |
| C4 | 全程无 JS 运行时错误 | PASS |
| R1 | 确认框弹出后**刷新页面**，仍可作答 | **缺陷**：刷新前弹窗=True → 刷新后=False，`run.pendingConfirm=None` |
| R2 | A 的确认框挂着时切到 B，作答提交给 **A** | **缺陷**：弹窗**跟着切过去**（仍显示 A 的命令），提交 `session='cfly_B'` |
| R3 | 全程无 JS 运行时错误 | PASS |

**结果：4 条判据中 4 条为缺陷（C1a/C1b/C2/C3），生命周期 2 条均为缺陷（R1/R2）；2 条对照/护栏通过。**

#### C1 的确切机制（比「被闸门挡掉」更深一层，两次修正后定稿）

我最初写成「confirm 事件被 `if(!shouldRender(rt)) return;` 挡掉」。**只对了一半**，
并行复核代理（H4）指出还漏了一条更靠前的机制，我逐条核对代码后确认如下：

| 环节 | 代码 | 后果 |
|---|---|---|
| ① 切走时主动断流 | `stashPane()` 里 `ctl.abort()`（`:417-442`） | 该会话的 SSE **被主动断开**，离开期间的事件根本**不会到达客户端** |
| ② 闸门在 `switch` 之前 | `:1273` 早于 `:1275` 的 `switch` / `:1280` 的 `case "confirm"` | 即便事件到达，`pendingConfirm` 也**不会**被更新（`:1274` 在闸门之后） |
| ③ 只有一个调用点 | `showConfirm` 全仓仅 `:1280` 一处 | 没有任何旁路能把 confirm 送进弹窗 |
| ④ 快照字段没人读 | `:404` 写入 `pendingConfirm:snap.pending_confirm`，之后**从不读回** `#overlay` | 快照里明明有，界面不用 |
| ⑤ **补播路径也跳过历史** | 磁盘重建分支 `:729` 用 `attach(id, runInfo.last_seq)` | 从「当前游标」开始订，**不重放**已过去的 confirm |

**所以「离开期间到达的 confirm」是不可恢复的**：既没有实时到达（①），
也没有补播（⑤），弹窗也拿不到（②③④）→ 用户永远无法作答 →
agent 线程等满 `CONFIRM_TIMEOUT`（默认 300s）后自动拒绝。

H4 用**真后端**端到端验到了这个终局：后端报 `pending uid … running=True`，
界面只显示「▸ 模型生成中」且**切换到该会话后弹窗也不回来**，
300s 后出现「该命令未获批准」+「确认请求超时…已自动拒绝」。

**同时必须记下 H4 的一处纠正**（它判 PARTIAL 的理由）：并非「只要会话在后台就丢」——
若弹窗**已经在屏幕上**，切到别的会话它**仍然挂着**（全局 `#overlay`，无归属校验），
此时还能答；但答错会话（见 C2/R2）。

#### R1 的探针缺陷（我自己的，诚实记录）

R1 第一版只调 `handleEventSafe()` 喂一帧合成事件、然后 `reload`。
这**没有**走到 `selectSession → attach → 后端补播` 那条真实路径 ——
它证明的只是「刷新会丢 DOM 状态」，**不是**「补播机制失效」。
我随后用 `tools/concurrency/ui_confirm_replay_probe.py` 做**静态核对**确认了
补播链路（`attach:1187 → consume:1207 → handleEventSafe:1288`）确实存在，
再读代码定位到真正的原因（⑤ `attach(id, last_seq)` 不重放）。
**结论未变（缺陷确实存在），但根据的代码位置变了** —— 这正是「探针绕开被测路径时，
测的是探针自己」。

根因（代码位置）：

| 缺陷 | 位置 | 说明 |
|---|---|---|
| C1-① | `index.html:417-442` | `stashPane()` 主动 `ctl.abort()` → 离开期间事件不到达 |
| C1-② | `index.html:1273` | 闸门在 `switch` 之前，`case "confirm"` 在 `:1280` → 事件到了也不更新状态 |
| C1-③ | `index.html:1280` | `showConfirm` **唯一**调用点，无旁路 |
| C1-④ | `index.html:404` | 快照的 `pending_confirm` 写进 runs 表后从**不读回** |
| C1-⑤ | `index.html:729` | 磁盘重建分支 `attach(id, runInfo.last_seq)` → **不重放**历史事件 |
| C2/R2 | `index.html:1306-1311` | `answerConfirm()` 提交 `session: SID`（**当前查看的**），不是发问方。`showConfirm(ev, rt)` 拿到了 `rt` 却没用 |
| C3 | `index.html:228` | `.overlay{position:fixed;inset:0;…;z-index:50}` 全屏模态 |
| R1 | `index.html:404` | 刷新后无任何代码读 `pending_confirm` 重建弹窗（同 C1-④） |
| 死代码 | `index.html:1317` vs `:1326` | `stopRun()` 的 `if(!isRunning(sid)) return;` 在提示语**之前** → 镜像陈旧时那句提示永远走不到 |

**死代码我单独实测确认过**（这个方法由并行复核代理 H3 提出，我复现了它）：

把镜像造成陈旧（`runs` 表里 `running=false`，但 `state.busy=true`），得到：

```
陈旧镜像下: {'btnTitle': '强制中断本轮任务', 'stopVisible': 'block',
             'busy': True, 'isRun': False}
按发送后: notices=[]  输入框='测试死代码'  busy=True
→ stopRun 的提示是否可见(死代码): False
→ 用户是否得到任何反馈: False
```

即：按钮写着「强制中断本轮任务」、按下**毫无反应**（连 `:1326` 那句「该会话当前没有
正在运行的轮次。」都到不了，因为 `:1317` 先 return 了），输入框里的字也不会被清掉。
用户面对的是一个**没有出口**的界面。这一条是 H3 找出来的，**我原先漏了** ——
我只查到「`send()` 会被静默 return」，没意识到 `stopRun()` 里也有一条**更靠前**的死路。

**严重性校准（诚实）**：我搜了全部 48 个真实会话，`确认请求超时` 出现 **0 次**、
真实 confirm 交互 **0 次**（53 条「permission」命中全是 `_meta` 里的配置字段）。
所以这几条是**潜在缺陷，不是用户本次症状的成因** —— 但代价很高（管线白等 300s 再被拒），
且 `webapp/session.py:115-135` 后端**已经**把待确认存进了 `pending_confirms` 与事件日志，
前端接上即可，成本很低。

---

## 3. ② 的现状：为什么「按轮次」是错的做法

### 3.1 实测：中继会返回真实的上下文占用

关键发现：**中继在流式响应里返回 `input_tokens`**，这就是「当前上下文占了多少」的权威答案。

```
message_start: message.usage = {"input_tokens": 4, ..., "output_tokens": 0}
message_delta: usage = {"input_tokens": 32, "output_tokens": 26, ...}
```

随 prompt 规模**线性增长**（实测）：

| 填充字符数 | API `input_tokens` | 本地估算 | 实测/估算 |
|---|---|---|---|
| ~100 | 122 | 140 | 0.87 |
| ~2,000 | 2,032 | 2,850 | 0.71 |
| ~8,000 | 8,032 | 11,400 | 0.70 |
| ~32,000 | 32,032 | 46,400 | **0.69** |

（`input_tokens` 恒等于「填充字符数 + 32」，那个 32 是消息框架开销。）

#### 3.1.1 **两个事件给出的值不一样 —— 必须取 `message_delta` 的那个**

这一点我实测过，是 G1 设计的硬要求。用**真实形态**的载荷
（system + tools + 多轮 + `tool_use`/`tool_result` 配对）：

```
事件序列里的 input_tokens: [('message_start', 2789), ('message_delta', 4076)]
两者是否一致 = False
```

**`message_start` 报 2789，`message_delta` 报 4076 —— 差 46%。**
取错那个会**低估近一半**的上下文占用，压缩阈值就会形同虚设。
（`message_start` 是流刚开始时的快照，`message_delta` 是整轮结束时的最终结算。）

配套要求：`client.py:179-182` 现在的 `message_start` 分支**只取了 model 名**，
`input_tokens` 就在同一个 `usage` 对象里被丢掉了 —— 两处都要改：
`message_start` 存一份（作为回退），`message_delta` 覆盖它（作为权威值）。

### 3.2 实测：本地估算器在两个方向上都会错

用**真实的最长会话**（`79b1dc01-…`，347 条消息，就是那次 WGS 管线）测：

| 口径 | 数值 | 与真实的关系 |
|---|---|---|
| 消息条数 | 347 | — |
| `_msg_bytes` 字符数 | 650,423 | `COMPACT_MAX_CHARS=600000` → **已超阈值** |
| 本地估算（`tokens.py` 字符/4） | 142,908 | **低估 1.55×** |
| **API 真实 `input_tokens`** | **221,323** | **占上限 77.2%** |

两个方向的错都实测到了：
- 对**单条大 prompt**（填充测试）：估算**高估** 1.45×（0.69 倒数）
- 对**真实长会话**（多轮中文混排）：估算**低估** 1.55×

**所以「字符数」不是可靠的触发器** —— 它连方向都不稳定。

换算：`COMPACT_MAX_CHARS=600000` 在真实 token 口径下约等于 **204,165 tokens ≈ 上限的 71%**。
即当前实现在**还剩 29% 余量**时才开始压缩；而且：

### 3.3 实测：默认配置下压缩**不会在磁盘上生效**（致命）

`config.json` 里**没有** `summary_compaction` 键 → 默认 `False`。
`webapp/session.py:259` 还要求 `cfg.memory_enabled`（默认也 `False`）才装配摘要器。
于是走的是 `agent.py:430-437` 的「**直接丢弃**」分支，而那个分支
**从不调用 `_persist_prefix`**（只有摘要分支 `:465` 会调）。

实测（构造 120 条消息 → 压缩 → 从磁盘 resume）：

```
压缩前：内存 120 条
压缩后：内存  40 条  → 是否缩小: True
  _compacted 标记 = True
磁盘 load 出来：120 条
resume 后：内存 120 条  → 压缩是否被撤销: True
```

**结论：压缩把内存砍到 1/3，resume 后全量回来了。** 每次网页重建会话/服务重启都会撤销压缩。
`_prefix` 机制本身是对的（`session_store.py:141-145` 会用它覆盖之前消息），
**只是丢弃分支没写**。

### 3.4 实测：撞上上限时用户看到什么

```
HTTP 400
This model's maximum context length is 286720 tokens. However, you requested 1 output tokens
and your prompt contains at least 286720 input tokens, for a total of at least 286721 tokens.
Please reduce the length of the input prompt or the number of requested output tokens.
(parameter=input_tokens, value=286720)
```

`_retryable_status()` 对 400 返回 `False` → **不重试、不压缩、直接抛出**，
`run_turn` 的 `except` 把它变成 `run.finish("error", error=...)`。
**即：当前实现撞上上限的方式是「整轮报错死掉」，而不是「压缩后继续」。**
这正是用户想改掉的。

### 3.5 轮次耗尽时的行为

`agent.py:146` `for step in range(budget["max_turns"])`，循环自然结束 →
`return "（达到最大工具轮次，已停止）"`（`agent.py:224`）。
硬停，不压缩、不续跑。

### 3.6 实测：压缩后**确实能继续同一对话**（用户诉求的这一半已经成立）

用户要的是「压缩完**继续这个对话框**」。我实测了这条不变量：

```
压缩前 history: 120
压缩后 history: 40   _compacted = True
压缩后仍在同一 history 列表里续写 = 继续下一阶段
角色序列前 6 条 = ['user','assistant','user','assistant','user','assistant']
存在连续两条同 role = False     （必须为 False）
tool_use 后未紧跟 user 的位置 = []  （必须为空）
```

**结论：压缩后 `history` 被替换、`_compacted=True`，下一轮 `_run` 正常继续 ——
「继续同一对话」在架构上已经是成立的**，两条硬不变量（不制造连续同 role、
不拆 `tool_use`/`tool_result` 配对）也都没被破坏。

**所以 ② 要改的只有两件事**：**触发时机**（字符/条数 → 实测 token）与
**落盘**（§3.3 那条 resume 撤销的缺陷）。`max_turns` 只是那个把对话掐断的闸门。

### 3.7 实测：固定开销可以忽略（不构成设计约束）

每次请求的 `system` + `tools` 是**固定成本**，会挤占上下文。实测：

| 项 | 实测 |
|---|---|
| 工具数 | 8 |
| `system` 字符数 | 1,052 |
| `tools` JSON 字符数 | 2,397 |
| **合计 input_tokens** | **1,553** |
| 占上限 | **0.5%** |

即无论对话多长，每轮先付约 1.5k token 的固定成本。相对 286,720 的余量可以忽略，
**不需要**为它做特殊预留。

---

## 4. 被测代码的现状速查（改之前必须知道的事实）

| 事实 | 位置 | 备注 |
|---|---|---|
| 已有 token 估算器，但**是死代码** | `skill_agent/tokens.py`（45 行） | `agent.py:18` 导入了 `messages_tokens`，**全仓无调用点** |
| 压缩触发是「字符数 + 条数」 | `agent.py:423-424` | `_msg_bytes` 求和 > 600000，或 `len(h) > 40` |
| 摘要压缩默认**关** | `config.py:211` | `summary_compaction: bool = False` |
| 摘要模板**已经**保留了管线不变量 | `compaction.py:14-27` | 阶段号/参数/路径/`task_id`/`pid`/`.done`/审批结论/TODO/错误根因 |
| 摘要失败会回退，不阻塞 | `compaction.py:6-7`、`agent.py:452-455` | `except Exception: summary = None` → 回退丢弃分支 |
| 合法切点规则 | `agent.py:499-510`、`_has_tool_use` | `role=user` 且前一条 assistant 无 `tool_use` |
| 用量只记 `output_tokens` | `agent.py:302-313`、`_new_usage:522-524` | **没有** input/上下文占用字段 |
| `client.create` 只返回 3 个值 | 调用点：`agent.py:163`、`session.py:61` | 签名 `(blocks, stop_reason, usage_out)` |
| 流式解析读了 `message_start` 却只取 model 名 | `client.py:179-182` | `input_tokens` 就在同一个对象里，被丢掉 |
| 无任何上下文溢出处理 | 全仓 grep 无命中 | 撞限即 400 抛错 |

---

## 5. 并行独立验证（对抗性复核）

用一个 4 代理的验证工作流，对四条候选根因做**证伪**式复核（默认判 REFUTED，
必须自己跑出测量才可判 CONFIRMED）。结果如下 —— **三条纠正了我的原判断**：

| 候选 | 我的原判断 | 独立复核 | 纠正内容 |
|---|---|---|---|
| H1 参数名不匹配 | 阻塞满时长 | **PARTIAL** | 机制真、但「后果」说过头了：①GWAS 文档那种**带重定向**的写法，即使键名写错也**立刻返回**（0.01s），所以「键名错」单独并不必然阻塞；②真正该修的是「命令串形态」；③**发现我漏掉的更重缺陷**：超时后报假错而子进程仍在跑 |
| H2 `nohup` 不重定向 | 只有 nohup 有这个坑 | **CONFIRMED** | 且推广成立：**任何**未重定向的后台命令都会冻住，**裸 `cmd &` 也一样**（实测 6.00s）；**部分重定向**（只写 `2>/dev/null`）仍然阻塞 8.01s |
| H3 `send()` 静默吞掉 | 这是用户感受的直接来源 | **PARTIAL** | 行是真的（`:1143`，400 字符窗口内无任何提示调用），**且用户的手势到不了它**：`onPrimary()` 先走 `stopRun()`。我把它当「用户感受的来源」是**错的**。复核还找出两个我没发现的状态：镜像陈旧为 `busy=true/running=false` 时按钮**永久**显示「强制中断」且 `stopRun()` 的提示是**死代码**；以及「同一个回车键，发出去还是中断取决于隐藏变量」 |
| H4 confirm 被丢弃 | 已由我自己确认 | 复核中（见 §2.2） | 我已独立测量并确认 C1/C2/C3/R1/R2 |

复核代理还报告：它核对过 `index.html` 的 sha256 前后一致
（`3239cc78…a75063`）—— 证明**探针没有改过源文件**，测的是被服务的那份。

**这次复核的价值**：H1 和 H3 我原本都准备按「确认」写进结论，
是复核把两者的**因果强度**都降了一级 —— 机制成立，但不是用户症状的成因。
按这个仓库的判据标准（「跑通不等于具备」／「无区分度的判据必须标废」），
结论里必须写清「机制成立」与「是当前主因」是两件事。

---

## 6. 我自己在写探针时引入、又被判据抓出的缺陷

| # | 缺陷 | 症状 | 修法 |
|---|---|---|---|
| P1 | C3 量**隐藏**的 `.overlay` | `display:none` 时 `getBoundingClientRect()` 返回 **0×0** → 判「不遮挡」，假 PASS | 先强制 `add('show')` 再量 → 实测全屏 1400×900 挡住三块区域 |
| P2 | B2b 用绝对阈值 `<1.0s` | 本机重负载下 `sleep 6` 本身要 8.19s（bash 启动+profile 开销 ~2.2s）→ 「立即返回」被误判为阻塞 | 先量纯开销再比：`耗时 − overhead`。实测 6.15s vs 0.04s |
| P3 | B3a 极性写反 | 「有 Cron 工具」才判 PASS，而缺陷恰恰是**没有** Cron | 改为「没有 Cron 才 PASS（缺陷确认）」 |
| P4 | B4c 重定向分类器只认 `>/dev/null` 字面量 | 把 `> logs/nohup.out 2>&1` 这种**正确**写法误判为未重定向 → 结论完全反了（报 82%，实际 0%） | 加**负控**跑 5 个样例；负控当场又抓出第二个 bug |
| P5 | `split("&")` 被 `2>&1` 里的 `&` 截断 | stderr 判断永远失败 | 先把重定向运算符占位保护，再切后台作业的 `&` |
| P6 | B4d 把「提到 nohup 的轮询命令」也算成后台启动 | 17 次里 14 次「未重定向」是假的 | 加 `_is_bg_launch()` 只认以 `&` 结尾的作业 → 真实是 9 次 / 0 次漏 |
| P7 | D2 v1 拿秒表当反馈信号 | 恒真 | 见 §2.1.1，改问「这个反应是不是发送」 |
| P8 | 探针里的正则被 Python 三引号吃掉 | `\\b` 变成退格转义，正则静默失效 | 改用按行过滤，不用正则 |
| P9 | 负控自身不隔离 | D5b 刚发过消息，渲染余波被当成负控的「已发出」→ 负控假失败 | 负控改用**专属标志词**，只看它 |
| P10 | 用不存在的函数名 | `refreshSessions` 不存在（真名 `renderSessions`）→ 探针崩 | 查真名；凡探针崩都不算「测出缺陷」 |

**其中 P4/P5 是由我**自己写的负控**抓出来的** —— 这正是负控存在的意义：
B4c 那条如果没写，我会带着「82% 未重定向」这个**完全错误**的结论去改文档。

---

## 7. 修复设计

### 7.1 ① 对话框解放

| 编号 | 改动 | 文件 |
|---|---|---|
| F1 | 4 处 `run_in_background` → `background`；并停止把带重定向的写法描述成「Harness 跟踪」（那种写法没有 task_id，是**未跟踪**的） | `wgs-expressive-pipeline/SKILL.md:6,76`、`references/workflow.md:80`、`gwas-pipeline/SKILL.md:81` |
| F2 | 2 处 `nohup … &` 补重定向（统一为 `> "${LOG}" 2>&1 &`） | `wgs-expressive-pipeline/SKILL.md:78`、`gwas-qc-filter/SKILL.md:57,64` |
| F3 | **A3 是关键**：`CronCreate` 不存在。两条路选一（需用户定） | 见下 |
| F4 | 前台命令超时不再撒谎：区分「命令失败」与「外层超时、子进程可能仍在跑」，并在文案里给出追踪方式 | `tools.py:282-285` |
| F5 | 运行中按发送键的语义显性化：按钮已注明「强制中断」，再补一条**首次触发时的确认或提示**（避免误杀长管线） | `index.html:1415`、`setBusy:1417` |
| F6 | 修 `stopRun()` 的死代码：把提示移到 `return` **之前** | `index.html:1317/1326` |
| F7 | confirm 四修：①`case "confirm"`（及 `pendingConfirm` 的更新，`:1274`）提到闸门**之前**，或至少让闸门**不拦** confirm；②`answerConfirm` 提交 `ev.session`（发问方）而不是 `SID`；③刷新/切回时从快照 `pending_confirm` **重建**弹窗；④`:729` 改用 `attach(id, runInfo.last_seq)` 时，先按快照补一次 `pending_confirm` | `index.html:1273/1274/1280/1306/729` + `applyRunSnapshot` |
| F8 | 全屏遮罩改非模态（或提供「稍后处理」旁路），使一个会话的确认不冻住整个界面 | `index.html:228` |
| F9 | `stashPane()` 的 `ctl.abort()` 保留（它保证了「任一时刻只有一个消费者」），但**必须**让离开期间的 confirm 有兜底 —— 否则它就是 C1-① 的成因。兜底即 F7-③④ | `index.html:417-442` |
| F10 | 前端「断开」与「丢弃」区分开：只断流是不够的，得保证**回来后能补齐**（现 `:729` 从 `last_seq` 起订，等于承认「过去的不要了」） | `index.html:729` |

**F3 的两条路（这是 ① 的核心，需要用户拍板）**：

- **F3-a 让等待发生在「轮次之外」**：新增 `Wait(until=..., timeout=...)` 或
  `TaskWait(task_id, timeout)` 工具，让模型**结束当前轮**、由 harness 在后台等 `.done`，
  到时再自动开新轮推进。用户此刻看到的是「本轮已结束，等待 STAGE.done」——对话框真正解放。
- **F3-b 补上 `CronCreate`/`CronList` 的真实实现**，让文档说的机制成立。

**推荐 F3-a**：它不引入常驻调度器，语义也更贴近生信管线（「等这个标记文件」比「每 30 分钟查一次」精确），且与已有的 `.done`/`.pid` 约定天然契合。F3-b 的问题是 cron 表达式对「等一个具体文件出现」是过度设计，且要处理进程重启后的调度恢复。

### 7.2 ② 按上下文上限压缩并续跑同一对话

**核心原则：用 API 实测的 `input_tokens` 当**唯一**触发器，不再用字符数/条数。**

| 编号 | 改动 | 文件 |
|---|---|---|
| G1 | `client.create` 返回值增加 `input_tokens`。**必须取 `message_delta.usage.input_tokens`**（实测比 `message_start` 的值大 46%，见 §3.1.1）；非流式从 `usage.input_tokens` 取。两处都要改：`message_start` 存一份作回退，`message_delta` 覆盖为权威值 | `client.py:138-227, 229-245` |
| G2 | `_new_usage` 增 `input_tokens`/`context_tokens`/`context_limit`；`_record_usage` 记住**最近一次**的 `input_tokens`（它是当前占用的实测值，不是累加量） | `agent.py:522-524, 302-313` |
| G3 | 阈值改为 token 口径：`CONTEXT_LIMIT=286720`（可被 config 覆盖），`COMPACT_AT_RATIO=0.75` → 触发线 ≈ **215,040 tokens**；再留 `max_tokens`（8192）+ 工具结果余量 | `agent.py:20-24` |
| G4 | `_maybe_compact` 改成：优先用**实测** `input_tokens`；实测还没有时（第一轮）退回 `messages_tokens()` 估算，并把估算值按 1.55× 校准（实测低估系数） | `agent.py:419-437` |
| G5 | 丢弃分支补 `_persist_prefix`（**最关键的一条**）：否则压缩每次 resume 都被撤销（§3.3 实测） | `agent.py:430-437` |
| G6 | 摘要压缩**默认开启**（至少在 webapp 路径），把 `memory_enabled` 与 `summary_compaction` 解耦 —— 摘要器不该依赖 memory | `config.py:211`、`webapp/session.py:259-267` |
| G7 | 遇 400 上下文超限时**自动压缩后重试一次**（而不是整轮报错死掉）；重试用完仍失败才如实报错 | `agent.py:163-167` 外包一层 |
| G8 | `max_turns` 降为**安全网**：默认不再是 100，而是「不按轮次停」；真撞上时文案要说清是兜底而非正常终止 | `agent.py:20, 224` |
| G9 | 界面显示上下文占用（如 `上下文 221k/287k (77%)`），压缩发生时给出**可见提示**，不静默丢历史 | `index.html` + `usage` 事件 |

**为什么阈值取 75% 而不是 95%**：单轮里模型可能连续调多个工具，
每次工具结果都可能很大（`MAX_RESULT_CHARS=100_000` ≈ 3.4 万 token）。
压缩只在**发请求前**发生（`agent.py:153`），所以必须留出一整轮工具结果的余量。
75% 时还剩 ~7.2 万 token，足以容纳两三个大工具结果。

**为什么必须「续跑同一对话」**：压缩后 `history` 被替换、`_compacted=True`，
下一轮 `_run` 正常继续 —— 这已经是现状的机制，缺的只是「触发得对」与「落盘」。
用户要的「继续这个对话框」在架构上**已经成立**，`max_turns` 才是那个把它掐断的东西。

---

## 8. 复现命令汇总

```bash
cd /home/chenyuxi/skill/agent

# ① 后端判据（阻塞三成因 + 负控）          期望 11/11
/home/chenyuxi/anaconda3/envs/Agent/bin/python -I tools/concurrency/blocking_probe.py

# ① 界面判据（输入区/多会话，含正负控）      期望 8/8
./start_web.sh restart
/home/chenyuxi/anaconda3/bin/python tools/concurrency/ui_composer_probe.py

# ① 确认框（事件路由 + 归属 + 遮挡）         期望 3 条缺陷 / 5 条判据
/home/chenyuxi/anaconda3/bin/python tools/concurrency/ui_confirm_probe.py

# ① 确认框生命周期（刷新 + 跨会话）          期望 2 条缺陷 / 3 条判据
/home/chenyuxi/anaconda3/bin/python tools/concurrency/ui_confirm_lifecycle_probe.py

# ② 上限数字（不打印密钥）—— 见 §0 的复现片段

# ② 压缩不落盘（§3.3）
/home/chenyuxi/anaconda3/envs/Agent/bin/python -I - <<'PY'
import sys, tempfile
sys.path.insert(0,'/home/chenyuxi/skill/agent')
from skill_agent.agent import Agent
from skill_agent.config import Config
from skill_agent.skills import SkillRegistry
from skill_agent.tools import ToolSet
from skill_agent.session_store import SessionStore
import os
cfg=Config.load(); tmp=tempfile.mkdtemp(); store=SessionStore(tmp); SID='c'
ts=ToolSet(SkillRegistry(os.path.join(cfg.cwd,'skills')),tmp,permission='auto')
ag=Agent(cfg,SkillRegistry(os.path.join(cfg.cwd,'skills')),ts); ag.store=store; ag.session_id=SID
for i in range(60):
    for r,c in (('user',f'u{i} '+'x'*300),('assistant',f'a{i} '+'y'*300)):
        m={'role':r,'content':[{'type':'text','text':c}]} if r=='assistant' else {'role':r,'content':c}
        ag.history.append(m); store.append(SID,m)
ag._maybe_compact(); print('压缩后内存', len(ag.history))
print('磁盘 load', len(store.load(SID)['messages']))
PY

# 探针不得改动源文件（并行复核用的同一条自查）
sha256sum webapp/static/index.html      # 前后应一致
```

> 环境须知：后端探针用 `envs/Agent` 的解释器，Playwright/pytest 只在 base，**不可混用**。
> `config.json` 含真实 API key（已 gitignore），本文与所有探针输出均不含其任何内容。
> 本机是**重负载**机器：绝对秒数阈值不可靠，判据必须相对化（见 P2）。
