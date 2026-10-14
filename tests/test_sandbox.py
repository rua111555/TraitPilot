"""M9 沙箱执行后端单测：默认宿主逐字节一致 + 后台 pid 修复 + rlimit 生效 + docker fail-soft。"""
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pathlib import Path  # noqa: E402

from skill_agent import sandbox as sb  # noqa: E402
from skill_agent.skills import SkillRegistry  # noqa: E402
from skill_agent.tools import ToolSet  # noqa: E402


def _ts(cwd, **kw):
    return ToolSet(SkillRegistry(Path("/nonexistent")), Path(cwd), **kw)


# ---------------- 后端工厂 / fail-soft ----------------

class _Cfg:
    sandbox = "host"
    sandbox_spec = None
    sandbox_strict = False


def test_build_backend_default_host():
    b, warn = sb.build_backend(_Cfg())
    assert isinstance(b, sb.HostBackend) and warn is None


def test_build_backend_unknown_falls_back():
    c = _Cfg(); c.sandbox = "bwrap"
    b, warn = sb.build_backend(c)
    assert isinstance(b, sb.HostBackend) and warn and "回退" in warn


def test_docker_without_image_fails_soft():
    c = _Cfg(); c.sandbox = "docker"        # 无镜像 → 不可用
    b, warn = sb.build_backend(c)
    assert isinstance(b, sb.HostBackend) and warn


def test_docker_strict_raises_when_unavailable():
    c = _Cfg(); c.sandbox = "docker"; c.sandbox_strict = True
    try:
        sb.build_backend(c)
    except sb.SandboxUnavailable:
        return
    raise AssertionError("strict 档位不可用应抛 SandboxUnavailable")


def test_toolset_sandbox_defaults_to_host():
    with tempfile.TemporaryDirectory() as d:
        assert isinstance(_ts(d).sandbox, sb.HostBackend)


# ---------------- Tier 0：前台逐字节一致 ----------------

def test_host_fg_argv_kwargs_match_legacy():
    argv, kw = sb.HostBackend().fg("echo hi", Path("/tmp"), 7)
    assert argv == "echo hi"
    assert kw == {"shell": True, "executable": "/bin/bash",
                  "cwd": "/tmp", "timeout": 7}


def test_foreground_bash_unchanged():
    with tempfile.TemporaryDirectory() as d:
        ts = _ts(d)
        r = ts.execute("Bash", {"command": "pwd"})
        assert not r.is_error
        assert str(Path(d).resolve()) in r.content       # cwd 生效
        assert r.meta.get("exit_code") == 0


# ---------------- 后台 pid 修复（本次核心缺陷修复） ----------------

def test_background_writes_live_pid_and_cds():
    """修复后：后台任务写在宿主可见的 pid 文件，且 TaskOutput 能判活。"""
    from skill_agent.tools import BG_ROOT
    with tempfile.TemporaryDirectory() as d:
        ts = _ts(d)
        r = ts.execute("Bash", {"command": "sleep 3", "background": True})
        tid = r.meta["task_id"]
        pidf = Path(BG_ROOT) / tid / "pid"
        # 轮询等待 pid 文件出现（原先恒不出现）
        for _ in range(50):
            if pidf.is_file() and pidf.read_text().strip():
                break
            time.sleep(0.05)
        assert pidf.is_file(), "后台应写下 pid 文件（修复前恒缺失）"
        pid = int(pidf.read_text().strip())
        # 进程应仍在运行
        try:
            os.kill(pid, 0)
            alive_now = True
        except OSError:
            alive_now = False
        assert alive_now, "pid 应指向正在运行的宿主进程"
        # TaskOutput 应报『运行中』（修复前恒为『进程已结束/None』）
        out = ts.execute("TaskOutput", {"task_id": tid})
        assert out.meta.get("pid_alive") is True
        # 结束后应转为 not alive
        time.sleep(3.2)
        out2 = ts.execute("TaskOutput", {"task_id": tid})
        assert out2.meta.get("pid_alive") is False


def test_background_runs_in_cwd():
    from skill_agent.tools import BG_ROOT
    with tempfile.TemporaryDirectory() as d:
        ts = _ts(d)
        ts.execute("Bash", {"command": "pwd > where.txt", "background": True})
        for _ in range(50):
            if Path(d, "where.txt").is_file():
                break
            time.sleep(0.05)
        assert Path(d, "where.txt").is_file(), "后台任务应在 self.cwd 下执行"


# ---------------- Tier 1：rlimit ----------------

def test_rlimit_backend_limits_address_space():
    with tempfile.TemporaryDirectory() as d:
        backend = sb.RlimitBackend({"as": 256 * 1024 * 1024})   # 256 MiB
        ts = _ts(d, sandbox=backend)
        # 尝试分配 1 GiB → 应失败（Python MemoryError / 非零退出）
        r = ts.execute("Bash", {"command":
                                "python3 -c \"b=bytearray(1024*1024*1024); print('ok')\""})
        assert r.is_error or "ok" not in r.content


def test_rlimit_fg_still_runs_normal_command():
    with tempfile.TemporaryDirectory() as d:
        backend = sb.RlimitBackend({"as": 4 * 1024 * 1024 * 1024})
        ts = _ts(d, sandbox=backend)
        r = ts.execute("Bash", {"command": "echo hello"})
        assert not r.is_error and "hello" in r.content


# ---------------- Tier 2：docker（有则验证，无则跳过） ----------------

def _docker_ready():
    ok, _ = sb.DockerBackend.probe({"image": "python:3.11-slim"})
    return ok


def test_docker_fg_network_isolated():
    if not _docker_ready():
        return
    with tempfile.TemporaryDirectory() as d:
        backend = sb.DockerBackend({"image": "python:3.11-slim", "network": "none"})
        ts = _ts(d, sandbox=backend)
        r = ts.execute("Bash", {"command":
                                "python3 -c \"import socket;socket.create_connection(('1.1.1.1',80),2)\"",
                                "timeout": 60})
        assert r.is_error           # 断网 → 连接失败


def test_docker_bg_pid_alive_via_taskoutput():
    if not _docker_ready():
        return
    from skill_agent.tools import BG_ROOT
    with tempfile.TemporaryDirectory() as d:
        backend = sb.DockerBackend({"image": "python:3.11-slim", "network": "none"})
        ts = _ts(d, sandbox=backend)
        r = ts.execute("Bash", {"command": "sleep 3", "background": True})
        tid = r.meta["task_id"]
        pidf = Path(BG_ROOT) / tid / "pid"
        for _ in range(120):
            if pidf.is_file() and pidf.read_text().strip():
                break
            time.sleep(0.1)
        assert pidf.is_file()
        out = ts.execute("TaskOutput", {"task_id": tid})
        assert out.meta.get("pid_alive") is True   # 宿主监督 pid 判活（Tier-2 关键回归）


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
