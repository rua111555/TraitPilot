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

    def fake_attempt(payload, stream, on_delta):
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
    def boom(payload, stream, on_delta):
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


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
