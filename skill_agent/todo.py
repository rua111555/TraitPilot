"""#9 规划 / todo（把计划外部化为可检查状态，纯 stdlib）。

借鉴 Claude Code 的 TodoWrite：模型通过一个工具把「当前计划」写成结构化清单，后续每轮都能
看到它（工具结果回填进 history），从而在长任务里维持外部化的计划态，而不是只靠 system prompt
里的口头建议。

设计要点：
- **结构化而非自由文本**：每条 ``{"content": str, "status": "pending|in_progress|completed"}``，
  便于渲染、校验、后续程序化消费。
- **单一 in_progress**：同一时刻最多一条进行中，避免计划态歧义（校验器强制）。
- **默认关**（opt-in）：仅当 ``--todo`` / ``SKILL_AGENT_TODO=1`` 时挂载 ``TodoWrite`` 工具，
  未开启时工具集与改造前**完全一致**（不新增模型可见工具，避免行为漂移）。
"""
from __future__ import annotations

import json

STATUSES = ("pending", "in_progress", "completed")
_GLYPH = {"pending": "[ ]", "in_progress": "[~]", "completed": "[x]"}


def parse_todos(text: str):
    """解析模型传入的 todos（JSON 字符串或已是 list）。

    返回 ``(items, error)``：``items`` 为规范化后的清单，``error`` 非空表示校验失败。
    """
    if isinstance(text, (list, tuple)):
        raw = list(text)
    else:
        s = str(text or "").strip()
        if not s:
            return None, "todos 不能为空。"
        try:
            raw = json.loads(s)
        except json.JSONDecodeError as e:
            return None, f"todos 不是合法 JSON：{e}"
    if not isinstance(raw, list):
        return None, "todos 必须是数组。"
    if not raw:
        return [], None
    items, in_progress = [], 0
    for i, it in enumerate(raw):
        if not isinstance(it, dict):
            return None, f"第 {i + 1} 项不是对象。"
        content = str(it.get("content", "")).strip()
        if not content:
            return None, f"第 {i + 1} 项缺少 content。"
        status = str(it.get("status", "pending")).strip() or "pending"
        if status not in STATUSES:
            return None, (f"第 {i + 1} 项 status 非法：{status!r}"
                          f"（应为 {', '.join(STATUSES)}）。")
        if status == "in_progress":
            in_progress += 1
        items.append({"content": content, "status": status})
    if in_progress > 1:
        return None, "同时最多只能有一条 in_progress（计划态须唯一）。"
    return items, None


def render(items: list[dict]) -> str:
    """把清单渲染成给人/模型看的文本。"""
    if not items:
        return "（无待办项）"
    lines = []
    done = sum(1 for it in items if it["status"] == "completed")
    lines.append(f"计划进度：{done}/{len(items)} 已完成")
    for it in items:
        lines.append(f"{_GLYPH.get(it['status'], '[?]')} {it['content']}")
    return "\n".join(lines)


def progress(items: list[dict]) -> dict:
    """给 meta 用的进度信号（只记不判，与 M3a 一致）。"""
    if not items:
        return {"total": 0, "completed": 0, "in_progress": 0, "pending": 0}
    return {
        "total": len(items),
        "completed": sum(1 for i in items if i["status"] == "completed"),
        "in_progress": sum(1 for i in items if i["status"] == "in_progress"),
        "pending": sum(1 for i in items if i["status"] == "pending"),
    }
