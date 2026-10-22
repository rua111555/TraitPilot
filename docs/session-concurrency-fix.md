# 会话并发缺陷修复与验证（切走 / 切回 / 解锁）

用户报告的原始症状（逐字）：

> 目前我发现了一个问题，对话中有正在运行的命令时，我退出了这个对话框，显示中断了，然后我询问目前跑的怎么样了，他提示我 **该会话正在运行中，请稍候**。但是我看不到任何别的东西，会话是否真的在运行中我也不清楚，而且整个 skill 应该是在跑的时候，**对话框是解放的**，而不是无法进行对话。同时，我也发现在**对话进行中时，无法切换到其他的对话**，应该是点击其他对话时，**保留原本会话的通信**，跳转到其他会话中。

本文记录：根因 → 修复 → 每一条测试的**内容与结果**（含判据的反例形式、区分度、以及被我自己判废的判据）。

---

## 0. 结论速览

| 用户症状 | 根因 | 修复 | 判据（下文编号） | 结果 |
|---|---|---|---|---|
| 「显示中断了」 | SSE 响应一断就无条件 `cancel` + 复位 busy | 断开只退订，Run 归 agent 线程独占 | I2 / F2c / F2d | 通过 |
| 「提示运行中，但看不到任何别的东西」 | 没有任何「这一轮在做什么」的对外入口 | `Run.progress` 快照 + `/api/session/<sid>/run` | I3 / I5 / F2b / F4b | 通过 |
| 「会话是否真在跑我也不清楚」 | `busy` 有三个写者（HTTP 响应生命周期 / 会话线程 / cancel），无单一事实来源 | `busy` 改为**只读派生**属性（全仓零赋值，可 grep 验证） | I3 + §4 | 通过 |
| 「跑的时候对话框应该解放」 | `setBusy(true)` 里 `inputEl.disabled = b` —— 一个会话在跑就锁死整个输入区 | 输入框不再禁用；忙碌判定按**会话**而非全局 | F1a / F1b / F6b | 通过 |
| 「无法切换到其他的对话」 | 切走 = 销毁窗格 + 断流，切回来只剩磁盘历史 | 切走时**暂存窗格 DOM**（Fragment）+ 记游标；切回原样搬回、按游标续订 | F3a / F3d / F5 | 通过 |

**测试总览**：后端探针 6/6（改造前 2/6）、浏览器探针 17/17（含 1 条负控）、pytest 196 passed、既有 E2E 4/4。

---

## 1. 四个后端缺陷（先复现，再修）

复现脚本 `/tmp/rename/repro_busy.py` 在改造前确定性复现了 4 条：

| 编号 | 缺陷 | 反例（改造前的实测） |
|---|---|---|
| R1 | 同一会话可并发双跑 | 断开 SSE 后再 POST，两次都 200，agent 并发峰值 = 2 |
| R1b | 断开即被判定为空闲 | 线程仍在跑，`busy` 已为假 |
| R2 | 断开 = 任务被取消 | 断开后 `runs_finished` 不再增长，被取消次数 +1 |
| R3 | 「运行中」无任何可查询入口 | 无 status 端点；唯一表述是 409 的文案 |

### 1.1 设计评审另外发现的两条（我此前漏掉）

这两条由设计阶段的独立评审给出，改造后**由架构本身消除**，不是补丁堵住的：

- **事件串台**：`queue.SimpleQueue.get()` 被每个 HTTP 响应破坏式共享 → 新一轮流的头几帧是**上一轮**的事件（实测：新流前 3 帧为旧轮事件）。
  修复：事件进 **追加式日志**（带单调 `seq`），订阅者各持游标做**非破坏式** `pull`。
- **确认请求悬挂**：`ask_confirm` 里 `ev.wait()` 无超时 → 断线后无人能应答，agent 线程**永久挂住**（R1 的「杀不掉的会话」多半就是它）。
  修复：`CONFIRM_TIMEOUT`（默认 300s，`SKILL_AGENT_CONFIRM_TIMEOUT` 可调），超时自动拒绝并 emit 一条 `notice` 让用户看得见。

---

## 2. 架构：把「一轮」升格为对象

改造前，**运行态 / 事件缓冲 / 一个 HTTP 响应**三者同生命周期。现在拆开：

