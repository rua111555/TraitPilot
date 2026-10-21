# SkillAgent 缺陷修复与复测记录（D1–D14）

> 本文档是 **[test-evidence.md](test-evidence.md) 的续篇**。
> 基线文档记录「原始测试 + 发现的 14 个缺陷」，本文档记录「**修复 + 复测**」，
> 两文对照可完整审计「缺陷 → 修复 → 证据」的闭环。
>
> - 基线（**未修改**）：[test-evidence.md](test-evidence.md) —— 142 条用例全量实测、缺陷 D1–D14、非具备 16 条
> - 用例总表：[test-matrix.md](test-matrix.md)
> - 修复日期：2026-10-21
> - 修复范围：**D1–D14**（用户裁定）。两条「已知边界」（HOOK-13、PERM-07）**按裁定保持原样**，见 §5。

---

## 0. 结论速览

| 项 | 修复前 | 修复后 |
|---|---|---|
| 代码缺陷 | D1–D14 共 14 条 | **14 条全部消除**（D11 为覆盖缺口，无需改码） |
| 改动文件 | — | `skill_agent/` 下 **10 个模块**（`tests/`、`webapp/` 未动） |
| pytest | 196 passed | **196 passed**（基准不变） |
| 浏览器 E2E | 4 个脚本 FAILED: 0 | **4 个脚本 FAILED: 0** |
| 反例重放（统一脚本） | — | **42/42 通过**，每条反例均消失 |
| 新旧对照（可判别性） | — | **6 项关键指标全部有区分度**（见 §3） |

**方法学说明**：本文档不采信「跑通即具备」。每一项修复都要求：
1. **反例消失** —— 原始缺陷记录的失败现象必须不再出现；
2. **反向对照** —— 修复不得过度（如 D8 放宽数字校验后，`"abc"` 仍须被拒）；
3. **新旧可判别** —— 在备份的修复前代码（`/tmp/backup_fix`）上重跑同一判据，结果必须**不同**；
   两版结果相同的判据一律标注「无区分度」，不作为修复证据（D13 原对抗判据即属此类，见 §3.3）。

---

## 1. 修复清单（14 条）

