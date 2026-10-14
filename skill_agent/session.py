"""M12/M13 装配工厂：把压缩/记忆/持久化接到 Agent 上（默认关闭）。

把「主循环怎么拿 summary 调用、store/memory 放哪、resume 怎么装」这些接线集中在一处，
既让 CLI/Web 复用，也让核心模块保持可独立单测。
"""
from __future__ import annotations

from pathlib import Path

from .agent import Agent
from .client import AnthropicClient
from .compaction import SummaryCompressor
from .config import Config, default_checkpoint_dir, default_memory_dir
from .memory import MemoryStore
from .session_store import SessionStore, new_session_id


def attach_all(agent: Agent, cfg: Config, *, session_id: str | None = None) -> Agent:
    """按 cfg 的开关装配 M12+M13；未开启的项不改变行为。"""
    # ---- M13 持久化 ----
    if cfg.persist:
        store = SessionStore(default_checkpoint_dir())
        agent.store = store
        agent.session_id = session_id or cfg.session_id or new_session_id()
        store.append_meta(agent.session_id, {
            "model": cfg.model, "permission": cfg.permission,
            "project": str(cfg.project), "created": _now()})
        cfg.session_id = agent.session_id
    elif session_id or cfg.session_id:
        agent.session_id = session_id or cfg.session_id

    # ---- M12 记忆 ----
    if cfg.memory_enabled:
        agent.memory = MemoryStore(default_memory_dir(cfg.project))

    # ---- M12 摘要压缩 ----
    if cfg.summary_compaction:
        agent.summary_compressor = SummaryCompressor()
        agent.summarize = _make_summarizer(cfg)
    return agent


def resume_into(agent: Agent, store: SessionStore, session_id: str) -> dict:
    """把某会话的持久化历史恢复进 agent，返回 {restored, discarded, meta}。"""
    data = store.load(session_id)
    n = agent.restore(data["messages"])
    agent.store = store
    agent.session_id = session_id
    return {"restored": n, "discarded": data["discarded"], "meta": data["meta"]}


def _make_summarizer(cfg: Config):
    """构造一个 summary 回调：非流式、低 max_tokens 的独立客户端，失败返回 None。"""
    client = AnthropicClient(
        base_url=cfg.base_url, api_key=cfg.api_key,
        model=cfg.summary_model or cfg.model,
        max_tokens=min(2048, cfg.max_tokens), verbose=False)

    def summarize(prompt: str) -> str | None:
        try:
            blocks, _stop, _tok = client.create(
                system="你是上下文压缩器，只输出结构化摘要，不要寒暄。",
                tools=[], messages=[{"role": "user", "content": prompt}],
                stream=False, on_delta=None)
        except Exception:
            return None
        return "".join(b.get("text", "") for b in blocks if b.get("type") == "text").strip() or None

    return summarize


def _now() -> str:
    import time
    return time.strftime("%Y-%m-%d %H:%M:%S")