```
agent 线程 ──独占──> Run（状态机 + 追加式事件日志 + 进度快照）
                        ↑ 只读
HTTP 响应 ──订阅──> Subscriber（自持游标，非破坏式拉取，可多订阅）
```

- **唯一写者**：只有 agent 线程的 `finally` 与 `/api/cancel` 能改 `Run.state`；SSE 断开只做 `run.unsubscribe(sub)`。
- **状态机**：`running → cancelling → done|error|cancelled`（`TERMINAL` 三元组）。
- **有界日志**：`LOG_MAX_EVENTS=4000` / `LOG_MAX_BYTES=2MiB`，按**整事件**淘汰并抬高 `truncated_before` 水位；被截断时发 `resync` 帧，而不是假装完整。
- **进度快照** `Run.progress`：`{step, phase, tool:{id,name,started_at}, hint, last_event_at}` —— 回答「在跑什么」的唯一权威来源。

新增/变更端点：

| 端点 | 作用 |
|---|---|
| `GET /api/session/<sid>/run` | 运行快照（running/state/elapsed/progress/pending_confirm/partial_text/last_seq） |
| `GET /api/session/<sid>/stream?after=N` | 附着到正在跑的 Run：回放 `(after, now]` + 续实时；已结束则回 JSON |
| `POST /api/chat` | 「判忙 + 注册 Run」在**同一把锁内**完成，关死 409 竞争窗口；409 时附当前 run 快照（前端可转为订阅） |
| `POST /api/cancel` | 协作式取消，**不动订阅者**；返回 `{cancelled, run, state}` |
| `GET /api/sessions` | 为列表项附 `running/state/elapsed/progress` → 左栏不用点进去就知道谁在跑 |

### 2.1 前端：切走 = 暂存窗格 + 断流；切回 = 原样搬回 + 按游标续订

- `runs: Map<sid, {running, state, run, progress, controller, ...}>` —— 运行态按会话分表。`state.busy` 降级为
  「**当前查看会话**是否在跑」的**派生镜像**（保留给既有 E2E 读），事实来源是 `runs` 表。
- `activeViewId` 收敛到**唯一写入口** `setCurrent(id)`，它一变就重算 `state.busy` 镜像。
- `panes: Map<sid, {frag, cursor, run, ...}>`：切走时把 `#chat` 的子节点整体搬进 `DocumentFragment`
  （节点还活着），并记下**游标 + 轮次 id**；切回时原样搬回。
  断开当前流（只退订）→ 切回时 `attach(after=cursor)` 补齐离开期间的事件，**不重不漏**。

为什么要「暂存窗格」而不是「切回来重建」：重建只能拿 `partial_text` 近似，会丢掉折叠状态、
已展开的工具卡、以及**末段正文之前的结构**。暂存是原样的。

---

## 3. 后端判据探针（`/tmp/rename/probe_after.py`）

实现无关：走真实 Flask（随机端口）+ 桩 agent（100 块 × 0.1s = 10s 运行期），用 HTTP/SSE 观测行为。
每条判据以**反例**形式给出。运行：

```bash
/home/chenyuxi/anaconda3/envs/Agent/bin/python -I /tmp/rename/probe_after.py
```

| 编号 | 判据（反例形式） | 内容 | 改造前 | 改造后 |
|---|---|---|---|---|
| I1 | **断开后不得并发双跑** | 未断开时 POST 应 409；**断开后（线程仍在跑）也应 409**；总启动数 == 1 | 缺陷 | PASS（409/409/1，峰值 1） |
| I2 | **SSE 断开 ≠ 任务被取消** | 断开瞬间 `runs_finished==0`；之后 ==1；被取消次数 ==0 | 缺陷 | PASS（1221B 已收；0→1；取消 0） |
| I3 | **「运行中」表述 == 线程真实状态** | 运行中与结束后各查一次，接口值须等于 `agent._running>0` | 缺陷 | PASS（True/True，False/False） |
| I4 | **[回归护栏]** 多会话并行 + 轮次结束后可复用 | A、B 可同时各跑一轮；线程退出后再次 POST == 200 | PASS（护栏，无区分度） | PASS（200/200；复用 0.1s） |
| I5 | **后加入的客户端能看到「正在跑」** | 断开后遍历所有合理入口，任一给出该轮证据 | 缺陷 | PASS（命中 `/api/session/<sid>/run`） |

