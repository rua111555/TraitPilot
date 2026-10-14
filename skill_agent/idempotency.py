"""M11 工具幂等与去重（纯 stdlib，默认关）。

为**有副作用**的工具调用加一层「同一次运行内，相同 (工具名, 规范化参数) 只真正执行一次」的
护栏，避免重复 `nohup` 起多份后台任务、重复写同一文件等。

设计要点（与 ``docs/harness-gap-analysis.md`` M11 一致）：

- **只对有副作用工具生效**（``Bash`` / ``Write`` / ``Edit``）；只读工具（``Read``/``Grep``/``Glob``/
  ``TaskOutput``）不参与去重——它们天然可重复且常需取最新状态。
- **键 = 工具名 + 规范化 JSON 参数**（键排序，保证稳定）。
- **默认关**（opt-in）：未显式开启时 ``ToolSet`` 行为与改造前完全一致。
- 命中策略二选一：
  - ``reuse``（默认）：复用上次结果，并附一行「幂等：相同调用，已复用结果」说明；
  - ``deny``：拒绝重放，返回 ``is_error`` 让模型改用其它参数/放弃重复。
- **只缓存成功结果**（``is_error=False``）；失败允许重试（网络抖动等不应被永久去重）。
- 生命周期：**内存态、按「一次用户请求」重置**——绝不落盘，不引入半吊子 resume 语义
  （崩溃一致性归 M13，不在此模块解决）。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

# 参与去重的有副作用工具（只读工具显式排除）
SIDE_EFFECT_TOOLS = frozenset({"Bash", "Write", "Edit"})

_MODES = ("reuse", "deny")


@dataclass
class Hit:
    """一次命中：复用结果或拒绝的判定。"""

    mode: str           # reuse | deny
    result: object      # ToolResult（reuse 时）或 None（deny 时）


class IdempotencyGuard:
    """一次运行内的幂等护栏。``enabled=False`` 时全部方法为空操作。"""

    def __init__(self, enabled: bool = False, mode: str = "reuse",
                 max_entries: int = 512, replay_note: bool = True):
        self.enabled = bool(enabled)
        self.mode = mode if mode in _MODES else "reuse"
        self.max_entries = int(max_entries)
        self.replay_note = bool(replay_note)
        self._seen: dict[str, object] = {}      # key -> ToolResult（成功后记录）

    # ---------------- 键 ----------------
    @staticmethod
    def key(name: str, args: dict) -> str:
        try:
            canon = json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            canon = repr(args)
        return name + "\x00" + hashlib.sha256(canon.encode("utf-8")).hexdigest()[:32]

    @staticmethod
    def eligible(name: str) -> bool:
        return name in SIDE_EFFECT_TOOLS

    # ---------------- 查询 / 记录 ----------------
    def check(self, name: str, args: dict):
        """执行前查询。返回 ``(hit_key, Hit)``；未命中返回 ``(key, None)``。"""
        if not self.enabled or not self.eligible(name):
            return None, None
        k = self.key(name, args)
        prev = self._seen.get(k)
        if prev is None:
            return k, None
        if self.mode == "deny":
            return k, Hit("deny", None)
        return k, Hit("reuse", self._copy(prev))

    def record(self, key: str | None, name: str, result) -> None:
        """成功执行后记账（只记成功结果）。"""
        if not self.enabled or key is None or not self.eligible(name):
            return
        if getattr(result, "is_error", False):
            return
        if len(self._seen) >= self.max_entries:
            # 简单的 FIFO 淘汰，避免无界增长
            self._seen.pop(next(iter(self._seen)), None)
        self._seen[key] = self._copy(result)

    def reset(self) -> None:
        """按「一次用户请求」清空（run 级语义）。"""
        self._seen.clear()

    def size(self) -> int:
        return len(self._seen)

    # ---------------- 内部 ----------------
    def _copy(self, result):
        """复制 ToolResult，避免复用/记录时共享可变 meta。"""
        try:
            from .tools import ToolResult
            tr = ToolResult(result.content, result.is_error,
                            dict(getattr(result, "meta", {}) or {}))
        except Exception:
            return result
        return tr

    def render(self, name: str, hit: Hit):
        """把命中渲染成给模型看的 ToolResult。"""
        from .tools import ToolResult
        if hit.mode == "deny":
            return ToolResult(
                f"[幂等] 本次运行已执行过相同调用（{name}），为防重复副作用已拒绝重放。"
                f"若确需重新执行，请改变参数或说明差异。", True)
        res = hit.result
        if res is None:
            return ToolResult(f"[幂等] 相同调用（{name}）已执行过。", True)
        if self.replay_note:
            res.content = ("[幂等] 相同调用已执行过，复用上次结果：\n" + res.content)
        res.meta = dict(getattr(res, "meta", {}) or {})
        res.meta["idempotent_replay"] = True
        return res
