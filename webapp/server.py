"""SkillAgent Web App — Flask + SSE。

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
    return jsonify({
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
    """M13：列出历史会话（读 checkpoint 目录）。?archived=1 列出归档会话。"""
    from skill_agent.config import default_checkpoint_dir
    from skill_agent.session_store import SessionStore
    archived = request.args.get("archived") in ("1", "true", "yes")
    try:
        store = SessionStore(default_checkpoint_dir())
        items = store.sessions(archived=archived)
    except Exception:
        items = []
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
    if sid and sid in _sessions():
        with CFG_REF["lock"]:
            _sessions()[sid].agent.clear()
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
    """停止某会话正在运行的轮次（协作式：置 cancel_event 并拒绝所有待确认命令）。"""
    data = request.get_json(silent=True) or {}
    sid = data.get("session")
    s = _sessions().get(sid) if sid else None
    if not s:
        return jsonify({"ok": False, "error": "session not found"}), 404
    s.cancel()
    return jsonify({"ok": True})


@app.post("/api/chat")
def api_chat():
    data = request.get_json(silent=True) or {}
    sid = data.get("session") or uuid.uuid4().hex[:12]
    message = (data.get("message") or "").strip()
    if not message:
        return jsonify({"error": "空消息"}), 400

    session = _get_or_create(sid)
    if session.busy:
        return jsonify({"error": "该会话正在运行中，请稍候"}), 409

    session.set_params(model=data.get("model"), permission=data.get("permission"))
    threading.Thread(target=session.run, args=(message,), daemon=True).start()
    return Response(_sse_stream(session), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache",
                             "X-Accel-Buffering": "no"})


def _sse_stream(session: WebSession):
    """读取会话事件队列，拼为 SSE；客户端断开时取消会话。"""
    def fmt(ev: dict) -> str:
        etype = ev.get("type", "message")
        return f"event: {etype}\ndata: {json.dumps(ev, ensure_ascii=False)}\n\n"

    stopped_normally = False
    # 全局耐心等待：模型思考期（首次输出可能 >8s，opus 尤甚）不设「首个事件必须快」，
    # 队列空时只发心跳继续等；用总时长上限兜底避免流永挂。
    MAX_STREAM_WAIT = 3600
    waited = 0
    try:
        while True:
            try:
                ev = session.event_queue.get(timeout=15)
            except queue.Empty:
                waited += 15
                if waited > MAX_STREAM_WAIT:
                    break
                yield ": keepalive\n\n"
                continue
            if ev.get("type") == "__end__":
                stopped_normally = True
                break
            yield fmt(ev)
    except GeneratorExit:
        stopped_normally = False
    finally:
        # 正常结束不取消；客户端中途断开才取消（回复待确认命令、终止任务）
        if not stopped_normally:
            session.cancel()
        session.busy = False


def create_app(cfg: Config, registry: SkillRegistry) -> Flask:
    CFG_REF["config"] = cfg
    CFG_REF["registry"] = registry
    CFG_REF["sessions"] = {}
    return app
