"""技能注册表：扫描 .claude/skills，解析 SKILL.md frontmatter，提供指令加载。

与 Claude Code 的技能约定一致：
  .claude/skills/<name>/SKILL.md
  frontmatter: name(必需) / description / allowed-tools / disable-model-invocation

同时兼容用户级目录 ~/.claude/skills 下的裸 .md 文件（视为无 frontmatter 的普通指令）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

SKILL_FILE = "SKILL.md"
MAX_INSTRUCTIONS = 40_000


@dataclass
class Skill:
    name: str
    description: str
    dir_path: Path
    frontmatter: dict = field(default_factory=dict)
    is_plain_md: bool = False      # 裸 .md 文件（无 frontmatter）视为普通指令

    @property
    def model_invocable(self) -> bool:
        """disable-model-invocation: false 表示模型可自动调用；true（或缺省）需用户显式要求。"""
        return self.frontmatter.get("disable-model-invocation", True) is False

    @property
    def allowed_tools(self) -> list[str]:
        tools = self.frontmatter.get("allowed-tools") or []
        if isinstance(tools, str):
            tools = [t.strip() for t in tools.split(",")]
        return [t for t in tools if isinstance(t, str)]

    @property
    def skel_md_path(self) -> Path:
        return self.dir_path / SKILL_FILE if not self.is_plain_md else self.dir_path

    def full_content(self) -> str:
        return self.skel_md_path.read_text(encoding="utf-8", errors="replace")

    def instructions(self) -> str:
        """Skill 工具返回的核心内容：SKILL.md 全文(+usage-guide) + 目录文件清单。"""
        parts: list[str] = []
        try:
            parts.append(self.full_content().strip())
        except OSError as e:
            return f"（读取 {self.skel_md_path} 失败：{e}）"

        # 若存在 usage-guide.md，一并拼上（体积小、含错误预防清单）
        usage = self.dir_path / "usage-guide.md"
        if usage.is_file():
            try:
                guide = usage.read_text(encoding="utf-8", errors="replace").strip()
                if guide:
                    parts.append("\n\n---\n\n### usage-guide.md\n\n" + guide)
            except OSError:
                pass

        # 目录文件清单，供模型按需 Read/执行
        try:
            files = sorted(p.relative_to(self.dir_path) for p in self.dir_path.rglob("*")
                           if p.is_file() and "__pycache__" not in p.parts and ".pyc" not in p.name)
            if files:
                listing = "\n".join(f"- {f}" for f in files)
                parts.append(f"\n\n---\n\n**Skill 目录文件清单（{self.dir_path}，可继续 Read 需要的）：**\n{listing}")
        except OSError:
            pass

        text = "\n".join(parts)
        if len(text) > MAX_INSTRUCTIONS:
            text = text[:MAX_INSTRUCTIONS] + "\n…[内容超长已截断]"
        return text


class SkillRegistry:
    def __init__(self, project: Path):
        self.project = Path(project)
        self.skills: dict[str, Skill] = {}
        self._discover()

    def _discover(self) -> None:
        paths = []
        # 1) 项目级：<project>/.claude/skills/<name>/SKILL.md
        proj_dir = self.project / ".claude" / "skills"
        if proj_dir.is_dir():
            paths.append(proj_dir)
        # 2) 用户级：~/.claude/skills
        user_dir = Path.home() / ".claude" / "skills"
        if user_dir.is_dir():
            paths.append(user_dir)

        for base in paths:
            for p in sorted(base.iterdir()):
                if p.is_dir():
                    skel = p / SKILL_FILE
                    if not skel.is_file():
                        continue
                    meta = _parse_frontmatter(skel)
                    name = meta.pop("name", None) or p.name
                    if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
                        name = p.name
                    desc = meta.pop("description", "") or f"技能 {name}"
                    self.skills[name] = Skill(
                        name=name, description=_clean(desc), dir_path=p,
                        frontmatter=meta, is_plain_md=False,
                    )
                elif p.suffix == ".md" and p.is_file():
                    # 用户级裸 .md 文件
                    meta = _parse_frontmatter(p) if p.name != SKILL_FILE else {}
                    name = meta.pop("name", None) or p.stem
                    desc = meta.pop("description", "") or f"技能 {name}"
                    # 覆盖注册（项目级优先级已保证：p.name!=SKILL_FILE 时不会撞）
                    prev = self.skills.get(name)
                    if prev and prev.dir_path != p:
                        continue  # 已存在同名，保留先注册的
                    self.skills[name] = Skill(
                        name=name, description=_clean(desc), dir_path=p,
                        frontmatter=meta, is_plain_md=True,
                    )
        # 未命中 path 时用生成名兜底，避免空名
        self.skills = dict(sorted(self.skills.items()))

    # ---- 查询 ----
    def get(self, name: str) -> Skill | None:
        return self.skills.get(name)

    def names(self) -> list[str]:
        return list(self.skills)

    def listing(self) -> str:
        """注入 system prompt 的技能清单表。"""
        if not self.skills:
            return "（当前未发现任何 skill）"
        rows = ["| 技能 | 自动可调 | 用途 |",
                "|------|:---:|------|"]
        for name, s in self.skills.items():
            auto = "✓" if s.model_invocable else ""
            desc = s.description.replace("|", "\\|").replace("\n", " ")
            if len(desc) > 150:
                desc = desc[:150] + "…"
            rows.append(f"| `{name}` | {auto} | {desc} |")
        return "\n".join(rows)


# ---------- frontmatter ----------

def _parse_frontmatter(path: Path) -> dict:
    """'---' 包裹的 YAML frontmatter 解析；失败则安全降级为空 dict。"""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end < 0:
        return {}
    try:
        data = yaml.safe_load(text[3:end])
        return data if isinstance(data, dict) else {}
    except yaml.YAMLError:
        return {}


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()
