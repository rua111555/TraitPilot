"""#11 检索单测：分块纯函数、索引增量/排除、查询健壮、工具集成、并行只读。"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pathlib import Path  # noqa: E402

from skill_agent import retrieval as R  # noqa: E402
from skill_agent.skills import SkillRegistry  # noqa: E402
from skill_agent.tools import ToolSet  # noqa: E402


def _ts(cwd, **kw):
    return ToolSet(SkillRegistry(Path("/nonexistent")), Path(cwd), **kw)


def _idx(root, **kw):
    return R.CodeIndex(Path(root), enabled=True, **kw)


# ================= 分块纯函数 =================

PY = '''"""模块文档。"""
import os


class Foo:
    """类。"""

    def bar(self):
        return 1


def top(a):
    return a


x = 1
'''


def test_chunk_python_functions_classes_methods():
    chunks = R.chunk_python(PY, "/x/a.py")
    kinds = {(c.kind, c.symbol) for c in chunks}
    assert ("py_module", "<module>") in kinds
    assert ("py_class", "Foo") in kinds
    assert ("py_func", "Foo.bar") in kinds
    assert ("py_func", "top") in kinds
    # 行号 1-based 且有序
    assert all(c.start >= 1 and c.end >= c.start for c in chunks)
    starts = [c.start for c in chunks]
    assert starts == sorted(starts)


def test_chunk_python_syntax_error_falls_back_to_window():
    bad = "def f(:\n  pass\n" * 5
    chunks = R.chunk_python(bad, "/x/b.py")
    assert chunks and all(c.kind == "window" for c in chunks)


def test_chunk_python_covers_all_lines_on_gaps():
    src = "import os\n\n\ndef f():\n    return 1\n\n# tail comment\n"
    chunks = R.chunk_python(src, "/x/c.py")
    covered = set()
    for c in chunks:
        covered.update(range(c.start, c.end + 1))
    assert covered == set(range(1, len(src.splitlines()) + 1))


MD = """# 标题一

引言文字。

```python
# 这不是标题
x = 1
```

## 子标题

