"""Token 估算（M12 辅助）。

无第三方 tokenizer 依赖，用「字符数 / 4」的粗估（对中英混排足够驱动压缩阈值判定）。
真实 usage 仍以 API 返回为准（见 ``agent._record_usage``）；本模块只用于**决定何时压缩**。
"""
from __future__ import annotations

import json
from typing import Any


def estimate_text_tokens(text: str) -> int:
    if not text:
        return 0
    # 中文等宽字符更省 token 的粗略拟合，够用即可
    return max(1, len(text) // 4)


def _content_tokens(content: Any) -> int:
    if isinstance(content, str):
        return estimate_text_tokens(content)
    if isinstance(content, list):
        total = 0
        for b in content:
            if not isinstance(b, dict):
                total += estimate_text_tokens(str(b))
                continue
            t = b.get("type")
            if t == "text":
                total += estimate_text_tokens(b.get("text", ""))
            elif t == "thinking":
                total += estimate_text_tokens(b.get("thinking", ""))
            elif t == "tool_use":
                total += estimate_text_tokens(b.get("name", "")) + \
                    estimate_text_tokens(json.dumps(b.get("input") or {}, ensure_ascii=False))
            elif t == "tool_result":
                total += _content_tokens(b.get("content"))
            else:
                total += estimate_text_tokens(json.dumps(b, ensure_ascii=False))
        return total
    return estimate_text_tokens(str(content))


def messages_tokens(messages: list[dict]) -> int:
    return sum(_content_tokens(m.get("content")) for m in messages if isinstance(m, dict))
