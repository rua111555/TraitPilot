"""CLI：list / chat（REPL 或单轮 -p）。"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from .agent import Agent
from .config import Config, default_checkpoint_dir, default_session_dir
from .loopguard import LoopGuard
from .permissions import PermissionModel
from .session import attach_all, resume_into
from .session_store import SessionStore
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
    c.add_argument("--restrict-paths", action="store_true", default=None,
                   help="M8a：启用工作区路径边界（越界拒绝）")
    c.add_argument("--write-approval", action="store_true", default=None,
                   help="M8a：把 Write/Edit 纳入审批门")
    c.add_argument("--no-stream", action="store_true", help="关闭流式输出")
    c.add_argument("--persist", action="store_true", default=None,
                   help="M13：逐条消息落盘，支持 --resume/--continue 恢复")
    c.add_argument("--resume", default=None, metavar="SID",
                   help="M13：恢复指定会话 id 的上下文")
    c.add_argument("--continue", dest="cont", action="store_true", default=None,
                   help="M13：恢复最近一次会话")
    c.add_argument("--summary-compact", action="store_true", default=None,
                   help="M12：开启摘要式上下文压缩（替代丢弃旧消息）")
    c.add_argument("--memory", action="store_true", default=None,
                   help="M12：开启分层记忆（core + archival 检索）")
    c.add_argument("--max-turns", type=int, default=None,
                   help="M1b：单轮最多模型轮次（默认 60）")
    c.add_argument("--max-seconds", type=int, default=None,
                   help="M1b：单轮墙钟时长上限（秒），超时熔断")
    c.add_argument("--max-output-tokens", type=int, default=None,
                   help="M1b：单轮累计输出 token 上限，超限熔断")
    c.add_argument("--loop-guard", action="store_true", default=None,
                   help="M3：启用死循环/无进展熔断（默认关）")
    c.add_argument("--plugin-dir", action="append", default=None, metavar="DIR",
                   help="M4：加载扩展工具规格目录（*.json，可重复）；默认禁用以防扩大执行面")
    c.add_argument("--trace", action="store_true", default=None,
                   help="M10：把 Pre/Post 工具调用与用量落盘到 ~/.skill-agent/traces/")
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
        max_turns=getattr(args, "max_turns", None),
    )
    if getattr(args, "max_seconds", None):
        cfg.max_seconds = args.max_seconds
    if getattr(args, "max_output_tokens", None):
        cfg.max_output_tokens = args.max_output_tokens
    if getattr(args, "loop_guard", None):
        cfg.loop_guard = True
    if getattr(args, "restrict_paths", None):
        cfg.restrict_paths = True
    if getattr(args, "write_approval", None):
        cfg.write_approval = True
    if getattr(args, "summary_compact", None):
        cfg.summary_compaction = True
    if getattr(args, "memory", None):
        cfg.memory_enabled = True
    if getattr(args, "plugin_dir", None):
        cfg.plugin_dirs = list(args.plugin_dir)
    registry = SkillRegistry(cfg.project)
    pmodel = PermissionModel.from_config(cfg, cfg.cwd)

    # ---- M10：可选 trace 钩子 ----
    hooks = None
    if getattr(args, "trace", None):
        from .observability import build_trace_hooks
        from .session_store import new_session_id
        hooks = build_trace_hooks(getattr(cfg, "session_id", None) or new_session_id())

    tools = ToolSet(registry, cfg.cwd, cfg.permission, verbose=cfg.verbose,
                    permission_model=pmodel, hooks=hooks)

    # ---- M4：可选插件工具目录（默认不加载） ----
    if cfg.plugin_dirs:
        from .registry import load_plugin_dir
        loaded = []
        for d in cfg.plugin_dirs:
            loaded += load_plugin_dir(tools.registry_tools, d, allow=cfg.plugin_allow)
        if loaded:
            print(f"[插件] 已加载扩展工具：{', '.join(loaded)}", file=sys.stderr)

    if getattr(pmodel, "enabled", False):
        print(f"[权限] 结构化策略已启用：路径边界={pmodel.restrict_paths} "
              f"写审批={pmodel.write_approval} 规则={len(pmodel.rules)}", file=sys.stderr)

    if args.command == "list":
        return _list(registry, args)

    agent = Agent(cfg, registry, tools)
    if cfg.loop_guard:
        agent.loopguard = LoopGuard(enabled=True, warn=cfg.loop_warn, halt=cfg.loop_halt)

    # ---- M13：解析 resume / continue ----
    persist = bool(getattr(args, "persist", None)) or bool(getattr(args, "resume", None)) \
        or bool(getattr(args, "cont", None))
    if persist:
        cfg.persist = True
    resume_sid = None
    if getattr(args, "resume", None):
        resume_sid = args.resume
    elif getattr(args, "cont", None):
        store0 = SessionStore(default_checkpoint_dir())
        sessions = store0.sessions()
        if not sessions:
            print("[resume] 没有可恢复的会话（先加 --persist 跑一次）。", file=sys.stderr)
        else:
            resume_sid = sessions[0]["id"]

    attach_all(agent, cfg, session_id=resume_sid)
    if resume_sid and agent.store is not None and agent.store.exists(resume_sid):
        info = resume_into(agent, agent.store, resume_sid)
        print(f"[resume] 已恢复会话 {resume_sid}：{info['restored']} 条消息"
              f"（丢弃未完成轮次 {info['discarded']}）", file=sys.stderr)
    if agent.store is not None:
        print(f"[persist] 会话 id={agent.session_id}（继续：--resume {agent.session_id}）",
              file=sys.stderr)

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
                        "base_url": cfg.base_url,
                        "session_id": getattr(cfg, "session_id", None)}) + "\n")
    return f


def _dump_history(f, history: list[dict]) -> None:
    for m in history:
        try:
            f.write(json.dumps(m, ensure_ascii=False) + "\n")
        except Exception:
            pass


def _finish_log(f, history, stop=None) -> None:
    _dump_history(f, history)
    rec = {"t": time.strftime("%Y-%m-%d %H:%M:%S"), "end": True}
    if stop is not None:
        rec["stop_reason"] = getattr(stop, "reason_code", None)
        rec["truncated"] = getattr(stop, "truncated", False)
    f.write(json.dumps(rec) + "\n")
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
    _finish_log(logf, agent.history, agent._last_stop)
    return 0


# ---------------- REPL ----------------
COMMANDS = {"/exit", "/quit", "/clear", "/skills", "/active", "/help", "/model", "/permission", "/cost"}


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
    _finish_log(logf, agent.history, agent._last_stop)
    return 0


def _handle_command(agent: Agent, cfg: Config, registry: SkillRegistry, line: str, logf) -> None:
    parts = line.split()
    cmd, _, arg = parts[0], parts[0], (parts[1] if len(parts) > 1 else "")
    if cmd == "/help":
        print("命令：/exit 退出 · /clear 清空对话 · /skills 列技能 · /active 当前激活"
              " · /drop <skill> 移除激活 · /model X 换模型 · /permission ask|auto · /cost 本轮用量")
    elif cmd == "/clear":
        agent.clear()
        print("上下文已清空。")
    elif cmd == "/cost":
        u = agent.usage_bar
        print(f"本轮用量：模型 {u['model']} · 模型轮次 {u['steps']} · "
              f"输出 token {u['output_tokens']} · 累计延迟 {u['latency_ms']} ms")
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