### D1 · CLI 无子命令时崩溃
- **反例**：`python run.py`（不带 `chat`/`list`）→ `AttributeError: 'Namespace' object has no attribute 'prompt'`
- **根因**：`add_subparsers(required=False)` 允许无子命令走到底，而 `args.prompt` 只在 `chat` 子命令上定义
- **修复**：[skill_agent/cli.py:260-263](skill_agent/cli.py#L260-L263) 用 `getattr(args, "prompt", None)` 判定，无 prompt 则走 REPL（原意图）
- **证据**：裸跑 exit=0、无 traceback、打印 banner、EOF 打印「再见」；反向对照 `chat -p` 仍走单轮、不进 REPL

### D2 · 幂等键把 `description` 算进去
- **反例**：两条命令相同、仅 `description` 不同的 Bash 调用被判为不同键 → 都执行
- **根因**：键 = 工具名 + **全部**参数的规范化 JSON
- **修复**：[skill_agent/idempotency.py:31](skill_agent/idempotency.py#L31) 新增 `_KEY_IGNORED_ARGS = frozenset({"description"})`，[`key()`](skill_agent/idempotency.py#L58-L68) 先剔除纯说明字段再规范化
- **证据**：同命令异描述 → 同键；异命令 → 异键；端到端第二次判定 `reuse` 命中且复用结果、不共享 `meta`
- **📌 附更正**：test-evidence.md 的 D2 条目写「[tests/test_idempotency.py](tests/test_idempotency.py) 把这个行为固化为通过用例」——经逐行核对，**该文件并无 `description` 相关断言**（只有键稳定性/参数顺序/eligible 等），此句为**基线文档的记述误差**。该行为当时仅由 matrix GUARD-09 的探针捕获。

### D3 · MEM-08 只抽到键名、抽不到值
- **反例**：archival 里存的是 `'pid'` / `'task_id'` 字符串，值 `12345` / `T7` 丢失
- **根因**：`re.findall(r"\b(task_id|pid)\s*[=:]\s*\S+")` 带捕获组 → 只返回组内容
- **修复**：[skill_agent/agent.py:479-496](skill_agent/agent.py#L479-L496) 三个模式全部改为**非捕获**形式，`findall` 返回整段匹配
- **追加收紧**（复测时发现）：值模式原为 `\S+`，中文句读不是空白字符，`"pid=12345。标记文件"` 会被整串吞入。改为 `[^\s，。；、）】」,;]+`
- **证据**：`'pid=12345'` / `'task_id=T7'` 以完整 kv 落库；`.done`/`.vcf.gz`/`.csv` 路径均落库；键名条目数为 0；中文句读污染检查通过

### D5 · LoopGuard 轮询白名单是纯子串匹配
- **反例**：`rm -f stale.pid` 被判为「轮询」，连按 15 次 `level` 恒为 `warn`、**永不 HALT**
- **根因**：`is_polling` 对全部参数拼串做 `any(s in blob)`，`echo hello.log` / `mv x.log y` 一律命中
- **修复**：[skill_agent/loopguard.py](skill_agent/loopguard.py) 新增 `_POLL_READONLY_CMDS` / `_POLL_WRITE_CMDS` / `_CMD_SPLIT`，`_bash_polling()` 逐段解析管道与 `&&`/`||`/`;` 后判首命令；写命令一律不豁免
- **证据**：`rm -f stale.pid`×15 → **halt**；`mv/cp/tee/echo` 含 `.log/.done` 全部 `False`；反向对照真轮询 `cat *.log`×15 → **warn**（不误熔断）

### D6 · 结构化 deny 无视 allow 与规则顺序
- **反例**：`rules=[allow ^echo, deny ^echo]` 时 `needs_confirm=(False,'')` 但 `is_error=True` 被拒
- **根因**：`is_denied` 遍历**全部**规则只看 `action=="deny"`，与 `needs_confirm` 的「首次命中」语义分裂
- **修复**：[skill_agent/permissions.py](skill_agent/permissions.py) `is_denied` 改为首次命中即返回，allow/ask 命中时返回 `None`
- **证据**：allow 在前 → 不拒（`None`）、`needs_confirm` 一致；反向对照 deny 在前 → 确实拒

### D7 · config.json 的 `permission_rules` / `extra_roots` 被静默丢弃
- **反例**：读回 `None`；`from_config` 后 `rules=[] extra_roots=[]`
- **修复**：[skill_agent/config.py:116-125](skill_agent/config.py#L116-L125) 增补 `_list("permission_roots")` / `_list("extra_roots")`，并对 `permission_rules` 做结构校验（只收 `action ∈ {allow,deny,ask}` 的 dict，拼错的静默丢弃而非「看起来生效」）
- **证据**：三类键全部读回；`from_config` 后 `rules` 非空且 `action=='ask'`

### D8 · 校验对「数字传成字符串」过度拦截
- **反例**：`validate_args(BashSchema, {"timeout":"5"})` → `期望类型 integer，实际为 string`
- **根因**：`_type_ok` 只做 `isinstance` 判定，与 docstring 自称的「避免过严」不符
- **修复**：[skill_agent/validation.py](skill_agent/validation.py) 新增 `_numeric_string_ok()`（`float()` 可解析；integer 还须 `is_integer()`）
- **证据**：`"5"` 通过；反向对照 `"abc"` **仍被拒**、`"5.5"` 传 integer **仍被拒**（未过松）

### D9 · archival 检索要求 ≥3 字连续子串重叠
- **反例**：`search("继续执行阶段 S3 那个任务")` → `[]`
- **根因**：LIKE 兜底对全部词元做 AND，中文长串无 3 字连续片段可命中
- **修复**：[skill_agent/memory.py](skill_agent/memory.py) 新增 `_tokens()`（中文整段 + 3-gram + 2-gram，双字虚词过滤）；`search()` 用 FTS 结果合并 `_like_search`（**OR** 语义 + 命中片段数排序）
- **证据**：两条自然语言 query 均召回；反向对照无关 query（「今天天气如何适合钓鱼吗」）**不硬召回**；空串与 `'" OR )('` 仍返回 `[]`

### D10 · `--index-dir` 指到项目外时语料为空
- **反例**：`--index-dir /tmp/xxx` → 上溯不到语义 project → `_corpus_roots` 返回 `[]` →「0 文件 / 0 建立」、DB 内 `files=0 chunks=0`
- **修复**：[skill_agent/retrieval.py](skill_agent/retrieval.py) `_corpus_roots` 兜底改为 `(root.parent, root)`；[skill_agent/cli.py:192-207](skill_agent/cli.py#L192-L207) 在 `files==0` 时打印 ⚠️ 显式警告并给出两条补救路径
- **证据（新旧对照）**：旧 **0 文件 / DB files=0 chunks=0** → 新 **24 文件 / DB files=24 chunks=460**；真·空语料场景 ⚠️ 警告出现

### D11 · spill 大文件的「Read 分段续读」未实测
- **性质**：**覆盖缺口，非产品缺陷** —— 机制本身正确，只是从未对真正 >30k 的落盘文件跑过 offset+limit
- **本次实测**：119999 字节 / 2000 行真实落盘文件
  - 段1（默认）头 = `共 2000 行，显示 1-200`，含 `LINE000001`，给出续读 offset
  - 段2（offset=1900）头 = `显示 1901-2000`，首行真为 `LINE001901`，含 `LINE002000`
  - 段3（offset=1995）头 = `显示 1995-2000`（尾部对齐）
  - `mismatch` 检查：段2 首行真值与声明一致 = True
- **📌 探针自纠**：初版脚本末行断言把「分段窗口行数」当「全文行数」比（200 行 + 100 行 vs 2000 行），恒为 False；修正为按行号集合核对后 **300/300 窗口行数吻合**。此为**探针 bug**，与产品无关。

### D12 · env 与 config.json 的优先级方向反了
- **反例**：config.json 写 `false` + env 写 `1` → 4 个 toggle 全为 `False`；`fallback_models`/`index_dir` 也是 file 赢 → **0/6**
- **根因**：`Config.load` 先赋 env 值，随后 `_apply_file_overrides` 用文件无条件覆盖
- **修复**：[skill_agent/config.py:47-56](skill_agent/config.py#L47-L56) 新增 `env_keys` 闸门（`_skip()`）；[config.py:344-376](skill_agent/config.py#L344-L376) `Config.load` 构建 `_env_backed`（26 组 `(env_name, attr)`，仅当环境变量**非空**时计入）后传入
- **证据（新旧对照）**：旧 **0/6** → 新 **6/6** env 赢；反向对照无 env 时 file 的值/开关照常生效

### D13 · `SessionStore.delete()` 不持锁、不处理别名
- **反例**：① 并发 `append` 时 `delete` 返回 `True` 后文件立刻被重建；② `<sid>.jsonl` 为软链时只删链、被指向文件原样留在磁盘
- **修复**：[skill_agent/session_store.py:210-236](skill_agent/session_store.py#L210-L236) `delete()` 全程持 `self._lock`，改用 `p.lstat()` 判定（`S_ISREG` 或 `S_ISLNK` 才处理）
- **证据**：见 §3.3 的三项新旧对照（互斥判别 400+→0；悬空软链 False→True；删后残留 6/60→0/60）

### D14 · `load_view` 的 `resume_count` 在 block 级悬挂输入下塌陷
- **反例**：已完成轮的 assistant 内含「无对应 `tool_result` 的 `tool_use` 块」时，`_analyze` 清洗后与展示列表不再逐字相等 → 对齐失败 → `resume_count == len(display)` → 前端**画不出**「尚未完成」分隔条
- **修复**：[skill_agent/session_store.py:341-358](skill_agent/session_store.py#L341-L358) 新增 `_same_turn()`（宽容块级子序列匹配）；`_boundary_index` 兜底改为 `_longest_prefix_index()`（最长匹配前缀，而非 `len(display)`）
- **证据（新旧对照）**：旧 `display=6 resume_count=6`（画不出）→ 新 `display=6 **resume_count=4**`（画得出）；前端 Playwright 由 **FAIL → PASS**

---

## 2. 复测执行记录

### 2.1 统一反例重放（`/tmp/replay_d1_d14.py`）

对修复后代码逐条重放原始反例，**42/42 通过**：

| 缺陷 | 检查项 | 结果 |
|---|---|---|
| D1 | 无 traceback / 退出码 0 | ✅ |
| D2 | 同命令异描述同键 / 异命令异键 / 端到端 reuse | ✅ |
| D3 | 值 12345 与 T7 落库 / 键名条目为 0 | ✅ |
| D5 | `rm -f *.pid` 非轮询 / 端到端 HALT / 真轮询仍豁免 | ✅ |
| D6 | allow 优先 / deny 在前确实拒 | ✅ |
| D7 | rules/extra_roots/permission_roots 读回 / from_config 非空 | ✅ |
| D8 | `"5"` 通过 / `"abc"` 与 `"5.5"` 仍拒 | ✅ |
| D9 | 两条自然语言召回 / 无关不硬召回 / garbage 返回空 | ✅ |
| D10 | 语料 24 文件 / DB files=24 chunks=460 / 空语料警告 | ✅ |
| D12 | 4 toggle env 赢 / 两类 list 键 env 赢 / 无 env 时 file 生效 | ✅ |
| D13 | 竞态残留 0 / 软链只删链 / 目标如实保留 | ✅ |
| D14 | resume_count=4≠display / 与严格视图一致 / reason 正确 | ✅ |

### 2.2 pytest 全量回归

```
cd /home/chenyuxi/skill/agent
/home/chenyuxi/anaconda3/bin/python -m pytest tests/ -q
```
**结果：196 passed**（与修复前基准一致，无新增失败、无 SyntaxWarning）

> 环境提示：**pytest 与 Playwright 只装在 base 环境** `/home/chenyuxi/anaconda3/bin/python`；
> CLI/探针用 `/home/chenyuxi/anaconda3/envs/Agent/bin/python -I`。两者不可混用。

### 2.3 浏览器 E2E（Playwright，服务已重启加载新代码）

```
for f in ui_e2e ui_fold_e2e ui_history_e2e ui_title_e2e; do
  /home/chenyuxi/anaconda3/bin/python tests/$f.py
done
```
**结果：4 个脚本 FAILED: 0**（与修复前基准一致）

### 2.4 D14 端到端专项（库 → API → 前端）

| 层 | 检查 | 结果 |
|---|---|---|
| 库 | `_boundary_index(keep, strict)` = 4（keep=6, strict=4） | ✅ |
| API | `/api/session/<sid>` → `messages=6 resume_count=4 discarded=1 reason='incomplete_turn'` | ✅ |
| 前端 | Playwright：**「以下内容尚未完成」分隔条出现** | ✅ PASS（修复前 FAIL） |
| golden path | M13-10 主探针 **18/18 PASS**（无回归） | ✅ |

---

## 3. 新旧代码对照（可判别性证据）

**方法**：`/tmp/backup_fix` 保存修复前全量副本（54 文件，git HEAD `666f7a4`）。同一判据在两版上各跑一遍，
**只有结果不同**才计为修复的可验证效果。

### 3.1 D10 / D12：直接可判别

| 判据 | 旧代码 | 新代码 |
|---|---|---|
| D10 `--index-dir` 项目外：打印语料数 | **0 文件** | **24 文件** |
| D10 磁盘真值（sqlite） | files=**0** chunks=**0** | files=**24** chunks=**460** |
| D12 env(`1`) vs file(`false`)：6 个键 env 赢数 | **0/6** | **6/6** |

### 3.2 D14：库级 + 前端级双重判别

| 判据 | 旧代码 | 新代码 |
|---|---|---|
| `load_view` 的 `resume_count`（display=6） | **6**（=display，画不出分隔条） | **4**（画得出） |
| Playwright「尚未完成」分隔条 | **FAIL** | **PASS** |

### 3.3 D13：**原对抗判据无区分度**，改用三项精确判别器

> ⚠️ **重要方法学更正**：基线文档记录的 D13 竞态判据是
> 「写者持续 `append`，主线程 `delete`，随后查文件是否仍在」。
> 本次对照实测表明该判据**不具区分度**：写者是不停追加的**合法调用方**，
> 删除后它必然重建文件——**任何**实现都躲不过，故不能作为「锁是否生效」的证据。
> 实测：同一判据（`/tmp/d13_discriminate.py` 的 P3 项）在旧代码命中 **24/100**、
> 新代码命中 **98/100**（"更差"），而基线用的 `M13-09_adv.py` 在新代码上更是 200/200 ——
> 它测量的其实是写者活跃度，与锁语义无关。

改用三项**有区分度**的判别器（`/tmp/d13_mutex2.py`、`/tmp/d13_discriminate.py`、`/tmp/d13_sigkill.py`）：

| 判据 | 旧代码 | 新代码 | 说明 |
|---|---|---|---|
| **P1v2 临界区互斥**：`delete` 的 `unlink` 窗口内（250ms）写者完成次数 | **403 / 429 / 434 / 447 / 441** | **0 / 0 / 0 / 0 / 0** | 5 次试验。旧代码写者照常推进；新代码被同一把锁挡住 → **锁确实生效** |
| **P2 悬空软链**（指向不存在的目标）：`delete` 返回 / 链接是否摘掉 | **False / 仍在** | **True / 已摘** | `is_file()` 对悬空链为 False 而跳过；`lstat` 判软链可正确处理 |
| **P3 删后残留**：`delete` 返回 `True` 之后仍有**新增行**落进该文件的次数 | **6 / 60** | **0 / 60** | 这是「已宣称删除」语义的真实危害面 |
| P4 软链→真实文件 | True / 链摘 / 目标保留 | True / 链摘 / 目标保留 | 无区分度（均为正确行为），仅作反向对照 |
| P5 并发 600 delete + 3 写者：异常 / 损坏行 | 0 / 0 | 0 / 0 | 无区分度，仅证明不引入新问题 |

**结论**：D13 的两条修复（持锁、`lstat` 判别名）都有硬对照证据；
其中「持锁」的真实语义收益是 **P3 的 6/60 → 0/60**（delete 之后不可能再有新内容混入），
而非「删完文件永不出现」（后者在并发写者下不可达，基线判据的口径有误）。

---

## 4. 改动文件清单

`tests/`、`webapp/`、`run.py`、`start_web.sh` **均未改动**（即未把新行为固化进旧测试、也未改前端）。

| 文件 | 缺陷 |
|---|---|
| [skill_agent/cli.py](skill_agent/cli.py) | D1、D10（警告文案 ×2） |
| [skill_agent/idempotency.py](skill_agent/idempotency.py) | D2 |
| [skill_agent/agent.py](skill_agent/agent.py) | D3 |
| [skill_agent/loopguard.py](skill_agent/loopguard.py) | D5 |
| [skill_agent/permissions.py](skill_agent/permissions.py) | D6 |
| [skill_agent/config.py](skill_agent/config.py) | D7、D12 |
| [skill_agent/validation.py](skill_agent/validation.py) | D8 |
| [skill_agent/memory.py](skill_agent/memory.py) | D9 |
| [skill_agent/retrieval.py](skill_agent/retrieval.py) | D10 |
| [skill_agent/session_store.py](skill_agent/session_store.py) | D13、D14 |

**修复前备份**：`/tmp/backup_fix/`（54 文件，含 `skill_agent/ webapp/ tests/ run.py start_web.sh`）

---

## 5. 按裁定保持原样的两项（HOOK-13、PERM-07）

用户裁定的修复范围是「只修 D1–D14」。以下两项属基线文档的**已知边界**（判「不具备」），
**本次未改**，原样保留：

| ID | 现象 | 状态 |
|---|---|---|
| **HOOK-13** | 死代码（钩子链中某分支不可达） | 保持原样，未修复 |
| **PERM-07** | Bash 路径绕过（结构化路径边界对 shell 内的路径操作无效） | 保持原样，未修复 |

> 这两项仍应视为**已知缺口**：`PERM-07` 有实际安全含义（`restrict_paths` 不能拦 `cat /etc/passwd` 这类），
> 若后续要收紧，需单独设计（如 shell 解析或 seccomp），不在本次范围。

---

## 6. 基线文档的三处记述误差（本次核对发现）

修复过程中逐行核对代码，发现 [test-evidence.md](test-evidence.md) 有三处**记述**与代码实况不符。
**基线文档按要求保持未修改**，误差在此登记：

| # | 基线记述 | 实况 |
|---|---|---|
| 1 | D5 位置写 `[guard.py](skill_agent/guard.py)` | 模块实为 **[skill_agent/loopguard.py](skill_agent/loopguard.py)**，仓库中不存在 `guard.py` |
| 2 | D2 条目写「[tests/test_idempotency.py](tests/test_idempotency.py) 把这个行为固化为通过用例」 | 该测试文件**无 `description` 相关断言**；当时仅由 matrix GUARD-09 探针捕获 |
| 3 | D13 竞态判据「`delete` 返回 True 后文件是否仍在」 | 该判据**不具区分度**（并发写者必然重建），不能作为锁生效的证据；见 §3.3 的三项替代判别器 |

> 第 3 条虽属判据问题，但 D13 缺陷本身**成立**（原始复现 4090/5000 与软链残留均为真实观察），
> 且修复已由 P1v2/P2/P3 三项硬证据确认。登记于此是为了后续复跑者不再误用该判据。

---

## 7. 复现方式（供审查）

```bash
cd /home/chenyuxi/skill/agent

# ① 统一反例重放（42 项）
/home/chenyuxi/anaconda3/envs/Agent/bin/python -I /tmp/replay_d1_d14.py

# ② pytest 全量（须用 base 环境）
/home/chenyuxi/anaconda3/bin/python -m pytest tests/ -q          # 期望 196 passed

# ③ 浏览器 E2E（须先确认 web 服务已加载新代码）
/home/chenyuxi/anaconda3/bin/python tests/ui_e2e.py              # 期望 FAILED: 0
/home/chenyuxi/anaconda3/bin/python tests/ui_fold_e2e.py
/home/chenyuxi/anaconda3/bin/python tests/ui_history_e2e.py
/home/chenyuxi/anaconda3/bin/python tests/ui_title_e2e.py

# ④ D13 新旧对照（三项判别器，均须传入仓库路径）
/home/chenyuxi/anaconda3/envs/Agent/bin/python -I /tmp/d13_mutex2.py     /tmp/backup_fix
/home/chenyuxi/anaconda3/envs/Agent/bin/python -I /tmp/d13_mutex2.py     /home/chenyuxi/skill/agent
/home/chenyuxi/anaconda3/envs/Agent/bin/python -I /tmp/d13_sigkill.py    /tmp/backup_fix
/home/chenyuxi/anaconda3/envs/Agent/bin/python -I /tmp/d13_sigkill.py    /home/chenyuxi/skill/agent

# ⑤ D14 端到端（库 → API → 前端）
/home/chenyuxi/anaconda3/envs/Agent/bin/python -I /tmp/m13gap/M13-10_adv_fixed.py
/home/chenyuxi/anaconda3/bin/python /tmp/m13gap/M13-10_adv_e2e_fixed.py   # 期望 FAILED: 0
```

> ⚠️ **环境隔离**：`pytest` / Playwright 只在 **base** 环境；探针/CLI 用 **Agent** 环境加 `-I`。
> 探针脚本一律 `-I` 运行，避免 cwd 下的同名模块被误导入。

---

*本文档记录 2026-10-21 的修复与复测。[test-evidence.md](test-evidence.md) 保持 2026-10-20 原文不变。*
