"""config.json 与 skills/ 目录发现（脱离 Claude Code）的测试。"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from skill_agent.config import Config, load_config_file, default_skills_dir  # noqa: E402
from skill_agent.skills import SkillRegistry  # noqa: E402


# ================= config.json =================

def _write_cfg(d, data):
    p = Path(d) / "config.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


def test_config_file_supplies_credentials():
    with tempfile.TemporaryDirectory() as d:
        _write_cfg(d, {"api_key": "sk-from-file", "base_url": "http://file.example",
                       "model": "model-from-file"})
        env_backup = {k: os.environ.pop(k, None)
                      for k in ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY",
                                "ANTHROPIC_BASE_URL", "SKILL_AGENT_MODEL")}
        try:
            cfg = Config.load(project=Path(d), cwd=Path(d))
            assert cfg.api_key == "sk-from-file"
            assert cfg.base_url == "http://file.example"
            assert cfg.model == "model-from-file"
        finally:
            for k, v in env_backup.items():
                if v is not None:
                    os.environ[k] = v


def test_env_overrides_config_file():
    with tempfile.TemporaryDirectory() as d:
        _write_cfg(d, {"api_key": "sk-from-file"})
        old = os.environ.get("ANTHROPIC_AUTH_TOKEN")
        os.environ["ANTHROPIC_AUTH_TOKEN"] = "sk-from-env"
        try:
            cfg = Config.load(project=Path(d), cwd=Path(d))
            assert cfg.api_key == "sk-from-env"
        finally:
            if old is None:
                os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)
            else:
                os.environ["ANTHROPIC_AUTH_TOKEN"] = old


def test_missing_or_invalid_config_does_not_raise():
    with tempfile.TemporaryDirectory() as d:
        # 进到空目录，避免命中仓库根 ./config.json（load_config_file 会回退到 cwd）
        old_cwd = os.getcwd()
        os.chdir(d)
        try:
            assert load_config_file(Path(d)) == {}     # 该目录无 config.json
            bad = Path(d) / "config.json"
            bad.write_text("{not valid json", encoding="utf-8")
            assert load_config_file(Path(d)) == {}
        finally:
            os.chdir(old_cwd)


def test_config_file_toggles_and_lists():
    with tempfile.TemporaryDirectory() as d:
        _write_cfg(d, {"api_key": "x", "retrieval": True, "todo": True,
                       "fallback_models": ["m1", "m2"]})
        old = os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)
        try:
            cfg = Config.load(project=Path(d), cwd=Path(d))
            assert cfg.retrieval is True and cfg.todo is True
            assert cfg.fallback_models == ["m1", "m2"]
        finally:
            if old is not None:
                os.environ["ANTHROPIC_AUTH_TOKEN"] = old


def test_config_file_controls_all_capabilities():
    """config.json 作为单一控制面板：所有可选能力键都能生效。"""
    data = {
        "api_key": "x",
        "retrieval": True, "todo": True, "parallel_tools": True, "prompt_cache": True,
        "loop_guard": True, "idempotency": True, "restrict_paths": True,
        "write_approval": True, "summary_compaction": True, "memory": True,
        "persist": True, "spill": True, "trace": True,
        "sandbox": "rlimit", "sandbox_strict": True, "sandbox_spec": {"memory": "4g"},
        "max_turns": 120, "max_seconds": 3600, "max_output_tokens": 400000,
        "loop_halt": 8, "spill_threshold": 12345,
        "idempotency_mode": "deny", "index_dir": "/tmp/idx",
        "retrieval_roots": ["/a", "/b"], "fallback_models": ["m1"],
    }
    with tempfile.TemporaryDirectory() as d:
        _write_cfg(d, data)
        old = os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)
        try:
            cfg = Config.load(project=Path(d), cwd=Path(d))
            assert cfg.retrieval and cfg.todo and cfg.parallel_tools and cfg.prompt_cache
            assert cfg.loop_guard and cfg.idempotency
            assert cfg.restrict_paths and cfg.write_approval
            assert cfg.summary_compaction and cfg.memory_enabled and cfg.persist
            assert cfg.hooks_enabled is True          # trace: true
            assert cfg.sandbox == "rlimit" and cfg.sandbox_strict
            assert cfg.sandbox_spec == {"memory": "4g"}
            assert cfg.max_turns == 120 and cfg.max_seconds == 3600
            assert cfg.max_output_tokens == 400000 and cfg.loop_halt == 8
            assert cfg.spill_dir and cfg.spill_threshold == 12345
            assert cfg.idempotency_mode == "deny" and cfg.index_dir == "/tmp/idx"
            assert cfg.retrieval_roots == ["/a", "/b"]
            assert cfg.fallback_models == ["m1"]
        finally:
            if old is not None:
                os.environ["ANTHROPIC_AUTH_TOKEN"] = old


def test_config_file_ignores_wrong_types():
    with tempfile.TemporaryDirectory() as d:
        _write_cfg(d, {"api_key": "x", "retrieval": "yes", "max_turns": "abc",
                       "sandbox": "nonsense", "fallback_models": 5})
        old = os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)
        try:
            cfg = Config.load(project=Path(d), cwd=Path(d))
            assert cfg.retrieval is False       # 非 bool 忽略
            assert cfg.max_turns is None         # 非整数忽略
            assert cfg.sandbox == "host"         # 非法档位忽略
            assert not cfg.fallback_models       # 非 list/str 忽略
        finally:
            if old is not None:
                os.environ["ANTHROPIC_AUTH_TOKEN"] = old


def test_no_credential_error_mentions_config_json():
    with tempfile.TemporaryDirectory() as d:
        env_backup = {k: os.environ.pop(k, None)
                      for k in ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY",
                                "SKILL_AGENT_CONFIG")}
        old_cwd = os.getcwd()
        os.chdir(d)     # 避免回退命中仓库根 ./config.json
        try:
            try:
                Config.load(project=Path(d), cwd=Path(d))
            except SystemExit as e:
                assert "config.json" in str(e)
            else:
                raise AssertionError("应因缺凭证抛出 SystemExit")
        finally:
            os.chdir(old_cwd)
            for k, v in env_backup.items():
                if v is not None:
                    os.environ[k] = v


def test_config_file_resolves_skills_dir_and_cwd():
    with tempfile.TemporaryDirectory() as d:
        proj = Path(d)
        (proj / "my-skills").mkdir()
        (proj / "work").mkdir()
        _write_cfg(d, {"api_key": "x", "skills_dir": "my-skills",
                       "cwd": "work", "max_tokens": 4096, "permission": "ask"})
        old = os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)
        try:
            cfg = Config.load(project=proj)
            assert cfg.skills_dir == proj / "my-skills"   # 相对路径按项目根解析
            assert cfg.cwd == proj / "work"
            assert cfg.max_tokens == 4096
            assert cfg.permission == "ask"
        finally:
            if old is not None:
                os.environ["ANTHROPIC_AUTH_TOKEN"] = old


def test_cli_skills_dir_beats_config_file():
    with tempfile.TemporaryDirectory() as d:
        proj = Path(d)
        _write_cfg(d, {"api_key": "x", "skills_dir": "my-skills"})
        old = os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)
        try:
            cfg = Config.load(project=proj, skills_dir=proj / "explicit")
            assert cfg.skills_dir == proj / "explicit"
        finally:
            if old is not None:
                os.environ["ANTHROPIC_AUTH_TOKEN"] = old


# ================= skills 目录发现 =================

def _skill(root: Path, name: str, desc: str = "测试技能"):
    p = root / name
    p.mkdir(parents=True, exist_ok=True)
    (p / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {desc}\n---\n\n指令正文。\n", encoding="utf-8")
    return p


def test_discovers_project_skills_dir():
    with tempfile.TemporaryDirectory() as d:
        proj = Path(d)
        _skill(proj / "skills", "alpha")
        reg = SkillRegistry(proj)
        assert "alpha" in reg.names()


def test_falls_back_to_legacy_claude_skills():
    with tempfile.TemporaryDirectory() as d:
        proj = Path(d)
        _skill(proj / ".claude" / "skills", "legacy-one")
        reg = SkillRegistry(proj)
        assert "legacy-one" in reg.names()


def test_skills_dir_overrides_and_prefers_own_over_legacy():
    with tempfile.TemporaryDirectory() as d:
        proj = Path(d)
        _skill(proj / "skills", "own")
        _skill(proj / ".claude" / "skills", "legacy")
        reg = SkillRegistry(proj)
        names = reg.names()
        assert "own" in names and "legacy" not in names

    with tempfile.TemporaryDirectory() as d:
        proj = Path(d)
        custom = Path(d) / "elsewhere"
        _skill(custom, "custom-skill")
        reg = SkillRegistry(proj, skills_dir=custom)
        assert "custom-skill" in reg.names()


def test_default_skills_dir_prefers_own():
    with tempfile.TemporaryDirectory() as d:
        proj = Path(d)
        _skill(proj / "skills", "x")
        assert default_skills_dir(proj) == proj / "skills"
    with tempfile.TemporaryDirectory() as d:
        proj = Path(d)
        _skill(proj / ".claude" / "skills", "x")
        assert default_skills_dir(proj) == proj / ".claude" / "skills"


def test_config_env_block_injects_os_environ():
    """config.json 的 `env` 块应注入进程环境（技能脚本据此读写数据路径）。"""
    from skill_agent.config import _apply_file_overrides
    with tempfile.TemporaryDirectory() as d:
        _write_cfg(d, {"env": {"GENO_DIR": "/tmp/tgeno", "PIPELINE_DB": "/tmp/t.db"}})
        os.environ.pop("GENO_DIR", None)
        os.environ.pop("PIPELINE_DB", None)
        try:
            cfg = Config(base_url="u", api_key="k", model="m")
            _apply_file_overrides(cfg, load_config_file(project=Path(d)))
            assert os.environ.get("GENO_DIR") == "/tmp/tgeno"
            assert os.environ.get("PIPELINE_DB") == "/tmp/t.db"
            # 已存在的真实环境变量不被 config.json 覆盖
            os.environ["GENO_DIR"] = "/already"
            _apply_file_overrides(cfg, load_config_file(project=Path(d)))
            assert os.environ.get("GENO_DIR") == "/already"
        finally:
            for k in ("GENO_DIR", "PIPELINE_DB"):
                os.environ.pop(k, None)


def test_config_env_block_ignores_malformed():
    from skill_agent.config import _apply_file_overrides
    with tempfile.TemporaryDirectory() as d:
        _write_cfg(d, {"env": {"X_OK": "v", "BAD": "", "NUM": 3}})
        os.environ.pop("X_OK", None)
        try:
            cfg = Config(base_url="u", api_key="k", model="m")
            _apply_file_overrides(cfg, load_config_file(project=Path(d)))
            assert os.environ.get("X_OK") == "v"
            assert "BAD" not in os.environ
            assert os.environ.get("NUM") == "3"
        finally:
            for k in ("X_OK", "BAD", "NUM"):
                os.environ.pop(k, None)