**结果：改造前 2/6 → 改造后 6/6**（改造前基线存档于 `/tmp/rename/probe_baseline_prefix.py`）。

### 3.1 被我自己判废的判据（诚实记录）

- **I1 初版无区分度**：只断言「未断开时被拒」，改造前也 PASS。重写为真实失败路径：
  **断开后再发**，断言 `409 and runs_started == 1`。
- **I4 无区分度**：改造前后都 PASS。已在表里标为「回归护栏」，不当作修复证据。
  「杀不掉的 Bash」场景并入 I4，桩接到会话的 `cancel_event` 上。
- **探针曾自己撒谎**：早期版本把时间标签写成硬编码的 `t=1.4s`，而非实测。
  已全部替换为实测的 `el()` 辅助函数 —— 探针输出必须是被测出来的，不是写上去的。

---

## 4. `busy` 是只读派生的可验证证据

```bash
grep -rn "\.busy\s*=" --include=*.py . | grep -v "==\|!=\|# "
# → 无任何输出：全仓没有一处给 busy 赋值
```

`WebSession.busy` 现为 `@property`，返回 `self.run is not None and self.run.running`。
改造前它是普通属性，被三处写：HTTP 响应生命周期、会话线程、cancel —— 这才是「busy 说谎」的根因。

---

## 5. 浏览器判据探针（`/tmp/rename/ui_concurrency_probe.py`）

驱动真实 Chromium（Playwright）验证后端探针覆盖不到的界面行为。
用 `sleep 20` 真机命令制造**确定的 20s 运行窗口**，避免「跑太快、窗口捕不到」的随机失败。

```bash
./start_web.sh restart
/home/chenyuxi/anaconda3/bin/python /tmp/rename/ui_concurrency_probe.py
```

| 编号 | 判据（反例形式） | 内容 | 结果 |
|---|---|---|---|
| F1a | 运行中输入框**未**被 disable | 直接读 `#input.disabled` | PASS |
| F1b | 切到别的会话后仍可输入 | 写入后读回值 | PASS |
| F6a | 在 A（在跑）时 `state.busy == True` | 镜像语义 | PASS |
| F6b | **切到空闲的 B 后 `state.busy` 翻 False** | 「界面解锁」的实质 | PASS |
| F6c | 切走后 A 的运行态仍在表里 | `isRunning(A) == True` | PASS |
| F2a | 本轮已进入工具执行阶段 | 出现 `.tool-card` | PASS |
| F2b | 后端快照指明在跑什么 | `running=True, phase=tool, tool=Bash, hint='sleep 20 && echo ZZMARK'` | PASS |
| F2c | **切走后该会话仍在跑** | 切走耗时 1.5s，处于 20s 窗口内，`running=True` | PASS |
| F2d | 切走没把它变成 `cancelled` | `state=running` | PASS |
| F2e | 主区已显示 B 自己的历史 | 含 `uiprobe_b` | PASS |
| F4 | 左栏显示运行中标记 | `.sess.running` + `.run-dot` + `.run-sub`（含工具名） | PASS |
| F4b | 状态条说出**在跑什么**（含工具名，非 `[object Object]`） | `执行 Bash` | PASS |
| F3a | 切回后本轮工具卡还在（**同一张**） | 卡数 1、id 唯一 | PASS |
| F3b | 切回后能看见本轮在做什么 | 含 `sleep` 提示 | PASS |
| F3c | 切回后后端仍认这个会话在跑 | `running=True` | PASS |
| F3d | **切回未造成消息节点翻倍** | user=2 / assist=2（磁盘历史 1 + 本轮 1） | PASS |
| F5 | **切回收尾后结构不翻倍** | 工具卡 1、id 数 1、user 2、assist 2、tail 2 | PASS |
| F7 | 本轮正常收尾（未被切走影响） | `state=done` | PASS |
| F8 | 全程无 JS 运行时错误 | `pageerror==[]` | PASS |
| 负控 | **人为复制一张工具卡后，F5 判据确实判为不合格** | `toolCards=2, ids 相同 → wouldFail=True` | PASS |

**结果：17/17 通过（含 1 条负控）。**

### 5.1 被我判废并重写的判据（诚实记录）

第一版浏览器探针报 13/13，但其中三条**没有区分度**，我复核后重写 —— 重写版立刻抓出了 3 个真缺陷：

