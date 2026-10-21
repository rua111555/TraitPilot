"""分层记忆（M12）。

模仿 Letta / MemGPT 的「core + archival」两层 + Mem0 式结构化事实：

- **core memory**（``<project>/.skill-agent/memory/core.md``）：常驻 system prompt 的
  稳定块，模型可显式编辑（像 Claude Code 的 MEMORY.md）。**刻意放在稳定前缀位置以
  不破坏 prompt caching**。
- **archival memory**（sqlite3 + FTS5）：从轨迹抽取的结构化记忆项，按需检索 top-k，
  **注入到消息尾部**（不进 system 前缀）。纯 stdlib，无向量库依赖。

并发：单文件 SQLite + WAL + 短事务；每次操作开新连接，避免跨线程共享句柄。
"""
from __future__ import annotations

import sqlite3
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

MAX_CORE_CHARS = 4000

# 拉丁词元 / 中文串（中文没有空格，FTS5 trigram 也需要 ≥3 字的连续片段）
_WORD = re.compile(r"[A-Za-z0-9_.\-/]+")
_CJK = re.compile(r"[一-鿿]+")
# 自然语言里的高频虚词，切 n-gram 时会把它们拼进去产生噪声（如「那个任」）
_STOP = frozenset("的了是在有个那这我你他和与及或就都也还很把被给对从到把")


def _tokens(query: str) -> list[str]:
    """把 query 切成可检索的词元：拉丁词原样 + 中文 3-gram/2-gram 片段。

    「继续执行阶段 S3 那个任务」→ ['继续执行阶段','继续执','续执行','执行阶','行阶段',
    'S3','那个任务','那个任','个任务', …]，这样即使目标文本里没有整段长串，
    也能靠 2–3 字片段命中（D9）。
    """
    q = query or ""
    out: list[str] = []
    for m in _WORD.finditer(q):
        out.append(m.group(0))
    for m in _CJK.finditer(q):
        s = m.group(0)
        if len(s) <= 3:
            out.append(s)
            continue
        out.append(s)                      # 整段先试（最精确）
        for n in (3, 2):                   # 再补 n-gram 片段
            for i in range(len(s) - n + 1):
                g = s[i:i + n]
                if n == 2 and (g[0] in _STOP or g[1] in _STOP):
                    continue               # 双字虚词噪声太大，跳过
                out.append(g)
    seen, uniq = set(), []
    for t in out:
        t = t.strip()
        if t and t not in seen:
            seen.add(t)
            uniq.append(t)
    return uniq


@dataclass
class MemoryItem:
    id: int
    text: str
    kind: str = "fact"
    source: str = ""
    ts: float = 0.0


