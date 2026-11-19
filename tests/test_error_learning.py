import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from skill_agent.agent import Agent
from skill_agent.config import Config
from skill_agent.learning import ErrorLearning
from skill_agent.skills import SkillRegistry
from skill_agent.tools import ToolSet


def agent(root, **options):
    cfg = Config("http://unused.invalid", "test", "model", project=root, cwd=root, **options)
    registry = SkillRegistry(root)
    return Agent(cfg, registry, ToolSet(registry, root))


@pytest.mark.parametrize("path,command,invalid,valid,kind", [
    ("job.py", "python3 job.py", "if True print('x')", "print('ok')", "python-syntax"),
    ("job.sh", "bash job.sh", "for x in a; do", "for x in a; do echo ok; done", "bash-syntax"),
])
def test_failure_changes_future_behavior_and_recovers(tmp_path, path, command, invalid, valid, kind):
    first = agent(tmp_path).tools
    write = {"file_path": path, "content": invalid}
    assert not first.execute("Write", write).is_error
    for _ in range(2):
        assert first.execute("Bash", {"command": command}).is_error
    previous, = first.learning.rows()
    assert previous["kind"] == kind and previous["failures"] == 2
    assert previous["recoveries"] == 0

    second = agent(tmp_path).tools
    assert second.execute("Write", {**write, "content": invalid + "\n"}).is_error
    assert (tmp_path / path).read_text() == invalid
    assert not second.execute("Edit", {"file_path": path, "old_string": invalid,
                                       "new_string": valid}).is_error
    assert not second.execute("Bash", {"command": "true"}).is_error
    assert not second.execute("Bash", {"command": command + " other"}).is_error
    assert not any(row["recoveries"] for row in second.learning.rows())
    result = second.execute("Bash", {"command": command, "repair_summary": "闭合语句后再输出结果"})
    assert not result.is_error and "相同调用" in result.content
    third = agent(tmp_path)
    row, = [row for row in third.tools.learning.rows() if row["kind"] == kind]
    assert row["recoveries"] == 1 and row["last_exit"] != 0 and row["repair"] == "闭合语句后再输出结果"
    assert "闭合语句后再输出结果" in third.tools.execute("Write", {**write, "content": valid}).content
    assert "闭合语句后再输出结果" in third.tools.execute("Read", {"file_path": path}).content
    prompts = []
    def model(**kwargs):
        prompts.append(kwargs["system"])
        return [{"type": "text", "text": "ok"}], "end_turn", 1
    third.client = SimpleNamespace(create=model)
    third.answer("编写脚本")
    assert "本项目曾发生的错误" in prompts[0] and "闭合语句后再输出结果" not in prompts[0]
    assert "闭合语句后再输出结果" in third.answer("/lessons")
    assert len(prompts) == 1
    assert third.tools.learning.forget(row["id"])
    assert not third.tools.execute("Write", write).is_error


def test_transient_and_denied_actions_do_not_validate_repairs(tmp_path):
    tools = agent(tmp_path).tools
    script = tmp_path / "job.py"
    script.write_text("from pathlib import Path\nPath('input.txt').read_text()")
    run = {"command": "python3 job.py"}
    assert tools.execute("Bash", run).is_error
    (tmp_path / "input.txt").touch()
    assert not tools.execute("Bash", run).is_error
    script.write_text("print('unrelated change')")
    assert not tools.execute("Bash", run).is_error
    tools.permission = "ask"
    tools.confirm_handler = lambda *_: False
    assert tools.execute("Bash", {"command": "exit 19"}).is_error
    row, = tools.learning.rows()
    assert row["recoveries"] == 0 and row["kind"] == "missing-input"
    other = agent(tmp_path / "other")
    assert not other.tools.learning.rows()
    off = agent(tmp_path, error_learning=False)
    assert off.tools.learning is None


def test_concurrent_failures_and_replayed_completion(tmp_path):
    learner = ErrorLearning(tmp_path)
    attempt = dict(invocation="inv", target="", revision="")
    barrier = threading.Barrier(4)
    def record(index):
        barrier.wait(timeout=3)
        return learner.observe(attempt, "runtime", job=str(index), exit_code=7)
    with ThreadPoolExecutor(4) as workers:
        assert all(workers.map(record, range(4)))
    assert learner.observe(attempt, "runtime", job="0", exit_code=7) == ""
    row, = learner.rows()
    assert row["failures"] == 4


def test_database_failure_does_not_break_command(tmp_path):
    tools = agent(tmp_path).tools
    (tmp_path / ".skill-agent").write_text("unwritable")
    result = tools.execute("Bash", {"command": "exit 7"})
    assert result.is_error and result.meta["exit_code"] == 7


def test_distinct_unknown_errors_and_config_opt_out(tmp_path, monkeypatch):
    tools = agent(tmp_path).tools
    script = tmp_path / "run.sh"
    for reason in ["invalid header", "missing footer", "invalid header"]:
        script.write_text(f"echo '{reason}' >&2; exit 4")
        assert tools.execute("Bash", {"command": "bash run.sh"}).is_error
    rows = tools.learning.rows()
    assert sorted(row["failures"] for row in rows) == [1, 2]
    assert "invalid header" not in tools.learning.describe()
    (tmp_path / "config.json").write_text('{"api_key":"dummy","error_learning":false}')
    monkeypatch.delenv("SKILL_AGENT_ERROR_LEARNING", raising=False)
    assert not Config.load(project=tmp_path).error_learning
    monkeypatch.setenv("SKILL_AGENT_ERROR_LEARNING", "1")
    assert Config.load(project=tmp_path).error_learning


def test_background_repairs_require_an_actual_completed_rerun(tmp_path, monkeypatch):
    from skill_agent import tools as tools_mod

    root = tmp_path / "tasks"
    monkeypatch.setattr(tools_mod, "BG_ROOT", root)
    tools = agent(tmp_path).tools
    jobs = []
    tools.on_bg_start = jobs.append
    script = tmp_path / "work.sh"
    def start(source):
        script.write_text(source)
        tools.begin_turn()
        result = tools.execute("Bash", {"command": "bash work.sh", "background": True,
                                        "repair_summary": "补全循环结束标记"})
        assert not result.is_error
        return jobs[-1]

    def complete(job):
        from pathlib import Path
        exit_file = Path(job["exit_file"])
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if exit_file.exists() and exit_file.read_text().strip():
                return "exit:" + exit_file.read_text().strip()
            time.sleep(0.01)
        pytest.fail("后台脚本未结束")

    failed = start("for i in a; do echo a")
    status = complete(failed)
    assert not tools.learning.rows()
    tools.learning.background(failed, status, root)
    tools.learning.background(failed, status, root)
    fixed = start("for i in a; do echo a; done")
    before, = tools.learning.rows()
    assert before["failures"] == 1 and before["recoveries"] == 0
    assert tools.learning.background(fixed, complete(fixed), root)
    assert not tools.learning.background(fixed, "exit:0", root)
    after, = tools.learning.rows()
    assert after["repair"] == "补全循环结束标记" and after["recoveries"] == 1
