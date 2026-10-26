"""Agent 主循环：system+tools → 模型 → 执行 tool_use → 续推 → 直到产出最终回答。

- active-skill 白名单（skill 的 allowed-tools 收窄当轮工具集）
- 长历史按消息边界安全压缩（不拆 tool_use/tool_result 配对）
- 最大轮次保护
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from .client import AnthropicClient, APIError
from .config import Config
from .skills import SkillRegistry
from .stops import StopAction, classify, make_truncation_result, text_of
from .tools import ToolSet, ToolResult
from .tokens import messages_tokens

MAX_TURNS = 60
COMPACT_MIN_MESSAGES = 40
COMPACT_MAX_CHARS = 600_000
# 真实上下文用量达到该比例即压缩（优先于字符/条数判据）。
#
# 中转的上下文上限实测 286,720 token（只以 400 错误形式暴露，不在 /v1/models 里）。
# 定 0.70 而非贴着上限：压缩本身要一次额外请求（摘要式还要多一次 summarize），
# 且压缩后仍需留足空间给这一轮余下的工具往返。按 286,720 算 ≈ 200k token。
# 字符判据（COMPACT_MAX_CHARS）保留为**兜底**：真实用量要等第一次响应回来才知道，
# 会话开头或客户端拿不到 usage 时它仍然生效。
COMPACT_AT_CONTEXT_RATIO = 0.70
# 无法从配置读到上下文上限时的兜底值。实测中转为 286,720 token。
CONTEXT_LIMIT_FALLBACK = 286_720
# 弹性轮次：到达 max_turns 后若上下文仍有余量，自动延长几段（每段 = max_turns）。
# 默认 3 段 → 默认上限从 60 提到最坏 240 轮。设 0 可恢复「到点即停」的旧行为。
# 为什么仍要有界：无进展循环由 loopguard 负责，这里只解决「正常任务被轮次切死」。
MAX_TURN_EXTENSIONS = 3
# 压力路径下按字节从尾部保留的窗口上限（≈ 上限 70% 的一半，给摘要与工具往返留余量）
COMPACT_KEEP_BYTES = 200_000
COMPACT_KEEP_RECENT = 12        # M12：摘要式压缩时原样保留的最近消息条数
MAX_RESULT_CHARS = 100_000


def _system_prompt(cfg: Config, listing: str,
                   core_memory: str = "", recall: str = "") -> str:
    base = f"""你是 TraitPilot —— 一个自主运行的编程 / 生信分析智能体，运行在用户的 Linux 机器上。