class MemoryStore:
    """core（文件） + archival（FTS5）双层记忆。"""

    def __init__(self, root: Path, enabled: bool = True):
        self.root = Path(root)
        self.enabled = enabled
        self.core_path = self.root / "core.md"
        self.db_path = self.root / "archival.db"
        if self.enabled:
            try:
                self.root.mkdir(parents=True, exist_ok=True)
                self._init_db()
            except OSError:
                self.enabled = False

    # ---------------- core ----------------
    def get_core(self) -> str:
        if not self.enabled:
            return ""
        try:
            return self.core_path.read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def set_core(self, text: str) -> None:
        if not self.enabled:
            return
        text = (text or "")[-MAX_CORE_CHARS:]
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            self.core_path.write_text(text, encoding="utf-8")
        except OSError:
            pass

    # ---------------- archival ----------------
    def remember(self, text: str, kind: str = "fact", source: str = "") -> None:
        if not self.enabled or not text.strip():
            return
        try:
            con = self._connect()
            con.execute(
                "INSERT INTO mem(text, kind, source, ts) VALUES (?,?,?,?)",
                (text.strip()[:2000], kind, source, time.time()))
            con.commit()
            con.close()
        except sqlite3.Error:
            pass

    def search(self, query: str, limit: int = 5) -> list[MemoryItem]:
        """检索：优先 FTS5(trigram) 关键词；不足则 LIKE 兜底（OR 语义 + 命中数排序）。

        D9 修正：自然语言 query（「继续执行阶段 S3 那个任务」）里没有任何一段 3 字连续
        子串能在目标文本中原样出现，旧的「全部词元 AND」LIKE 兜底必然召回为空。
        现在把中文长串拆成 2 字/3 字片段、LIKE 用 OR，并按命中片段数排序。
        """
        if not self.enabled or not (query or "").strip():
            return []
        text_tokens = _tokens(query)
        fts_terms = [t for t in text_tokens if len(t) >= 3]
        rows = []
        if fts_terms:
            match = " OR ".join(dict.fromkeys(fts_terms[:8]))
            try:
                con = self._connect()
                rows = con.execute(
                    "SELECT m.id, m.text, m.kind, m.source, m.ts FROM mem_fts f "
                    "JOIN mem m ON m.id = f.rowid WHERE mem_fts MATCH ? "
                    "ORDER BY rank LIMIT ?", (match, int(limit))).fetchall()
                con.close()
            except sqlite3.Error:
                rows = []
        if len(rows) < limit:              # 不足 → LIKE(OR) 兜底并补足
            seen = {r[0] for r in rows}
            for r in self._like_search(query.strip(), limit, skip=seen):
                if len(rows) >= limit:
                    break
                rows.append(r)
        return [MemoryItem(*r) for r in rows]

    def _like_search(self, query: str, limit: int, skip: set | None = None) -> list:
        """按「命中词元数」排序的 LIKE 兜底（OR 语义，任一命中即可）。"""
        needles = _tokens(query)[:8] or [query]
        try:
            con = self._connect()
            where = " OR ".join(["text LIKE ?"] * len(needles))
            rows = con.execute(
                f"SELECT id, text, kind, source, ts FROM mem WHERE {where} "
                f"ORDER BY id DESC LIMIT ?",
                tuple(f"%{n}%" for n in needles) + (max(int(limit) * 4, 20),)).fetchall()
            con.close()
        except sqlite3.Error:
            return []
        skip = skip or set()
        scored = [(sum(1 for n in needles if n in r[1]), r) for r in rows if r[0] not in skip]
        scored.sort(key=lambda x: (-x[0], -x[1][0]))
        return [r for _s, r in scored[:limit]]

    def all_items(self, limit: int = 100) -> list[MemoryItem]:
        if not self.enabled:
            return []
        try:
            con = self._connect()
            rows = con.execute(
                "SELECT id, text, kind, source, ts FROM mem ORDER BY id DESC LIMIT ?",
                (int(limit),)).fetchall()
            con.close()
            return [MemoryItem(*r) for r in rows]
        except sqlite3.Error:
            return []

    def render_context(self, query: str, limit: int = 5) -> str:
        hits = self.search(query, limit)
        if not hits:
            return ""
        lines = [f"- {h.text}" for h in hits]
        return "## 相关历史记忆（按需参考）\n" + "\n".join(lines)

    # ---------------- 内部 ----------------
    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(str(self.db_path), timeout=5)
        con.execute("PRAGMA journal_mode=WAL")
        return con

    def _init_db(self) -> None:
        con = self._connect()
        con.execute("CREATE TABLE IF NOT EXISTS mem("
                    "id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT, "
                    "kind TEXT, source TEXT, ts REAL)")
        try:
            con.execute("CREATE VIRTUAL TABLE IF NOT EXISTS mem_fts "
                        "USING fts5(text, content='mem', content_rowid='id', "
                        "tokenize='trigram')")
            con.executescript(
                "CREATE TRIGGER IF NOT EXISTS mem_ai AFTER INSERT ON mem BEGIN "
                "  INSERT INTO mem_fts(rowid, text) VALUES (new.id, new.text); END;"
                "CREATE TRIGGER IF NOT EXISTS mem_ad AFTER DELETE ON mem BEGIN "
                "  INSERT INTO mem_fts(mem_fts, rowid, text) "
                "  VALUES('delete', old.id, old.text); END;")
        except sqlite3.Error:
            self.enabled = False   # 无 FTS5/trigram 则整层停用（不影响 core）
        con.commit()
        con.close()
