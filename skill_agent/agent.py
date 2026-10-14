"""Agent 主循环：system+tools → 模型 → 执行 tool_use → 续推 → 直到产出最终回答。

- active-skill 白名单（skill 的 allowed-tools 收窄当轮工具集）
- 长历史按消息边界安全压缩（不拆 tool_use/tool_result 配对）
- 最大轮次保护
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from .client import AnthropicClient
from .config import Config
from .skills import SkillRegistry
from .stops import StopAction, classify, make_truncation_result, text_of
from .tools import ToolSet, ToolResult
from .tokens import messages_tokens

MAX_TURNS = 60
COMPACT_MIN_MESSAGES = 40
COMPACT_MAX_CHARS = 600_000
COMPACT_KEEP_RECENT = 12        # M12：摘要式压缩时原样保留的最近消息条数
MAX_RESULT_CHARS = 100_000


def _system_prompt(cfg: Config, listing: str,
                   core_memory: str = "", recall: str = "") -> str:
    base = f"""你是 SkillAgent —— 一个自主运行的编程 / 生信分析智能体，运行在用户的 Linux 机器上。
你通过 Anthropic Messages API 的 tool_use 循环工作，并像 Claude Code 那样按需加载
`.claude/skills` 下的技能指令来完成任务。模型：{cfg.model}；工作目录：{cfg.cwd}。

## 工作守则
1. 任务命中某个 skill 的「用途」时，先调用 `Skill(name)` 加载并阅读其完整指令，再按其要求执行
   （解析输入、生成脚本、运行命令、产出文件）。自动可调=✓ 的才能主动调用；否则需用户显式要求。
2. 编排类 skill（`wgs-expressive-pipeline`、`gwas-pipeline`）会指示你继续调用对应的子 skill，
   逐阶段推进；你随时可按需再 `Skill(...)` 取回子 skill 的实现细节。
3. **重要不变量**：编排管线（WGS / GWAS）每阶段必须「先生成脚本 → 征得用户同意 → 再执行」。
   交互模式下用简短文字把要执行的脚本/命令展示出来，然后停下来等用户回答。后台长任务用
   Bash background=true（nohup + .done/.pid）启动，随后周期性检查日志，不要阻塞干等。
4. 优先使用专属工具而不是 shell：cat→Read、echo 写文件→Write、sed 替换→Edit；复杂文本仍可用 Bash。
5. 命令输出可能被截断；要更多内容就用 Read/tail 分片读。
6. 绝不编造文件内容、命令输出或分析结果，一律以真实执行为准；操作前先探查再动手。
7. 报错流程：读日志 → 定位根因 → 最小化修复；不要盲目重跑。管道类 skill 要求在各自
   SKILL.md 的「错误预防清单」中记录新错误。

## 当前可用的 Skill（按需加载，勿一次性全读）
{listing}

## 工具
可用工具体现在当轮请求的 tools 定义中；某个 skill 被激活后，工具集按其 allowed-tools 收窄
（`Skill` / `TaskOutput` 始终可用）。工具失败时返回 is_error=true 的可见解释，据此修正。

