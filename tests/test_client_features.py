"""#14 prompt cache_control 与 #12 模型降级单测（不发真实网络请求）。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from skill_agent.client import AnthropicClient, APIError  # noqa: E402


def _client(**kw):
    return AnthropicClient(base_url="http://x", api_key="k", model="A",
                           max_tokens=16, **kw)


SYS = "you are a helper"
TOOLS = [{"name": "Noop", "description": "d", "input_schema": {"type": "object"}}]


# ---------------- #14 prompt cache ----------------

def test_cache_off_by_default():
    c = _client()
    p = c._payload("A", SYS, TOOLS, [{"role": "user", "content": "hi"}], False)
    assert p["system"] == SYS                      # 仍是原字符串，无 cache_control
    assert "cache_control" not in p["tools"][-1]


def test_cache_marks_system_and_last_tool():
    c = _client(prompt_cache=True)
    p = c._payload("A", SYS, TOOLS + [{"name": "B", "description": "d",
                                       "input_schema": {"type": "object"}}],
                   [{"role": "user", "content": "hi"}], False)
    assert isinstance(p["system"], list)
    assert p["system"][-1]["cache_control"] == {"type": "ephemeral"}
    assert p["system"][-1]["text"] == SYS
    # 只标最后一个工具（稳定前缀末端）
    assert p["tools"][-1]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in p["tools"][0]


# ---------------- #12 模型降级 ----------------

def test_fallback_on_primary_failure(monkeypatch):
    c = _client(fallback_models=["B", "C"])
    seen = []

    def fake_attempt(payload, stream, on_delta, on_thinking=None):
        seen.append(payload["model"])
        if payload["model"] != "B":
            raise APIError("overloaded")
        return [{"type": "text", "text": "ok"}], "end_turn", 1

    monkeypatch.setattr(c, "_attempt", fake_attempt)
    blocks, stop, tok = c.create(system=SYS, tools=TOOLS,
                                 messages=[{"role": "user", "content": "hi"}], stream=False)
    assert seen == ["A", "B"]           # A 失败后切 B，不再试 C
    assert c.model == "B" and stop == "end_turn"


def test_no_fallback_raises_after_primary(monkeypatch):
    c = _client()                        # 无后备
    def boom(payload, stream, on_delta, on_thinking=None):
        raise APIError("down")
    monkeypatch.setattr(c, "_attempt", boom)
    try:
        c.create(system=SYS, tools=TOOLS,
                 messages=[{"role": "user", "content": "hi"}], stream=False)
    except APIError:
        return
    raise AssertionError("无后备模型时应抛出 APIError")


def test_fallback_dedupes_primary():
    c = _client(fallback_models=["A", "B"])   # A 与主模型重复
    # create 里 models = [self.model] + [m for m in fallback if m != self.model]
    assert [m for m in c.fallback_models if m != c.model] == ["B"]


# ---------------- 429/529/5xx 重试 ----------------

def _resp(status, body=None, hdrs=None):
    hdrs = hdrs or {}

    class R:
        status_code = status
        text = str(body or "")
        headers = hdrs

        def json(self):
            if isinstance(body, dict):
                return body
            raise ValueError("no json")
    return R()


def test_http_error_marks_retryable():
    c = _client()
    for st in (429, 529, 500, 502, 503):
        e = c._http_error(_resp(st, {"error": {"message": "overloaded"}}))
        assert isinstance(e, APIError) and e.retryable and e.status == st
        assert "将自动重试" in str(e)
    for st in (400, 401, 404, 422):
        e = c._http_error(_resp(st, {"error": {"message": "bad"}}))
        assert not e.retryable and e.status == st
        assert "将自动重试" not in str(e)


def test_retry_after_parsed():
    c = _client()
    assert c._retry_after_seconds(_resp(429, hdrs={"Retry-After": "3"})) == 3.0
    assert c._retry_delay(0, retry_after=2.5) == 2.5
    assert c._retry_after_seconds(_resp(429)) is None      # 无头 → None


def test_attempt_retries_then_succeeds(monkeypatch):
    c = _client()
    monkeypatch.setattr("time.sleep", lambda *_a, **_k: None)   # 不真等
    calls = []

    def flaky(payload, stream, on_delta, on_thinking=None):
        calls.append(1)
        if len(calls) < 3:
            raise APIError("HTTP 429", status=429, retryable=True)
        return [{"type": "text", "text": "ok"}], "end_turn", 1

    monkeypatch.setattr(c, "_request", lambda p, stream=False: flaky(p, stream, None))
    blocks, stop, tok = c.create(system=SYS, tools=TOOLS,
                                 messages=[{"role": "user", "content": "hi"}], stream=False)
    assert len(calls) == 3 and stop == "end_turn"


def test_attempt_gives_up_after_max(monkeypatch):
    c = _client(max_attempts=3)
    monkeypatch.setattr("time.sleep", lambda *_a, **_k: None)
    calls = []

    def always_429(payload, stream, on_delta, on_thinking=None):
        calls.append(1)
        raise APIError("HTTP 429", status=429, retryable=True)

    monkeypatch.setattr(c, "_request", lambda p, stream=False: always_429(p, stream, None))
    try:
        c.create(system=SYS, tools=TOOLS,
                 messages=[{"role": "user", "content": "hi"}], stream=False)
    except APIError as e:
        assert e.retryable and len(calls) == 3
        return
    raise AssertionError("重试耗尽后应抛出 APIError")


def test_auth_error_not_retried(monkeypatch):
    c = _client(max_attempts=4)
    monkeypatch.setattr("time.sleep", lambda *_a, **_k: None)
    calls = []

    def boom(payload, stream, on_delta, on_thinking=None):
        calls.append(1)
        raise APIError("HTTP 401", status=401, retryable=False)

    monkeypatch.setattr(c, "_request", lambda p, stream=False: boom(p, stream, None))
    try:
        c.create(system=SYS, tools=TOOLS,
                 messages=[{"role": "user", "content": "hi"}], stream=False)
    except APIError as e:
        assert not e.retryable and len(calls) == 1   # 不可重试 → 只请求一次
        return
    raise AssertionError("401 应立即抛出")


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
