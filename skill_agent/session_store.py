"""会话持久化与恢复（M13）。

为纯内存的 ``Agent.history`` 增加消息级 append-only 落盘与崩溃恢复：

- 每会话一个 ``<dir>/<sid>.jsonl``：首行 meta（model/permission/created），其后逐条消息。
- **恢复校验（关键）**：加载时若最后一条 assistant 带 tool_use 而缺对应 tool_result，
  剔除该轮 assistant 里**孤立的 tool_use 块**（保留 text/thinking，维持 ThinkingBlock
  须以 assistant 消息开头的约束），否则 Messages API 会因悬挂 tool_use 直接 400。
- 路径安全：``load``/``list`` 校验 sid 不越出存储目录（防 ``../`` 穿越读任意文件）。
- 并发：追加带进程内锁 + fsync；写入失败静默（持久化是旁路，绝不拖垮主循环）。
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path

_SAFE_SID = re.compile(r"^[A-Za-z0-9_\-]+$")


class SessionStore:
    def __init__(self, root: Path, enabled: bool = True):
        self.root = Path(root)
        self.enabled = enabled
        self._lock = threading.Lock()
        if self.enabled:
            try:
                self.root.mkdir(parents=True, exist_ok=True)
            except OSError:
                self.enabled = False

    # ---------------- 写 ----------------
    def append(self, sid: str, message: dict) -> None:
        if not self.enabled or not self._valid(sid):
            return
        p = self.root / f"{sid}.jsonl"
        try:
            line = json.dumps(message, ensure_ascii=False)
        except (TypeError, ValueError):
            return
        with self._lock:
            try:
                with open(p, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
                    f.flush()
                    os.fsync(f.fileno())
            except OSError:
                pass

    def append_meta(self, sid: str, meta: dict) -> None:
        """写首行 meta（若尚无）。"""
        if not self.enabled or not self._valid(sid):
            return
        p = self.root / f"{sid}.jsonl"
        if p.exists():
            return
        self.append(sid, {"_meta": True, **(meta or {})})

    # ---------------- 读 ----------------
    def load(self, sid: str) -> dict:
        """返回 {messages, meta, discarded}。messages 已做恢复校验。"""
        if not self.enabled or not self._valid(sid):
            return {"messages": [], "meta": {}, "discarded": 0}
        p = self.root / f"{sid}.jsonl"
        if not p.is_file():
            return {"messages": [], "meta": {}, "discarded": 0}
        raw: list[dict] = []
        meta: dict = {}
        try:
            for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue   # 容忍尾行损坏
                if isinstance(obj, dict) and obj.get("_meta"):
                    meta = obj
                elif isinstance(obj, dict) and obj.get("_merge"):
                    # pause_turn 合并续推：并入上一条 assistant
                    if raw and raw[-1].get("role") == "assistant":
                        raw[-1]["content"] = list(raw[-1].get("content") or []) + \
                            list(obj.get("content") or [])
                elif isinstance(obj, dict) and obj.get("_prefix"):
                    # M12：压缩产生的边界前缀 → 作为「本轮起点」覆盖之前消息
                    prefix = obj.get("message")
                    if isinstance(prefix, dict) and prefix.get("role"):
                        raw = [prefix]
                elif isinstance(obj, dict) and obj.get("role"):
                    raw.append(obj)
        except OSError:
            return {"messages": [], "meta": {}, "discarded": 0}
        messages, discarded = _repair(raw)
        return {"messages": messages, "meta": meta, "discarded": discarded}

    def sessions(self) -> list[dict]:
        if not self.enabled:
            return []
        out = []
        try:
            for p in self.root.glob("*.jsonl"):
                sid = p.stem
                if not self._valid(sid):
                    continue
                out.append({"id": sid, "mtime": p.stat().st_mtime,
                            "size": p.stat().st_size})
        except OSError:
            return []
        return sorted(out, key=lambda d: d["mtime"], reverse=True)

    def exists(self, sid: str) -> bool:
        return self._valid(sid) and (self.root / f"{sid}.jsonl").is_file()

    # ---------------- 内部 ----------------
    def _valid(self, sid: str) -> bool:
        return bool(sid) and bool(_SAFE_SID.fullmatch(str(sid)))


def _repair(messages: list[dict]) -> tuple[list[dict], int]:
    """恢复校验：修复悬挂 tool_use 并截断到「可安全续跑的边界」。

    续跑不变量：恢复后的 history 必须**以「不含 tool_use 的 assistant」结尾**（或为空），
    这样下一次 ``answer()`` 追加的新 user 才不会造成「连续两条 user」，且不会有悬空的
    tool_use（否则 Messages API 400）。因此：
      1. 剔除所有「无对应 tool_result」的孤立 tool_use 块；
      2. 从尾部回退，丢弃最后一个「最终回答 assistant」之后的所有消息（未完成的一轮）。
    返回 (修复后消息, 丢弃的进行中轮次数)。
    """
    answered: set[str] = set()
    for m in messages:
        if m.get("role") != "user":
            continue
        for b in _blocks(m):
            if b.get("type") == "tool_result" and b.get("tool_use_id"):
                answered.add(b["tool_use_id"])

    # 1) 剔除孤立 tool_use
    cleaned: list[dict] = []
    for m in messages:
        if m.get("role") != "assistant":
            cleaned.append(m)
            continue
        blocks = _blocks(m)
        if not any(b.get("type") == "tool_use" for b in blocks):
            cleaned.append(m)
            continue
        kept = [b for b in blocks
                if b.get("type") != "tool_use" or b.get("id") in answered]
        has_content = any(b.get("type") != "text" or b.get("text") for b in kept)
        if has_content:
            cleaned.append({"role": "assistant", "content": kept})
        # 全空 → 丢弃

    # 2) 截断到最后一个「不含 tool_use 的 assistant」（可安全续跑的边界）
    boundary = -1
    for i, m in enumerate(cleaned):
        if m.get("role") == "assistant" and not _has_tool_use(m):
            boundary = i
    if boundary < 0:
        return [], len(cleaned)
    return cleaned[:boundary + 1], max(0, len(cleaned) - boundary - 1)



def _blocks(m: dict) -> list[dict]:
    c = m.get("content")
    if isinstance(c, list):
        return [b for b in c if isinstance(b, dict)]
    if isinstance(c, str) and c:
        return [{"type": "text", "text": c}]
    return []


def _has_tool_use(m: dict) -> bool:
    return any(b.get("type") == "tool_use" for b in _blocks(m))


def new_session_id() -> str:
    return time.strftime("%Y%m%dT%H%M%S") + "-" + os.urandom(2).hex()
