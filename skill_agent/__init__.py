"""Skill-Agent — 自带 skills 的单 Agent 系统。

技能模型（Agent 内建，不依赖 Claude Code）：
  - 从 `skills/*/SKILL.md` 自动发现技能（frontmatter 含
    name / description / allowed-tools / disable-model-invocation）
  - 模型按需通过 `Skill` 工具加载技能指令再执行
  - 标准工具循环（tool_use / tool_result）+ 流式输出
"""
import os

# ---- 版本号：**唯一载体是仓库根的 `VERSION` 文件** ----
# 早期这里是写死的字符串 `__version__ = "0.1.0"`（2026-10-15 起），
# 从未被任何地方读取 —— 没有 --version、没有消费者、没有 CHANGELOG，
# 于是"发布新版本"这件事在仓库里没有任何可观察的痕迹：
# 2026-10-26 那次提交信息就叫 `version change`，版本号却一字未动。
#
# 现在改为从 VERSION 文件读：**只有一处需要改**，不会出现文件与代码两处
# 硬编码各自漂移（本项目反复踩到的"纸面具备"就是这一类）。
# 读不到时回退 "0.0.0+unknown" 而非抛异常：版本号查询不该让 import 失败
# （例如被拷贝到只有 skill_agent/ 没有 VERSION 的环境里）。
_VERSION_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "VERSION")


def _read_version():
    try:
        with open(_VERSION_FILE, encoding="utf-8") as f:
            v = f.read().strip()
        return v or "0.0.0+unknown"
    except OSError:
        return "0.0.0+unknown"


__version__ = _read_version()