| 原判据 | 为什么废 | 重写后 |
|---|---|---|
| 旧 F3 量 `#chat .md`（第一段） | 命中的是**回放的历史**（"先前的回答 uiprobe_a" 恰好 15 字符），根本没验证本轮实时内容 | 量末段 + 断言工具卡**数量与 id** |
| 旧 F2d 只等 1.5s | 任务往往已 done，判据退化成恒真 | 用 `sleep 20` 造确定窗口后再断言 `running=True` |
| 旧 F6a `state.busy == isRunning(SID)` | 两边同一个会话，永远相等 | 在 A 跑着时**切到 B**，断言 `busy` 翻 False |
| 旧 F4b `"Bash" in text or "运行" in text` | `or` 太松，放过了 `执行 [object Object]` | 必须含工具名**且**不含 `[object Object]` |
| 旧 F5 数标志词出现次数（得 7） | **7 次是正常内容**：工具参数里有、工具结果里有、模型原样回显 —— 判据本身错了 | 改为**结构计数**（工具卡/气泡/游标不翻倍），并加**负控**证明有区分度 |

### 5.2 探针自身的捷径造成的两次假失败（诚实记录）

- `setCurrent(sid)` 只改 id **不渲染**，用它「切到 SS」会让窗格里没有磁盘历史，
  于是 F3d/F5 的基准缺失 → 报 `user=1 assist=1` 假失败。
  修正为走**真实路径**（点左栏 = `selectSession` → `replayMessages`）后即通过。
  —— 这说明：探针若绕开被测路径，测的是探针自己。
- 同理，早前 I3/I5 的失败是探针的**端点列表过期**（用了不存在的 `/api/session/status`），
  不是实现问题；补上真实端点后 6/6。

---

## 6. 修复过程中我自己引入、又被判据抓出的缺陷

这些是**本次实现过程中**产生的真实缺陷，全部由上面的判据（而非事后复查）抓出：

| # | 缺陷 | 症状 | 修复 |
|---|---|---|---|
| D1 | `progress.tool` 是**对象** `{id,name}`，前端当字符串拼模板 | 页面显示「执行 **[object Object]**」 | 加 `toolLabel(p)` 统一取值；三处调用点改用它 |
| D2 | `activeViewId` 有三个写者，只有 `setRun` 会同步镜像 | 切到空闲会话后发送按钮仍显示「停止」，点下去走 `stopRun` | 收敛到唯一写入口 `setCurrent(id)` |
| D3 | `viewingHistory` 被引用但**从未声明**（严格模式） | `ReferenceError`（本次改造把它从 `let` 改成隐式全局时引入） | 恢复声明为兼容位，并注明真正的闸门是 `shouldRender()` |
| D4 | `addNoticeMsg` **不存在** | 点停止时抛 `ReferenceError`，收尾路径中断 | 改用已存在的 `notice()` |
| D5 | `consume()` 尾包 flush 丢了 `sid` | 尾部事件会被画到**当时正在看的别的会话**里 | `handleEventSafe(buf, sid)` |
| D6 | 暂存窗格只记游标数字，不记**轮次 id** | 每轮是新 Run、seq 从 1 重开 → 下一轮会拿旧大游标续订，**静默跳过新轮开头** | 游标连 `run` 一起存；`paneAfter/restorePane` 校验轮次一致 |
| D7 | 切走时 abort 流会被 `send()` 的 `AbortError` 分支当成「停止」 | 切走会弹「已请求停止本轮」—— 撒谎 | 加 `detached` 标记区分「切走」与「停止」；`stopRun` 先清标记 |
| D8 | 暂存时未 flush 挂起的节流渲染 | 存下来的是半截正文（正文每 200ms 才重渲染） | `stashPane` 里先 `flushRender()` |
| D9 | `end` 帧无条件丢弃暂存窗格 | 取消/出错时尾部内容**不在磁盘上**，切回来那段输出就没了 | 仅 `state==="done"` 时丢弃 |

### 6.1 取消的诚实语义（不撒谎）

Python 杀不掉线程：`cancel` 是协作式的，只在 `agent._run` 的轮次边界检查（`agent.py:147`）；
正在执行的 Bash 阻塞在 `subprocess.run`（`tools.py:281`）里，要等它自己的内层超时返回。
所以界面显示 **「正在取消…（正在执行的命令要等它自己返回）」**，而不是「已停止」。
`Run` 状态会先停在 `cancelling`，前端据此显示黄色状态条。

