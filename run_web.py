#!/usr/bin/env python3
"""SkillAgent Web 前端启动器。

默认绑定 127.0.0.1:8787，仅本机可访问。若需局域网访问加 --host 0.0.0.0（注意：页面可执行本机命令）。

示例：
  python agent/run_web.py                          # http://127.0.0.1:8787
  python agent/run_web.py --port 9000 --permission ask
"""
import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from skill_agent.config import Config  # noqa: E402
from skill_agent.skills import SkillRegistry  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--project", type=Path, default=None)
    ap.add_argument("--cwd", type=Path, default=None)
    ap.add_argument("--skills-dir", type=Path, default=None,
                    help="显式技能目录（默认 <project>/skills）")
    ap.add_argument("--config", dest="config_file", default=None, metavar="FILE")
    ap.add_argument("-m", "--model", default=None)
    ap.add_argument("--permission", choices=["auto", "ask"], default=None)
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()

    cfg = Config.load(model=args.model, project=args.project, cwd=args.cwd,
                      permission=args.permission, stream=True,
                      skills_dir=args.skills_dir, config_file=args.config_file)
    registry = SkillRegistry(cfg.project, skills_dir=cfg.skills_dir)

    from webapp.server import create_app

    app = create_app(cfg, registry)
    n = len(registry.names())
    print(f"SkillAgent Web 已启动  http://{args.host}:{args.port}")
    print(f"  project = {cfg.project}（发现 {n} 个 skill）")
    print(f"  skills  = {cfg.skills_dir or (cfg.project / 'skills')}")
    print(f"  model   = {cfg.model}    permission = {cfg.permission}")
    app.run(host=args.host, port=args.port, threaded=True, debug=args.debug, use_reloader=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
