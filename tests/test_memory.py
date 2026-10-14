"""M12 分层记忆单测：core 文件 + archival FTS5 检索；禁用时静默无副作用。"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pathlib import Path  # noqa: E402

from skill_agent.memory import MemoryStore  # noqa: E402


def test_core_roundtrip():
    with tempfile.TemporaryDirectory() as d:
        m = MemoryStore(Path(d))
        assert m.get_core() == ""
        m.set_core("当前项目：GWAS；注意 LD_LIBRARY_PATH")
        assert "GWAS" in m.get_core()


def test_archival_remember_and_search():
    with tempfile.TemporaryDirectory() as d:
        m = MemoryStore(Path(d))
        m.remember("GWAS 阶段 S3 使用 rMVP FarmCPU 方法", kind="fact")
        m.remember("后台任务 log=/p/run.log pid=123", kind="task")
        hits = m.search("阶段 S3")
        assert hits and any("S3" in h.text for h in hits)


def test_search_empty_and_garbage_returns_empty():
    with tempfile.TemporaryDirectory() as d:
        m = MemoryStore(Path(d))
        assert m.search("") == []
        assert m.search("!!!") == []            # 无有效词元
        assert m.search('" OR )(') == []         # FTS 语法错误不抛


def test_render_context():
    with tempfile.TemporaryDirectory() as d:
        m = MemoryStore(Path(d))
        m.remember("关键路径 /p/grm.txt 是亲缘矩阵")
        ctx = m.render_context("亲缘矩阵")
        assert "相关历史记忆" in ctx and "grm.txt" in ctx


def test_disabled_store_is_noop():
    with tempfile.TemporaryDirectory() as d:
        m = MemoryStore(Path(d), enabled=False)
        m.set_core("x"); m.remember("y")
        assert m.get_core() == "" and m.search("y") == [] and m.all_items() == []


def test_all_items_lists_recent_first():
    with tempfile.TemporaryDirectory() as d:
        m = MemoryStore(Path(d))
        m.remember("first")
        m.remember("second")
        items = m.all_items()
        assert items[0].text == "second"


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
