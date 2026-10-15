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
        assert load_config_file(Path(d)) == {}
        bad = Path(d) / "config.json"
        bad.write_text("{not valid json", encoding="utf-8")
        assert load_config_file(Path(d)) == {}


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


def test_no_credential_error_mentions_config_json():
    with tempfile.TemporaryDirectory() as d:
        env_backup = {k: os.environ.pop(k, None)
                      for k in ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY")}
        try:
            try:
                Config.load(project=Path(d), cwd=Path(d))
            except SystemExit as e:
                assert "config.json" in str(e)
            else:
                raise AssertionError("应因缺凭证抛出 SystemExit")
        finally:
            for k, v in env_backup.items():
                if v is not None:
                    os.environ[k] = v


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
