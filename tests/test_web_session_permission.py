# -*- coding: utf-8 -*-
"""网页端权限必须按会话同步。

事故（2026-11-10）：A 会话把共享 cfg.permission 从 auto 改成 ask 后，
B 会话（仍是 auto）下一次 set_params(permission="ask") 看到
`permission == self.cfg.permission` 就跳过，B.tools.permission 停在 auto
→ B 里的 Bash 完全不弹确认。用户看到的现象是「另一个会话问了、这个会话没问」。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from types import SimpleNamespace  # noqa: E402

from webapp.session import WebSession  # noqa: E402


def test_permission_syncs_to_each_session_not_only_the_one_that_changed_cfg():
    cfg = SimpleNamespace(permission="auto")
    a = SimpleNamespace(cfg=cfg, tools=SimpleNamespace(permission="auto"), agent=None)
    b = SimpleNamespace(cfg=cfg, tools=SimpleNamespace(permission="auto"), agent=None)

    WebSession.set_params(a, permission="ask")
    WebSession.set_params(b, permission="ask")

    assert a.tools.permission == "ask"
    assert b.tools.permission == "ask", "B 会话仍停在 auto → 它的 Bash 不会弹确认"
