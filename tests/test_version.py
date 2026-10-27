# -*- coding: utf-8 -*-
"""版本载体（VERSION 文件 → `__version__` → `--version` / REPL）的可读性测试。

**为什么需要这些测试**：版本号在本仓库曾经是**纯装饰**——
`skill_agent/__init__.py` 里写死的 `__version__ = "0.1.0"` 自 2026-10-15 起
从未被任何代码读取，没有 `--version`、没有 CHANGELOG、没有 git tag。
后果实测：2026-10-26 那次提交信息就叫 `version change`，而版本号一字未动 ——
"发布了新版本"这件事在仓库里没有任何可观察的痕迹，全靠提交信息自述。

所以这里测的不是"常量等于某个值"（那种测试只会随改版本一起改、从不失败），
而是**载体链是否真的接通**：一处改动能同时反映到 VERSION 文件、`__version__`、
`--version` 输出三者。断链即失败。
"""
import os
import re
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VERSION_FILE = os.path.join(REPO, "VERSION")
PY = sys.executable

SEMVER = re.compile(r"^\d+\.\d+\.\d+$")


def test_version_file_exists_and_is_semver():
    """VERSION 文件存在，且是干净的 `X.Y.Z`（无 v 前缀/无多余空白）。"""
    assert os.path.isfile(VERSION_FILE), (
        f"缺少 {VERSION_FILE} —— 版本号的唯一载体不见了。"
        f"若改用了别的载体（pyproject/setup.py），本测试要跟着改，而不是删掉。")
    raw = open(VERSION_FILE, encoding="utf-8").read()
    v = raw.strip()
    assert SEMVER.match(v), f"VERSION 内容不是 X.Y.Z 形式: {raw!r}"
    assert raw == v + "\n", (
        f"VERSION 文件应只有一行且以换行结尾（避免读取方各自 strip、比对时对不上）：{raw!r}")


def test_dunder_version_matches_version_file():
    """`__version__` 必须等于 VERSION 文件内容 —— 两处硬编码就会漂移，这里只允许一处。"""
    sys.path.insert(0, REPO)
    from skill_agent import __version__
    want = open(VERSION_FILE, encoding="utf-8").read().strip()
    assert __version__ == want, (
        f"__version__={__version__!r} 与 VERSION 文件 {want!r} 不一致 —— "
        f"说明又出现了第二处硬编码的版本号")


def test_cli_version_flag_prints_and_exits_zero():
    """`--version` 真的可读（这是"版本号无人可读"的直接回归防线）。

    子进程跑真 CLI：`--version` 由 argparse 的 action="version" 处理，
    它在解析参数后立刻退出，**不应该**要求 API key 或 config.json ——
    这也是本测试的价值：若有人把版本打印挪到 Config.load 之后，
    在没有凭证的环境里 `--version` 就会失败，此处会红。
    """
    p = subprocess.run([PY, os.path.join(REPO, "run.py"), "--version"],
                       capture_output=True, text=True, cwd=REPO,
                       env={k: v for k, v in os.environ.items()
                            if k not in ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY")})
    assert p.returncode == 0, f"`run.py --version` rc={p.returncode}；stderr={p.stderr[-400:]}"
    out = p.stdout.strip()
    want = open(VERSION_FILE, encoding="utf-8").read().strip()
    assert want in out, f"`--version` 输出里没有版本号 {want!r}：{out!r}"


def test_module_entry_also_supports_version():
    """`python -m skill_agent --version` 与 `run.py --version` 同源同值。"""
    p = subprocess.run([PY, "-m", "skill_agent", "--version"],
                       capture_output=True, text=True, cwd=REPO)
    assert p.returncode == 0, f"rc={p.returncode}；stderr={p.stderr[-400:]}"
    want = open(VERSION_FILE, encoding="utf-8").read().strip()
    assert want in p.stdout, f"输出里没有 {want!r}：{p.stdout!r}"


def test_missing_version_file_does_not_crash_import():
    """VERSION 缺失时回退成占位版本号，**不得抛异常**。

    场景真实：把 `skill_agent/` 单独拷进另一个项目（没有仓库根的 VERSION）时，
    `import skill_agent` 若因此失败，等于把"版本号查不到"升级成"整个包用不了"。
    """
    import importlib.util
    import shutil
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        pkg = os.path.join(tmp, "skill_agent")
        shutil.copytree(os.path.join(REPO, "skill_agent"), pkg,
                        ignore=shutil.ignore_patterns("__pycache__"))
        # 该副本的上一级没有 VERSION 文件
        assert not os.path.exists(os.path.join(tmp, "VERSION"))
        spec = importlib.util.spec_from_file_location(
            "isolated_vt", os.path.join(pkg, "__init__.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)      # 不抛异常即通过
        assert "unknown" in mod.__version__, (
            f"没有 VERSION 文件时应回退到占位值，实际 {mod.__version__!r}")
