"""工具/会话钩子系统（M10）。

在工具执行前后与会话关键节点提供可注册钩子，使审计、脱敏、合规、日志扩展不必
散落进各 executor。借鉴 Claude Code 的 hooks（PreToolUse / PostToolUse /
UserPromptSubmit）+ OpenHands 的 callback 机制。

契约：
- PreToolUse(tool_name, args) -> None | str | ToolResult
    返回 ``str`` → 视为**阻断**理由（工具不执行，回该字符串为 is_error）；
    返回 ``ToolResult`` → 直接作为结果；返回 ``None`` → 放行（可原地修改 args）。
- PostToolUse(tool_name, args, result) -> None | ToolResult
    返回 ``ToolResult`` 则替换结果（用于脱敏/改写）。
- SessionStart / SessionEnd / PreCompact(ctx) -> None（纯观测）

健壮性：**任一钩子抛异常一律吞掉并记录**，绝不拖垮工具执行或主循环。
默认为空链 → 行为与改造前完全一致。钩子可声明 ``blocking=True/False``。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable


@dataclass
class Hook:
    event: str                      # pre_tool_use | post_tool_use | session_start | session_end | pre_compact
    fn: Callable
    name: str = ""
    blocking: bool = True           # 仅对 pre_tool_use 有意义

    def __post_init__(self):
        if not self.name:
            self.name = getattr(self.fn, "__name__", "hook")


class HookManager:
    """注册与触发钩子。空实例等价于「无钩子」。"""

    EVENTS = ("pre_tool_use", "post_tool_use", "session_start", "session_end", "pre_compact")

    def __init__(self):
        self._hooks: dict[str, list[Hook]] = {e: [] for e in self.EVENTS}
        self.last_errors: list[str] = []

    # ---------------- 注册 ----------------
    def register(self, hook: Hook) -> None:
        if hook.event not in self._hooks:
            raise ValueError(f"未知事件 {hook.event}")
        self._hooks[hook.event].append(hook)

    def add(self, event: str, fn: Callable, name: str = "", blocking: bool = True) -> Hook:
        h = Hook(event=event, fn=fn, name=name, blocking=blocking)
        self.register(h)
        return h

    def clear(self, event: str | None = None) -> None:
        if event:
            self._hooks[event] = []
        else:
            for e in self._hooks:
                self._hooks[e] = []

    @property
    def empty(self) -> bool:
        return all(not v for v in self._hooks.values())

    def count(self, event: str) -> int:
        return len(self._hooks.get(event, []))

    # ---------------- 触发 ----------------
    def pre_tool_use(self, tool_name: str, args: dict):
        """返回 None（放行）/ str（阻断理由）/ ToolResult（直接结果）。"""
        for h in self._hooks["pre_tool_use"]:
            try:
                out = h.fn(tool_name, args)
            except Exception as e:                       # noqa: BLE001
                self.last_errors.append(f"pre_tool_use[{h.name}]: {e}")
                continue
            if out is None:
                continue
            if h.blocking:
                return out
        return None

    def post_tool_use(self, tool_name: str, args: dict, result):
        """返回 None（原样）或替换后的 ToolResult。"""
        for h in self._hooks["post_tool_use"]:
            try:
                out = h.fn(tool_name, args, result)
            except Exception as e:                       # noqa: BLE001
                self.last_errors.append(f"post_tool_use[{h.name}]: {e}")
                continue
            if out is not None:
                result = out
        return result

    def emit(self, event: str, ctx: dict) -> None:
        """触发纯观测事件（session_start / session_end / pre_compact）。"""
        for h in self._hooks.get(event, []):
            try:
                h.fn(ctx)
            except Exception as e:                       # noqa: BLE001
                self.last_errors.append(f"{event}[{h.name}]: {e}")