你通过 Anthropic Messages API 的 tool_use 循环工作，并按需加载 `skills/` 下的技能指令来完成任务。
模型：{cfg.model}；工作目录：{cfg.cwd}。

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
            prompt_cache=bool(getattr(cfg, "prompt_cache", False)),
            fallback_models=list(getattr(cfg, "fallback_models", None) or []),
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

    def answer(self, user_text: str, on_delta=None, on_thinking=None) -> str:
        """执行一个用户请求（可能多轮工具调用），返回最终文本。"""
        self._usage = _new_usage(self.cfg.model)   # 每轮用户请求重置用量累计
        self._last_stop = None
        # M11：幂等去重按「一次用户请求」为界（run 级），跨请求不残留
        guard = getattr(self.tools, "idempotency", None)
        if guard is not None:
            guard.reset()
        self.history.append({"role": "user", "content": user_text})
        self._persist({"role": "user", "content": user_text})
        try:
            return self._run(on_delta=on_delta, on_thinking=on_thinking)
        finally:
            self._active_skills.clear()   # 新的一轮用户请求，白名单复位

    def _run(self, on_delta=None, on_thinking=None) -> str:
        budget = self._init_budget()
        self._guard_halted = False
        if self.loopguard is not None:
            self.loopguard.reset()
        # 用 while True + 顶部判上限，而不是 for range(max_turns)：轮次上限可以被
        # **延长**（见下面的弹性续跑），for range 的边界在进入时就固定了，改不动。
        # 也把步数递增放在顶部，这样体内所有 `continue` 都不必各自维护计数。
        step = 0
        while True:
            if step >= budget["next_cap"]:
                # 轮次用尽：**先看上下文还有没有余量**再决定停不停。
                #
                # 用户诉求（逐字）：「我不想太固定最大轮次，而是每次快达到上下文上限的
                # 时候，进行上下文的压缩，然后继续这个对话框」。真正的约束应该是
                # **上下文**而不是轮次计数：上下文还有余量时，再给一段轮次继续跑。
                # 延长次数**有界**（max_extensions），所以最坏情况仍是有限轮次 ——
                # 既满足「别太固定」，又不会真的跑飞（无进展循环另有 loopguard 管）。
                if budget["extensions"] < budget["max_extensions"]:
                    budget["extensions"] += 1
                    budget["next_cap"] += budget["per_extension"]
                    self._notify(
                        "notice",
                        f"已达轮次上限 {step}，上下文仍有余量 → 自动延长轮次预算"
                        f"（第 {budget['extensions']}/{budget['max_extensions']} 次延长，"
                        f"本次上限 {budget['next_cap']} 轮）")
                else:
                    extra = (f"；已自动延长 {budget['extensions']} 次"
                             if budget["extensions"] else "")
                    return f"（达到最大工具轮次 {budget['next_cap']}，已停止{extra}）"
            step += 1
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
            try:
                blocks, stop_reason, out_tokens = self.client.create(
                    system=system, tools=tools, messages=self.history,
                    stream=self.cfg.stream, on_delta=on_delta, on_thinking=on_thinking,
                )
            except APIError as e:
                # 上下文溢出是 400 → client 判为**不可重试**，会直接终止整轮。
                # 但这里恰好是最该继续、而不是放弃的地方：历史是本地可裁的，
                # 裁掉旧内容再发一次就能继续（用户诉求「继续这个对话框」）。
                # 只在真正识别出「上下文超长」时才走这条路，其余 400 照旧抛出 ——
                # 不能把参数错误之类的真错误也吞成「再试一次」。
                if not _is_context_overflow(e):
                    raise
                if not self._force_compact_for_retry():
                    raise                     # 裁不动（内容不可再分）→ 如实报错
                self._notify("notice",
                             "上下文超出模型上限，已压缩较早内容后自动重试。")
                t0 = time.monotonic()
                blocks, stop_reason, out_tokens = self.client.create(
                    system=system, tools=tools, messages=self.history,
                    stream=self.cfg.stream, on_delta=on_delta, on_thinking=on_thinking,
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

    # ---------------- 预算（M1b） ----------------
    def _init_budget(self) -> dict:
        cfg = self.cfg
        per_turn = int(getattr(cfg, "max_turns", None) or MAX_TURNS)
        return {
            "max_turns": per_turn,
            # 弹性轮次：next_cap 是当前允许的累计上限，每次延长加一段 per_extension。
            "next_cap": per_turn,
            "per_extension": per_turn,
            "extensions": 0,
            "max_extensions": max(0, _as_int(getattr(cfg, "max_turn_extensions", None),
                                             MAX_TURN_EXTENSIONS)),
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
        # 真实用量判据（首选）：上一次请求的 input token 已达上限的
        # COMPACT_AT_CONTEXT_RATIO。取真实值而非字符估算 —— 估算在同一条会话上
        # 低估 1.55 倍、在合成 prompt 上又高估 1.45 倍，方向不稳定。
        ctx_hit = self._context_pressure()
        # M12：可选摘要式压缩（默认关；未注入 summarize/compressor 时等价于旧行为）
        if (over_msgs or budget_hit or ctx_hit) and self.summary_compressor is not None \
                and self.summarize is not None:
            if self._summary_compact():
                return
        if over_msgs or budget_hit or ctx_hit:
            cut = self._safe_cut(len(h) - COMPACT_MIN_MESSAGES) if over_msgs else -1
            if cut < 0 and (budget_hit or ctx_hit):
                # 条数没超但**上下文压力/字节压力**到了：不能干等 —— 按**字节预算**
                # 从尾部往前留一段，而不是按条数。生信会话常见「条数少、单条极大」
                # （一条 VCF 或日志输出几十万字符），此时 len(h) 远小于
                # COMPACT_MIN_MESSAGES，任何按条数算的裁点都是 0，压缩形同不存在 ——
                # 而恰恰是这类会话最容易撞上下文上限（实测：12 条、用量已到阈值，
                # 按条数裁不动）。
                cut = self._cut_by_bytes()
            if budget_hit and cut > 0:
                # 还不够就多裁一段（向后找不到合法边界就维持原切点，别用 -1 覆盖掉）
                cut = max(cut, self._safe_cut(cut))
            if cut > 0:
                self.history = h[cut:]
                self._compacted = True
                # 关键：丢弃分支也必须落锚点，否则压缩只活在内存里 ——
                # 磁盘上旧消息仍逐条留着，resume 会**原样读回压缩前的历史**，
                # 压缩被静默撤销（实测：内存 42 条 / resume 回 102 条）。
                self._persist_prefix(self.history)

    def _cut_by_bytes(self, keep_bytes: int = COMPACT_KEEP_BYTES) -> int:
        """按**字节预算**从尾部往前留一段，返回合法切点（0 = 不该裁）。

        与 ``_safe_cut`` 的分工：``_safe_cut`` 只保证「切点合法」（落在 user 且前一条
        assistant 不带 tool_use），不管切在哪里；本方法决定**切在哪**，用于
        「条数没超但上下文压力已到」的路径。

        为什么按字节而不是按条数：生信会话常见「条数少、单条极大」（一条 VCF 或
        日志几十万字符）。按条数留 KEEP_RECENT 条会算出 len(h)-12 ≤ 0 → 裁不动，
        而这类会话恰恰最容易撞上下文上限。

        找不到合法切点时返回 0（不裁）——宁可这一轮不动，也不制造不合法的历史边界。
        **绝不返回 len(h)**：那会让 ``self.history = h[cut:]`` 把历史整段清空
        （消息本身不可再切分时，只能保留它，不能连它一起丢）。
        """
        n = len(self.history)
        total = 0
        idx = n
        for i in range(n - 1, -1, -1):
            total += _msg_bytes(self.history[i])
            if total > keep_bytes:
                idx = i + 1
                break
            idx = i
        # 单条消息本身就超过预算 → 上面的循环会给出 idx=n（保留 0 条）。
        # 必须退到 n-1：那一条虽然大，也总比清空历史强（清了就等于丢失本轮全部上下文）。
        idx = min(idx, n - 1)
        if idx <= 0:
            return 0                       # 整段都在预算内 → 无需裁
        cut = self._safe_cut(idx)
        if cut < 0:
            # 预算线之后（含末尾）没有合法边界：向前兜底，保留稍多一点。
            # 不能返回 0 就了事 —— 那正是「条数少、单条极大」会话裁不动的原因。
            cut = self._cut_before(idx)
        if cut <= 0:
            return 0
        return cut

    def _force_compact_for_retry(self) -> bool:
        """上下文溢出后的**强制**压缩（比常规压缩更狠）。成功返回 True。

        与 ``_maybe_compact`` 的区别：这里已经撞了 400，没有「再等等」的余地 ——
        必须真正腾出空间，否则重试必然再撞一次。
        因此依次加深：按字节预算裁 → 仍不够就直接裁到最近 KEEP_RECENT 条。
        """
        h = self.history
        if len(h) <= 2:
            return False                  # 已无可裁的余量
        before = len(h)
        cut = self._cut_by_bytes(COMPACT_KEEP_BYTES // 2)
        if cut <= 0:
            cut = self._cut_before(len(h) - COMPACT_KEEP_RECENT)
        if cut <= 0:
            # 最后一档：只要还存在合法边界就按它裁（哪怕只剩很少）
            cut = self._safe_cut(len(h) - 2)
            if cut < 0:
                cut = self._cut_before(len(h) - 2)
        if cut <= 0:
            return False
        self.history = h[cut:]
        self._compacted = True
        self._persist_prefix(self.history)
        return len(self.history) < before

    def _context_pressure(self) -> bool:
        """上一次请求的真实上下文用量是否已达压缩阈值。

        没有可用用量时返回 False（回退到字符/条数判据）——绝不因为「量不到」
        就误判为「已满」，那会在会话开头就把历史裁掉。
        """
        client = getattr(self, "client", None)
        used = int(getattr(client, "last_input_tokens", 0) or 0)
        if used <= 0:
            return False
        limit = _as_int(getattr(self.cfg, "context_limit", None),
                        CONTEXT_LIMIT_FALLBACK)
        if limit <= 0:
            return False
        return used >= limit * COMPACT_AT_CONTEXT_RATIO

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
                self._persist_prefix(recent)
                self._remember_phase_facts(summary)
                return True
        return False   # 任何不满足 → 回退旧逻辑

    def _persist_prefix(self, messages) -> None:
        """把压缩后的**整个保留窗口**写成一条 ``_prefix`` 锚点（供 resume 时不重复压缩）。

        参数是**列表**（整个窗口），不是单条：``_prefix`` 在读侧是覆盖点
        （``raw = [...]``），其之前落盘的行全部作废。保留窗口里的消息恰恰是在
        锚点之前逐条落盘的，只传边界那一条会把窗口其余部分一起丢掉
        （详见 ``SessionStore.append_prefix``）。传单条时自动包成单元素列表，
        兼容老调用方。
        """
        if self.store is None or not self.session_id:
            return
        msgs = messages if isinstance(messages, list) else [messages]
        if not msgs:
            return
        try:
            self.store.append_prefix(self.session_id, msgs)
        except Exception:
            pass

    def _remember_phase_facts(self, summary: str) -> None:
        """把摘要中携带的关键事实（路径/pid/.done 等）抽进 archival 记忆。

        注意：``task_id``/``pid`` 这类必须捕获**整段 ``key=value``**，不能只捕获键名
        （``re.findall`` 带组时只返回组内容，会丢掉 ``12345``/``T7`` 这些值）。
        值用 ``[^\\s，。；、）】」,;]+`` 而非 ``\\S+``：中文句读不是空白字符，``\\S+`` 会把
        ``pid=12345。标记文件`` 整串吞进来，落库成畸形事实。
        """
        if not self.memory:
            return
        import re as _re
        for pat, kind in (
            (r"/[\w./\-]+\.(?:done|pid)", "marker"),
            (r"\b(?:task_id|pid)\s*[=:]\s*[^\s，。；、）】」,;]+", "task"),
            (r"/[\w./\-]+(?:\.vcf\.gz|\.bed|\.bim|\.fam|\.grm|\.log|\.csv)", "path"),
        ):
            for m in _re.findall(pat, summary)[:20]:
                if isinstance(m, str) and m.strip():
                    self.memory.remember(m.strip(), kind=kind, source="summary")

    def _safe_cut(self, start: int) -> int:
        """start 起向后找第一个「合法消息边界」：role=user，且前一条 assistant 无 tool_use。

        **找不到合法边界时返回 -1**（而不是 start）。返回 start 会让调用方执行
        ``h[start:]``，而 h[start] 此时并不是 user —— 等于留下一个「以 assistant
        开头」的畸形历史，Messages API 会直接 400。返回 -1 时调用方的 ``cut > 0``
        判定自然不成立 → 这一轮不裁（宁可不动，也不制造坏边界）。
        """
        h = self.history
        i = max(0, int(start))
        while i < len(h):
            m = h[i]
            prev = h[i - 1] if i > 0 else None
            if m.get("role") == "user":
                if prev is None or not _has_tool_use(prev):
                    return i
            i += 1
        return -1

    def _cut_before(self, idx: int) -> int:
        """在 idx 及之前找**最大**的合法切点；找不到返回 -1（向前兜底）。

        与 ``_safe_cut``（向后找）方向相反，用于「预算线之后没有合法边界」的情形：
        向后找不到就只能清空或留一条 assistant 开头的畸形历史，此时向前退一格
        保留稍多一点，远好于把上下文丢光。
        """
        h = self.history
        i = min(int(idx), len(h) - 1)
        while i >= 1:
            m = h[i]
            if m.get("role") == "user" and not _has_tool_use(h[i - 1]):
                return i
            i -= 1
        return -1


# M7：同轮可安全并行的只读工具（无副作用、无共享状态）
_READONLY_TOOLS = frozenset({"Read", "Glob", "Grep", "TaskOutput",
                             "Search", "RepoMap"})


def _is_readonly(name: str) -> bool:
    return name in _READONLY_TOOLS


def _new_usage(model: str = "") -> dict:
    """M1a：本轮运行的用量累加器（local，绝不挂共享 Config）。"""
    return {"model": model, "steps": 0, "output_tokens": 0, "latency_ms": 0}


def _has_tool_use(msg: dict) -> bool:
    content = msg.get("content")
    return isinstance(content, list) and any(
        b.get("type") == "tool_use" for b in content if isinstance(b, dict))


# 上下文溢出的错误特征。判据刻意收窄：必须是 400（不是语法错误就是参数错误）
# **且**命中明确的超长措辞。宁可不识别（照旧报错），也不能把别的 400 误吞成
# 「压缩后再试」——那会把真正的参数错误伪装成「重试也没用」。
_OVERFLOW_PATTERNS = (
    "maximum context length",
    "max_model_len",
    "max_total_tokens",
    "context length",
    "context_length_exceeded",
    "too many tokens",
    "prompt is too long",
    "reduce the length",
)


def _is_context_overflow(err: Exception) -> bool:
    """这是不是「上下文超长」这一类错误（可压缩后重试）。"""
    status = getattr(err, "status", None)
    if status is not None and int(status) != 400:
        return False
    text = str(err).lower()
    return any(p.lower() in text for p in _OVERFLOW_PATTERNS)


def _msg_bytes(m: dict) -> int:
    try:
        return len(json.dumps(m, ensure_ascii=False))
    except Exception:
        return 0


def _as_int(value, fallback: int) -> int:
    """把配置值转成 int；``None``/非法值才用 fallback，**0 是有效值**。

    不能写成 ``int(v or fallback)``：那样 0 会被当成「没设」而回落到默认值，
    于是「显式关闭」配置项根本关不掉（实测：max_turn_extensions=0 仍延长了 3 次）。
    """
    if value is None:
        return fallback
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback
