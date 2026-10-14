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
        """检索：优先 FTS5(trigram) 关键词；短查询回退 LIKE；出错一律返回空。"""
        if not self.enabled or not (query or "").strip():
            return []
        terms = [t for t in re.findall(r"[\w一-龥]+", query) if len(t) >= 3]
        rows = []
        if terms:
            match = " OR ".join(dict.fromkeys(terms[:8]))
            try:
                con = self._connect()
                rows = con.execute(
                    "SELECT m.id, m.text, m.kind, m.source, m.ts FROM mem_fts f "
                    "JOIN mem m ON m.id = f.rowid WHERE mem_fts MATCH ? "
                    "ORDER BY rank LIMIT ?", (match, int(limit))).fetchall()
                con.close()
            except sqlite3.Error:
                rows = []
        if not rows:                       # 短查询/无命中 → LIKE 兜底（2 字中文等）
            rows = self._like_search(query.strip(), limit)
        return [MemoryItem(*r) for r in rows]

    def _like_search(self, query: str, limit: int) -> list:
        needles = [t for t in re.findall(r"[\w一-龥]+", query) if len(t) >= 2][:4] or [query]
        try:
            con = self._connect()
            where = " AND ".join(["text LIKE ?"] * len(needles))
            rows = con.execute(
                f"SELECT id, text, kind, source, ts FROM mem WHERE {where} "
                f"ORDER BY id DESC LIMIT ?",
                tuple(f"%{n}%" for n in needles) + (int(limit),)).fetchall()
            con.close()
            return rows
        except sqlite3.Error:
            return []

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
