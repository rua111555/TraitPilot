"""Anthropic Messages API 客户端（纯 requests，无外部 SDK）。

- 复用 ANTHROPIC_BASE_URL / ANTHROPIC_AUTH_TOKEN
- SSE 流式 + 非流式回退
- 限流/过载指数退避重试
- 保留 thinking 块 / tool_use 块，可正确回传多轮
"""
from __future__ import annotations

import json
import random
import time
from typing import Callable, Iterable

import requests


class APIError(Exception):
    pass


class AnthropicClient:
    def __init__(self, base_url: str, api_key: str,
                 model: str, max_tokens: int, verbose: bool = False):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.max_tokens = max_tokens
        self.verbose = verbose

    # ---------------- 主入口 ----------------
    def create(
        self,
        *,
        system: str,
        tools: list[dict],
        messages: list[dict],
        stream: bool = True,
        on_delta: Callable[[str], None] | None = None,
    ) -> tuple[list[dict], str, int]:
        """返回 (assistant content blocks, stop_reason, est_output_tokens)。"""
        payload = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": system,
            "tools": tools,
            "messages": messages,
            "stream": stream,
        }
        for attempt in range(4):
            try:
                if stream:
                    try:
                        return self._stream(payload, on_delta)
                    except (requests.RequestException, ValueError) as e:
                        if attempt >= 3:
                            raise APIError(f"流式请求失败：{e}")
                        time.sleep(self._backoff(attempt))
                        continue
                return self._request(payload, stream=False)
            except APIError as e:
                raise e
            except requests.RequestException as e:
                if attempt >= 3:
                    raise APIError(f"请求失败：{e}")
                time.sleep(self._backoff(attempt))

    def _stream(self, payload: dict, on_delta) -> tuple[list[dict], str, int]:
        headers = self._headers()
        try:
            resp = requests.post(
                f"{self.base_url}/v1/messages",
                headers=headers, json=payload, stream=True,
                timeout=(30, 900),
            )
        except requests.RequestException:
            raise
        if resp.status_code != 200:
            raise APIError(self._error_text(resp))
        # 关键：text/event-stream 常缺 charset，requests 默认按 ISO-8859-1 解码 → 中文变乱码
        resp.encoding = "utf-8"
        it = resp.iter_lines(decode_unicode=True)

        blocks: list[dict] = []
        stop_reason = ""
        usage_out = 0
        last_type = ""

        def cur(index: int) -> dict:
            while len(blocks) <= index:
                blocks.append({})
            return blocks[index]

        for line in it:
            if not line or line == "data: [DONE]":
                # 兼容部分代理用 [DONE] 结束
                if line == "data: [DONE]":
                    break
                continue
            if not line.startswith("data:"):
                continue
            try:
                ev = json.loads(line[5:].strip())
            except json.JSONDecodeError:
                continue
            t = ev.get("type", "")
            if t == "error":
                raise APIError(ev.get("error", {}).get("message", "stream error"))
            if t == "message_start":
                last_type = "message"
                if self.verbose:
                    print(f"[client] model={ev.get('message', {}).get('model', '')}", file=__import__("sys").stderr)
            elif t == "content_block_start":
                idx = ev.get("index", len(blocks))
                block = dict(ev.get("content_block", {}))
                block.setdefault("input", {})
                block.setdefault("_buf", "")
                blocks.insert(idx, block)
            elif t == "content_block_delta":
                idx = ev.get("index", 0)
                blk = cur(idx)
                delta = ev.get("delta", {})
                dt = delta.get("type")
                if dt == "text_delta":
                    txt = delta.get("text", "")
                    blk["text"] = blk.get("text", "") + txt
                    if on_delta:
                        on_delta(txt)
                elif dt == "thinking_delta":
                    blk["thinking"] = blk.get("thinking", "") + delta.get("thinking", "")
                    blk["signature"] = delta.get("signature", "") or blk.get("signature", "")
                elif dt in ("input_json_delta", "input_json"):
                    blk["_buf"] += delta.get("partial_json", "") or delta.get("data", "") or ""
            elif t == "content_block_stop":
                idx = ev.get("index", 0)
                blk = cur(idx)
                if blk.get("type") == "tool_use" and blk.get("_buf"):
                    try:
                        blk["input"] = json.loads(blk["_buf"])
                    except json.JSONDecodeError:
                        # M2：标记解析失败（可能被 max_tokens 截断），交由 StopController 决定修复
                        blk["_parse_error"] = True
                        blk["_raw"] = blk.get("_buf", "")
                blk.pop("_buf", None)
            elif t == "message_delta":
                dr = ev.get("delta", {})
                stop_reason = dr.get("stop_reason", "") or stop_reason
                usage_out = ev.get("usage", {}).get("output_tokens") or usage_out
            elif t == "message_stop":
                break
        resp.close()

        blocks = [b for b in blocks if b]
        return blocks, stop_reason, usage_out

    def _request(self, payload: dict, stream: bool = False) -> tuple[list[dict], str, int]:
        resp = requests.post(
            f"{self.base_url}/v1/messages",
            headers=self._headers(), json=payload, stream=False,
            timeout=(30, 900),
        )
        if resp.status_code != 200:
            raise APIError(self._error_text(resp))
        resp.encoding = "utf-8"
        data = resp.json()
        blocks = data.get("content", [])
        stop_reason = data.get("stop_reason", "")
        usage_out = (data.get("usage") or {}).get("output_tokens", 0)
        # 流式被禁用时的模型名
        if self.verbose:
            print(f"[client] model={data.get('model', '')}", file=__import__("sys").stderr)
        return blocks, stop_reason, usage_out

    # ---------------- 辅助 ----------------
    _ERROR_MSG_KEYS = ("message", "error", "detail")

    def _error_text(self, resp: requests.Response) -> str:
        status = resp.status_code
        body = ""
        try:
            j = resp.json()
            body = j.get("error", j) if isinstance(j, dict) else str(j)
            if isinstance(body, dict):
                for k in self._ERROR_MSG_KEYS:
                    if body.get(k):
                        body = str(body[k])
                        break
                else:
                    body = json.dumps(body, ensure_ascii=False)[:400]
            body = str(body)
        except Exception:
            body = resp.text[:400]
        if status in (429, 529) or status >= 500:
            retry = "retry" in body
            hint = "，将自动重试" if retry else ""
            return f"HTTP {status}(过载/限流{hint})：{body}"
        return f"HTTP {status}：{body}"

    def _backoff(self, attempt: int) -> float:
        return min(2 ** attempt * 1.5, 30) + random.uniform(0, 1)

    def _headers(self) -> dict:
        return {
            "x-api-key": self.api_key,
            "authorization": f"Bearer {self.api_key}",
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