## 输出
所有回答用 GitHub Flavored Markdown；要点化、精简；引用文件用 `路径:行号` 形式。
"""
    # M12：core memory 为稳定前缀（利于 prompt caching）；recall 命中放在尾部（动态）
    if core_memory:
        base += f"\n## 长期记忆（core，可经 Memory 工具编辑）\n{core_memory}\n"
    if recall:
        base += f"\n{recall}\n"
    return base


class Agent:
    def __init__(self, cfg: Config, registry: SkillRegistry, tools: ToolSet):
        self.cfg = cfg
        self.registry = registry
        self.tools = tools
        self.client = AnthropicClient(
            base_url=cfg.base_url, api_key=cfg.api_key,
            model=cfg.model, max_tokens=cfg.max_tokens, verbose=cfg.verbose,
        )
        self.history: list[dict] = []
        self._active_skills: set[str] = set()   # 本轮 Skill 调用（决定下轮白名单）
        self._compacted = False
        # 网页模式注入：事件回调 + 取消信号（CLI 不设，行为不变）
        self.on_event = None
        self.cancel_event = None
        # M1a：本轮运行的 local 用量累加器（绝不写入共享 Config，避免多 Web 会话串扰）
        self._usage: dict = _new_usage()
        # M12：上下文压缩 / 分层记忆（默认关闭 → 行为与改造前一致）
        self.summarize = None            # 由入口注入：callable(prompt) -> str | None
        self.summary_compressor = None   # SummaryCompressor 实例
        self.memory = None               # MemoryStore 实例
        self._prev_summary = ""          # 滚动摘要
        # M13：会话持久化（默认 None → 纯内存，行为不变）
        self.store = None
        self.session_id = None
        # M2：最近一次终止决策（供 CLI/Web 透出原因）
        self._last_stop = None
        # M3：死循环/无进展熔断（默认 None → 不启用，行为不变）
        self.loopguard = None
        self._guard_halted = False
        # M7：同轮多个只读工具并行执行（默认关 → 与改造前一致的串行顺序）
        self.parallel_tools = bool(getattr(cfg, "parallel_tools", False))

    # ---------------- 对外 ----------------
    @property
    def active_skills(self):
        return sorted(self._active_skills)

    @property
    def usage_bar(self) -> dict:
        """当前累计用量的只读快照（M1a，供 CLI /cost 与 Web /api/usage）。"""
        return dict(self._usage)

    def reset_skills(self) -> None:
        self._active_skills.clear()

    def clear(self) -> None:
        self.history = []
        self.reset_skills()
        self._compacted = False
        self._usage = _new_usage()

    def restore(self, messages: list[dict]) -> int:
        """M13：把已校验的历史灌回内存（供 resume）。返回恢复的消息条数。"""
        self.history = [m for m in messages if isinstance(m, dict) and m.get("role")]
        self._compacted = False
        self._active_skills.clear()
        return len(self.history)

    def answer(self, user_text: str, on_delta=None) -> str:
        """执行一个用户请求（可能多轮工具调用），返回最终文本。"""
        self._usage = _new_usage(self.cfg.model)   # 每轮用户请求重置用量累计
        self._last_stop = None
        self.history.append({"role": "user", "content": user_text})
        self._persist({"role": "user", "content": user_text})
        try:
            return self._run(on_delta=on_delta)
        finally:
            self._active_skills.clear()   # 新的一轮用户请求，白名单复位

    def _run(self, on_delta=None) -> str:
        budget = self._init_budget()
        self._guard_halted = False
        if self.loopguard is not None:
            self.loopguard.reset()
        for step in range(budget["max_turns"]):
            if self.cancel_event is not None and self.cancel_event.is_set():
                return "（任务已取消）"
            # M1b：发请求前的预算熔断（绝不在工具执行中途硬切）
            stop = self._budget_gate(budget, step)
            if stop:
                return stop
            self._maybe_compact()
            tools = self.tools.schemas_for(self._allowed_set())
            # M12：core memory 作稳定前缀；recall 命中放尾部（不破坏 prompt caching）
            core = self.memory.get_core() if self.memory else ""
            recall = self._recall_for_context()
            system = _system_prompt(self.cfg, self.registry.listing(), core, recall)
            if self._compacted:
                system += "\n\n（注：较早的对话内容已因长度被压缩为前情摘要，请基于当前可见内容继续。）"

            t0 = time.monotonic()
            blocks, stop_reason, out_tokens = self.client.create(
                system=system, tools=tools, messages=self.history,
                stream=self.cfg.stream, on_delta=on_delta,
            )
            self._record_usage(out_tokens, time.monotonic() - t0)
            # 去伪：忽略空的 thinking 块
            blocks = [b for b in blocks if b.get("type") != "empty"]

            # M2：由 StopController 决定下一步（替代原 stop_reason 白名单）
            decision = classify(stop_reason, blocks)
            self._last_stop = decision
            if decision.warn:
                self._notify("notice", f"（协议提示：{decision.warn}，stop_reason={stop_reason}）")

            # 追加 assistant；若上一条已是 assistant（仅 pause_turn 续推会出现）则**合并**，
            # 避免制造连续两条同 role 消息
            if self.history and self.history[-1].get("role") == "assistant":
                self.history[-1]["content"] = list(self.history[-1].get("content") or []) + blocks
                self._persist_merge(blocks)
            else:
                assistant = {"role": "assistant", "content": blocks}
                self.history.append(assistant)
                self._persist(assistant)

            if decision.action == StopAction.CONTINUE_TOOLS:
                tool_blocks = [b for b in blocks if b.get("type") == "tool_use"]
                results = self._execute_tools(tool_blocks)
                if not results:
                    # 防御：模型声明 tool_use 但没有可执行块 → 终止防死循环
                    break
                msg = {"role": "user", "content": results}
                self.history.append(msg)
                self._persist(msg)
                if self._guard_halted:
                    return "（已熔断：检测到无进展循环，已停止。）"
                continue

            if decision.action == StopAction.REPAIR_TRUNCATION:
                # 为本轮所有被截断的 tool_use 合成配对 tool_result，让模型重试
                results = [make_truncation_result(b.get("id", ""), decision.user_message)
                           for b in blocks if b.get("type") == "tool_use"]
                if results:
                    msg = {"role": "user", "content": results}
                    self.history.append(msg)
                    self._persist(msg)
                    self._notify("notice", decision.user_message)
                    continue
                return decision.user_message or "（工具参数被截断，未能继续）"

            if decision.action == StopAction.RESUME_SERVER_TOOL:
                # pause_turn：原样回传 assistant，**不追加 user**，直接再请求
                continue

            if decision.action == StopAction.REFUSE:
                return decision.user_message or "（模型拒绝继续）"

            # FINALIZE / ABORT
            text = text_of(blocks)
            if decision.truncated and decision.user_message:
                text = (text + "\n\n" + decision.user_message).strip()
            return text or "（模型未返回文本内容）"
        return "（达到最大工具轮次，已停止）"

    # ---------------- 预算（M1b） ----------------
    def _init_budget(self) -> dict:
        cfg = self.cfg
        return {
            "max_turns": int(getattr(cfg, "max_turns", None) or MAX_TURNS),
            "max_output_tokens": getattr(cfg, "max_output_tokens", None) or 0,
            "max_seconds": getattr(cfg, "max_seconds", None) or 0,
            "warn_ratio": float(getattr(cfg, "warn_ratio", 0.8) or 0.8),
            "t0": time.monotonic(),
            "warned": False,
        }

    def _budget_gate(self, b: dict, step: int) -> str | None:
        """返回终止文案（已到上限）或 None。只在发请求前调用。"""
        u = self._usage
        # 1) 输出 token 上限
        if b["max_output_tokens"] and u["output_tokens"] >= b["max_output_tokens"]:
            return self._budget_stop(b, f"已达输出 token 上限 {b['max_output_tokens']}（累计 {u['output_tokens']}）")
        # 2) 墙钟时长上限
        if b["max_seconds"]:
            elapsed = time.monotonic() - b["t0"]
            if elapsed >= b["max_seconds"]:
                return self._budget_stop(b, f"已达时长上限 {b['max_seconds']}s（实际 {elapsed:.0f}s）")
        # 3) 预警（只提醒一次）
        if not b["warned"]:
            msg = ""
            if b["max_output_tokens"] and u["output_tokens"] >= b["max_output_tokens"] * b["warn_ratio"]:
                msg = f"输出 token 已用 {u['output_tokens']}/{b['max_output_tokens']}（接近上限）"
            elif b["max_seconds"] and (time.monotonic() - b["t0"]) >= b["max_seconds"] * b["warn_ratio"]:
                msg = f"时长已用 {time.monotonic() - b['t0']:.0f}s/{b['max_seconds']}s（接近上限）"
            if msg:
                b["warned"] = True
                self._notify("notice", f"预算提醒：{msg}")
        return None

    def _budget_stop(self, b: dict, why: str) -> str:
        # 关键：不杀后台进程，仅停止继续轮询/调用
        self._notify("notice", f"预算熔断：{why}。后台任务（若有）仍在运行，可用 TaskOutput 查看。")
        return f"（预算熔断：{why}。已停止继续调用；后台任务不受影响。）"

    # ---------------- 事件通知（M2 termination surface） ----------------
    def _notify(self, kind: str, message: str) -> None:
        if self.on_event is not None and message:
            self.on_event({"type": kind, "message": message})

    # ---------------- 分层记忆（M12） ----------------
    def _recall_for_context(self, limit: int = 5) -> str:
        """按最新 user 文本检索 archival 记忆，渲染为可注入尾部的块。"""
        if not self.memory:
            return ""
        query = ""
        for m in reversed(self.history):
            if m.get("role") == "user" and isinstance(m.get("content"), str):
                query = m["content"]
                break
        return self.memory.render_context(query, limit) if query else ""

    def _persist(self, message: dict) -> None:
        """M13：把每条消息追加落盘（store 为 None 时为空操作）。"""
        if self.store is None or not self.session_id:
            return
        try:
            self.store.append(self.session_id, message)
        except Exception:
            pass

    def _persist_merge(self, blocks: list) -> None:
        """pause_turn 合并续推：追加一个「并入上一条 assistant」的块增量。"""
        if self.store is None or not self.session_id:
            return
        try:
            self.store.append(self.session_id, {"_merge": True, "content": blocks})
        except Exception:
            pass

    # ---------------- 用量采集（M1a） ----------------
    def _record_usage(self, out_tokens: int, latency_s: float) -> None:
        """累计本轮用量并 emit usage 事件。纯被动累加，不做任何限流/终止判定。"""
        if not isinstance(out_tokens, int) or out_tokens < 0:
            out_tokens = 0
        u = self._usage
        u["steps"] += 1
        u["output_tokens"] += out_tokens
        u["latency_ms"] += int(latency_s * 1000)
        if self.on_event is not None:
            self.on_event({"type": "usage", "step": u["steps"],
                           "output_tokens": out_tokens, "total_output_tokens": u["output_tokens"],
                           "latency_ms": int(latency_s * 1000)})

    # ---------------- 工具执行 ----------------
    def _allowed_set(self) -> set[str] | None:
        if not self._active_skills:
            return None
        union: set[str] = set()
        unbound = False
        for name in self._active_skills:
            s = self.registry.get(name)
            if s is None:
                continue
            if not s.allowed_tools:
                unbound = True
            union |= set(s.allowed_tools)
        return None if unbound else union

    def _execute_tools(self, blocks: list[dict]) -> list[dict]:
        # M7：同轮多个无副作用工具（Read/Grep/Glob/TaskOutput）并行执行；其余串行。
        outcomes: dict[str, dict] = {}
        para, serial = [], []
        for b in blocks:
            (para if (self.parallel_tools and _is_readonly(b.get("name", ""))) else serial).append(b)
        if para:
            outcomes.update(self._exec_parallel(para))
        for b in serial:
            outcomes[b.get("id", "")] = self._exec_one(b, allow_parallel_skip=True)
        return [outcomes.get(b.get("id", ""), self._tool_result_block(
            b.get("id", ""), ToolResult("（内部错误：无结果）", True), b.get("name", "")))
            for b in blocks]

    # ---------------- 单个工具执行（串行/并行共用） ----------------
    def _exec_one(self, b: dict, allow_parallel_skip: bool = False) -> dict:
        name = b.get("name", "")
        aid = b.get("id", "")
        args = b.get("input") or {}
        if not isinstance(args, dict):
            args = {}
        if self.on_event is not None:
            self.on_event({"type": "tool_start", "id": aid, "name": name,
                           "input": json.dumps(args, ensure_ascii=False)[:2000]})
        # M3：若已熔断，跳过执行并返回可见原因
        if self.loopguard is not None and self._guard_halted:
            tr = ToolResult("[已熔断] 检测到无进展循环，本工具调用未执行。", True)
            return self._tool_result_block(aid, tr, name)
        tr = self.tools.execute(name, args)
        if self.on_event is not None:
            content = tr.content
            if len(content) > 4000:
                content = content[:4000] + "\n…（过长已截断）"
            self.on_event({"type": "tool_end", "id": aid, "name": name,
                           "is_error": tr.is_error, "result": content})
        # 副作用：激活 skill 与死循环判定只在**串行**路径做（避免并发下竞态）
        guard_msg = ""
        if allow_parallel_skip:
            if name == "Skill" and not tr.is_error:
                skill = str(args.get("skill", "")).strip()
                if skill:
                    self._active_skills.add(skill)
            guard_msg = self._guard_record(name, args, tr)
        if self.cfg.verbose:
            tag = "ok" if not tr.is_error else "ERR"
            print(f"\n[工具] {name} [{tag}] -> {tr.content.splitlines()[0][:80] if tr.content else ''}",
                  flush=True)
        return self._tool_result_block(aid, tr, name, guard_msg)

    def _exec_parallel(self, blocks: list[dict]) -> dict:
        """并行执行一组**只读**工具。只读工具无共享状态，故不做 guard 记录、不激活 skill。"""
        from concurrent.futures import ThreadPoolExecutor, as_completed
        outcomes: dict[str, dict] = {}
        with ThreadPoolExecutor(max_workers=min(len(blocks), 8)) as ex:
            futs = {ex.submit(self._exec_one, b, False): b for b in blocks}
            for f in as_completed(futs):
                b = futs[f]
                try:
                    outcomes[b.get("id", "")] = f.result()
                except Exception as e:  # noqa: BLE001
                    outcomes[b.get("id", "")] = self._tool_result_block(
                        b.get("id", ""), ToolResult(f"[并行执行异常] {e}", True), b.get("name", ""))
        return outcomes

    def _tool_result_block(self, aid: str, tr, name: str, guard_msg: str = "") -> dict:
        content = tr.content
        if len(content) > MAX_RESULT_CHARS:
            content = content[:MAX_RESULT_CHARS] + "\n…[结果过长已截断]"
        if guard_msg:                        # 纠偏提示并入同一轮 tool_result（严禁另起 user）
            content = (content + "\n\n" + guard_msg).strip()
        return {"type": "tool_result", "tool_use_id": aid,
                "content": content, "is_error": tr.is_error}

    # ---------------- 死循环熔断（M3） ----------------
    def _guard_record(self, name: str, args: dict, tr) -> str:
        if self.loopguard is None or not self.loopguard.enabled:
            return ""
        meta = getattr(tr, "meta", None)
        v = self.loopguard.record(name, args, tr.content, meta)
        if v.level == "halt":
            self._guard_halted = True
            self._notify("loop_guard", v.message)
            return v.message
        if v.level == "warn":
            self._notify("loop_guard", v.message)
            return v.message
        return ""

    # ---------------- 上下文压缩 ----------------
    def _maybe_compact(self) -> None:
        h = self.history
        if not h:
            return
        budget_hit = sum(_msg_bytes(m) for m in h) > COMPACT_MAX_CHARS
        over_msgs = len(h) > COMPACT_MIN_MESSAGES
        # M12：可选摘要式压缩（默认关；未注入 summarize/compressor 时等价于旧行为）
        if (over_msgs or budget_hit) and self.summary_compressor is not None \
                and self.summarize is not None:
            if self._summary_compact():
                return
        if over_msgs or budget_hit:
            cut = self._safe_cut(len(h) - COMPACT_MIN_MESSAGES) if over_msgs else 0
            if budget_hit and cut > 0:
                # 还不够就多裁一段
                cut = self._safe_cut(cut)
            if cut > 0:
                self.history = h[cut:]
                self._compacted = True

    def _summary_compact(self) -> bool:
        """摘要式压缩：保留最近 K 条，把更早区间压成一条结构化摘要。成功返回 True。"""
        h = self.history
        target = len(h) - COMPACT_KEEP_RECENT
        if target <= 0:
            return False
        cut = self._safe_cut(target)
        if cut <= 0:
            return False
        early, recent = h[:cut], h[cut:]
        # 必须落在合法边界：cut 处应为 user，且前一条不是带 tool_use 的 assistant
        if recent and recent[0].get("role") == "user" and not _has_tool_use(early[-1]):
            prompt = self.summary_compressor.render_prompt(early, self._prev_summary)
            try:
                summary = self.summarize(prompt)
            except Exception:
                summary = None
            if summary and summary.strip():
                self._prev_summary = summary.strip()
                # 把摘要并入最近窗口的首条 user 消息，避免制造连续两条 user
                recent = list(recent)
                recent[0] = self.summary_compressor.prepend_to_message(
                    recent[0], self.summary_compressor.prefix_text(summary))
                self.history = recent
                self._compacted = True
                self._prev_summary = summary.strip()
                self._persist_prefix(recent[0])
                self._remember_phase_facts(summary)
                return True
        return False   # 任何不满足 → 回退旧逻辑

    def _persist_prefix(self, message: dict) -> None:
        """把压缩后的边界写进持久化日志（供 resume 时不重复压缩）。"""
        if self.store is None or not self.session_id:
            return
        try:
            self.store.append(self.session_id, {"_prefix": True, "message": message})
        except Exception:
            pass

    def _remember_phase_facts(self, summary: str) -> None:
        """把摘要中携带的关键事实（路径/pid/.done 等）抽进 archival 记忆。"""
        if not self.memory:
            return
        import re as _re
        for pat, kind in (
            (r"(/[\w./\-]+\.(?:done|pid))", "marker"),
            (r"\b(task_id|pid)\s*[=:]\s*\S+", "task"),
            (r"(/[\w./\-]+(?:\.vcf\.gz|\.bed|\.bim|\.fam|\.grm|\.log|\.csv))", "path"),
        ):
            for m in _re.findall(pat, summary)[:20]:
                self.memory.remember(m if isinstance(m, str) else str(m),
                                     kind=kind, source="summary")

    def _safe_cut(self, start: int) -> int:
        """start 起向后找到第一个「合法消息边界」：role=user，且前一条 assistant 无 tool_use。"""
        h = self.history
        i = max(0, start)
        while i < len(h):
            m = h[i]
            prev = h[i - 1] if i > 0 else None
            if m.get("role") == "user":
                if prev is None or not _has_tool_use(prev):
                    return i
            i += 1
        return start


# M7：同轮可安全并行的只读工具（无副作用、无共享状态）
_READONLY_TOOLS = frozenset({"Read", "Glob", "Grep", "TaskOutput"})


def _is_readonly(name: str) -> bool:
    return name in _READONLY_TOOLS


def _new_usage(model: str = "") -> dict:
    """M1a：本轮运行的用量累加器（local，绝不挂共享 Config）。"""
    return {"model": model, "steps": 0, "output_tokens": 0, "latency_ms": 0}


def _has_tool_use(msg: dict) -> bool:
    content = msg.get("content")
    return isinstance(content, list) and any(
        b.get("type") == "tool_use" for b in content if isinstance(b, dict))


def _msg_bytes(m: dict) -> int:
    try:
        return len(json.dumps(m, ensure_ascii=False))
    except Exception:
        return 0
