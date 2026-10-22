# 改名记录：WGS_Agent / SkillAgent → TraitPilot

> 任务：仓库改名为 TraitPilot，把仓库内所有旧名相关内容改掉。
> 范围（用户裁定）：**仓库名 + 产品名**。包名 `skill_agent`、环境变量前缀 `SKILL_AGENT_*`、运行时目录 `~/.skill-agent` 保持不变。
> 执行日期：2026-10-21 ｜ 改名起点 commit：`645a98c8e5fbdc7a0b364fdc05f465ae7573ed7c`

---

## 0. 结论

| 项 | 结果 |
|---|---|
| 实际改动文件 | **22 个**（21 个入库文件 + 1 个不入库的 `config.json`） |
| 旧名残留（入库文件） | `WGS_Agent` 0 处、`SkillAgent` 0 处、`skillagent` 0 处 |
| 保留层计数（改前→改后） | `skill_agent` 440→440、`SKILL_AGENT_` 174→174、`.skill-agent` 80→80、`WGS` 领域词 58→58 |
| 运行时证据 | **9/9 PASS**（§3） |
| pytest | **196 passed**，与改名前基线一致 |
| E2E | **4/4 FAILED:0** |
| 远程 URL | `git@github.com:rua111555/TraitPilot.git`，HEAD 与原 URL 同一 commit |

> ⚠️ **判据例外（必须连同本文档一起读）**：本文档**自身以引用形式存档旧名**（§1 分层表、§2 清单、§5 原名存档、§8 复核命令）。
> 因此「旧名是否绝迹」的 grep 判据**必须排除本文件**，否则计数非 0 属预期行为、不是残留：
> ```bash
> git grep -I -o -e "WGS_Agent" -e "SkillAgent" -e "skillagent" -- . ':!docs/rename-to-traitpilot.md' | wc -l   # 应为 0
> ```
> 存档旧名是刻意的：§5 说明若不登记原值，`test-evidence.md` 的「实测证据」列与其被改后的内容之间将无法对齐。

---

## 1. 改名范围与保留层

三层名字必须分清，混改会破坏运行：

| 层 | 旧值 | 处置 | 理由 |
|---|---|---|---|
| **仓库名** | `WGS_Agent` | → `TraitPilot` | 用户改的是仓库名 |
| **产品名** | `SkillAgent` | → `TraitPilot` | 用户口径「产品名一并改」 |
| 包名 | `skill_agent` | **保留** | 440 处 import，改则全仓崩 |
| 环境变量前缀 | `SKILL_AGENT_*` | **保留** | 用户既有 shell 配置依赖 |
| 运行时目录 | `~/.skill-agent` | **保留** | 本机已有 53 会话 + 44 checkpoint |
| 领域词 | `WGS`（全基因组测序） | **保留** | 生信术语，非产品名 |

变体穷举扫描（大小写 + 分隔符组合）确认**只有两个变体存在**：

```
WGS_Agent          15 处
SkillAgent         28 处
（WGS_AGENT / wgs_agent / Skill_Agent / skillAgent / skill agent / SKILL_AGENT_AGENT 均 0 处）
```

外加两个小写变体（属产品名层）：`skillagent-`（docker 容器名前缀 / 导出文件名，13 处）、`prog="skill-agent"`（argparse 对外显示名，1 处）。

### 1.1 判为「在范围内」的边界项

三处需人工判定，结论均为**改**：

| 处 | 内容 | 判定 |
|---|---|---|
| `skill_agent/cli.py:88` | `prog="skill-agent"` | argparse 对外显示名，`--help` 里给用户看 → 产品名层，改。核验：`tests/` 无任何断言依赖它 |
| `webapp/static/index.html:988` | 导出文件名 `skillagent-<SID>.md` | 用户下载到手的东西 → 产品名层，改 |
| `skill_agent/sandbox.py:198` | docker 容器名 `skillagent-<task_id>` | 进程列表里可见 → 产品名层，改（连带 SBOX-09/11/12 证据同步） |

### 1.2 判为「不在范围内」的边界项