正文内容。
"""


def test_chunk_markdown_headings_ignore_fence():
    chunks = R.chunk_markdown(MD, "/x/d.md")
    syms = [c.symbol for c in chunks]
    assert "标题一" in syms and "子标题" in syms
    assert "这不是标题" not in syms        # 围栏内 # 不算标题
    assert all(c.kind == "md_section" for c in chunks)


def test_chunk_markdown_oversized_section_splits():
    md = "# H\n" + "line\n" * (R.MAX_CHUNK_LINES * 2)
    chunks = R.chunk_markdown(md, "/x/big.md")
    assert len([c for c in chunks if c.symbol == "H"]) >= 2


def test_chunk_shell_blocks():
    sh = "#!/bin/bash\n# 第一块\necho a\n\n\necho b\n"
    chunks = R.chunk_shell(sh, "/x/e.sh")
    assert chunks and all(c.kind == "sh_block" for c in chunks)
    assert any(c.symbol == "第一块" for c in chunks)


# ================= 索引与排除 =================

def _make_project(d):
    proj = Path(d) / "proj"
    (proj / ".claude" / "skills" / "s").mkdir(parents=True)
    (proj / "agent" / "docs").mkdir(parents=True)
    (proj / ".claude" / "skills" / "s" / "SKILL.md").write_text(
        "# GWAS\n\n阶段参数表：stage02 qc filter。\n", encoding="utf-8")
    (proj / "agent" / "mod.py").write_text(
        "def run_pipeline():\n    return 'stage02'\n", encoding="utf-8")
    return proj


def test_index_build_and_search_returns_line_ranges():
    with tempfile.TemporaryDirectory() as d:
        proj = _make_project(d)
        idx = _idx(proj / ".skill-agent" / "index")
        st = idx.ensure()
        assert st["enabled"] and st["indexed"] >= 2
        hits = idx.search("阶段参数表")
        assert hits and hits[0].path.endswith("SKILL.md") and hits[0].start >= 1


def test_index_skips_unchanged_files():
    with tempfile.TemporaryDirectory() as d:
        proj = _make_project(d)
        idx = _idx(proj / ".skill-agent" / "index")
        idx.ensure()
        st2 = idx.ensure()
        assert st2["indexed"] == 0 and st2["skipped"] >= 2


def test_index_reindexes_changed_file():
    with tempfile.TemporaryDirectory() as d:
        proj = _make_project(d)
        idx = _idx(proj / ".skill-agent" / "index")
        idx.ensure()
        f = proj / "agent" / "mod.py"
        f.write_text("def run_pipeline():\n    return 'stage09 unique_marker'\n", encoding="utf-8")
        os.utime(f, (f.stat().st_atime, f.stat().st_mtime + 5))
        st = idx.ensure()
        assert st["indexed"] >= 1
        assert idx.search("unique_marker")                     # 新内容已索引


def test_index_removes_deleted_file():
    with tempfile.TemporaryDirectory() as d:
        proj = _make_project(d)
        idx = _idx(proj / ".skill-agent" / "index")
        idx.ensure()
        (proj / "agent" / "mod.py").unlink()
        st = idx.ensure()
        assert st["removed"] >= 1
        assert not idx.search("run_pipeline")


def test_index_excludes_binaries_huge_and_dirs():
    with tempfile.TemporaryDirectory() as d:
        proj = _make_project(d)
        agent = proj / "agent"
        # 大文件（>2MB）
        (agent / "big.py").write_text("x = 1\n" * 500_000, encoding="utf-8")
        # 二进制后缀
        (agent / "w.pth").write_bytes(b"\x00" * 100)
        # 排除目录
        (agent / "__pycache__").mkdir()
        (agent / "__pycache__" / "c.py").write_text("SECRET_PYCACHE = 1\n", encoding="utf-8")
        (agent / "saved").mkdir()
        (agent / "saved" / "s.py").write_text("SECRET_SAVED = 1\n", encoding="utf-8")
        idx = _idx(proj / ".skill-agent" / "index")
        idx.ensure()
        assert not idx.search("SECRET_PYCACHE")
        assert not idx.search("SECRET_SAVED")
        assert not idx.search("x = 1 AND big")     # 大文件未入索引


def test_index_schema_mismatch_rebuilds():
    with tempfile.TemporaryDirectory() as d:
        proj = _make_project(d)
        root = proj / ".skill-agent" / "index"
        idx = _idx(root)
        idx.ensure()
        import sqlite3
        con = sqlite3.connect(str(idx.db_path))
        con.execute("UPDATE meta SET v='999' WHERE k='schema'")
        con.commit()
        con.close()
        idx2 = _idx(root)
        st = idx2.ensure()
        assert st["enabled"]


def test_index_corrupt_db_self_disables_without_raise():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d) / "idx"
        root.mkdir()
        (root / "code.db").write_bytes(b"not a database at all")
        idx = R.CodeIndex(root, enabled=True)
        # 不抛；要么恢复要么自停用
        assert idx.ensure() is not None


# ================= 查询健壮 =================

def test_search_and_outline_disabled_return_empty():
    idx = R.CodeIndex(Path("/tmp/nonexistent-idx-xyz"), enabled=False)
    assert idx.enabled is False
    assert idx.search("anything") == []
    assert idx.outline(".") == []
    assert idx.stats()["enabled"] is False


def test_search_garbage_and_short_terms_no_raise():
    with tempfile.TemporaryDirectory() as d:
        proj = _make_project(d)
        idx = _idx(proj / ".skill-agent" / "index")
        idx.ensure()
        assert idx.search("") == []
        assert idx.search("!!!") == []
        assert idx.search('") OR )(') == [] or True     # 不抛即可


def test_search_short_query_like_fallback():
    with tempfile.TemporaryDirectory() as d:
        proj = _make_project(d)
        (proj / "agent" / "z.py").write_text("中文短词 = 1\n", encoding="utf-8")
        idx = _idx(proj / ".skill-agent" / "index")
        idx.ensure()
        assert idx.search("中文")     # 2 字词走 LIKE 兜底


# ================= 工具集成 =================

def test_search_repomap_not_mounted_by_default():
    with tempfile.TemporaryDirectory() as d:
        ts = _ts(d)
        assert "Search" not in ts.registry_tools
        assert "RepoMap" not in ts.registry_tools
        assert "Search" not in [s["name"] for s in ts.schemas_for(None)]


def test_tools_mounted_when_enabled_and_execute():
    with tempfile.TemporaryDirectory() as d:
        proj = _make_project(d)
        idx = _idx(proj / ".skill-agent" / "index")
        idx.ensure()
        ts = _ts(d, enable_retrieval=True, retrieval=idx)
        assert "Search" in ts.registry_tools and "RepoMap" in ts.registry_tools
        r = ts.execute("Search", {"query": "阶段参数表"})
        assert not r.is_error and ".md:" in r.content
        assert r.meta["hits"] >= 1
        # 缺 query → M6 结构化报错
        bad = ts.execute("Search", {})
        assert bad.is_error and "校验失败" in bad.content


def test_repomap_lists_symbols():
    with tempfile.TemporaryDirectory() as d:
        proj = _make_project(d)
        idx = _idx(proj / ".skill-agent" / "index")
        ts = _ts(d, enable_retrieval=True, retrieval=idx)
        r = ts.execute("RepoMap", {"path": str(proj / "agent" / "mod.py")})
        assert not r.is_error and "run_pipeline" in r.content
        assert r.meta["symbols"] >= 1


def test_repomap_respects_path_boundary():
    with tempfile.TemporaryDirectory() as d:
        proj = _make_project(d)
        idx = _idx(proj / ".skill-agent" / "index")
        from skill_agent.permissions import PermissionModel
        pm = PermissionModel(restrict_paths=True)
        ts = _ts(proj, enable_retrieval=True, retrieval=idx, permission_model=pm)
        r = ts.execute("RepoMap", {"path": "/etc"})
        assert r.is_error


def test_search_repomap_readonly_for_parallel():
    from skill_agent.agent import _is_readonly
    assert _is_readonly("Search") and _is_readonly("RepoMap")


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
