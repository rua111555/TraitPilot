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
import stat
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

    def append_prefix(self, sid: str, messages: list[dict]) -> None:
        """把「压缩后的整个保留窗口」写成一条 ``_prefix`` 锚点（M12）。

        必须是**整窗**而不是单条边界：``_read_raw`` 读到 ``_prefix`` 时执行
        ``raw = [...]`` —— 锚点**之前**追加过的行全部作废。而保留窗口里的消息是在
        锚点写入之前就逐条落盘的，只写边界那一条会把窗口的其余部分一起丢掉
        （实测：摘要式压缩后内存 14 条、resume 只回来 3 条）。

        为什么不是「重写日志文件」：append-only + fsync 是并发安全的根基
        （见模块 docstring），重写会与并发 append 相互撕裂。锚点覆盖法既保留
        append-only，又让磁盘视图在读取时收敛到压缩后的窗口。
        """
        if not self.enabled or not self._valid(sid) or not messages:
            return
        p = self.root / f"{sid}.jsonl"
        try:
            line = json.dumps({"_prefix": True, "messages": list(messages)},
                              ensure_ascii=False)
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

    # ---------------- 读 ----------------
    def load(self, sid: str) -> dict:
        """续跑视图：返回 {messages, meta, discarded}。messages 已做恢复校验。

        这里的截断是**续跑不变量**的硬要求（见 ``_analyze``），因此可能丢弃
        尾部未完成的一轮、甚至整段。仅用于 resume；**展示请用** ``load_view``，
        否则用户会看到「切换历史后内容凭空消失」。

        归档（archive/）里的会话同样可读，便于历史查看。
        """
        raw, meta, found = self._read_raw(sid)
        if not found:
            return {"messages": [], "meta": {}, "discarded": 0}
        resumable, _tail, discarded, _reason = _analyze(raw)
        return {"messages": resumable, "meta": meta, "discarded": discarded}

    def load_view(self, sid: str) -> dict:
        """展示视图（只读）：如实回放磁盘上**全部**已落盘消息，不做续跑截断。

        与 ``load()`` 的唯一区别是**不套用续跑边界**：``load`` 为了满足 Messages
        API 的续跑不变量（history 必须以「不含 tool_use 的 assistant」结尾），会把
        「尚无完成回复」的会话整段清空、把尾部未完成的一轮整轮丢弃 —— 那是续跑
        语义，用来渲染必然出现「切过去一片空白」。

        返回 {messages, meta, incomplete, resume_count, discarded, reason, found}：
          messages     全部已落盘消息（含未完成轮次），供前端完整重建
          incomplete   被续跑边界丢弃的尾部消息（前端据此标注「不会发给模型」）
          resume_count 续跑实际会恢复的条数（= 前端插入分隔条的位置）
          discarded    续跑将丢弃的条数
          reason       None | "no_completed_assistant" | "incomplete_turn"
        **严禁用于 resume。**
        """
        raw, meta, found = self._read_raw(sid)
        if not found:
            return {"messages": [], "meta": {}, "incomplete": [], "resume_count": 0,
                    "discarded": 0, "reason": None, "found": False}
        resumable, tail, discarded, reason = _analyze(raw)
        # 展示用**原始**消息（_showable 只滤掉完全空的 assistant）：不能用 resumable+tail，
        # 因为 _analyze 在清洗阶段就把「只剩悬空 tool_use 的 assistant」整条删了，
        # 那样用户会看不到「调了工具但中断了」这一事实（多轮工具会话甚至整段消失）。
        keep = _showable(raw)
        resume_count = _boundary_index(keep, _repair(raw)[0])
        return {"messages": keep, "meta": meta,
                "incomplete": keep[resume_count:],
                "resume_count": resume_count,
                "discarded": discarded, "reason": reason, "found": True}

    def _read_raw(self, sid: str) -> tuple[list[dict], dict, bool]:
        """读并解析 ``<sid>.jsonl``，**不做恢复校验**。

        返回 ``(raw_messages, meta, found)``；found=False 表示文件不存在/不可读
        （含路径校验失败）。含归档回退，语义与旧 ``load`` 中该段逐字一致。
        """
        if not self.enabled or not self._valid(sid):
            return [], {}, False
        p = self.root / f"{sid}.jsonl"
        if not p.is_file():
            p = self.archive_dir / f"{sid}.jsonl"
        if not p.is_file():
            return [], {}, False
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
                    # M12：压缩产生的边界前缀 → 作为「本轮起点」覆盖之前消息。
                    #
                    # ⚠️ 这是**覆盖点**（`raw = ...`），不是追加锚点：它让「旧消息仍在
                    # 文件里、但读的时候被丢掉」成立，于是压缩不必改写（重写日志会
                    # 破坏 append-only 的并发安全与 fsync 语义）。
                    #
                    # 两种形态：
                    #   messages=[...]  压缩后的**整个保留窗口**（现用）。必须整窗写入：
                    #                   单条形态只把边界那一条当起点，而保留窗口的其余
                    #                   消息是**在锚点之前**追加的 → 一并被覆盖丢弃
                    #                   （实测摘要式压缩后内存 14 条、resume 只回 3 条）。
                    #   message={...}   单条边界（旧形态，仅为读旧日志保留）。
                    pm = obj.get("messages")
                    if isinstance(pm, list) and pm:
                        raw = list(pm)
                    else:
                        prefix = obj.get("message")
                        if isinstance(prefix, dict) and prefix.get("role"):
                            raw = [prefix]
                elif isinstance(obj, dict) and obj.get("role"):
                    raw.append(obj)
        except OSError:
            return [], {}, False
        return raw, meta, True

    def sessions(self, archived: bool = False) -> list[dict]:
        if not self.enabled:
            return []
        out = []
        base = self.archive_dir if archived else self.root
        try:
            for p in base.glob("*.jsonl"):
                sid = p.stem
                if not self._valid(sid):
                    continue
                out.append({"id": sid, "mtime": p.stat().st_mtime,
                            "size": p.stat().st_size, "archived": bool(archived)})
        except OSError:
            return []
        return sorted(out, key=lambda d: d["mtime"], reverse=True)

    # ---------------- 归档 / 删除 ----------------
    @property
    def archive_dir(self) -> Path:
        return self.root / "archive"

    def _path(self, sid: str, archived: bool = False) -> Path:
        return (self.archive_dir if archived else self.root) / f"{sid}.jsonl"

    def is_archived(self, sid: str) -> bool:
        return self._valid(sid) and self._path(sid, True).is_file()

    def archive(self, sid: str) -> bool:
        """把会话移入 archive/ 子目录（不可见于默认列表）。返回是否发生移动。"""
        if not self.enabled or not self._valid(sid):
            return False
        src, dst = self._path(sid), self._path(sid, True)
        if not src.is_file():
            return False
        try:
            self.archive_dir.mkdir(parents=True, exist_ok=True)
            with self._lock:
                os.replace(src, dst)
            self.append_meta_at(dst, {"id": sid, "archived": True,
                                      "archived_at": time.strftime("%Y-%m-%d %H:%M:%S")})
            return True
        except OSError:
            return False

    def unarchive(self, sid: str) -> bool:
        if not self.enabled or not self._valid(sid):
            return False
        src, dst = self._path(sid, True), self._path(sid)
        if not src.is_file():
            return False
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            with self._lock:
                os.replace(src, dst)
            return True
        except OSError:
            return False

    def delete(self, sid: str) -> bool:
        """删除会话文件（archive 与根目录都尝试）。返回是否删掉了至少一个文件。

        两条要点（D13）：
        - **持 ``self._lock``**：``append`` 也持同一把锁，否则「delete 返回 True」之后
          并发的 writer 可能立刻把文件重建出来，「已删除」就成了假的。
        - **不跟随符号链接**：``<sid>.jsonl`` 若是指向外部真实文件的软链，直接
          ``unlink`` 只删链、目标内容原样留在磁盘；这里用 ``lstat`` 判定，对软链只删
          链接本身并如实返回，绝不假装「内容已从磁盘消失」。
        """
        if not self.enabled or not self._valid(sid):
            return False
        ok = False
        with self._lock:
            for p in (self._path(sid), self._path(sid, True)):
                try:
                    st = p.lstat()             # lstat：不跟随软链
                except OSError:
                    continue
                if not stat.S_ISREG(st.st_mode) and not stat.S_ISLNK(st.st_mode):
                    continue
                try:
                    p.unlink()
                    ok = True
                except OSError:
                    pass
        return ok

    def append_meta_at(self, path: Path, meta: dict) -> None:
        """在给定文件**开头**插入一行 meta（归档标记）。失败静默。"""
        try:
            old = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return
        try:
            line = json.dumps({"_meta": True, **(meta or {})}, ensure_ascii=False)
            with open(path, "w", encoding="utf-8") as f:
                f.write(line + "\n" + old)
                f.flush()
                os.fsync(f.fileno())
        except OSError:
            pass

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

    这是 ``load()``（续跑）的语义。**展示**请用 ``_analyze`` 拿到的 tail，别用这里的
    截断结果去渲染 —— 否则「尚无完成回复」的会话会整段消失（用户会看到空白）。
    """
    resumable, _tail, discarded, _reason = _analyze(messages)
    return resumable, discarded


def _analyze(messages: list[dict]) -> tuple[list[dict], list[dict], int, str | None]:
    """把 ``_repair`` 拆开：既要「可续跑前缀」，也要「被截断的尾部」。

    返回 ``(resumable, tail, discarded, reason)``：
      resumable  可安全续跑的前缀（可能为 []）——与旧 ``_repair`` 的第一个返回值逐字相同
      tail       被边界丢弃的尾部（未完成的一轮），供**展示**如实回放
      discarded  ``max(0, len(cleaned) - boundary - 1)``；boundary<0 时为 ``len(cleaned)``
      reason     None | "no_completed_assistant" | "incomplete_turn"
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
        return [], list(cleaned), len(cleaned), \
            ("no_completed_assistant" if cleaned else None)
    tail = cleaned[boundary + 1:]
    return cleaned[:boundary + 1], tail, len(tail), \
        ("incomplete_turn" if tail else None)


