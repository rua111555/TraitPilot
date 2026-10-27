"""TraitPilot Web App — Flask + SSE。

- POST /api/chat           发起一轮对话，返回 text/event-stream（流式增量/工具事件/确认请求）
- POST /api/confirm        对某条待确认命令给出 approve / reject
- GET  /api/skills|models|config
- POST /api/clear          清空某会话上下文
"""
from __future__ import annotations

import json
import os
import queue
import threading
import time
import uuid
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_from_directory

from skill_agent.config import Config
from skill_agent.skills import SkillRegistry
from . import runstate
from .session import WebSession, build_session

STATIC_DIR = Path(__file__).resolve().parent / "static"

app = Flask(__name__, static_folder=None)

# 全局共享（run_web 里注入）
CFG_REF: dict = {"registry": None, "config": None, "sessions": {}, "lock": threading.Lock()}


def _sessions() -> dict:
    return CFG_REF["sessions"]


def _get_or_create(sid: str) -> WebSession:
    with CFG_REF["lock"]:
        s = _sessions().get(sid)
        if s is None:
            s = build_session(sid, CFG_REF["config"], CFG_REF["registry"])
            _sessions()[sid] = s
        return s


@app.get("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


@app.get("/favicon.ico")
def favicon():
    return Response("", status=204)


@app.get("/api/config")
def api_config():
    cfg = CFG_REF["config"]
    from skill_agent import __version__

    return jsonify({
        "version": __version__,
        "model": cfg.model,
        "models": _fetch_models(),
        "permission": cfg.permission,
        "project": str(cfg.project),
        "skills_dir": str(cfg.skills_dir or (cfg.project / "skills")),
        "cwd": str(cfg.cwd),
        "base_url": cfg.base_url,
        "skills_count": len(CFG_REF["registry"].names()),
    })


def _fetch_models() -> list[str]:
    cache = CFG_REF.get("models_cache")
    now = time.time()
    if cache and now - cache[1] < 60:
        return cache[0]
    ids = []
    try:
        from skill_agent.client import AnthropicClient

        client = AnthropicClient(CFG_REF["config"].base_url, CFG_REF["config"].api_key,
                                 CFG_REF["config"].model, 64)
        import requests

        r = requests.get(f"{client.base_url}/v1/models",
                         headers={"Authorization": f"Bearer {client.api_key}"}, timeout=8)
        if r.ok:
            ids = [m.get("id") for m in r.json().get("data", []) if m.get("id")]
    except Exception:
        pass
    ids = ids or ["claude-opus-4-5", "claude-sonnet-4-5", "claude-haiku-4-5"]
    CFG_REF["models_cache"] = (ids, now)
    return ids


@app.get("/api/sessions")
def api_sessions():
    """M13：列出历史会话（读 checkpoint 目录）。?archived=1 列出归档会话。

    内存里有活跃 Run 的会话附运行摘要（左栏据此显示「运行中」），
    这样用户不用切进去就知道哪个会话在跑、跑到哪一步了。
    """
    from skill_agent.config import default_checkpoint_dir
    from skill_agent.session_store import SessionStore
    archived = request.args.get("archived") in ("1", "true", "yes")
    try:
        store = SessionStore(default_checkpoint_dir())
        items = store.sessions(archived=archived)
    except Exception:
        items = []
    with CFG_REF["lock"]:
        live = dict(_sessions())
    for it in items:
        s = live.get(it.get("id"))
        r = s.last_run() if s else None
        if r is not None:
            snap = r.snapshot()
            it["running"] = snap["running"]
            it["state"] = snap["state"]
            if snap["running"]:
                it["run"] = snap["run"]
                it["elapsed"] = snap["elapsed"]
                it["progress"] = snap["progress"]
            else:
                it.pop("progress", None)
    return jsonify(items)


@app.post("/api/session/archive")
def api_session_archive():
    """归档 / 取消归档某会话。body: {session, archived}"""
    from skill_agent.config import default_checkpoint_dir
    from skill_agent.session_store import SessionStore
    data = request.get_json(silent=True) or {}
    sid = data.get("session")
    archived = bool(data.get("archived", True))
    store = SessionStore(default_checkpoint_dir())
    if not sid:
        return jsonify({"ok": False, "error": "缺少 session"}), 400
    ok = store.archive(sid) if archived else store.unarchive(sid)
    return jsonify({"ok": ok})


@app.post("/api/session/delete")
def api_session_delete():
    """永久删除某会话文件。body: {session}"""
    from skill_agent.config import default_checkpoint_dir
    from skill_agent.session_store import SessionStore
    data = request.get_json(silent=True) or {}
    sid = data.get("session")
    if not sid:
        return jsonify({"ok": False, "error": "缺少 session"}), 400
    store = SessionStore(default_checkpoint_dir())
    ok = store.delete(sid)
    return jsonify({"ok": ok})


@app.get("/api/session/<sid>")
def api_session_messages(sid):
    """读取某历史会话的原始消息（用于侧栏点击后在主区重建对话）。

    仅暴露渲染所需字段：role / type / text / thinking / name / input / content。

    用 ``load_view``（展示视图）而**不是** ``load``（续跑视图）：后者为满足 Messages API
    的续跑不变量会丢弃尾部未完成的一轮、甚至在无完成回复时整段清空 —— 拿它渲染会让
    用户「切到历史会话后一片空白」。这里如数回放磁盘内容，并通过
    ``resume_count``/``discarded``/``reason`` 告诉前端「哪一段续跑不会带上」。
    """
    from skill_agent.config import default_checkpoint_dir
    from skill_agent.session_store import SessionStore
    store = SessionStore(default_checkpoint_dir())
    if not (store.exists(sid) or store.is_archived(sid)):
        return jsonify({"ok": False, "error": "not found"}), 404
    data = store.load_view(sid)             # 已做路径安全校验
    out = []
    for m in data.get("messages", []):
        role = m.get("role")
        c = m.get("content")
        if isinstance(c, str):
            out.append({"role": role, "blocks": [{"type": "text", "text": c}]})
            continue
        blocks = []
        for b in (c or []):
            if not isinstance(b, dict):
                continue
            t = b.get("type")
            if t == "text":
                blocks.append({"type": "text", "text": b.get("text", "")})
            elif t == "thinking":
                blocks.append({"type": "thinking", "text": b.get("thinking", "")})
            elif t == "tool_use":
                blocks.append({"type": "tool_use", "id": b.get("id", ""),
                               "name": b.get("name", ""), "input": b.get("input", {})})
            elif t == "tool_result":
                content = b.get("content", "")
                if isinstance(content, list):
                    content = "".join(x.get("text", "") for x in content
                                      if isinstance(x, dict))
                blocks.append({"type": "tool_result", "tool_use_id": b.get("tool_use_id", ""),
                               "content": content, "is_error": bool(b.get("is_error"))})
        out.append({"role": role, "blocks": blocks})
    return jsonify({"ok": True, "meta": {k: v for k, v in data.get("meta", {}).items()
                                         if k != "_meta"},
                    "messages": out,
                    "resume_count": data.get("resume_count", len(out)),
                    "discarded": data.get("discarded", 0),
                    "reason": data.get("reason")})


@app.get("/api/usage")
def api_usage():
    """M1a：返回某会话本轮累计用量。"""
    sid = request.args.get("session")
    s = _sessions().get(sid) if sid else None
    if not s:
        return jsonify({"ok": False, "error": "session not found"}), 404
    return jsonify({"ok": True, "usage": s.agent.usage_bar})


@app.get("/api/skills")
def api_skills():
    reg = CFG_REF["registry"]
    out = []
    for name, s in reg.skills.items():
        out.append({
            "name": name,
            "description": s.description,
            "allowed_tools": s.allowed_tools,
            "model_invocable": s.model_invocable,
            "path": str(s.dir_path),
        })
    return jsonify(out)


@app.post("/api/clear")
def api_clear():
    data = request.get_json(silent=True) or {}
    sid = data.get("session")
    s = _sessions().get(sid) if sid else None
    if s is not None:
        if s.busy:
            return jsonify({"ok": False, "error": "会话正在运行中，无法清空"}), 409
        with CFG_REF["lock"]:
            s.agent.clear()
        s.run = None
    return jsonify({"ok": True})


@app.post("/api/confirm")
def api_confirm():
    data = request.get_json(silent=True) or {}
    sid, uid = data.get("session"), data.get("uid")
    approve = bool(data.get("approve"))
    s = _sessions().get(sid)
    if not s:
        return jsonify({"ok": False, "error": "session not found"}), 404
    ok = s.resolve_confirm(uid, approve)
    return jsonify({"ok": ok})


@app.post("/api/cancel")
def api_cancel():
    """停止某会话正在运行的轮次（协作式：置 cancelling + 拒绝所有待确认命令）。

    幂等；**不动订阅者** —— 前端可以留在流上看着这一轮收尾，而不是立刻断线。
    注意：正在执行的 Bash 无法被强杀（python 不能杀线程），要等它的内层超时返回，
    所以这之后 state 会先停在 cancelling，前端据此如实显示「正在取消…」。
    """
    data = request.get_json(silent=True) or {}
    sid = data.get("session")
    s = _sessions().get(sid) if sid else None
    if not s:
        return jsonify({"ok": False, "error": "session not found"}), 404
    ok = s.cancel(data.get("run"))
    r = s.last_run()
    return jsonify({"ok": True, "cancelled": ok,
                    "run": r.run_id if r else None,
                    "state": r.state if r else "idle"})


@app.post("/api/chat")
def api_chat():
    data = request.get_json(silent=True) or {}
    sid = data.get("session") or uuid.uuid4().hex[:12]
    message = (data.get("message") or "").strip()
    if not message:
        return jsonify({"error": "空消息"}), 400

    session = _get_or_create(sid)
    session.set_params(model=data.get("model"), permission=data.get("permission"))
    # 「判忙 + 注册 Run」在同一把锁内完成 → 两个并发 POST 恰好一个成功
    run = session.begin_run(message, prefix=_disk_prefix(sid))
    if run is None:
        cur = session.attach_run()
        # 409 不再是死胡同：把当前 run 快照一并返回，前端可直接转为订阅
        return jsonify({"error": "该会话正在运行中，请稍候",
                        "run": cur.snapshot() if cur else None}), 409

    threading.Thread(target=session.run_turn, args=(run,), daemon=True).start()
    return Response(_sse_for(session, run), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache",
                             "X-Accel-Buffering": "no"})


