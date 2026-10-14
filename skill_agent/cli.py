"""CLI：list / chat（REPL 或单轮 -p）。"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from .agent import Agent
from .config import Config, default_session_dir
from .skills import SkillRegistry
from .tools import ToolSet

BANNER = "\n".join([
    "╭──────────────────────────────────────────────╮",
    "│  SkillAgent — 可调用 Claude Code skills 的本机 Agent",
    "╰──────────────────────────────────────────────╯",
])


def _common_opts(*, with_cwd: bool = False) -> argparse.ArgumentParser:
    """可同时挂在主解析器和 chat 子命令上的共享参数。"""
    c = argparse.ArgumentParser(add_help=False)
    c.add_argument("--cwd", type=Path, default=None, help="工具执行的工作目录（默认=项目根）")
    c.add_argument("-m", "--model", default=None, help="模型（默认 claude-opus-4-5）")
    c.add_argument("--max-tokens", type=int, default=None, help="单次响应上限 token")
    c.add_argument("--permission", choices=["auto", "ask"], default=None,
                   help="auto=危险命令仅告警；ask=每个 Bash 都询问（交互模式）")
    c.add_argument("--no-stream", action="store_true", help="关闭流式输出")
    c.add_argument("--verbose", action="store_true")
    return c


def build_parser() -> argparse.ArgumentParser:
    common = _common_opts()
    p = argparse.ArgumentParser(prog="skill-agent", description="可调用 skills 的本机 Agent",
                                parents=[common])
    p.add_argument("--project", type=Path, default=None, help="含 .claude/skills 的项目根（默认自动探测）")
    sub = p.add_subparsers(dest="command", required=False)

    sp = sub.add_parser("list", help="列出发现的 skills")
    sp.add_argument("--json", action="store_true", help="JSON 输出")

    sc = sub.add_parser("chat", help="交互对话（REPL）", parents=[_common_opts()])
    sc.add_argument("-p", "--prompt", help="单轮运行（非交互）后退出")
    return p


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    cfg = Config.load(
        model=args.model, project=args.project, cwd=args.cwd,
        permission=args.permission, max_tokens=args.max_tokens,
        stream=not args.no_stream, verbose=args.verbose,
    )
    registry = SkillRegistry(cfg.project)
    tools = ToolSet(registry, cfg.cwd, cfg.permission, verbose=cfg.verbose)

    if args.command == "list":
        return _list(registry, args)

    agent = Agent(cfg, registry, tools)
    if args.prompt:
        return _oneshot(agent, cfg, args.prompt)
    return _repl(agent, cfg, registry, tools)


# ---------------- list ----------------
def _list(registry: SkillRegistry, args) -> int:
    if args.json:
        payload = {
            n: {
                "description": s.description,
                "path": str(s.dir_path),
                "model_invocable": s.model_invocable,
                "allowed_tools": s.allowed_tools,
            }
            for n, s in registry.skills.items()
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    print(registry.listing())
    print(f"\n共 {len(registry.names())} 个 skill。")
    return 0


# ---------------- 会话日志 ----------------
def _open_session_log(cfg: Config):
    if cfg.session_log is None:
        stamp = time.strftime("%Y%m%dT%H%M%S")
        cfg.session_log = default_session_dir() / f"{stamp}.jsonl"
    f = open(cfg.session_log, "a", encoding="utf-8")
    f.write(json.dumps({"t": time.strftime("%Y-%m-%d %H:%M:%S"),
                        "model": cfg.model, "project": str(cfg.project),
                        "base_url": cfg.base_url}) + "\n")
    return f


def _dump_history(f, history: list[dict]) -> None:
    for m in history:
        try:
            f.write(json.dumps(m, ensure_ascii=False) + "\n")
        except Exception:
            pass


def _finish_log(f, history) -> None:
    _dump_history(f, history)
    f.write(json.dumps({"t": time.strftime("%Y-%m-%d %H:%M:%S"), "end": True}) + "\n")
    f.close()


# ---------------- 单轮 ----------------
def _oneshot(agent: Agent, cfg: Config, prompt: str) -> int:
    logf = _open_session_log(cfg)
    delta_seen = [False]

    def on_delta(t: str) -> None:
        delta_seen[0] = True
        sys.stdout.write(t)
        sys.stdout.flush()

    try:
        final = agent.answer(prompt, on_delta=on_delta if cfg.stream else None)
    except Exception as e:
        _finish_log(logf, agent.history)
        print(f"\n[错误] {e}", file=sys.stderr)
        return 1
    if not cfg.stream:
        print(final)
    else:
        # 流式已显示增量；补一个换行（若有输出）
        if delta_seen[0]:
            sys.stdout.write("\n" if not final.endswith("\n") else "")
            sys.stdout.flush()
        if not delta_seen[0]:
            print(final)
    if cfg.verbose:
        print(f"\n[信息] 模型轮次 {len(agent.history)} 条消息；会话日志 {cfg.session_log}")
    _finish_log(logf, agent.history)
    return 0


# ---------------- REPL ----------------
COMMANDS = {"/exit", "/quit", "/clear", "/skills", "/active", "/help", "/model", "/permission"}


def _repl(agent: Agent, cfg: Config, registry: SkillRegistry, tools: ToolSet) -> int:
    logf = _open_session_log(cfg)
    print(BANNER)
    print(f"模型：{cfg.model} | 项目：{cfg.project} | 权限：{cfg.permission} | 流式：{'开' if cfg.stream else '关'}")
    print("输入 /help 查看命令；/exit 退出")
    while True:
        try:
            line = input("❯ ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见。")
            break
        if not line:
            continue
        if line in ("/exit", "/quit"):
            print("再见。")
            break
        if line.startswith("/"):
            _handle_command(agent, cfg, registry, line, logf)
            continue
        delta_seen = [False]

        def on_delta(t: str) -> None:
            delta_seen[0] = True
            sys.stdout.write(t)
            sys.stdout.flush()

        try:
            final = agent.answer(line, on_delta=on_delta if cfg.stream else None)
        except KeyboardInterrupt:
            print("\n[中断]")
            continue
        except Exception as e:
            print(f"\n[错误] {type(e).__name__}: {e}")
            continue
        if not cfg.stream:
            print(final)
        else:
            if delta_seen[0]:
                sys.stdout.write("\n" if not final.endswith("\n") else "")
                sys.stdout.flush()
            else:
                print(final)
        print("─" * 40)
    _finish_log(logf, agent.history)
    return 0


def _handle_command(agent: Agent, cfg: Config, registry: SkillRegistry, line: str, logf) -> None:
    parts = line.split()
    cmd, _, arg = parts[0], parts[0], (parts[1] if len(parts) > 1 else "")
    if cmd == "/help":
        print("命令：/exit 退出 · /clear 清空对话 · /skills 列技能 · /active 当前激活"
              " · /drop <skill> 移除激活 · /model X 换模型 · /permission ask|auto")
    elif cmd == "/clear":
        agent.clear()
        print("上下文已清空。")
    elif cmd == "/skills":
        print(registry.listing())
    elif cmd == "/active":
        print("当前激活 skill：" + ("、".join(agent.active_skills) or "无"))
    elif cmd == "/drop" and arg:
        agent.reset_skills()
        print("已重置激活 skill。")
    elif cmd == "/model" and arg:
        cfg.model = arg
        agent.client.model = arg
        print(f"模型已切换为 {arg}")
    elif cmd == "/permission" and arg in ("ask", "auto"):
        cfg.permission = agent.tools.permission = arg
        print(f"权限模式：{arg}")
    else:
        print("未知命令，/help 查看。")
