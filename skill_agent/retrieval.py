"""代码/文档检索（#11）。

纯 stdlib 的 FTS5 检索层，复用 ``memory.py`` 已验证的配方（外内容 FTS5 表 +
``tokenize='trigram'`` + WAL + ``ORDER BY rank`` + LIKE 兜底 + FTS5 缺失自停用）：

- **Search 工具**：按 `(name, 规范化 args)` 对 `chunks` 做 BM25 相关性检索，返回
  「文件:起止行 + 片段」，模型再 ``Read(offset, limit)`` 精读——不整篇回灌。
- **RepoMap 工具**：给目录/文件的结构化大纲（Python 类/函数/方法、Markdown 标题）。

分块器是纯函数（``str -> list[Chunk]``）：Python 用 stdlib ``ast``，Markdown 按标题、
Shell 按空行/注释块。**不引入 tree-sitter / ripgrep / 向量库**。

安全核心：**绝不整仓遍历**——只从显式语料根 ``os.walk``，就地剪枝排除目录、读前按
大小拒绝，确保不会 stat 到仓库里 24.5GB 的 ML 权重。

并发：单文件 SQLite + WAL + 短事务；每次操作开新连接（适配 threaded Flask）。
"""
from __future__ import annotations

import ast
import os
import re
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

SCHEMA_VERSION = 1
MAX_CHUNK_LINES = 120
MAX_CHUNK_CHARS = 6000
SNIPPET_CHARS = 400
DEFAULT_MAX_FILE_BYTES = 2_000_000
DEFAULT_MAX_FILES = 20_000

# 语料只取这几种扩展（其余一律不索引）
_INDEX_EXTS = {".py", ".md", ".sh", ".markdown"}
# 单文件大小上限（读前 stat 拒绝）——防止误入权重/大二进制
# 目录名就地剪枝（不进入、不 stat）
_EXCLUDE_DIRS = {"__pycache__", ".git", ".skill-agent", ".pytest_cache",
                 "node_modules", "saved", "venv", ".venv", "dist", "build",
                 "site-packages", ".mypy_cache", ".ruff_cache", ".idea", ".vscode"}
# 显式后缀拒绝（双保险，防止扩展名伪装）
_EXCLUDE_SUFFIXES = (".pth", ".pt", ".ckpt", ".onnx", ".h5", ".png", ".jpg",
                     ".jpeg", ".gif", ".mp4", ".mov", ".avi", ".zip", ".tar",
                     ".gz", ".tgz", ".bz2", ".xz", ".pyc", ".so", ".o", ".a",
                     ".bin", ".npy", ".npz", ".parquet", ".bam", ".cram",
                     ".vcf", ".log", ".lock")

_TERM_RE = re.compile(r"[\w一-鿿]+")


@dataclass
class Chunk:
    path: str
    start: int          # 1-based 首行
    end: int            # 1-based 末行（含）
    text: str
    kind: str           # py_func|py_class|py_module|md_section|sh_block|window
    symbol: str = ""


@dataclass
class Hit:
    path: str
    start: int
    end: int
    score: float = 0.0
    snippet: str = ""
    symbol: str = ""
    kind: str = ""


@dataclass
class Symbol:
    name: str
    kind: str           # class|function|method|heading
    start: int
    end: int
    depth: int = 0


# ======================================================================
# 分块器（纯函数，可脱离 DB 单测）
# ======================================================================

def _windows(lines: list[str], step: int = MAX_CHUNK_LINES,
             overlap: int = 0) -> list[tuple[int, int]]:
    """把 [0, len(lines)) 按 step 行切窗（可带 overlap），返回 0-based 半开区间列表。"""
    out = []
    n = len(lines)
    i = 0
    while i < n:
        j = min(i + step, n)
        out.append((i, j))
        if j >= n:
            break
        i = j - overlap if overlap else j
    return out


