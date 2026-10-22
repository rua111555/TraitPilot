"""WebAgent 会话：承载一次对某用户的对话，桥接线程中的 agent 运行与 SSE 事件流。"""
from __future__ import annotations

import os
import queue
import threading
import time
import uuid

from skill_agent.config import default_checkpoint_dir

# 确认请求在无人应答时的等待上限：超时自动拒绝。
# 没有它，断线后无人能回答的 confirm 会让 agent 线程**永久挂住**（改造前确有此事）。
CONFIRM_TIMEOUT = int(os.environ.get("SKILL_AGENT_CONFIRM_TIMEOUT", "300"))


class Hooks:
    """从 agent 线程回调到 WebSession 的桥（文本增量 / 工具事件 / 命令确认）。"""

    def __init__(self, session: "WebSession"):
        self.session = session

    @property
    def cancelled(self) -> bool:
        return self.session.cancel_event.is_set()

    def emit(self, ev: dict) -> None:
        r = self.session.attach_run() or self.session.last_run()
        if r is not None:
            r.emit(ev)

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
        self.cancel_event = threading.Event()
        self._confirms: dict[str, threading.Event] = {}
        self._answers: dict[str, bool] = {}
        self.created_at = time.time()
        self.last_model = cfg.model
        self.last_permission = cfg.permission
        self.resume_info: dict = {}          # M13：resume 的 {restored, discarded, meta}
        # 一轮运行升格为对象：Run 拥有 agent 线程与事件日志；HTTP 只是订阅者
        self.run = None                      # 当前唯一的非终态 Run（可能为 None）
        self._turn = 0
        self._run_lock = threading.Lock()    # 保护 run 的注册/交接（临界区内不做 I/O）

    @property
    def busy(self) -> bool:
        """只读派生：「有非终态 Run」= 真在跑。全仓无任何地方能对它赋值。"""
        r = self.run
        return bool(r is not None and r.running)

    def begin_run(self, message: str, prefix: int = 0) -> "Run | None":
        """在锁内一次性完成「判忙 + 注册 Run」，把 409 的竞争窗口关死。

        返回 None 表示该会话已有非终态 Run（调用方应回 409 并附当前 run 快照）。
        """
        from .runstate import Run
        with self._run_lock:
            if self.run is not None and self.run.running:
                return None
            self._turn += 1
            r = Run(self.id, message, turn_index=self._turn)
            r.prefix = prefix
            self.run = r
            self.cancel_event = r.cancel_event   # 线程与我们共用同一个取消事件
            return r

    def attach_run(self):
        """取当前仍在跑的 Run（供切回来/断线重连订阅）。没有则 None。"""
        r = self.run
        return r if (r is not None and r.running) else None

    def last_run(self):
        """取最近一轮 Run（含已终态的，供展示收尾信息）。"""
        return self.run

    # ---------- 参数 ----------
    def set_params(self, model: str | None = None, permission: str | None = None) -> None:
        if model and model != self.cfg.model:
            self.cfg.model = model
            if self.agent is not None:
                self.agent.client.model = model
        if permission and permission != self.cfg.permission:
            self.cfg.permission = permission
            if self.tools is not None:
                self.tools.permission = permission

    def cancel(self, run_id: str | None = None) -> bool:
        """请求取消当前轮次。幂等。不改订阅者（前端可留在流上看收尾）。"""
        r = self.run
        if r is None:
            return False
        if run_id and r.run_id != run_id:
            return False
        r.mark_cancelling()
        for uid, ev in list(self._confirms.items()):
            self._answers[uid] = False
            ev.set()
        return True

    def cancelled(self) -> bool:
        return self.cancel_event.is_set()

    # ---------- 确认桥 ----------
    def ask_confirm(self, command: str, reason: str) -> bool:
        """把确认请求同时放进事件日志（可回放）与待确认表（可查询）。"""
        uid = uuid.uuid4().hex[:8]
        ev = threading.Event()
        self._confirms[uid] = ev
        r = self.run
        if r is not None:
            with r._lock:
                r.pending_confirms[uid] = {"command": command, "reason": reason}
            r.emit({"type": "confirm", "uid": uid, "command": command, "reason": reason})
        # 无人能回答时不该永久挂住线程：超时自动拒绝，并让用户看得见
        answered = ev.wait(timeout=CONFIRM_TIMEOUT)
        if not answered:
            if r is not None:
                r.emit({"type": "notice",
                        "message": f"确认请求超时（{CONFIRM_TIMEOUT}s）无人应答，已自动拒绝："
                                   f"{command[:120]}"})
        self._confirms.pop(uid, None)
        if r is not None:
            with r._lock:
                r.pending_confirms.pop(uid, None)
        return self._answers.pop(uid, False)

    def resolve_confirm(self, uid: str, approve: bool) -> bool:
        ev = self._confirms.get(uid)
        if ev is None:
            return False
        self._answers[uid] = approve
        ev.set()
        return True

    # ---------- 运行 ----------
    def run_turn(self, run) -> None:
        """agent 线程入口：Run 由本线程独占拥有，只有这里能把它推向终态。

        事件写进 run 的日志（无人订阅也不丢）；HTTP 断开只是退订，不碰这里。
        """
        self.cancel_event = run.cancel_event
        try:
            final = self.agent.answer(
                run.prompt,
                on_delta=lambda t: run.emit({"type": "text", "delta": t}),
                on_thinking=lambda t: run.emit({"type": "thinking", "delta": t}),
            )
            st = "cancelled" if run.cancel_event.is_set() else "done"
            run.finish(st, final=final, result={
                "messages": len(self.agent.history),
                "active": self.agent.active_skills,
                "usage": self.agent.usage_bar,
                "stop_reason": getattr(self.agent._last_stop, "reason_code", None),
                "resume_discarded": int(self.resume_info.get("discarded", 0) or 0),
            })
            run.emit({"type": "done", "final": final,
                      "messages": len(self.agent.history),
                      "active": self.agent.active_skills,
                      "usage": self.agent.usage_bar,
                      "stop_reason": getattr(self.agent._last_stop, "reason_code", None),
                      "resume_discarded": int(self.resume_info.get("discarded", 0) or 0),
                      "cancelled": st == "cancelled"})
            self.resume_info = {}            # 只提示一次
        except Exception as e:  # noqa: BLE001
            msg = f"{type(e).__name__}: {e}"
            run.finish("error", error=msg)
            run.emit({"type": "error", "message": msg})
        finally:
            run.emit({"type": "__end__"})
            self.cancel_event = threading.Event()   # 复位，供下一轮使用

    # 兼容旧调用名（改造前叫 run，接收裸字符串）
    def run(self, user_text: str) -> None:
        r = self.attach_run()
        if r is None:
            r = self.begin_run(user_text)
        if r is None:
            return
        self.run_turn(r)


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
            # 命中磁盘 id → 恢复。resume 走**严格** load：尾部未完成的一轮会被丢弃
            # （续跑不变量要求 history 以「不含 tool_use 的 assistant」结尾）。
            # 记下丢弃数，稍后透给前端——否则用户只会觉得「历史莫名其妙少了」。
            session.resume_info = resume_into(agent, store, session_id)
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