- `localStorage.sid` / `sessionStorage.sid`：通用键名，不含旧名，未动。
- `.gitignore` 的 `.skill-agent/`：运行时目录，保留。
- 本机 `config.json` 的 `api_key` / `base_url` / `model`：**未动**（见 §4 指纹校验）。

---

## 2. 改动清单（22 个文件）

改动方式：定点字符串替换（`WGS_Agent`→`TraitPilot`、`SkillAgent`→`TraitPilot`、`skillagent-`→`traitpilot-`、`prog="skill-agent"`→`prog="traitpilot"`），**非**全文 sed；改后逐文件 diff 审计，确认无预期外改动。

### 仓库名层（3 文件 / 15 处）
```
README.md:14,15,51,67             clone URL + cd ~/TraitPilot
docs/usage.md:36,60,112,122,144,289,522
environment/README.md:213,214     clone URL + cd ~/TraitPilot
```
`docs/usage.md:144` 是「入口等价」说明段，含 `cd ~/TraitPilot && python3 -m skill_agent` —— 仓库名已换、包名保留，正体现两层分离。

### 产品名层（19 文件 / 28 处 + 2 变体）
```
README.md:1                       标题
docs/usage.md:1                   标题
docs/test-matrix.md:1,3           标题 + 引言
docs/test-evidence.md:1           标题
docs/test-fix-report.md:1         标题
docs/harness-gap-analysis.md:1    标题
environment/README.md:3           面向读者的说明
environment/install.sh:3,152      注释 + 安装横幅
environment/check_env.sh:2        注释
requirements.txt:2                注释
run.py:2                          docstring
run_web.py:2,44                   docstring + 启动提示
skill_agent/agent.py:29           system prompt「你是 TraitPilot」
skill_agent/cli.py:22             BANNER
skills/README.md:1                标题
start_web.sh:2                    注释
webapp/server.py:1                docstring
webapp/static/index.html:6,253,631,722,734,967,984   <title>/角色标签/导出表头
config.json.example:2             "//" 注释键
```

### 小写变体（4 文件 / 13 处）
```
skill_agent/sandbox.py:198                    容器名 f"skillagent-{task_id}"
webapp/static/index.html:988                  导出文件名
docs/dependency-strategy.md:130               记录的 docker 命令样式
docs/test-evidence.md:152,154,285             实测证据里的观测 argv（见 §5 说明）
docs/test-matrix.md:164,238                   SBOX-12 判据 + WEB-11 步骤里的可复现命令行
```

### 仓库外
```
git remote set-url origin git@github.com:rua111555/TraitPilot.git
config.json（gitignored，本机私有）: "//" 注释键
```

---

## 3. 运行时证据（9/9 PASS）

判据：**不能只靠 grep 说改好了**，要让代码真的把新名字吐出来。全部为真跑，非静态读取。

| # | 检查项 | 实测值 | 结果 |
|---|---|---|---|
| 1 | CLI 横幅（真跑 REPL 入口 `run.py chat`，喂 `/exit`） | `│  TraitPilot — 自带 skills 的本机 Agent` | **PASS** |
| 2 | argparse prog（真跑 `run.py --help`） | `usage: traitpilot [-h] ...` | **PASS** |
| 3 | system prompt 自称（`agent.py` 正则取 `base = f"""...`） | `你是 TraitPilot` | **PASS** |
| 4 | docker 容器名 fg（真造 `DockerBackend({'memory':'64m'})` 取 argv） | `traitpilot-fg` | **PASS** |
| 5 | docker 容器名 bg | `traitpilot-t1` | **PASS** |
| 6 | 静态页 `<title>` | `TraitPilot` | **PASS** |
| 7 | 导出文件名模板 | `` traitpilot-${SID}-${Date.now()}.md `` | **PASS** |
| 8 | 导出表头 | `# TraitPilot 对话导出` | **PASS** |
| 9 | **Flask 真起服务取 HTTP 响应**（`make_server` + `urllib`） | 首页 `<title>` = `TraitPilot` | **PASS** |