def _showable(messages: list[dict]) -> list[dict]:
    """展示清洗：只丢**完全空的** assistant（异常轮不渲染空气泡），其余原样保留。

    故意不剔除悬挂 tool_use —— 展示路径不喂给 Messages API，保留才能让用户看到
    「调用了工具但没有结果」，配合前端 tool-card 的「（无记录）」就是如实呈现。
    """
    return [m for m in messages
            if m.get("role") != "assistant" or _blocks(m)]


def _same_message(a: dict, b: dict) -> bool:
    """两条消息是否指同一件事（比较 role + content；_analyze 会产生新 dict）。"""
    return (a or {}).get("role") == (b or {}).get("role") \
        and (a or {}).get("content") == (b or {}).get("content")


def _same_turn(a: dict, b: dict) -> bool:
    """宽松同轮判定：``_analyze`` 会从 assistant 里剔掉悬挂的 tool_use 块，
    导致清洗后的 content 与展示用的原文不再逐字相等。此时只要 role 相同、
    且比较短的那份 content 是另一份的**子序列**，就认为指同一轮。

    没有这一层，``_boundary_index`` 会在「已完成轮里混了未答 tool_use 块」的输入上
    对不齐、一路走到兜底 ``return len(display)``，前端据此画不出「尚未完成」分隔条（D14）。
    """
    if _same_message(a, b):
        return True
    if (a or {}).get("role") != (b or {}).get("role"):
        return False
    ba, bb = _blocks(a), _blocks(b)
    if not ba or not bb:
        return ba == bb
    short, long_ = (ba, bb) if len(ba) <= len(bb) else (bb, ba)
    it = iter(long_)
    return all(any(x == y for y in it) for x in short)   # 子序列匹配


