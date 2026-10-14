"""配置：与 Claude Code 复用同一组配置来源，不写死 token。

读取优先级：
  1) 环境变量（ANTHROPIC_BASE_URL / ANTHROPIC_AUTH_TOKEN / ANTHROPIC_API_KEY）
  2) Claude Code 的用户级 ~/.claude/settings.json 的 env 块（单一来源，兼容已有配置）
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_MODEL = "claude-opus-4-5"
DEFAULT_MAX_TOKENS = 8192


def _read_user_settings_env() -> dict:
    """从 ~/.claude/settings.json 的 env 块取 ANTHROPIC_* 变量。"""
    try:
        raw = Path.home() / ".claude" / "settings.json"
        sid = json.loads(raw.read_text(encoding="utf-8"))
        return {k: v for k, v in (sid.get("env") or {}).items()
                if k.startswith("ANTHROPIC_")}
    except Exception:
        return {}


@dataclass
class Config:
    base_url: str
    api_key: str
    model: str
    max_tokens: int = DEFAULT_MAX_TOKENS
    cwd: Path = Path(".")          # 工具执行的 cwd（默认=项目根）
    project: Path = Path(".")      # 扫描 .claude/skills 的项目目录
    permission: str = "auto"       # auto | ask（ask=每个 Bash 都询问）
    stream: bool = True
    verbose: bool = False
    session_log: Path | None = None
    # M8a 结构化权限（默认全部关闭 → 行为与改造前一致）
    restrict_paths: bool = False           # 工作区路径边界
    permission_roots: list | None = None   # 额外允许根
    extra_roots: list | None = None        # 系统必要路径白名单（如 ~/.claude）
    write_approval: bool = False           # 把 Write/Edit 纳入审批
    permission_rules: list | None = None   # [{tool, match, action}]
    # M12 上下文压缩 / 分层记忆（默认关）
    summary_compaction: bool = False       # 摘要式压缩（需注入 summary 模型调用）
    memory_enabled: bool = False           # core + archival 记忆
    summary_model: str | None = None       # 用于生成摘要的模型（默认同主模型）
    # M13 会话持久化（默认关）
    persist: bool = False                  # 逐条消息落盘 + 支持 resume
    session_id: str | None = None
    # M1b 预算熔断（默认 None/0 = 不限制 → 行为与改造前一致）
    max_turns: int | None = None           # 覆盖内置 MAX_TURNS
    max_output_tokens: int | None = None   # 本轮累计输出 token 上限
    max_seconds: int | None = None         # 本轮墙钟时长上限（秒）
    warn_ratio: float = 0.8                # 达到上限的此比例时发一次提醒
    # M3 死循环熔断（默认关）
    loop_guard: bool = False
    loop_warn: int = 3                     # 连续无进展达此次数 → 注入纠偏提示
    loop_halt: int = 6                     # 连续无进展达此次数 → 熔断
    # M4 可插拔工具 / M10 钩子（默认关）
    plugin_dirs: list | None = None        # 扩展工具规格目录（*.json）
    plugin_allow: list | None = None       # 只加载这些插件工具名（None=全部）
    hooks_enabled: bool = False            # 预留：外部钩子由调用方注入 HookManager

    @classmethod
    def load(cls, *, model: str | None = None, project: Path | None = None,
             cwd: Path | None = None, permission: str | None = None,
             max_tokens: int | None = None, stream: bool | None = None,
             verbose: bool = False, max_turns: int | None = None) -> "Config":
        user_env = _read_user_settings_env()

        base_url = (os.environ.get("ANTHROPIC_BASE_URL")
                    or user_env.get("ANTHROPIC_BASE_URL")
                    or "https://api.anthropic.com").rstrip("/")
        api_key = (os.environ.get("ANTHROPIC_AUTH_TOKEN")
                   or os.environ.get("ANTHROPIC_API_KEY")
                   or user_env.get("ANTHROPIC_AUTH_TOKEN")
                   or user_env.get("ANTHROPIC_API_KEY"))
        if not api_key:
            raise SystemExit(
                "缺少 API 凭证：请在 ~/.claude/settings.json 的 env 块配置 "
                "ANTHROPIC_AUTH_TOKEN（或设置环境变量 ANTHROPIC_AUTH_TOKEN / "
                "ANTHROPIC_API_KEY），与 Claude Code 相同。"
            )

        # 项目根：显式指定 > 环境变量 > 从 cwd 向上找含 .claude/skills 的目录 > 直接取 cwd
        if project is None:
            project = _auto_project(cwd or Path.cwd())
        if cwd is None:
            cwd = project

        return cls(
            base_url=base_url,
            api_key=api_key,
            model=model or os.environ.get("SKILL_AGENT_MODEL") or DEFAULT_MODEL,
            max_tokens=max_tokens or DEFAULT_MAX_TOKENS,
            cwd=cwd,
            project=project,
            permission=permission or os.environ.get("SKILL_AGENT_PERMISSION", "auto"),
            stream=True if stream is None else stream,
            verbose=verbose,
            restrict_paths=_env_bool("SKILL_AGENT_RESTRICT_PATHS"),
            write_approval=_env_bool("SKILL_AGENT_WRITE_APPROVAL"),
            summary_compaction=_env_bool("SKILL_AGENT_SUMMARY_COMPACTION"),
            memory_enabled=_env_bool("SKILL_AGENT_MEMORY"),
            summary_model=os.environ.get("SKILL_AGENT_SUMMARY_MODEL") or None,
            persist=_env_bool("SKILL_AGENT_PERSIST"),
            session_id=os.environ.get("SKILL_AGENT_SESSION_ID") or None,
            max_turns=_env_int("SKILL_AGENT_MAX_TURNS", max_turns),
            max_output_tokens=_env_int("SKILL_AGENT_MAX_OUTPUT_TOKENS"),
            max_seconds=_env_int("SKILL_AGENT_MAX_SECONDS"),
            loop_guard=_env_bool("SKILL_AGENT_LOOP_GUARD"),
            plugin_dirs=_env_list("SKILL_AGENT_PLUGIN_DIRS"),
            hooks_enabled=_env_bool("SKILL_AGENT_HOOKS"),
        )


def _env_bool(name: str, default: bool = False) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default=None):
    v = os.environ.get(name)
    if v is None or not str(v).strip():
        return default
    try:
        return int(v)
    except ValueError:
        return default


def _env_list(name: str, sep: str = ":") -> list | None:
    """以分隔符拆分的路径列表（默认用 ':'，兼容 Linux PATH 风格）。"""
    v = os.environ.get(name)
    if not v or not v.strip():
        return None
    return [p for p in v.split(sep) if p.strip()] or None


def _auto_project(start: Path) -> Path:
    p = start.resolve()
    if p.is_file():
        p = p.parent
    for d in [p, *p.parents]:
        if (d / ".claude" / "skills").is_dir():
            return d
    return p


def default_session_dir() -> Path:
    d = Path.home() / ".skill-agent" / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    return d


def default_checkpoint_dir() -> Path:
    """M13：会话 checkpoint 存储目录。"""
    d = Path.home() / ".skill-agent" / "contexts"
    d.mkdir(parents=True, exist_ok=True)
    return d


def default_memory_dir(project: Path) -> Path:
    """M12：项目级记忆目录（core.md + archival.db）。"""
    return Path(project) / ".skill-agent" / "memory"