---

## 7. 回归测试

| 套件 | 内容 | 结果 |
|---|---|---|
| `pytest tests/ -q` | 全量单元/集成测试 | **196 passed in 8.40s**（与改造前基线一致） |
| `tests/ui_e2e.py` | 右键菜单归档/删除、切历史会话、高亮跟随、发送按钮形态（读 `state.busy`） | **FAILED: 0** |
| `tests/ui_fold_e2e.py` | 折叠（流式期间就折上）、导出不丢、中断收尾 | **FAILED: 0** |
| `tests/ui_history_e2e.py` | 历史重建、思考折叠、左栏标题/预览 | **FAILED: 0** |
| `tests/ui_title_e2e.py` | 标题与历史回放 | **FAILED: 0** |

前端符号契约（`/tmp/rename/e2e_contract.md`）全部保持可用：`setBusy / stopRun / send / onPrimary /
cleanupTurn / addAssistMsg / state.busy / chatEl / curAssist / curFlow / curTextBox /
#sendBtn.running / #sendIconRun / #sendIconIdle` 一个都没删，只在其上扩展。
`setBusy` 的唯一行为变更是**不再 `inputEl.disabled=b`** —— 这正是本次修复的目标。

前端语法用 Node 独立解析校验（`new Function(<script 块>)`）：**语法 OK，1236 行**。

---

## 8. 涉及文件

| 文件 | 变化 |
|---|---|
| `webapp/runstate.py` | **新增**（232 行）：`Run` / `Subscriber` / 状态机 / 有界事件日志 / 进度快照 |
| `webapp/session.py` | `busy` 改只读派生；`begin_run` 锁内判忙+注册；`run_turn` 唯一终态写者；`ask_confirm` 加超时；`cancel` 幂等 |
| `webapp/server.py` | 新增 `/run`、`/stream`；`/api/chat` 锁内判忙、409 附快照；`/api/sessions` 附运行摘要；`/api/clear` 忙碌时 409；`_sse_for` 断开只退订 |
| `webapp/static/index.html` | `runs` 分表；`panes` 窗格暂存；`toolLabel`；`setCurrent` 唯一入口；输入框解锁；运行状态条 + 左栏运行标记 |
| `tools/concurrency/` | **新增**：三条可复跑的判据脚本（`probe_after.py` 后端 6 条 / `ui_concurrency_probe.py` 浏览器 17 条含负控 / `repro_busy.py` 改造前白盒对照，现已不适用） |

改动量：`3 files changed, 674 insertions(+), 111 deletions(-)` + 新增 `runstate.py`。

---

## 9. 复现命令汇总

```bash
cd /home/chenyuxi/skill/agent

# 后端判据（实现无关，走真实 Flask + 桩 agent）
/home/chenyuxi/anaconda3/envs/Agent/bin/python -I tools/concurrency/probe_after.py       # 期望 6/6

# 浏览器判据（真机 sleep 20 造窗口 + 负控）
./start_web.sh restart
/home/chenyuxi/anaconda3/bin/python tools/concurrency/ui_concurrency_probe.py            # 期望 17/17

# 改造前的缺陷复现（白盒脚本，依赖已删除的 server._sse_stream；
# 在当前代码上它只报告「不适用」，指向黑盒判据，不作为测试）
/home/chenyuxi/anaconda3/envs/Agent/bin/python -I tools/concurrency/repro_busy.py

# 回归
/home/chenyuxi/anaconda3/bin/python -m pytest tests/ -q                           # 期望 196 passed
for f in ui_e2e ui_fold_e2e ui_history_e2e ui_title_e2e; do \
  /home/chenyuxi/anaconda3/bin/python tests/$f.py; done                           # 期望各 FAILED: 0

# 根因可验证性：busy 无写入点
grep -rn "\.busy\s*=" --include=*.py . | grep -v "==\|!=\|# "                    # 期望无输出
```

> 环境须知：后端探针与 pytest 使用**不同解释器**（`envs/Agent` vs base），不可混用；
> Playwright 只装在 base。`config.json` 含真实 API key（已 gitignore），本文不含其任何内容。
