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
    """API 调用失败。

    ``retryable`` 为 True 时（429/529/5xx）由 ``_attempt`` 做指数退避重试；
    ``retry_after`` 若上游给了 ``Retry-After`` 头则优先按其等待。
    """

    def __init__(self, message: str, *, status: int | None = None,
                 retryable: bool = False, retry_after: float | None = None):
        super().__init__(message)
        self.status = status
        self.retryable = retryable
        self.retry_after = retry_after


class AnthropicClient:
    def __init__(self, base_url: str, api_key: str,
                 model: str, max_tokens: int, verbose: bool = False,
                 prompt_cache: bool = False, fallback_models: list | None = None,
                 max_attempts: int = 4):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.max_tokens = max_tokens
        self.verbose = verbose
        # #14：在稳定前缀（system / 工具定义）标注 cache_control（默认关）
        self.prompt_cache = bool(prompt_cache)
        # #12：过载重试耗尽后按列表切换后备模型（默认空 = 不切换）
        self.fallback_models = list(fallback_models or [])
        self.max_attempts = int(max_attempts)

    # ---------------- 主入口 ----------------
    def create(
        self,
        *,
        system: str,
        tools: list[dict],
        messages: list[dict],
        stream: bool = True,
        on_delta: Callable[[str], None] | None = None,
        on_thinking: Callable[[str], None] | None = None,
    ) -> tuple[list[dict], str, int]:
        """返回 (assistant content blocks, stop_reason, est_output_tokens)。

        #12：对 ``self.model`` 先重试 ``max_attempts`` 次；仍失败则依次尝试
        ``fallback_models``。切换后 ``self.model`` 更新为实际生效的模型。

        ``on_delta`` 收正文增量；``on_thinking`` 收思考（thinking）增量，二者独立。
        """
        models = [self.model] + [m for m in self.fallback_models if m != self.model]
        last_err: Exception | None = None
        for mi, model in enumerate(models):
            payload = self._payload(model, system, tools, messages, stream)
            try:
                blocks, stop_reason, out_tokens = self._attempt(
                    payload, stream, on_delta, on_thinking)
                if mi > 0:
                    self.model = model        # 记录实际生效模型
                    if self.verbose:
                        print(f"[client] 已降级到后备模型 {model}", file=__import__("sys").stderr)
                return blocks, stop_reason, out_tokens
            except APIError as e:
                last_err = e
                if mi + 1 < len(models):
                    continue                  # 切下一个模型
                raise
        raise last_err or APIError("请求失败")

    def _payload(self, model, system, tools, messages, stream) -> dict:
        payload = {
            "model": model,
            "max_tokens": self.max_tokens,
            "system": system,
            "tools": tools,
            "messages": messages,
            "stream": stream,
        }
        if self.prompt_cache:
            self._apply_cache_control(payload)
        return payload

    def _apply_cache_control(self, payload: dict) -> None:
        """#14：给稳定前缀打 cache_control（system 末块 + 最后一个工具定义）。

        仅改请求体形态，不改消息内容；对不支持缓存的代理是 no-op（多余字段被忽略）。
        """
        sysv = payload.get("system")
        if isinstance(sysv, str) and sysv:
            payload["system"] = [{"type": "text", "text": sysv,
                                  "cache_control": {"type": "ephemeral"}}]
        elif isinstance(sysv, list) and sysv and isinstance(sysv[-1], dict):
            sysv = list(sysv)
            sysv[-1] = {**sysv[-1], "cache_control": {"type": "ephemeral"}}
            payload["system"] = sysv
        tools = payload.get("tools")
        if isinstance(tools, list) and tools and isinstance(tools[-1], dict):
            tools = list(tools)
            tools[-1] = {**tools[-1], "cache_control": {"type": "ephemeral"}}
            payload["tools"] = tools

    def _attempt(self, payload: dict, stream: bool, on_delta, on_thinking=None):
        """对单个模型做退避重试（连接类异常 + 429/529/5xx 均重试）。"""
        for attempt in range(self.max_attempts):
            last = (attempt >= self.max_attempts - 1)
            retry_after: float | None = None
            try:
                if stream:
                    return self._stream(payload, on_delta, on_thinking)
                return self._request(payload, stream=False)
            except APIError as e:
                # 仅限流/过载/服务端错误可重试；鉴权、参数错误等立即抛出
                if not e.retryable or last:
                    raise
                retry_after = e.retry_after
            except (requests.RequestException, ValueError) as e:
                if last:
                    raise APIError(f"请求失败：{e}")
            time.sleep(self._retry_delay(attempt, retry_after))
        raise APIError("请求失败（重试耗尽）")

    def _stream(self, payload: dict, on_delta, on_thinking=None) -> tuple[list[dict], str, int]:
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
            raise self._http_error(resp)
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
                    tk = delta.get("thinking", "")
                    blk["thinking"] = blk.get("thinking", "") + tk
                    blk["signature"] = delta.get("signature", "") or blk.get("signature", "")
                    if on_thinking:
                        on_thinking(tk)
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
            raise self._http_error(resp)
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

    def _http_error(self, resp: requests.Response) -> APIError:
        """依据状态码构造 APIError，并标记是否可重试。"""
        retryable = self._retryable_status(resp.status_code)
        ra = self._retry_after_seconds(resp) if retryable else None
        return APIError(self._error_text(resp, retryable),
                        status=resp.status_code, retryable=retryable, retry_after=ra)

    @staticmethod
    def _retryable_status(status: int) -> bool:
        # 429 限流 / 529 过载 / 5xx 服务端错误 → 退避重试
        return status in (429, 529) or status >= 500

    def _error_text(self, resp: requests.Response, retryable: bool | None = None) -> str:
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
        if retryable is None:
            retryable = self._retryable_status(status)
        if status in (429, 529) or status >= 500:
            hint = "，将自动重试" if retryable else ""
            return f"HTTP {status}(过载/限流{hint})：{body}"
        return f"HTTP {status}：{body}"

    @staticmethod
    def _retry_after_seconds(resp: requests.Response) -> float | None:
        """解析 Retry-After 头（秒数或 HTTP 日期），拿不到返回 None。"""
        raw = resp.headers.get("retry-after") or resp.headers.get("Retry-After")
        if not raw:
            return None
        raw = raw.strip()
        try:
            return max(0.0, float(raw))
        except ValueError:
            pass
        try:
            from email.utils import parsedate_to_datetime
            import datetime as _dt
            when = parsedate_to_datetime(raw)
            if when is None:
                return None
            if when.tzinfo is None:
                now = _dt.datetime.now()
            else:
                now = _dt.datetime.now(when.tzinfo)
            return max(0.0, (when - now).total_seconds())
        except Exception:
            return None

    def _retry_delay(self, attempt: int, retry_after: float | None = None) -> float:
        """退避秒数：优先 Retry-After，否则指数退避 + 抖动；上限 60s。"""
        if retry_after is not None and retry_after > 0:
            return min(retry_after, 60.0)
        return min(2 ** attempt * 1.5, 30) + random.uniform(0, 1)

    def _headers(self) -> dict:
        return {
            "x-api-key": self.api_key,
            "authorization": f"Bearer {self.api_key}",
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
