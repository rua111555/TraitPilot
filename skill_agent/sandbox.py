"""M9 沙箱执行后端（纯 stdlib）。

给 ``Bash`` 工具抽象「外部进程怎么起」这一层，三档执行后端：

  - ``HostBackend``  （Tier 0，默认）：与改造前**逐字节一致**的宿主 ``subprocess``。
  - ``RlimitBackend``（Tier 1）：宿主进程 + stdlib ``resource.setrlimit`` 资源上限（弱隔离，零依赖）。
  - ``DockerBackend``（Tier 2）：``docker run`` 强隔离（断网、内存/CPU 上限、可挂载工作目录）。

设计契约（与 ``docs/dependency-strategy.md`` 一致）：
  - **零新依赖**：本模块只用 stdlib；docker 是**系统依赖**，不是 pip 依赖。
  - **惰性导入**：``resource`` 只在真正设置上限时于函数内部导入；模块顶层不引入任何可选依赖。
  - **fail-soft**：``build_backend`` 探测失败时回退 ``HostBackend`` 并给出一行提示，绝不抛异常
    （除非显式 ``sandbox_strict``）。
  - **默认关闭**：未显式选择档位时行为与今天完全一致。

只拦截 Bash 的**两个**进程启动点（``tools.py`` 前台 ``subprocess.run`` 与后台
``_bash_background``）；不介入 ``ToolSet.execute`` 的治理顺序。
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
from pathlib import Path

# Tier 1 默认资源上限（宽松，避免误伤正常命令；可经 sandbox_spec 覆盖）
DEFAULT_RLIMIT_SPEC = {
    "as": 4 * 1024 * 1024 * 1024,   # 虚拟内存上限 4 GiB（None = 不限）
    "cpu": None,                    # CPU 秒数（None = 不限）
    "nofile": None,                 # 打开文件数（None = 保持继承）
    "nproc": None,                  # 进程/线程数（None = 保持继承）
}

DEFAULT_DOCKER_SPEC = {
    "image": None,                  # 必填：容器镜像（如 python:3.11-slim / 自建生信镜像）
    "network": "none",              # 默认断网
    "memory": None,                 # 如 "8g"
    "cpus": None,                   # 如 "4"
    "user": None,                   # 如 "1005:985"
    "read_only": False,             # 只读 rootfs
    "mounts": None,                 # 额外挂载 ["/host:/container[:ro]", ...]
    "workdir": True,                # 在容器内把工作目录设为 cwd（host 同路径）
}


class SandboxUnavailable(RuntimeError):
    """仅在 ``sandbox_strict`` 且所选档位不可用时抛出。"""


class SandboxBackend:
    """执行后端协议基类。"""

    name = "base"

    def available(self) -> tuple[bool, str]:
        return True, ""

    # ---- 前台：返回 (argv, kwargs) 交给 subprocess.run(argv, **kwargs) ----
    def fg(self, command: str, cwd, timeout: int) -> tuple:
        raise NotImplementedError

    # ---- 后台：返回 (script, launch_argv)。
    # script 会写入 command.sh；launch_argv 交给 subprocess.Popen(launch_argv, cwd=cwd)，
    # 其 stdout/stderr 由调用方重定向到 log。launch_argv 里 nohup 的即 script_path。
    def bg(self, command: str, cwd, task_id: str, pidf, log, script_path) -> tuple:
        raise NotImplementedError


# --------------------------------------------------------------------------- #
# Tier 0：宿主
# --------------------------------------------------------------------------- #
class HostBackend(SandboxBackend):
    """与改造前完全一致的宿主执行（默认档）。"""

    name = "host"

    def fg(self, command: str, cwd, timeout: int) -> tuple:
        return command, {
            "shell": True, "executable": "/bin/bash",
            "cwd": str(cwd), "timeout": timeout,
        }

    def bg(self, command: str, cwd, task_id: str, pidf, log, script_path) -> tuple:
        script = (f'cd "{cwd}" && echo $$ > "{pidf}"\n'
                  f'exec bash -c {json.dumps(command)}\n')
        launch = ["/bin/bash", "-c",
                  f'nohup /bin/bash "{script_path}" > "{log}" 2>&1 &']
        return script, launch


# --------------------------------------------------------------------------- #
# Tier 1：宿主 + stdlib rlimit（弱隔离）
# --------------------------------------------------------------------------- #
class RlimitBackend(SandboxBackend):
    """宿主进程 + ``resource.setrlimit`` 资源上限。零依赖、低风险。

    注意：**会改变语义**——原本可用超过上限内存/CPU 的命令会失败。故默认仅给宽松的内存上限。
    """

    name = "rlimit"

    def __init__(self, spec: dict | None = None):
        self.spec = {**DEFAULT_RLIMIT_SPEC, **(spec or {})}

    def _preexec(self):
        """在 fork 后、exec 前设置资源上限（惰性导入 resource）。"""
        import resource  # noqa: WPS433 (惰性：顶层不引入)

        def _set(kind, value):
            if value is None:
                return
            try:
                soft, hard = resource.getrlimit(kind)
                hard = value if hard in (resource.RLIM_INFINITY, -1) else min(hard, value)
                resource.setrlimit(kind, (value, hard))
            except (ValueError, OSError):
                pass

        _set(resource.RLIMIT_AS, self.spec.get("as"))
        _set(resource.RLIMIT_CPU, self.spec.get("cpu"))
        _set(resource.RLIMIT_NOFILE, self.spec.get("nofile"))
        _set(resource.RLIMIT_NPROC, self.spec.get("nproc"))

    def fg(self, command: str, cwd, timeout: int) -> tuple:
        return command, {
            "shell": True, "executable": "/bin/bash",
            "cwd": str(cwd), "timeout": timeout,
            "preexec_fn": self._preexec,
        }

    def _ulimit_prefix(self) -> str:
        """后台脚本无法用 preexec_fn（nohup 会脱离），改用内联 ``ulimit``。"""
        parts = []
        as_bytes = self.spec.get("as")
        if as_bytes:
            parts.append(f"ulimit -v {int(as_bytes) // 1024}")
        cpu = self.spec.get("cpu")
        if cpu:
            parts.append(f"ulimit -t {int(cpu)}")
        nofile = self.spec.get("nofile")
        if nofile:
            parts.append(f"ulimit -n {int(nofile)}")
        nproc = self.spec.get("nproc")
        if nproc:
            parts.append(f"ulimit -u {int(nproc)}")
        return ("; ".join(parts) + "; ") if parts else ""

    def bg(self, command: str, cwd, task_id: str, pidf, log, script_path) -> tuple:
        prefix = self._ulimit_prefix()
        script = (f'{prefix}cd "{cwd}" && echo $$ > "{pidf}"\n'
                  f'exec bash -c {json.dumps(command)}\n')
        launch = ["/bin/bash", "-c",
                  f'nohup /bin/bash "{script_path}" > "{log}" 2>&1 &']
        return script, launch


# --------------------------------------------------------------------------- #
# Tier 2：docker run（强隔离）
# --------------------------------------------------------------------------- #
class DockerBackend(SandboxBackend):
    """``docker run`` 强隔离。

    与编排后台机制的耦合（唯一真风险点）：后台脚本采用**宿主监督 pid** 方案——
    ``echo $$`` 写在宿主 shell，随后 ``exec docker run --rm`` 前台接管，故该 pid 有效且生命周期
    = 容器生命周期，``TaskOutput`` 的宿主 ``os.kill`` 判活无需改动。要求把 ``cwd`` 以**相同宿主路径**
    bind-mount 进容器，使编排脚本写入的 ``.done`` 落回宿主（自动推进/``--resume`` 依赖它）。
    """

    name = "docker"

    def __init__(self, spec: dict | None = None):
        self.spec = {**DEFAULT_DOCKER_SPEC, **(spec or {})}

    # ---- 可用性探测（永不抛） ----
    @classmethod
    def probe(cls, spec: dict | None = None) -> tuple[bool, str]:
        spec = {**DEFAULT_DOCKER_SPEC, **(spec or {})}
        if not spec.get("image"):
            return False, "未配置容器镜像（sandbox_spec.image）"
        if shutil.which("docker") is None:
            return False, "未找到 docker 可执行文件"
        try:
            p = subprocess.run(["docker", "info"], capture_output=True,
                               text=True, timeout=8)
        except (OSError, subprocess.TimeoutExpired) as e:
            return False, f"docker 不可用（{type(e).__name__}）"
        if p.returncode != 0:
            return False, "docker daemon 未运行或无权限"
        return True, ""

    def available(self) -> tuple[bool, str]:
        return self.probe(self.spec)

    def _docker_argv(self, command: str, cwd, task_id: str):
        s = self.spec
        argv = ["docker", "run", "--rm", "--name", f"traitpilot-{task_id}"]
        argv += ["--network", str(s.get("network") or "none")]
        if s.get("memory"):
            argv += ["--memory", str(s["memory"])]
        if s.get("cpus"):
            argv += ["--cpus", str(s["cpus"])]
        if s.get("user"):
            argv += ["--user", str(s["user"])]
        if s.get("read_only"):
            argv += ["--read-only"]
        cwd = str(cwd)
        # 主工作目录以相同宿主路径挂载（后台 .done/.pid 回写宿主、前台读写一致）
        argv += ["-v", f"{cwd}:{cwd}"]
        for m in (s.get("mounts") or []):
            argv += ["-v", str(m)]
        if s.get("workdir", True):
            argv += ["-w", cwd]
        argv += [str(s["image"]), "/bin/bash", "-c", command]
        return argv

    def fg(self, command: str, cwd, timeout: int) -> tuple:
        argv = self._docker_argv(command, cwd, "fg")
        return argv, {"cwd": str(cwd), "timeout": timeout}

    def bg(self, command: str, cwd, task_id: str, pidf, log, script_path) -> tuple:
        # 宿主监督：先写宿主 pid，再 exec 前台 docker run —— 该 pid 在整段后台任务期间保持有效，
        # TaskOutput 的宿主判活因此正确；--rm 随容器退出清理。
        argv = self._docker_argv(command, cwd, task_id)
        run = " ".join(shlex.quote(a) for a in argv)
        script = (f'cd "{cwd}" && echo $$ > "{pidf}"\n'
                  f'exec {run}\n')
        launch = ["/bin/bash", "-c",
                  f'nohup /bin/bash "{script_path}" > "{log}" 2>&1 &']
        return script, launch


# --------------------------------------------------------------------------- #
# 工厂
# --------------------------------------------------------------------------- #
_BUILDERS = {
    "host": lambda spec: HostBackend(),
    "rlimit": lambda spec: RlimitBackend(spec),
    "docker": lambda spec: DockerBackend(spec),
}


def build_backend(cfg=None) -> tuple[SandboxBackend, str | None]:
    """按 ``cfg.sandbox`` 选择后端。返回 ``(backend, warning_or_None)``。

    fail-soft：所选档位不可用（如未装 docker / 未给镜像）时回退 ``HostBackend`` 并返回提示；
    仅当 ``cfg.sandbox_strict`` 为真时抛 ``SandboxUnavailable``。
    """
    tier = (getattr(cfg, "sandbox", "host") or "host")
    spec = getattr(cfg, "sandbox_spec", None) or {}
    strict = bool(getattr(cfg, "sandbox_strict", False))

    def _fallback(msg: str) -> tuple[SandboxBackend, str | None]:
        if strict:
            raise SandboxUnavailable(msg)
        return HostBackend(), msg

    builder = _BUILDERS.get(tier)
    if builder is None:
        return _fallback(f"M9：未知沙箱档位 {tier!r}，已回退宿主执行。")

    backend = builder(spec)
    ok, reason = backend.available()
    if not ok:
        return _fallback(f"M9：沙箱档位 {tier} 不可用（{reason}），已回退宿主执行。")
    return backend, None
