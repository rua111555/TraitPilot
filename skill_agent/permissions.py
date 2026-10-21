"""结构化权限模型（M8a）。

把 ``tools.py`` 里「11 条正则黑名单 + 仅 Bash 的 auto/ask」升级为：

1. **结构化 allow/deny/ask 规则表**：按「工具 + 参数模式（正则）」声明动作，按序首次
   命中生效；未命中则回落到工具内置的默认判定（危险正则 / 全局 ask）。
2. **工作区路径边界**：``_resolve`` 在解析文件路径后校验其是否落在允许根内，越界抛错。
3. **写操作审批**：可选把 ``Write`` / ``Edit`` 纳入审批门（现状它们无任何审批）。

**默认全部关闭** —— 这是刻意的：路径边界是「行为破坏性变更」（工作区探测错误会误拒
合法访问，如 skill 读 ``~/.claude`` 或全局数据目录），且当前 Bash 直连宿主、权限门是
唯一防线。因此本模块以 opt-in 方式接入：未显式开启时行为与改造前**完全一致**。
"""
from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from pathlib import Path

# 每个工具「取哪个参数做规则匹配/预览」的约定
_PRIMARY_ARG = {
    "Bash": "command",
    "Read": "file_path",
    "Write": "file_path",
    "Edit": "file_path",
    "Glob": "path",
    "Grep": "path",
    "TaskOutput": "log_path",
}
_ACTIONS = ("allow", "deny", "ask")


@dataclass
class PermissionModel:
    """一次会话的权限策略快照（由 Config 构造，可被 CLI/Web 覆盖）。"""

    # 路径边界（默认关）
    restrict_paths: bool = False
    roots: list[Path] = field(default_factory=list)          # 允许根（含工作区）
    extra_roots: list[str] = field(default_factory=list)     # 系统必要路径白名单
    # 写操作审批（默认关）
    write_approval: bool = False
    write_tools: tuple[str, ...] = ("Write", "Edit")
    # 结构化规则表（默认空）；每项 {"tool": name|"*", "match": regex|glob, "action": ...}
    rules: list[dict] = field(default_factory=list)

    # ---------------- 构造 ----------------
    @classmethod
    def from_config(cls, cfg, cwd: Path) -> "PermissionModel":
        roots = [Path(cwd).resolve()]
        for r in getattr(cfg, "permission_roots", None) or []:
            try:
                roots.append(Path(r).expanduser().resolve())
            except Exception:
                pass
        return cls(
            restrict_paths=bool(getattr(cfg, "restrict_paths", False)),
            roots=roots,
            extra_roots=list(getattr(cfg, "extra_roots", None) or []),
            write_approval=bool(getattr(cfg, "write_approval", False)),
            rules=list(getattr(cfg, "permission_rules", None) or []),
        )

    @property
    def enabled(self) -> bool:
        return self.restrict_paths or self.write_approval or bool(self.rules)

    # ---------------- 路径边界 ----------------
    def check_path(self, path: Path) -> str | None:
        """返回越界原因或 None。仅在 ``restrict_paths`` 开启时生效。"""
        if not self.restrict_paths:
            return None
        try:
            p = Path(path).resolve()
        except OSError:
            return None
        for root in self._all_roots():
            if _is_relative_to(p, root):
                return None
        allowed = "、".join(str(r) for r in self._all_roots())
        return (f"路径越出允许的工作区边界：{p}\n"
                f"允许根：{allowed}\n"
                f"如需访问，请把它加入 extra_roots（或关闭 restrict_paths）。")

    def _all_roots(self) -> list[Path]:
        roots = list(self.roots)
        for r in self.extra_roots:
            try:
                roots.append(Path(r).expanduser().resolve())
            except Exception:
                pass
        return roots

    # ---------------- 审批判定 ----------------
    def needs_confirm(self, name: str, args: dict) -> tuple[bool, str]:
        """返回 (是否需要用户确认, 原因)。仅补充判定，不改变默认关闭时的行为。"""
        primary = str(args.get(_PRIMARY_ARG.get(name, ""), "") or "")

        # 1) 结构化规则表（按序首次命中）
        for rule in self.rules:
            if not isinstance(rule, dict):
                continue
            rtool = rule.get("tool", "*")
            if rtool not in ("*", name):
                continue
            if not _rule_match(rule.get("match"), primary):
                continue
            action = rule.get("action")
            if action == "deny":
                return True, f"命中 deny 规则（tool={rtool}, match={rule.get('match')!r}）"
            if action == "ask":
                return True, f"命中 ask 规则（tool={rtool}, match={rule.get('match')!r}）"
            if action == "allow":
                return False, ""

        # 2) 写操作审批
        if self.write_approval and name in self.write_tools:
            return True, "写操作审批已开启"
        return False, ""

    def is_denied(self, name: str, args: dict) -> str | None:
        """显式 deny 返回原因（供调用方直接拒绝，不进入交互确认）。

        必须与 ``needs_confirm`` 用同一套「按序首次命中」语义：一旦某条 **allow/ask**
        规则先命中，后面的 deny 就不再生效。否则 ``rules=[allow ^echo, deny ^echo]``
        里 allow 形同虚设、和模块自称的「按序首次命中」自相矛盾。
        """
        primary = str(args.get(_PRIMARY_ARG.get(name, ""), "") or "")
        for rule in self.rules:
            if not isinstance(rule, dict):
                continue
            rtool = rule.get("tool", "*")
            if rtool not in ("*", name):
                continue
            if not _rule_match(rule.get("match"), primary):
                continue
            if rule.get("action") == "deny":
                return f"命中 deny 规则：tool={rtool}, match={rule.get('match')!r}"
            return None                # 首次命中的是 allow/ask → 不再是「显式 deny」
        return None

    def preview(self, name: str, args: dict) -> str:
        """给审批弹窗/提示用的简短预览。"""
        primary = str(args.get(_PRIMARY_ARG.get(name, ""), "") or "")
        if primary:
            return primary[:200]
        return f"{name} {args}"


def _rule_match(match, text: str) -> bool:
    """match 缺省→任意；支持逗号分隔多条。

    先用正则 ``re.search`` 判定（多数模式如 ``^curl``、``.*``、``rm\\s+-rf`` 都是正则）；
    正则无法编译或未命中时，回退 fnmatch glob（支持 ``*.env`` 这类写法）。两者都尝试，
    取并集，避免把正则 ``.*`` 误当 glob。
    """
    if not match:
        return True
    for pat in str(match).split(","):
        pat = pat.strip()
        if not pat:
            continue
        try:
            if re.search(pat, text):
                return True
        except re.error:
            pass
        if any(ch in pat for ch in "*?[") and fnmatch.fnmatchcase(text, pat):
            return True
    return False


def _is_relative_to(p: Path, root: Path) -> bool:
    try:
        p.relative_to(root)
        return True
    except ValueError:
        return False
