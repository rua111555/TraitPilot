"""可选的工具调用 trace 钩子（M10 示例实现 + 轻量可观测性）。

提供一个 ``HookManager`` 工厂：注册 Pre/Post 工具钩子与用量钩子，把每次工具调用
（工具名 / 参数摘要 / 结果或错误 / 耗时）和每轮用量以 jsonl 追加到
``~/.skill-agent/traces/<sid>.jsonl``。纯观测、非阻断；钩子异常已被 HookManager 吞掉。

**默认不启用**（CLI ``--trace`` 开启），因为写盘有 I/O 成本。
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

from .hooks import HookManager


def trace_dir() -> Path:
    d = Path.home() / ".skill-agent" / "traces"
    d.mkdir(parents=True, exist_ok=True)
    return d


def build_trace_hooks(session_id: str, directory: Path | None = None) -> HookManager:
    hm = HookManager()
    directory = directory or trace_dir()
    path = Path(directory) / f"{session_id}.jsonl"
    lock = threading.Lock()
    started: dict[str, float] = {}

    def _write(rec: dict) -> None:
        try:
            with lock, open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def pre(tool_name: str, args: dict):
        started[f"{tool_name}"] = time.monotonic()
        _write({"t": _now(), "event": "pre_tool_use", "tool": tool_name,
                "args": _brief(args)})
        return None

    def post(tool_name: str, args: dict, result):
        t0 = started.pop(tool_name, None)
        _write({"t": _now(), "event": "post_tool_use", "tool": tool_name,
                "is_error": getattr(result, "is_error", False),
                "duration_ms": int((time.monotonic() - t0) * 1000) if t0 else None,
                "result": (getattr(result, "content", "") or "")[:1000],
                "meta": getattr(result, "meta", {}) or {}})
        return None

    hm.add("pre_tool_use", pre, name="trace_pre", blocking=False)
    hm.add("post_tool_use", post, name="trace_post")
    hm.add("session_start", lambda ctx: _write({"t": _now(), "event": "session_start", **ctx}))
    hm.add("session_end", lambda ctx: _write({"t": _now(), "event": "session_end", **ctx}))
    return hm


def _brief(args: dict) -> dict:
    out = {}
    for k, v in (args or {}).items():
        s = str(v)
        out[k] = s[:300] + ("…" if len(s) > 300 else "")
    return out


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")
