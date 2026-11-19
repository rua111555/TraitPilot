import re
import threading
import time
from pathlib import Path

import pytest

from skill_agent import tools
from skill_agent.client import AnthropicClient
from skill_agent.config import Config
from skill_agent.skills import SkillRegistry
from webapp import bgwatch, server


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(tools, "BG_ROOT", tmp_path / "tasks")
    monkeypatch.setattr(bgwatch, "MIN_AGE", 0)
    monkeypatch.setattr(server, "_fetch_models", lambda: ["test"])
    monkeypatch.setattr(server, "CFG_REF", dict(server.CFG_REF))
    cfg = Config("http://unused.invalid", "mock", "test", project=tmp_path, cwd=tmp_path)
    app = server.create_app(cfg, SkillRegistry(tmp_path))

    def model(self, *, messages, **kwargs):
        prompt = messages[-1]["content"]
        if prompt == "启动":
            return [{"type": "tool_use", "id": "launch", "name": "Bash",
                     "input": {"command": "for i in {1..200}; do test -f release && break; sleep 0.05; done; "
                                          "echo RESULT; exit " + ("7" if (tmp_path / "fail").exists() else "0"),
                               "background": True}},
                    {"type": "tool_use", "id": "later", "name": "Bash",
                     "input": {"command": "touch should_not_run"}}], "tool_use", 1
        if isinstance(prompt, str) and prompt.startswith("[系统]"):
            assert "Bash" not in {schema["name"] for schema in kwargs["tools"]}
            if (tmp_path / "hold_report").exists():
                deadline = time.monotonic() + 10
                while (tmp_path / "hold_report").exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
            if (tmp_path / "fail").exists():
                raise RuntimeError("simulated model outage")
            task_id = re.search(r"- ([a-f0-9]+)：", prompt).group(1)
            return [{"type": "tool_use", "id": "output", "name": "TaskOutput",
                     "input": {"task_id": task_id}}], "tool_use", 1
        if isinstance(prompt, list) and prompt[0]["tool_use_id"] == "output":
            assert "RESULT" in prompt[0]["content"]
            return [{"type": "text", "text": "日志输出了 RESULT，产物待核验。"}], "end_turn", 1
        assert prompt == "继续聊"
        return [{"type": "text", "text": "这条追问已经回答。"}], "end_turn", 1

    monkeypatch.setattr(AnthropicClient, "create", model)
    return app, tmp_path


def finish_job(root, session):
    (root / "release").touch()
    watcher = bgwatch.BackgroundWatcher()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        watcher._scan_session(session)
        if session.last_bg_report and not session.busy:
            return
        time.sleep(0.02)
    pytest.fail("后台任务结束后没有完成汇报")


@pytest.mark.parametrize("fail", [False, True])
def test_background_chat_and_durable_report(project, fail):
    app, root = project
    if fail:
        (root / "fail").touch()
    client = app.test_client()

    def chat(message):
        response = client.post("/api/chat", json={"session": "bg", "message": message})
        assert response.status_code == 200
        return response.get_data(as_text=True)

    assert "你可以继续对话" in chat("启动")
    session = server.CFG_REF["sessions"]["bg"]
    assert not (root / "should_not_run").exists()
    assert "这条追问已经回答" in chat("继续聊")
    task, = session.bg_tasks.values()
    assert not task["notified"]
    finish_job(root, session)
    report = session.last_run()
    assert "下一步：" in report.final
    assert task["result"] == ("exit:7" if fail else "exit:0")
    if fail:
        assert "退出码 7" in report.final and task["log"] in report.final
        incident, = session.tools.learning.rows()
        assert incident["kind"] == "runtime" and incident["last_exit"] == 7
    else:
        assert "日志输出了 RESULT" in report.final
    assert Path(task["log"]).read_text().splitlines()[-1] == "RESULT"
    assert task["notified"]
    bgwatch.BackgroundWatcher()._scan_session(session)
    assert session.last_run() is report
    assert client.get("/api/session/bg").json["messages"][-1]["blocks"][-1]["text"] in report.final


@pytest.mark.parametrize("race", [False, True], ids=["short-report", "send-during-report"])
def test_page_delivers_report_and_followups(project, race):
    playwright = pytest.importorskip("playwright.sync_api")
    from werkzeug.serving import make_server

    app, root = project
    httpd = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        with playwright.sync_playwright() as driver:
            if not Path(driver.chromium.executable_path).exists():
                pytest.skip("需要 Playwright Chromium")
            browser = driver.chromium.launch()
            page = browser.new_page()
            page.goto(f"http://127.0.0.1:{httpd.server_port}")
            page.locator("#input").fill("启动")
            page.locator("#input").press("Enter")
            page.wait_for_function("!state.busy && chatEl.innerText.includes('你可以继续对话')")
            page.locator("#input").fill("继续聊")
            page.locator("#input").press("Enter")
            page.wait_for_function("!state.busy && chatEl.innerText.includes('这条追问已经回答')")
            page.locator("#input").fill("未发送的草稿")
            sid = page.evaluate("SID")
            session = server.CFG_REF["sessions"][sid]
            if race:
                hold = root / "hold_report"
                hold.touch()
                (root / "release").touch()
                watcher = bgwatch.BackgroundWatcher()
                deadline = time.monotonic() + 3
                while not session.busy and time.monotonic() < deadline:
                    watcher._scan_session(session)
                    time.sleep(0.02)
                assert session.busy
                page.locator("#input").fill("继续聊")
                page.locator("#input").press("Enter")
                page.wait_for_function("queuedBySid[SID]?.length === 1")
                page.locator("#input").fill("未发送的草稿")
                hold.unlink()
                page.wait_for_function("!state.busy && chatEl.innerText.split('这条追问已经回答。').length === 3")
            else:
                finish_job(root, session)
            report = "下一步：核对日志中列出的输出文件"
            page.get_by_text(report, exact=False).first.wait_for(timeout=12000)
            page.evaluate("pollBackground()")
            assert page.locator("#chat").inner_text().count(report) == 1
            assert page.locator("#input").input_value() == "未发送的草稿"
            browser.close()
    finally:
        httpd.shutdown()
        thread.join(timeout=5)
        httpd.server_close()