> 第 1 项注意：`BANNER` 只在 REPL 入口打印，`--help` 不走它。首轮探针误用 `--help` 取横幅判 FAIL，是探针取数方式错，非代码问题；改用真跑 `chat` 入口后 PASS。
> 第 4/5 项注意：`_docker_argv` 真实签名是 `(command, cwd, task_id)`，`--name` 的值在 `argv[4]`。首轮探针签名与下标都写错，修正后 PASS。

探针脚本：`/tmp/rename/probe_runtime.py`（非仓库文件）。

---

## 4. 保留层未被误伤的证明

改名前先固定基线计数，改后逐项对照。**计数完全一致 = 保留层逐字未动**。

| 保留层 | 改前 | 改后 | |
|---|---|---|---|
| `skill_agent`（包名） | 440 | 440 | ✅ |
| `SKILL_AGENT_`（环境前缀） | 174 | 174 | ✅ |
| `.skill-agent`（运行时目录） | 80 | 80 | ✅ |
| `WGS`（领域词，剔除 `WGS_Agent` 后） | 58 | 58 | ✅ |

`git grep` 口径复核（只看入库文件）：`skill_agent` 440、`SKILL_AGENT_` 174、`WGS` 领域词 58 —— 与工作区口径一致。

**本机 `config.json` 改动校验**（该文件含真实凭据，不入库）：

```
旧名残留      : 0 处
JSON 仍合法   : True（键数 16）
api_key 指纹  : 7adc03e9138a35c7 → 7adc03e9138a35c7  未变 ✅
base_url 完好 : True ; model 完好: True
字节差        : 0（'SkillAgent'→'TraitPilot' 等长替换）
```

---

## 5. 改到了「历史观测值」，审计链说明

`docs/test-evidence.md` 是被要求**保持原文**的基线实测文档。本次按「全部修改」口径动了它，涉及 4 处 —— 性质必须讲清：

- **SBOX-12（line 154）**：`skillagent-fg` 是当时**真实观测到的容器名**，写在「实测证据」列。改后会与当时的事实不符。
- **SBOX-09（line 152）/ SBOX-11（line 285）**：同样，`skillagent-fg`、`skillagent-t1` 是当时打印出的 argv 原值。
- **WEB-11（test-matrix.md line 238）**：`skillagent-<SID>-*.md` 是操作步骤，属于会被照着执行的**可复现命令行**。

保留旧名的实际危害更大：`test-matrix.md` SBOX-12 的判据列写着可复现命令行 `docker run --name skillagent-fg ...`，而代码改后实际生成 `traitpilot-fg` —— **照着文档重跑就复现不出记录的结果**，文档会变成自相矛盾的。故一并改掉，并在此处登记原值。

原名存档（供追溯）：
```
docs/test-evidence.md:152  '--name','skillagent-fg'
docs/test-evidence.md:154  固定容器名 `skillagent-fg`；stderr 含 "/skillagent-fg"
docs/test-evidence.md:285  --name skillagent-t1
docs/test-matrix.md:164    --name skillagent-fg（命令行 + 反例串）
docs/test-matrix.md:238    下载的 skillagent-<SID>-*.md
```
原件完整备份：`/tmp/rename/backup_pre/`（28 个文件，改前状态）。

---

## 6. 回归测试（改名的爆炸半径核验）

先确认爆炸半径：`tests/` 对 `WGS_Agent`/`SkillAgent`/`skillagent` **零命中**，故预期改名不打红任何测试。

### 6.1 pytest

```
$ /home/chenyuxi/anaconda3/bin/python -m pytest -q
196 passed in 7.73s
```
→ 与改名前基线（196）**一致**，无回归。

### 6.2 E2E（Playwright，base 环境）

```
$ for t in ui_e2e ui_fold_e2e ui_history_e2e ui_title_e2e; do python tests/$t.py; done
ui_e2e.py          FAILED: 0    （运行中按钮 running 样式 / 点按走中断分支 / 空闲恢复，全 PASS）
ui_fold_e2e.py     FAILED: 0    （折叠/中断收尾/导出不丢全文，全 PASS）
ui_history_e2e.py  FAILED: 0    （无丢弃提示 / 有思考折叠块 / 左栏标题取用户第一句，全 PASS）
ui_title_e2e.py    FAILED: 0    （顶部提示条 + 历史回放，全 PASS）
```
→ 4/4 全绿，与改名前基线一致。