@app.get("/api/session/<sid>/run")
def api_session_run(sid):
    """一轮运行的进度快照 —— 回答「这个会话到底在不在跑、在跑什么」。

    没有活跃 Run 时返回 running:false（并附最近一轮的收尾信息，若有）。
    """
    s = _sessions().get(sid)
    if s is None:
        return jsonify({"ok": True, "running": False, "run": None, "session": sid})
    r = s.last_run()
    if r is None:
        return jsonify({"ok": True, "running": False, "run": None, "session": sid})
    snap = r.snapshot()
    snap["ok"] = True
    return jsonify(snap)


@app.get("/api/session/<sid>/stream")
def api_session_stream(sid):
    """附着到一个正在跑的 Run 上（切回来 / 断线重连的唯一入口）。

    ``after`` 为本轮游标：从该 seq 之后开始回放，再续实时。非破坏式、可多订阅者。
    """
    s = _sessions().get(sid)
    run = s.attach_run() if s else None
    if run is None:
        r_last = s.last_run() if s else None
        if r_last is not None:
            # 轮次已结束：告知终态，让前端退回读磁盘
            return jsonify({"ok": True, "running": False, "run": r_last.run_id,
                            "state": r_last.state, "reason": "finished"})
        return jsonify({"ok": False, "running": False, "error": "no run"}), 404
    try:
        after = int(request.args.get("after") or 0)
    except ValueError:
        after = 0
    return Response(_sse_for(s, run, after=after), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache",
                             "X-Accel-Buffering": "no"})


