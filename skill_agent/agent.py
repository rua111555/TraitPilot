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
from .tools import ToolSet

MAX_TURNS = 60
COMPACT_MIN_MESSAGES = 40
COMPACT_MAX_CHARS = 600_000
MAX_RESULT_CHARS = 100_000


def _system_prompt(cfg: Config, listing: str) -> str:
    return f"""你是 SkillAgent —— 一个自主运行的编程 / 生信分析智能体，运行在用户的 Linux 机器上。
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

    def answer(self, user_text: str, on_delta=None) -> str:
        """执行一个用户请求（可能多轮工具调用），返回最终文本。"""
        self._usage = _new_usage(self.cfg.model)   # 每轮用户请求重置用量累计
        self.history.append({"role": "user", "content": user_text})
        try:
            return self._run(on_delta=on_delta)
        finally:
            self._active_skills.clear()   # 新的一轮用户请求，白名单复位

    def _run(self, on_delta=None) -> str:
        for step in range(MAX_TURNS):
            if self.cancel_event is not None and self.cancel_event.is_set():
                return "（任务已取消）"
            self._maybe_compact()
            tools = self.tools.schemas_for(self._allowed_set())
            system = _system_prompt(self.cfg, self.registry.listing())
            if self._compacted:
                system += "\n\n（注：较早的对话内容已因长度被安全压缩略去，请基于当前可见内容继续。）"

            t0 = time.monotonic()
            blocks, stop_reason, out_tokens = self.client.create(
                system=system, tools=tools, messages=self.history,
                stream=self.cfg.stream, on_delta=on_delta,
            )
            self._record_usage(out_tokens, time.monotonic() - t0)
            # 去伪：忽略空的 thinking 块
            blocks = [b for b in blocks if b.get("type") != "empty"]
            assistant = {"role": "assistant", "content": blocks}
            self.history.append(assistant)

            tool_blocks = [b for b in blocks if b.get("type") == "tool_use"]
            if tool_blocks and stop_reason in ("tool_use", "max_tokens", ""):
                results = self._execute_tools(tool_blocks)
                if not results:
                    # 防御：模型声明 tool_use 但没有可执行块 → 终止防死循环
                    break
                self.history.append({"role": "user", "content": results})
                continue

            # 无工具调用：返回最终回答
            text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
            return text.strip() or "（模型未返回文本内容）"
        return "（达到最大工具轮次，已停止）"

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
        results: list[dict] = []
        for b in blocks:
            name = b.get("name", "")
            aid = b.get("id", "")
            args = b.get("input") or {}
            if not isinstance(args, dict):
                args = {}
            if self.on_event is not None:
                self.on_event({"type": "tool_start", "id": aid, "name": name,
                               "input": json.dumps(args, ensure_ascii=False)[:2000]})
            tr = self.tools.execute(name, args)
            if self.on_event is not None:
                content = tr.content
                if len(content) > 4000:
                    content = content[:4000] + "\n…（过长已截断）"
                self.on_event({"type": "tool_end", "id": aid, "name": name,
                               "is_error": tr.is_error, "result": content})
            if name == "Skill" and not tr.is_error:
                skill = str(args.get("skill", "")).strip()
                if skill:
                    self._active_skills.add(skill)
            content = tr.content
            if len(content) > MAX_RESULT_CHARS:
                content = content[:MAX_RESULT_CHARS] + "\n…[结果过长已截断]"
            results.append({
                "type": "tool_result", "tool_use_id": aid,
                "content": content, "is_error": tr.is_error,
            })
            if self.cfg.verbose:
                tag = "ok" if not tr.is_error else "ERR"
                print(f"\n[工具] {name} [{tag}] -> {content.splitlines()[0][:80] if content else ''}",
                      flush=True)
        return results

    # ---------------- 上下文压缩 ----------------
    def _maybe_compact(self) -> None:
        h = self.history
        if not h:
            return
        budget_hit = sum(_msg_bytes(m) for m in h) > COMPACT_MAX_CHARS
        if len(h) > COMPACT_MIN_MESSAGES or budget_hit:
            cut = self._safe_cut(len(h) - COMPACT_MIN_MESSAGES) if len(h) > COMPACT_MIN_MESSAGES else 0
            if budget_hit and cut > 0:
                # 还不够就多裁一段
                cut = self._safe_cut(cut)
            if cut > 0:
                self.history = h[cut:]
                self._compacted = True

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