### 6.3 前端改名在浏览器路径上的真实验证

`ui_title_e2e.py` 实际输出中，页面顶部提示条打出的是 **`TraitPilot`** —— 这是新名字在真实渲染路径上的证据，不是静态 grep：

```
pig_geno 库里有哪些表？能做什么分析？
TraitPilot          ← 页面提示条的真实文本
```

---

## 7. 远程与提交

```
$ git remote -v
origin  git@github.com:rua111555/TraitPilot.git (fetch)
origin  git@github.com:rua111555/TraitPilot.git (push)

$ git ls-remote git@github.com:rua111555/TraitPilot.git HEAD
645a98c8e5fbdc7a0b364fdc05f465ae7573ed7c    ← 与本地 HEAD 同一 commit，指向同一仓库
```

旧 URL `WGS_Agent.git` 会重定向到新仓库，但已改为直连新 URL。

**未做**：本地目录名 `/home/chenyuxi/skill/agent` 保持原样，未改名（不在「仓库内容」范围，且会影响 `~/.claude` 下的既有会话记录路径）。

### 7.1 从远程克隆的副本实测（推送后复核）

推送后从**新远程**重新克隆一份到 `/tmp/rename/servercheck/repo`，在副本上复核 —— 判据是「查真正推上去的东西」，不是查本地工作区：

| 检查 | 结果 |
|---|---|
| 克隆 HEAD | `a35fd95`，与本地 HEAD 同一 commit |
| 入库文件数 | 109 |
| 旧名残留（排除本文档） | `WGS_Agent` 0、`SkillAgent` 0、`skillagent` 0 ✅ |
| 新名落地 | `TraitPilot` 66 处、`traitpilot-` 18 处 ✅ |
| 保留层（排除本文档后） | `skill_agent` **440**、`SKILL_AGENT_` **174**、`WGS` 领域词 **58** —— 与基线逐字相同 ✅ |
| 副本可直接跑 | `run.py --help` → `usage: traitpilot ...`；`run.py list` 正常列出技能 ✅ |

**计数差量归因**（克隆副本初次计数为 452 / 181 / 75，高于基线，必须查清而非想当然）：
差量 +12 / +7 / +17，与克隆时本文件（`docs/rename-to-traitpilot.md`）内的引用数 **12 / 7 / 17 精确相等** —— 增长 100% 来自本记录文档自身，保留层零误伤；排除本文件后即回到基线值。

> **自指说明**：上述克隆取的是 commit `a35fd95`，当时本文件尚无 §7.1 这一节。补写本节后本文件自身的引用数已增至 13 / 8 / 19，因此**用当前版本重数会高于 12 / 7 / 17，属预期而非漂移**；要复现原始差量，请对 `a35fd95` 的版本计数。

---

## 8. 复核命令（可照此重跑）

```bash
cd /home/chenyuxi/skill/agent

# ① 旧名是否绝迹（应全为 0；必须排除本文档自身——它以引用形式存档旧名，见 §0 判据例外）
git grep -I -o -e "WGS_Agent" -e "SkillAgent" -e "skillagent" -- . ':!docs/rename-to-traitpilot.md' | wc -l

# ② 保留层是否未动（应 440 / 174 / 58）
git grep -I -o -e "skill_agent" | wc -l
git grep -I -o -e "SKILL_AGENT_" | wc -l
git grep -I -o -e "WGS" | grep -v WGS_Agent | wc -l

# ③ 运行时证据（9/9 PASS）
/home/chenyuxi/anaconda3/envs/Agent/bin/python -I /tmp/rename/probe_runtime.py

# ④ 回归
/home/chenyuxi/anaconda3/bin/python -m pytest -q                 # 196 passed
/home/chenyuxi/anaconda3/bin/python tests/ui_e2e.py              # FAILED: 0（其余 3 个同）

# ⑤ 逐文件 diff 审计（改前备份）
diff -r /tmp/rename/backup_pre . 2>/dev/null | grep -E "^[<>]" | head
```