def _boundary_index(display: list[dict], strict: list[dict]) -> int:
    """在展示列表里找出「续跑前缀」的结束下标（分隔条画在这里）。

    严格视图严格是展示列表的子序列：``_analyze`` 在清洗阶段会整条删掉「只剩悬空
    tool_use 的 assistant」（展示列表保留它），所以需要按序对齐前缀，而不是拿长度
    直接相减 —— 否则分隔条会画错位置（多轮工具会话甚至会画到最前面）。

    对齐用 ``_same_turn``（宽容子序列匹配）：``_analyze`` 还会从 assistant 里剔掉单个
    悬挂 tool_use 块，逐字比较会对不上，那样兜底会把分隔条推到末尾、等于没画。

    严格视图为空 → 0（整段都属于「续跑不会带上」）。
    """
    if not strict:
        return 0
    j = 0
    for i, m in enumerate(display):
        if j < len(strict) and _same_turn(m, strict[j]):
            j += 1
            if j == len(strict):
                return i + 1
    # 对不齐时的兜底：严格前缀没能在展示序列里走完。仍取**最长的匹配前缀**所到的位置，
    # 而不是 len(display)（后者会让前端彻底画不出分隔条）。
    return _longest_prefix_index(display, strict)


def _longest_prefix_index(display: list[dict], strict: list[dict]) -> int:
    """兜底：贪心对齐能走多远就切多远；一个都对不上则退化为 0。"""
    j = 0
    last = 0
    for i, m in enumerate(display):
        if j >= len(strict):
            break
        if _same_turn(m, strict[j]):
            j += 1
            last = i + 1
    return last



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
