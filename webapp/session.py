"""WebAgent 会话：承载一次对某用户的对话，桥接线程中的 agent 运行与 SSE 事件流。"""
from __future__ import annotations

import queue
import threading
import time
import uuid

from skill_agent.config import default_checkpoint_dir


class Hooks:
    """从 agent 线程回调到 WebSession 的桥（文本增量 / 工具事件 / 命令确认）。"""

    def __init__(self, session: "WebSession"):
        self.session = session

    @property
    def cancelled(self) -> bool:
        return self.session.cancel_event.is_set()

    def emit(self, ev: dict) -> None:
        self.session.event_queue.put(ev)

    def confirm(self, command: str, reason: str) -> bool:
        return self.session.ask_confirm(command, reason)


class WebSession:
    def __init__(self, session_id: str, cfg, registry, tools, hooks: Hooks):
        self.id = session_id
        self.cfg = cfg
        self.registry = registry
        self.tools = tools
        self.hooks = hooks
        self.agent = None                     # 由 SessionManager 构建（共享 hooks）
        self.event_queue: queue.SimpleQueue = queue.SimpleQueue()
        self.cancel_event = threading.Event()
        self.busy = False
        self._confirms: dict[str, threading.Event] = {}
        self._answers: dict[str, bool] = {}
        self.created_at = time.time()
        self.last_model = cfg.model
        self.last_permission = cfg.permission

    # ---------- 参数 ----------
    def set_params(self, model: str | None = None, permission: str | None = None) -> None:
        if model and model != self.cfg.model:
            self.cfg.model = model
            self.agent.client.model = model
        if permission and permission != self.cfg.permission:
            self.cfg.permission = permission
            self.tools.permission = permission

    def cancel(self) -> None:
        self.cancel_event.set()
        for uid, ev in list(self._confirms.items()):
            self._answers[uid] = False
            ev.set()

    # ---------- 确认桥 ----------
    def ask_confirm(self, command: str, reason: str) -> bool:
        uid = uuid.uuid4().hex[:8]
        ev = threading.Event()
        self._confirms[uid] = ev
        self.event_queue.put({
            "type": "confirm", "uid": uid, "command": command, "reason": reason})
        ev.wait()
        self._confirms.pop(uid, None)
        return self._answers.pop(uid, False)

    def resolve_confirm(self, uid: str, approve: bool) -> bool:
        ev = self._confirms.get(uid)
        if ev is None:
            return False
        self._answers[uid] = approve
        ev.set()
        return True

    # ---------- 运行 ----------
    def run(self, user_text: str) -> None:
        """在独立线程中被调用；事件经由 event_queue 提供给 SSE。"""
        self.cancel_event.clear()
        self.busy = True
        try:
            final = self.agent.answer(
                user_text,
                on_delta=lambda t: self.event_queue.put({"type": "text", "delta": t}),
            )
            self.event_queue.put({
                "type": "done", "final": final,
                "messages": len(self.agent.history),
                "active": self.agent.active_skills,
                "usage": self.agent.usage_bar,
                "stop_reason": getattr(self.agent._last_stop, "reason_code", None),
            })
        except Exception as e:  # noqa: BLE001
            self.event_queue.put({"type": "error", "message": f"{type(e).__name__}: {e}"})
        finally:
            self.event_queue.put({"type": "__end__"})
            self.busy = False


def build_session(session_id: str, cfg, registry) -> WebSession:
    """构建一个带 hooks 的会话（tools/agent 绑定到本会话）。"""
    from skill_agent import tools as tools_mod
    from skill_agent.agent import Agent
    from skill_agent.permissions import PermissionModel
    from skill_agent.session import attach_all, resume_into
    from skill_agent.session_store import SessionStore, new_session_id

    session = WebSession(session_id, cfg, registry, tools=None, hooks=None)
    hooks = Hooks(session)
    session.hooks = hooks
    trace_hooks = None
    if cfg.hooks_enabled:
        from skill_agent.observability import build_trace_hooks
        trace_hooks = build_trace_hooks(session_id)
    # M9：按配置选择沙箱执行后端（webapp 无 argparse，直接读 cfg；fail-soft 回退宿主）
    from skill_agent.sandbox import build_backend
    sandbox_backend, _sb_warn = build_backend(cfg)
    # M11：按配置启用幂等去重（默认关）
    from skill_agent.idempotency import IdempotencyGuard
    idem = IdempotencyGuard(enabled=bool(getattr(cfg, "idempotency", False)),
                            mode=getattr(cfg, "idempotency_mode", "reuse"))
    # #11：按配置装配检索索引（默认关，不挂载 Search/RepoMap）
    _retrieval = None
    if bool(getattr(cfg, "retrieval", False)):
        from pathlib import Path
        from skill_agent.config import default_index_dir
        from skill_agent.retrieval import CodeIndex
        _idx_root = Path(cfg.index_dir) if getattr(cfg, "index_dir", None) \
            else default_index_dir(cfg.project)
        _retrieval = CodeIndex(_idx_root, enabled=True,
                               include_dirs=getattr(cfg, "retrieval_roots", None),
                               extra_excludes=getattr(cfg, "retrieval_excludes", None))
        if not _retrieval.enabled:
            _retrieval = None
    session.tools = tools_mod.ToolSet(
        registry, cfg.cwd, permission=cfg.permission,
        verbose=cfg.verbose, confirm_handler=hooks.confirm,
        permission_model=PermissionModel.from_config(cfg, cfg.cwd),
        hooks=trace_hooks, spill_dir=cfg.spill_dir, spill_threshold=cfg.spill_threshold,
        sandbox=sandbox_backend, idempotency=idem,
        enable_todo=bool(getattr(cfg, "todo", False)),
        enable_retrieval=bool(getattr(cfg, "retrieval", False)), retrieval=_retrieval)
    agent = Agent(cfg, registry, session.tools)
    agent.on_event = hooks.emit
    agent.cancel_event = session.cancel_event
    if cfg.loop_guard:                       # M3：按配置启用死循环熔断
        from skill_agent.loopguard import LoopGuard
        agent.loopguard = LoopGuard(enabled=True, warn=cfg.loop_warn, halt=cfg.loop_halt)

    # M13：网页默认持久化，支持断线后按 sid 恢复（可用 SKILL_AGENT_PERSIST=0 关闭）
    import os as _os
    if _os.environ.get("SKILL_AGENT_PERSIST", "1") != "0":
        store = SessionStore(default_checkpoint_dir())
        if not store.exists(session_id):
            store.append_meta(session_id, {"model": cfg.model, "permission": cfg.permission,
                                           "created": time.strftime("%Y-%m-%d %H:%M:%S")})
        else:
            resume_into(agent, store, session_id)   # 命中磁盘 id → 恢复
        agent.store = store
        agent.session_id = session_id

    # M12：按配置装配记忆/压缩
    if cfg.memory_enabled:
        from skill_agent.compaction import SummaryCompressor
        from skill_agent.config import default_memory_dir
        from skill_agent.memory import MemoryStore
        agent.memory = MemoryStore(default_memory_dir(cfg.project))
        if cfg.summary_compaction:
            agent.summary_compressor = SummaryCompressor()
            from skill_agent.session import _make_summarizer
            agent.summarize = _make_summarizer(cfg)

    session.agent = agent
    return session
