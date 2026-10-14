"""停止原因分类与终止决策（M2）。

把 ``agent._run`` 里「一行 stop_reason 白名单」升级为「分类 → 决策 → 守卫」：

    blocks, stop_reason = client.create(...)
    decision = classify(stop_reason, blocks)

决策动作（``StopAction``）:
- ``CONTINUE_TOOLS``     存在**完整** tool_use → 执行工具并续推
- ``REPAIR_TRUNCATION``  全部 tool_use 都解析失败（被 max_tokens 截断）→ 为该轮合成
                         is_error 的 tool_result 续推（维持配对不变量，让模型重试）
- ``RESUME_SERVER_TOOL`` stop_reason=pause_turn → 原样回传 assistant 内容续推，**不追加 user**
- ``REFUSE``             stop_reason=refusal → 终止并如实告知
- ``FINALIZE``           收尾（end_turn / stop_sequence / max_tokens 纯文本 / 未知）
- ``ABORT``              预留：外部强制终止

参照：Anthropic Messages API 的 stop_reason 权威枚举与 pause_turn 语义、Claude Code 的
按 stop_reason 分支、smolagents 的「解析失败作为 error observation 回灌」。
本模块为纯函数，无副作用，可独立单测。
"""
from __future__ import annotations

import enum
from dataclasses import dataclass


class StopAction(str, enum.Enum):
    CONTINUE_TOOLS = "continue_tools"
    FINALIZE = "finalize"
    RESUME_SERVER_TOOL = "resume_server_tool"
    REFUSE = "refuse"
    REPAIR_TRUNCATION = "repair_truncation"
    ABORT = "abort"


@dataclass
class StopDecision:
    action: StopAction
    reason_code: str
    user_message: str = ""
    truncated: bool = False
    warn: str = ""

    @property
    def needs_tools(self) -> bool:
        return self.action in (StopAction.CONTINUE_TOOLS, StopAction.REPAIR_TRUNCATION)


_FINAL_STOP = ("end_turn", "stop_sequence")


def _tool_use_blocks(blocks: list) -> list[dict]:
    return [b for b in blocks if isinstance(b, dict) and b.get("type") == "tool_use"]


def _is_parse_error(b: dict) -> bool:
    return bool(b.get("_parse_error"))


def classify(stop_reason: str, blocks: list[dict]) -> StopDecision:
    """根据 stop_reason 与本轮 blocks 决定下一步动作。"""
    tus = _tool_use_blocks(blocks or [])
    usable = [b for b in tus if not _is_parse_error(b)]

    # 1) 存在完整 tool_use（无论 stop_reason）→ 继续执行工具
    if usable:
        warn = "protocol_anomaly" if stop_reason in _FINAL_STOP else ""
        return StopDecision(StopAction.CONTINUE_TOOLS, "tool_use", warn=warn)

    # 2) 有 tool_use 但全部解析失败（截断）→ 修截断续推
    if tus and not usable:
        return StopDecision(StopAction.REPAIR_TRUNCATION, "tool_parse_error",
                            truncated=True,
                            user_message="工具参数被长度上限截断，未执行；请重试或简化参数。")

    # 3) max_tokens + 纯文本 → 收尾且标记截断
    if stop_reason == "max_tokens":
        return StopDecision(StopAction.FINALIZE, "max_tokens", truncated=True,
                            user_message="响应达到 max_tokens 被截断。")

    # 4) pause_turn → 原样回传续推
    if stop_reason == "pause_turn":
        return StopDecision(StopAction.RESUME_SERVER_TOOL, "pause_turn")

    # 5) refusal → 终止并如实告知
    if stop_reason == "refusal":
        return StopDecision(StopAction.REFUSE, "refusal",
                            user_message="模型拒绝继续该请求。")

    # 6) 正常收尾
    if stop_reason in _FINAL_STOP or stop_reason in ("", None):
        return StopDecision(StopAction.FINALIZE, "end_turn")

    # 7) 未知 stop_reason → 收尾并告警
    return StopDecision(StopAction.FINALIZE, "unknown",
                        warn=f"unknown_stop_reason:{stop_reason}")


def make_truncation_result(tool_id: str, message: str = "") -> dict:
    """为被截断/解析失败的 tool_use 合成 is_error 的配对 tool_result。"""
    return {"type": "tool_result", "tool_use_id": tool_id,
            "content": message or "工具参数解析失败（可能被截断），未执行。",
            "is_error": True}


def text_of(blocks: list[dict]) -> str:
    return "".join(b.get("text", "") for b in (blocks or [])
                   if isinstance(b, dict) and b.get("type") == "text").strip()
