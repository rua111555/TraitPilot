"""Skill-Agent — 可调用 Claude Code skills 的单 Agent 系统。

与 Claude Code 相同的技能模型：
  - 从 `.claude/skills/*/SKILL.md` 自动发现技能（frontmatter 含
    name / description / allowed-tools / disable-model-invocation）
  - 模型按需通过 `Skill` 工具加载技能指令再执行
  - 标准工具循环（tool_use / tool_result）+ 流式输出
"""

__version__ = "0.1.0"