def _disk_prefix(sid: str) -> int:
    """本轮开始前该会话磁盘上已完成的消息条数（供前端拼接，避免重复渲染）。"""
    try:
        from skill_agent.config import default_checkpoint_dir
        from skill_agent.session_store import SessionStore
        store = SessionStore(default_checkpoint_dir())
        if not store.exists(sid):
            return 0
        return int(store.load_view(sid).get("resume_count") or 0)
    except Exception:
        return 0


def _sse_for(session, run, after: int = 0):
    """把某个 Run 的事件日志变成 SSE 流。

    关键不变量：**断开只退订**。既不取消 run，也不改任何状态 —— 改造前正是在这里
    无条件 cancel + 复位 busy，导致「关掉页面 = 任务被掐」与「busy 说谎」。
    """
    def fmt(ev: dict) -> str:
        etype = ev.get("type", "message")
        return f"event: {etype}\ndata: {json.dumps(ev, ensure_ascii=False)}\n\n"

    sub = run.subscribe(after=after)
    try:
        snap = run.snapshot()
        yield fmt({"type": "attach", **snap})
        if run.truncated_before > after:
            yield fmt({"type": "resync", "truncated_before": run.truncated_before,
                       **snap})
        while True:
            ev = sub.next_event(timeout=15.0)
            if ev is None:
                if run.state in runstate.TERMINAL:
                    break
                yield ": keepalive\n\n"
                continue
            if ev.get("type") == "__end__":
                # 终结帧：只发给「跟到末尾」的订阅者，回放旧日志时不误触发
                if run.state in runstate.TERMINAL:
                    yield fmt({"type": "end", "run": run.run_id, "state": run.state})
                    break
                continue
            yield fmt(ev)
            if run.state in runstate.TERMINAL and sub.cursor >= run.last_seq:
                yield fmt({"type": "end", "run": run.run_id, "state": run.state})
                break
    except GeneratorExit:
        pass
    finally:
        # ← 断开只做这一件事。run 继续跑，busy 仍为真，事件照常记入日志。
        run.unsubscribe(sub)


def create_app(cfg: Config, registry: SkillRegistry) -> Flask:
    CFG_REF["config"] = cfg
    CFG_REF["registry"] = registry
    CFG_REF["sessions"] = {}
    return app