def chunk_python(source: str, path: str) -> list[Chunk]:
    """用 stdlib ``ast`` 按函数/类/方法切分；语法错误时退化为行窗口。"""
    lines = source.splitlines()
    if not lines:
        return []
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return [Chunk(path, s + 1, e, "\n".join(lines[s:e])[:MAX_CHUNK_CHARS],
                      "window") for s, e in _windows(lines)]

    covered = [False] * len(lines)   # 0-based 行是否已归入某 chunk
    chunks: list[Chunk] = []

    def spans(node) -> tuple[int, int]:
        s = getattr(node, "lineno", 1) - 1
        e = getattr(node, "end_lineno", s + 1)
        return s, e

    def add(name: str, kind: str, s: int, e: int, sym: str) -> None:
        e = min(e, len(lines))
        if s >= e:
            return
        for k in range(s, e):
            covered[k] = True
        chunks.append(Chunk(path, s + 1, e,
                            "\n".join(lines[s:e])[:MAX_CHUNK_CHARS], kind, sym))

    # 模块 header：docstring + import 段（到第一个顶层 def/class 之前）
    body = list(getattr(tree, "body", []))
    first_def = next((n for n in body if isinstance(
        n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))), None)
    header_end = spans(first_def)[0] if first_def is not None else len(lines)
    if header_end > 0:
        add("<module>", "py_module", 0, header_end, "<module>")

    def walk(node, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                s, e = spans(child)
                add(child.name, "py_class", s, e, prefix + child.name)
                walk(child, prefix + child.name + ".")
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                s, e = spans(child)
                kind = "py_func"
                add(child.name, kind, s, e, prefix + child.name)
                # 函数内嵌套定义（多为闭包）：单独成块，便于检索
                walk(child, prefix + child.name + ".")

    walk(tree, "")

    # 未覆盖的行（装饰器空隙、模块尾部等）→ 行窗口补块，保证无遗漏
    gap_start = None
    for i, ok in enumerate(covered + [True]):
        if not ok and gap_start is None:
            gap_start = i
        elif ok and gap_start is not None:
            for s, e in _windows(lines[gap_start:i]):
                chunks.append(Chunk(path, gap_start + s + 1, gap_start + e,
                                    "\n".join(lines[gap_start + s:gap_start + e])[:MAX_CHUNK_CHARS],
                                    "window"))
            gap_start = None
    chunks.sort(key=lambda c: (c.start, c.end))
    return chunks


def chunk_markdown(source: str, path: str) -> list[Chunk]:
    """按 ATX 标题切分；````` ````` 围栏内的 ``#`` 不算标题。"""
    lines = source.splitlines()
    if not lines:
        return []
    heads: list[tuple[int, str]] = []   # (0-based 行号, 标题文本)
    in_fence = False
    fence_re = re.compile(r"^\s*(```|~~~)")
    for i, ln in enumerate(lines):
        if fence_re.match(ln):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = re.match(r"^(#{1,6})\s+(.*)$", ln)
        if m:
            heads.append((i, m.group(2).strip()))

    chunks: list[Chunk] = []
    if not heads:
        return [Chunk(path, s + 1, e, "\n".join(lines[s:e])[:MAX_CHUNK_CHARS],
                      "window") for s, e in _windows(lines)]

    if heads[0][0] > 0:      # 首个标题之前的引言
        chunks.append(Chunk(path, 1, heads[0][0],
                            "\n".join(lines[:heads[0][0]])[:MAX_CHUNK_CHARS],
                            "md_section", "<intro>"))
    for idx, (start, title) in enumerate(heads):
        end = heads[idx + 1][0] if idx + 1 < len(heads) else len(lines)
        if end - start <= MAX_CHUNK_LINES:
            chunks.append(Chunk(path, start + 1, end,
                                "\n".join(lines[start:end])[:MAX_CHUNK_CHARS],
                                "md_section", title))
        else:                # 超长章节 → 段落窗口（每片带标题行便于识别）
            for s, e in _windows(lines[start:end]):
                seg = lines[start:end][s:e]
                text = ("\n".join([lines[start]] + seg))[:MAX_CHUNK_CHARS]
                chunks.append(Chunk(path, start + s + 1, start + e, text,
                                    "md_section", title))
    return chunks


def chunk_shell(source: str, path: str) -> list[Chunk]:
    """按「连续块」划分：空行或 leading 注释作为块边界。"""
    lines = source.splitlines()
    if not lines:
        return []
    chunks: list[Chunk] = []
    start = 0
    for i in range(1, len(lines)):
        if lines[i].strip() == "" and lines[i - 1].strip() == "" and i > start:
            chunks.append(_sh_chunk(path, lines, start, i))
            start = i
    if start < len(lines):
        chunks.append(_sh_chunk(path, lines, start, len(lines)))
    return [c for c in chunks if c.end > c.start]


def _sh_chunk(path: str, lines: list[str], start: int, end: int) -> Chunk:
    if end - start > MAX_CHUNK_LINES:
        end = start + MAX_CHUNK_LINES
    sym = ""
    for ln in lines[start:end]:
        s = ln.strip()
        if s.startswith("#") and not s.startswith("#!"):
            sym = s.lstrip("#").strip()
            break
    return Chunk(path, start + 1, end, "\n".join(lines[start:end])[:MAX_CHUNK_CHARS],
                 "sh_block", sym)


def chunk_file(path: str, source: str, ext: str) -> list[Chunk]:
    """按扩展名分派分块器。"""
    if ext == ".py":
        return chunk_python(source, path)
    if ext in (".md", ".markdown"):
        return chunk_markdown(source, path)
    if ext == ".sh":
        return chunk_shell(source, path)
    return [Chunk(path, s + 1, e, "\n".join(source.splitlines()[s:e])[:MAX_CHUNK_CHARS],
                  "window") for s, e in _windows(source.splitlines())]


# ======================================================================
# 结构大纲（RepoMap 用；轻量，直接解析源码，不查库）
# ======================================================================

def outline_python(source: str) -> list[Symbol]:
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return []
    out: list[Symbol] = []

    def walk(node, depth: int) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                out.append(Symbol(child.name, "class",
                                  child.lineno, getattr(child, "end_lineno", child.lineno), depth))
                walk(child, depth + 1)
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                kind = "method" if depth > 0 else "function"
                out.append(Symbol(child.name, kind,
                                  child.lineno, getattr(child, "end_lineno", child.lineno), depth))
                walk(child, depth + 1)
    walk(tree, 0)
    return out


def outline_markdown(source: str) -> list[Symbol]:
    out: list[Symbol] = []
    in_fence = False
    fence_re = re.compile(r"^\s*(```|~~~)")
    lines = source.splitlines()
    heads: list[tuple[int, int, str]] = []   # (0-based, depth, title)
    for i, ln in enumerate(lines):
        if fence_re.match(ln):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = re.match(r"^(#{1,6})\s+(.*)$", ln)
        if m:
            heads.append((i, len(m.group(1)), m.group(2).strip()))
    for idx, (i, depth, title) in enumerate(heads):
        end = heads[idx + 1][0] if idx + 1 < len(heads) else len(lines)
        out.append(Symbol(title, "heading", i + 1, max(end, i + 1), depth - 1))
    return out


def outline_file(source: str, ext: str) -> list[Symbol]:
    if ext == ".py":
        return outline_python(source)
    if ext in (".md", ".markdown"):
        return outline_markdown(source)
    return []


# ======================================================================
# 索引
# ======================================================================

class CodeIndex:
    """FTS5 增量分块索引；FTS5 缺失/目录不可写时自停用（不抛）。"""

    def __init__(self, root: Path, *, enabled: bool = True,
                 include_dirs: list[str] | None = None,
                 extra_excludes: list[str] | None = None,
                 max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
                 max_files: int = DEFAULT_MAX_FILES):
        self.root = Path(root)
        self.db_path = self.root / "code.db"
        self.enabled = enabled
        self.include_dirs = [Path(p) for p in include_dirs] if include_dirs else None
        self._extra_excludes = set(extra_excludes or [])
        self.max_file_bytes = max_file_bytes
        self.max_files = max_files
        self._built = False
        if not enabled:
            return
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            self._init_db()
        except (OSError, sqlite3.Error):
            self.enabled = False

    # ---------------- 连接 / schema ----------------

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(str(self.db_path), timeout=5)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=NORMAL")
        con.execute("PRAGMA busy_timeout=5000")
        con.execute("PRAGMA foreign_keys=ON")
        return con

    def _init_db(self) -> None:
        con = self._connect()
        try:
            cur = con.execute("CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT)")
            row = cur.execute("SELECT v FROM meta WHERE k='schema'").fetchone()
            if row is not None and str(row[0]) != str(SCHEMA_VERSION):
                # 版本不符 → 整库重建
                for t in ("chunks_fts", "chunks", "files", "meta"):
                    con.execute(f"DROP TABLE IF EXISTS {t}")
            con.execute("CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT)")
            con.execute("CREATE TABLE IF NOT EXISTS files("
                        "id INTEGER PRIMARY KEY AUTOINCREMENT, path TEXT UNIQUE NOT NULL,"
                        "mtime REAL NOT NULL, size INTEGER NOT NULL,"
                        "nchunks INTEGER NOT NULL DEFAULT 0, indexed_ts REAL NOT NULL)")
            con.execute("CREATE TABLE IF NOT EXISTS chunks("
                        "id INTEGER PRIMARY KEY AUTOINCREMENT,"
                        "file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,"
                        "path TEXT NOT NULL, start INTEGER NOT NULL, end INTEGER NOT NULL,"
                        "kind TEXT NOT NULL, symbol TEXT DEFAULT '', text TEXT NOT NULL)")
            con.execute("CREATE INDEX IF NOT EXISTS chunks_file ON chunks(file_id)")
            con.execute("CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5("
                        "text, symbol, content='chunks', content_rowid='id', tokenize='trigram')")
            con.executescript(
                "CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN "
                "  INSERT INTO chunks_fts(rowid, text, symbol) "
                "  VALUES (new.id, new.text, new.symbol); END;"
                "CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN "
                "  INSERT INTO chunks_fts(chunks_fts, rowid, text, symbol) "
                "  VALUES('delete', old.id, old.text, old.symbol); END;"
                "CREATE TRIGGER IF NOT EXISTS chunks_au AFTER UPDATE ON chunks BEGIN "
                "  INSERT INTO chunks_fts(chunks_fts, rowid, text, symbol) "
                "  VALUES('delete', old.id, old.text, old.symbol);"
                "  INSERT INTO chunks_fts(rowid, text, symbol) "
                "  VALUES (new.id, new.text, new.symbol); END;")
            con.execute("INSERT OR REPLACE INTO meta(k, v) VALUES('schema', ?)",
                        (str(SCHEMA_VERSION),))
            con.commit()
        except sqlite3.Error:
            self.enabled = False   # 无 FTS5/trigram 或损坏 → 整层停用
        finally:
            con.close()

    # ---------------- 语料遍历（安全核心） ----------------

    def _corpus_roots(self) -> list[tuple[Path, str | None]]:
        """返回 (目录, 仅这些扩展) 列表；include_dirs 覆盖时只取它们。

        默认从 index root 上溯推断 project：``<project>/.skill-agent/index`` → ``<project>``。
        找不到语义 project 时**回退到索引目录的父目录**，而不是静默返回空 ——
        旧行为下 ``--index-dir /tmp/idx_probe`` 会一路建成空库、只打印「0 文件 / 0 建立」，
        用户完全看不出索引是空的（D10）。
        """
        if self.include_dirs:
            return [(d, None) for d in self.include_dirs]
        proj = self.root
        for _ in range(3):
            if proj.name == ".skill-agent":
                proj = proj.parent
                break
            proj = proj.parent
        roots: list[tuple[Path, str | None]] = []
        # project 本身就是 agent 仓库：整目录为一个语料根（skills/ 会在其中被遍历，
        # .pth 等权重按后缀在 stat 前即被拒绝）。避免 skills 被重复挂为两个根。
        if (proj / "skill_agent").is_dir() or (proj / "webapp").is_dir():
            roots.append((proj, None))
            return roots
        # 否则：技能语料（仅自带 skills/）+ 源码 <project>/agent
        skills = proj / "skills"
        if skills.is_dir():
            roots.append((skills, None))
        agent_src = proj / "agent"
        if agent_src.is_dir():
            roots.append((agent_src, None))
        if roots:
            return roots
        # 兜底：索引目录就建在某个真实目录里 → 拿它当语料根（至少索引点东西出来），
        # 让「--index-dir 指向项目外」不再产出静默空库。
        for cand in (self.root.parent, self.root):
            if cand and cand.is_dir():
                return [(cand, None)]
        return []

    def _should_index(self, p: Path, ext: str) -> bool:
        if ext not in _INDEX_EXTS:
            return False
        name = p.name
        low = name.lower()
        if ".tfevents." in low:
            return False
        if any(low.endswith(s) for s in _EXCLUDE_SUFFIXES):
            return False
        if name in self._extra_excludes or p.parent.name in self._extra_excludes:
            return False
        return True

    def _iter_files(self):
        """os.walk 显式语料根，就地剪枝；读前不 stat 大文件之外的一切。"""
        excl = _EXCLUDE_DIRS | self._extra_excludes
        seen = 0
        for base, _only in self._corpus_roots():
            if not base.is_dir():
                continue
            for dirpath, dirnames, filenames in os.walk(str(base), followlinks=False):
                # 就地剪枝：排除目录既不入栈也不 stat
                dirnames[:] = [d for d in dirnames if d not in excl]
                for fn in filenames:
                    p = Path(dirpath) / fn
                    ext = p.suffix.lower()
                    if not self._should_index(p, ext):
                        continue
                    try:
                        st = p.stat()
                    except OSError:
                        continue
                    if st.st_size > self.max_file_bytes:   # 读前按大小拒绝
                        continue
                    seen += 1
                    if seen > self.max_files:
                        return
                    yield p, st

    # ---------------- 构建 / 刷新 ----------------

    def ensure(self, force: bool = False) -> dict:
        """惰性建/增量刷新。返回 {'indexed','skipped','removed','files','enabled'}。"""
        empty = {"indexed": 0, "skipped": 0, "removed": 0, "files": 0, "enabled": self.enabled}
        if not self.enabled:
            return empty
        if self._built and not force:
            # 仍做 mtime 增量（廉价），但避免重复全量
            pass
        try:
            return self._refresh(force=force)
        except sqlite3.Error:
            self.enabled = False
            return {**empty, "enabled": False}

    def refresh(self, force: bool = True) -> dict:
        return self.ensure(force=force)

    def _refresh(self, force: bool) -> dict:
        con = self._connect()
        indexed = skipped = removed = 0
        try:
            known = {r[0]: (r[1], r[2]) for r in
                     con.execute("SELECT path, mtime, size FROM files").fetchall()}
            seen: set[str] = set()
            for p, st in self._iter_files():
                key = str(p)
                seen.add(key)
                if not force and known.get(key) == (st.st_mtime, st.st_size):
                    skipped += 1
                    continue
                try:
                    src = p.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                chunks = chunk_file(key, src, p.suffix.lower())
                # 删旧 + 插新（触发器同步 FTS），单事务
                row = con.execute("SELECT id FROM files WHERE path=?", (key,)).fetchone()
                if row is not None:
                    fid = row[0]
                    con.execute("DELETE FROM chunks WHERE file_id=?", (fid,))
                    con.execute("UPDATE files SET mtime=?, size=?, nchunks=?, indexed_ts=? "
                                "WHERE id=?", (st.st_mtime, st.st_size, len(chunks), time.time(), fid))
                else:
                    fid = con.execute("INSERT INTO files(path, mtime, size, nchunks, indexed_ts) "
                                      "VALUES(?,?,?,?,?)",
                                      (key, st.st_mtime, st.st_size, len(chunks), time.time())
                                      ).lastrowid
                for c in chunks:
                    con.execute("INSERT INTO chunks(file_id, path, start, end, kind, symbol, text) "
                                "VALUES(?,?,?,?,?,?,?)",
                                (fid, c.path, c.start, c.end, c.kind, c.symbol, c.text))
                con.commit()
                indexed += 1
            # 消失的文件 → 删除（级联删 chunks）
            for gone in set(known) - seen:
                con.execute("DELETE FROM files WHERE path=?", (gone,))
                removed += 1
            if removed:
                con.commit()
            self._built = True
            total = con.execute("SELECT COUNT(*) FROM files").fetchone()[0]
            return {"indexed": indexed, "skipped": skipped, "removed": removed,
                    "files": total, "enabled": True}
        finally:
            con.close()

    def stats(self) -> dict:
        if not self.enabled:
            return {"files": 0, "chunks": 0, "db_bytes": 0, "enabled": False,
                    "schema": SCHEMA_VERSION}
        try:
            con = self._connect()
            files = con.execute("SELECT COUNT(*) FROM files").fetchone()[0]
            chunks = con.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
            con.close()
        except sqlite3.Error:
            files = chunks = 0
        try:
            size = self.db_path.stat().st_size
        except OSError:
            size = 0
        return {"files": files, "chunks": chunks, "db_bytes": size,
                "enabled": True, "schema": SCHEMA_VERSION}

    # ---------------- 查询 ----------------

    def search(self, query: str, limit: int = 8, path_glob: str | None = None,
               kind: str | None = None) -> list[Hit]:
        if not self.enabled or not query or not query.strip():
            return []
        terms = [t for t in _TERM_RE.findall(query) if len(t) >= 3]
        try:
            con = self._connect()
        except sqlite3.Error:
            return []
        try:
            hits: list[Hit] = []
            if terms:
                match = " OR ".join(dict.fromkeys(terms[:8]))
                sql = ("SELECT c.path, c.start, c.end, c.symbol, c.kind, c.text, bm25(chunks_fts) "
                       "FROM chunks_fts f JOIN chunks c ON c.id = f.rowid "
                       "WHERE chunks_fts MATCH ?")
                params: list = [match]
                if path_glob:
                    sql += " AND c.path GLOB ?"
                    params.append(path_glob)
                if kind:
                    sql += " AND c.kind = ?"
                    params.append(kind)
                sql += " ORDER BY rank LIMIT ?"
                params.append(int(limit))
                for r in con.execute(sql, params).fetchall():
                    hits.append(self._to_hit(r, query))
            if not hits:
                hits = self._like_search(con, query, limit, path_glob, kind)
            return hits
        except sqlite3.Error:
            return []
        finally:
            con.close()

    def _like_search(self, con, query: str, limit: int,
                     path_glob: str | None, kind: str | None) -> list[Hit]:
        q = query.strip()
        if not q:
            return []
        sql = ("SELECT path, start, end, symbol, kind, text, 0.0 FROM chunks "
               "WHERE text LIKE ?")
        params: list = [f"%{q}%"]
        if path_glob:
            sql += " AND path GLOB ?"
            params.append(path_glob)
        if kind:
            sql += " AND kind = ?"
            params.append(kind)
        sql += " LIMIT ?"
        params.append(int(limit))
        return [self._to_hit(r, query) for r in con.execute(sql, params).fetchall()]

    @staticmethod
    def _to_hit(r, query: str) -> Hit:
        path, start, end, symbol, kind, text, score = r
        return Hit(path=path, start=start, end=end, score=float(score or 0.0),
                   snippet=_snippet(text, query), symbol=symbol or "", kind=kind or "")

    def outline(self, path: Path | str, max_symbols: int = 200) -> list[Symbol]:
        if not self.enabled:
            return []
        p = Path(path)
        if p.is_file():
            ext = p.suffix.lower()
            try:
                src = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                return []
            return outline_file(src, ext)[:max_symbols]
        if p.is_dir():
            out: list[Symbol] = []
            excl = _EXCLUDE_DIRS | self._extra_excludes
            for dirpath, dirnames, filenames in os.walk(str(p), followlinks=False):
                dirnames[:] = [d for d in dirnames if d not in excl]
                for fn in sorted(filenames):
                    fp = Path(dirpath) / fn
                    ext = fp.suffix.lower()
                    if ext not in (".py", ".md", ".markdown"):
                        continue
                    try:
                        if fp.stat().st_size > self.max_file_bytes:
                            continue
                        src = fp.read_text(encoding="utf-8", errors="replace")
                    except OSError:
                        continue
                    for s in outline_file(src, ext):
                        out.append(s)
                        if len(out) >= max_symbols:
                            return out
            return out
        return []


def _snippet(text: str, query: str) -> str:
    """截取含查询词的一段（否则取开头），压到 SNIPPET_CHARS 内。"""
    if not text:
        return ""
    terms = [t for t in _TERM_RE.findall(query) if len(t) >= 2]
    pos = -1
    for t in terms:
        pos = text.find(t)
        if pos >= 0:
            break
    if pos < 0:
        seg = text[:SNIPPET_CHARS]
    else:
        lo = max(0, pos - SNIPPET_CHARS // 3)
        seg = text[lo:lo + SNIPPET_CHARS]
    return seg


def default_index_dir(project: Path) -> Path:   # 便利转发，避免循环 import
    return Path(project) / ".skill-agent" / "index"
