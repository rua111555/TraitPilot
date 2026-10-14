"""M2 StopController 单测：六种 stop_reason × 有无工具 × 是否截断的组合分类。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from skill_agent.stops import (StopAction, classify, make_truncation_result,  # noqa: E402
                               text_of)


def _tu(i="t1"):
    return {"type": "tool_use", "id": i, "name": "Bash", "input": {}}


def test_tool_use_with_usable_block():
    d = classify("tool_use", [_tu()])
    assert d.action == StopAction.CONTINUE_TOOLS and not d.warn


def test_end_turn_with_tool_flagged_as_anomaly():
    d = classify("end_turn", [_tu()])
    assert d.action == StopAction.CONTINUE_TOOLS and d.warn == "protocol_anomaly"


def test_all_tools_parse_error_repair():
    bad = {"type": "tool_use", "id": "t1", "name": "Bash", "input": {}, "_parse_error": True}
    d = classify("max_tokens", [bad])
    assert d.action == StopAction.REPAIR_TRUNCATION and d.truncated


def test_mixed_usable_and_broken_is_continue():
    bad = {"type": "tool_use", "id": "t2", "name": "Read", "input": {}, "_parse_error": True}
    d = classify("tool_use", [_tu("t1"), bad])
    assert d.action == StopAction.CONTINUE_TOOLS     # 有可用块即继续


def test_max_tokens_text_only_finalize_truncated():
    d = classify("max_tokens", [{"type": "text", "text": "half"}])
    assert d.action == StopAction.FINALIZE and d.truncated


def test_pause_turn_resume():
    d = classify("pause_turn", [{"type": "text", "text": "..."}])
    assert d.action == StopAction.RESUME_SERVER_TOOL


def test_refusal():
    d = classify("refusal", [])
    assert d.action == StopAction.REFUSE and d.user_message


def test_end_turn_text_finalize():
    d = classify("end_turn", [{"type": "text", "text": "done"}])
    assert d.action == StopAction.FINALIZE and not d.truncated


def test_empty_stop_reason_finalize():
    assert classify("", []) .action == StopAction.FINALIZE


def test_unknown_stop_reason_warns():
    d = classify("something_new", [{"type": "text", "text": "x"}])
    assert d.action == StopAction.FINALIZE and "unknown_stop_reason" in d.warn


def test_make_truncation_result_pairs():
    r = make_truncation_result("t9")
    assert r["type"] == "tool_result" and r["tool_use_id"] == "t9" and r["is_error"]


def test_text_of():
    assert text_of([{"type": "text", "text": "a"},
                    {"type": "tool_use", "id": "x"}]) == "a"


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
