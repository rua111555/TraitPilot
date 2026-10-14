"""工具层：schema 定义 + 执行器 + 权限门 + 后台任务跟踪。

与 Claude Code 的工具语义保持一致：
  - Skill / Read / Write / Edit / Bash / Glob / Grep / TaskOutput
  - Bash 支持后台运行（nohup + 日志文件，供编排 skill 的 .done/.pid 机制配合）
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .skills import SkillRegistry

MAX_OUTPUT = 30_000
MAX_LINES = 2000


@dataclass
class ToolResult:
    content: str
    is_error: bool = False


# 危险命令告警（不拦截，仅提示；ask 模式下会转化为确认）
DANGEROUS_PATTERNS = [
    r"\brm\s+-rf\b", r"\bmkfs\b", r"\bdd\s+if=\b", r">\s*/dev/sd",
    r"\bdrop\s+table\b", r"\bdrop\s+database\b", r"\bgit\s+push\s+(-f|--force)\b",
    r"\bmv\s+/\s", r"\bchmod\s+\d{3}\s+/\s", r":\(\)\s*\{",
    r"\bcurl\s+\|?\s*bash\b", r"\bwget\s+.*\|?\s*bash\b",
]

BG_ROOT = Path.home() / ".skill-agent" / "tasks"


@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict
    executor: object          # callable(args: dict) -> ToolResult
    # 编排 skill 的 allowed-tools 之外仍保底可见
    always: bool = False

    @property
    def api_schema(self) -> dict:
        return {"name": self.name, "description": self.description,
                "input_schema": self.input_schema}


class ToolError(Exception):
    pass


class ToolSet:
    """持有并过滤可用工具。"""

    def __init__(self, registry: SkillRegistry, cwd: Path,
                 permission: str = "auto", verbose: bool = False,
                 confirm_handler=None):
        self.registry = registry
        self.cwd = Path(cwd)
        self.permission = permission            # auto | ask
        self.verbose = verbose
        # 网页模式：由外部桥接确认（CLI 传 None 走原 input() 逻辑）
        self.confirm_handler = confirm_handler
        self._bg_tasks: dict[str, dict] = {}    # task_id -> {log, pid, cmd}
        self.max_tokens_warning_shown = False

        self._tools: dict[str, Tool] = {}
        for t in [self._tool_skill(), self._tool_bash(), self._tool_read(),
                  self._tool_write(), self._tool_edit(), self._tool_glob(),
                  self._tool_grep(), self._tool_taskoutput()]:
            self._tools[t.name] = t

    # ---------------- schema 对外 ----------------
    def schemas_for(self, allowed: set[str] | None = None) -> list[dict]:
        """active skill 的 allowed-tools 白名单没有命中任何本套件工具时视为不限制。"""
        if allowed:
            names = {n for n in allowed if n in self._tools}
            names |= {"Skill", "TaskOutput"}          # 保底可见
            return [t.api_schema for n, t in self._tools.items() if n in names]
        return [t.api_schema for t in self._tools.values()]

    def execute(self, name: str, args: dict) -> ToolResult:
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult(f"未知工具 {name}", True)
        # 权限门：仅对 Bash 做交互确认/危险告警
        if name == "Bash":
            reason = self._check_danger(args.get("command", ""))
            need_confirm = (self.permission == "ask") or bool(reason)
            if need_confirm:
                if self.confirm_handler is not None:
                    # 网页模式：交给前端弹窗确认
                    ok = self.confirm_handler(args.get("command", ""), reason or "ask-all")
                    if not ok:
                        return ToolResult("用户拒绝执行该命令。", True)
                else:
                    # CLI 模式：危险命令告警；ask 模式逐条询问
                    if reason:
                        print(f"\n[权限] 注意：命令可能危险 → {reason}", file=sys.stderr, flush=True)
                    if self.permission == "ask":
                        if not sys.stdin.isatty():
                            return ToolResult("交互确认不可用（无 TTY），命令已拒绝——请用 --permission auto 或进入交互模式。", True)
                        ans = input(f"[权限] 允许执行? [y/N]  {args.get('command','')[:120]}... \n> ").strip().lower()
                        if ans not in ("y", "yes"):
                            return ToolResult("用户拒绝执行该命令。", True)
        try:
            return tool.executor(args)
        except ToolError as e:
            return ToolResult(f"[ToolError] {e}", True)
        except Exception as e:  # noqa: BLE001
            return ToolResult(f"[异常] {name} 执行失败：{type(e).__name__}: {e}", True)

    # ---------------- executor 实现 ----------------
    def _tool_skill(self) -> Tool:
        def run(args: dict) -> ToolResult:
            name = str(args.get("skill", "")).strip()
            if not name:
                return ToolResult(self._skill_help(), True)
            skill = self.registry.get(name)
            if skill is None:
                return ToolResult(
                    f"没有名为 `{name}` 的 skill。可用：\n" + self._skill_help(), True)
            return ToolResult(skill.instructions())
        return Tool(
            name="Skill",
            description=(
                "加载一个 skill 的完整指令到上下文中，随后按它执行。"
                "可用 skill 见系统提示中的清单。用于触发本机的生信/分析管线技能。"
            ),
            input_schema={"type": "object",
                          "properties": {
                              "skill": {"type": "string",
                                        "description": "skill 名称，如 gwas-pipeline / wgs-expressive-pipeline"},
                              "args": {"type": "string",
                                       "description": "可选：传给 skill 的参数/输入,例如 --vcf xx.vcf.gz --prefix p"}},
                          "required": ["skill"]},
            executor=run,
            always=True,
        )

    def _skill_help(self) -> str:
        lines = ["可用 skill：" + " ".join(f"`{n}`" for n in self.registry.names())]
        return "\n".join(lines)

    def _tool_bash(self) -> Tool:
        def run(args: dict) -> ToolResult:
            command = str(args.get("command", "")).strip()
            if not command:
                return ToolResult("Bash 需要 command 参数。", True)
            timeout = int(args.get("timeout") or 120)
            timeout = min(max(timeout, 1), 3600)
            if args.get("background"):
                return self._bash_background(command, str(args.get("description", "")))
            try:
                proc = subprocess.run(
                    command, shell=True, executable="/bin/bash",
                    capture_output=True, text=True, timeout=timeout, cwd=str(self.cwd),
                )
            except subprocess.TimeoutExpired:
                return ToolResult(f"命令超时（>{timeout}s）：\n$ {command}", True)
            out = (proc.stdout or "") + (proc.stderr or "")
            out = out.strip() or f"（无输出，返回码 {proc.returncode}）"
            if len(out) > MAX_OUTPUT:
                out = out[:MAX_OUTPUT] + f"\n…[输出过长，已截断为 {MAX_OUTPUT} 字符]"
            note = f"退出码 {proc.returncode}。" if proc.returncode != 0 else ""
            res = f"$ {command}\n{out}"
            if note:
                res += "\n" + note
            return ToolResult(res, is_error=proc.returncode != 0)
        return Tool(
            name="Bash",
            description=(
                "在项目根目录执行 shell 命令（/bin/bash）。可配 background=true 后台启动；"
                "长任务请用后台 + .done/.pid 标记 + 日志轮询。"
            ),
            input_schema={"type": "object",
                          "properties": {
                              "command": {"type": "string", "description": "要执行的 shell 命令"},
                              "description": {"type": "string", "description": "命令意图（便于审查）"},
                              "background": {"type": "boolean", "description": "后台启动"},
                              "timeout": {"type": "integer", "description": "超时秒数，默认120，最大3600"}},
                          "required": ["command"]},
            executor=run,
        )

    def _bash_background(self, command: str, desc: str) -> ToolResult:
        task_id = uuid.uuid4().hex[:10]
        d = BG_ROOT / task_id
        d.mkdir(parents=True, exist_ok=True)
        log = d / "output.log"
        pidf = d / "pid"
        (d / "command.sh").write_text(command, encoding="utf-8")
        # nohup + 写 PID，兼容 Bash(python后台) 场景
        script = f'cd "{self.cwd}" && echo $$ > "{pidf}"\nexec bash -c {json.dumps(command)}\n'
        subprocess.Popen(
            [ "/bin/bash", "-c",
              f'nohup /bin/bash "{d.joinpath("command.sh")}" > "{log}" 2>&1 &' ],
        )
        self._bg_tasks[task_id] = {"log": log, "pid": pidf, "cmd": command}
        return ToolResult(
            f"已在后台启动（task_id={task_id}）。\n日志：{log}\n"
            "后续可用 TaskOutput(log_path=...) 或 Read/Bash 查看进度；"
            "编排管线请按各自 SKILL.md 用 .done/.pid 标记确认完成。")

    def _tool_read(self) -> Tool:
        def run(args: dict) -> ToolResult:
            path = self._resolve(args.get("file_path", ""))
            if not path.is_file():
                return ToolResult(f"文件不存在或不可读：{path}", True)
            offset = max(int(args.get("offset") or 1), 1)
            limit = min(int(args.get("limit") or MAX_LINES), MAX_LINES)
            try:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError as e:
                return ToolResult(f"读取失败：{e}", True)
            total = len(lines)
            # 行号前缀
            chosen = lines[offset - 1: offset - 1 + limit]
            body = "\n".join(f"{i + offset}\t{l}" for i, l in enumerate(chosen))
            head = f"文件：{path}（共 {total} 行，显示 {offset}-{offset + len(chosen) - 1}）"
            if not body.strip():
                body = "（空文件或无内容）"
            return ToolResult(head + "\n" + body)
        return Tool(
            name="Read",
            description="读取文本文件，默认前2000行，可用 offset/limit 分段。",
            input_schema={"type": "object",
                          "properties": {
                              "file_path": {"type": "string"},
                              "offset": {"type": "integer"},
                              "limit": {"type": "integer"}},
                          "required": ["file_path"]},
            executor=run,
        )

    def _tool_write(self) -> Tool:
        def run(args: dict) -> ToolResult:
            path = self._resolve(args.get("file_path", ""))
            content = str(args.get("content", ""))
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
            except OSError as e:
                return ToolResult(f"写入失败：{e}", True)
            size = len(content.encode("utf-8"))
            return ToolResult(f"已写入 {path}（{size} 字节）。")
        return Tool(
            name="Write",
            description="创建或覆盖文件（含父目录）。",
            input_schema={"type": "object",
                          "properties": {
                              "file_path": {"type": "string"},
                              "content": {"type": "string"}},
                          "required": ["file_path", "content"]},
            executor=run,
        )

    def _tool_edit(self) -> Tool:
        def run(args: dict) -> ToolResult:
            path = self._resolve(args.get("file_path", ""))
            old = str(args.get("old_string", ""))
            new = str(args.get("new_string", ""))
            replace_all = bool(args.get("replace_all"))
            try:
                text = path.read_text(encoding="utf-8")
            except OSError as e:
                return ToolResult(f"读取失败：{e}", True)
            if old not in text:
                return ToolResult("old_string 未在文件中找到（注意首尾空白精确匹配）。", True)
            if not replace_all and text.count(old) > 1:
                return ToolResult("old_string 出现多次，请加 replace_all=true 或提供更精确的串。", True)
            new_text = text.replace(old, new) if replace_all else text.replace(old, new, 1)
            try:
                path.write_text(new_text, encoding="utf-8")
            except OSError as e:
                return ToolResult(f"写入失败：{e}", True)
            return ToolResult(f"已编辑 {path}。")
        return Tool(
            name="Edit",
            description="精确字符串替换（单次/全部 replace_all）。",
            input_schema={"type": "object",
                          "properties": {
                              "file_path": {"type": "string"},
                              "old_string": {"type": "string"},
                              "new_string": {"type": "string"},
                              "replace_all": {"type": "boolean"}},
                          "required": ["file_path", "old_string", "new_string"]},
            executor=run,
        )

    def _tool_glob(self) -> Tool:
        def run(args: dict) -> ToolResult:
            pattern = str(args.get("pattern", ""))
            base = Path(args.get("path") or self.cwd)
            base = self._resolve(str(base), allow_abs_default=True)
            import glob as _glob
            raw = sorted(_glob.glob(str(base / pattern)))
            files = [p for p in raw if Path(p).is_file()]
            if not files:
                return ToolResult(f"没有匹配 {base / pattern} 的文件。")
            body = "\n".join(files[:200])
            if len(files) > 200:
                body += f"\n…（共 {len(files)} 个）"
            return ToolResult(body)
        return Tool(
            name="Glob",
            description="按 glob 模式列出文件（相对项目根）。",
            input_schema={"type": "object",
                          "properties": {"pattern": {"type": "string"},
                                         "path": {"type": "string"}},
                          "required": ["pattern"]},
            executor=run,
        )

    def _tool_grep(self) -> Tool:
        def run(args: dict) -> ToolResult:
            pattern = str(args.get("pattern", ""))
            path = self._resolve(args.get("path", "."), allow_abs_default=True)
            include = args.get("include")
            try:
                rp = re.compile(pattern)
            except re.error as e:
                return ToolResult(f"正则错误：{e}", True)
            # 优先 ripgrep
            rg = shutil.which("rg")
            if rg and not include:
                try:
                    proc = subprocess.run(
                        [rg, "-n", "--no-heading", "-m", "200", pattern, str(path)],
                        capture_output=True, text=True, timeout=60)
                    if proc.stdout.strip():
                        return ToolResult(proc.stdout[:MAX_OUTPUT])
                except Exception:
                    pass
            # 回退纯 python
            hits: list[str] = []
            if path.is_file():
                iterator = [path]
            else:
                iterator = (p for p in path.rglob("*")
                            if p.is_file() and "__pycache__" not in p.parts
                            and p.suffix in {".py", ".sh", ".md", ".txt", ".json",
                                             ".c", ".h", ".cpp", ".hpp", ".r", ".R",
                                             ".vcf", ".csv", ".pl", ".yml", ".yaml", ".conf"})
            for p in iterator:
                try:
                    for i, line in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                        if rp.search(line):
                            hits.append(f"{p}:{i}: {line[:200]}")
                            if len(hits) >= 200:
                                break
                except OSError:
                    continue
                if len(hits) >= 200:
                    break
            if not hits:
                return ToolResult("无匹配。")
            return ToolResult("\n".join(hits))
        return Tool(
            name="Grep",
            description="在目录/文件中按正则搜索（优先 ripgrep）。",
            input_schema={"type": "object",
                          "properties": {"pattern": {"type": "string"},
                                         "path": {"type": "string", "description": "默认项目根"},
                                         "include": {"type": "string", "description": "文件名 glob，如 *.py"}},
                          "required": ["pattern"]},
            executor=run,
        )

    def _tool_taskoutput(self) -> Tool:
        def run(args: dict) -> ToolResult:
            log = args.get("log_path") or args.get("task_id")
            if not log:
                return ToolResult("需要 log_path 或 task_id。", True)
            if str(args.get("task_id", "")) in self._bg_tasks:
                log = self._bg_tasks[str(args["task_id"])]["log"]
            p = Path(log) if log else None
            p = self._resolve(str(p), allow_abs_default=True) if p else None
            if not p or not p.is_file():
                return ToolResult(f"日志不存在：{log}", True)
            lines = (int(args.get("lines") or 60))
            try:
                all_lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError as e:
                return ToolResult(f"读取失败：{e}", True)
            tail = all_lines[-lines:]
            # 进程存活判断（配合 pid 文件）
            alive = ""
            pidf = p.parent / "pid"
            if pidf.is_file():
                pid = pidf.read_text().strip()
                try:
                    os.kill(int(pid), 0)
                    alive = f"（进程 {pid} 运行中）"
                except (ProcessLookupError, ValueError, PermissionError, OSError):
                    alive = "（进程已结束）"
            body = "\n".join(tail) or "（日志为空）"
            return ToolResult(f"日志：{p}{alive}\n---- 末 {len(tail)} 行 ----\n{body}")
        return Tool(
            name="TaskOutput",
            description="查看后台任务日志的末尾行数（配合 Bash background=true）。",
            input_schema={"type": "object",
                          "properties": {
                              "log_path": {"type": "string", "description": "日志文件绝对/相对路径"},
                              "task_id": {"type": "string", "description": "后台启动返回的 task_id"},
                              "lines": {"type": "integer", "description": "读多少行，默认60"}},
                          "required": []},
            executor=run,
        )

    # ---------------- 工具方法 ----------------
    def _check_danger(self, command: str) -> str:
        low = command.lower()
        for pat in DANGEROUS_PATTERNS:
            if re.search(pat, low):
                return f"命中危险模式 `{pat}`"
        return ""

    def _resolve(self, raw: str, allow_abs_default: bool = False) -> Path:
        p = Path(raw).expanduser()
        if not p.is_absolute():
            p = self.cwd / p
        p = p.resolve()
        return p

    def summarize_tasks(self) -> str:
        if not self._bg_tasks:
            return ""
        rows = [f"- {k}: {v['cmd'][:60]} → {v['log']}" for k, v in self._bg_tasks.items()]
        return "\n".join(rows)
