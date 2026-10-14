"""摘要式上下文压缩（M12）。

替换 ``agent._maybe_compact`` 的「直接丢弃旧消息」。安全设计：

- **默认关闭** —— 未开启时行为与改造前完全一致（仍按条数/字节丢弃）。
- 只有 ``cfg.summary_compaction=True`` 且主循环注入了 ``summarize`` 回调时才启用。
- 摘要失败 / 无回调 → **回退**为原有的「丢旧消息」，绝不阻塞主循环。
- 保留最近 K 条消息原样；更早区间用既有 ``_safe_cut`` 取合法边界（**不拆 tool_use/
  tool_result 配对、不制造连续两条同 role**），压缩为**单条** user「前情摘要」前置。
"""
from __future__ import annotations

# 结构化摘要模板：强制保留编排管线（WGS/GWAS）的关键不变量
SUMMARY_TEMPLATE = """你是上下文压缩器。把下面的早期对话压缩为一条**结构化前情摘要**，\
供后续继续执行长管线任务。必须原样保留以下字段（有则保留，没有的省略）：
- 当前阶段号/阶段名（如 GWAS S3、WGS Stage 4）
- 各阶段参数（如 --geno/--maf/--hwe、-lmm 模型、nPC、阈值）
- 关键输入/输出文件路径（VCF、bfile、GRM、关联结果、日志）
- 后台任务：task_id、log_path、pid、.done/.pid 路径
- 用户审批结论（哪些脚本已批准/拒绝、批准时的参数）
- 未决 TODO / 待推进的下一阶段
- 已定位的错误根因与修复动作
不要编造内容；信息缺失就省略。

早期对话：
{history}
"""


class SummaryCompressor:
    """无状态压缩器：把一段历史渲染成结构化摘要（具体调用模型由 Agent 注入回调）。"""

    def __init__(self, template: str = SUMMARY_TEMPLATE, max_chars: int = 20000):
        self.template = template
        self.max_chars = max_chars

    def render_prompt(self, early_messages: list[dict], prev_summary: str = "") -> str:
        body = self._render_messages(early_messages)
        if prev_summary:
            body = f"【上一次的前情摘要（需与新内容合并）】\n{prev_summary}\n\n{body}"
        if len(body) > self.max_chars:
            body = body[:self.max_chars] + "\n…（更早内容已省略）"
        return self.template.format(history=body)

    def make_prefix_message(self, summary: str) -> dict:
        """压缩后的单条 user 消息（与「最近窗口」相邻，不破坏配对）。"""
        return {"role": "user",
                "content": "【前情摘要（较早对话已压缩）】\n" + summary.strip()}

    def prefix_text(self, summary: str) -> str:
        """压缩后的前缀文本（与最近窗口的**首条 user 消息合并**，避免连续两条 user）。"""
        return "【前情摘要（较早对话已压缩）】\n" + summary.strip()

    @staticmethod
    def prepend_to_message(message: dict, text: str) -> dict:
        """把前缀文本并入一条 user 消息（内容为 str 直接前插；为块列表则前插 text 块）。"""
        content = message.get("content")
        if isinstance(content, list):
            new_content = [{"type": "text", "text": text}] + content
        else:
            new_content = f"{text}\n\n{content}" if content else text
        return {"role": "user", "content": new_content}

    # ---------------- 内部 ----------------
    @staticmethod
    def _render_messages(messages: list[dict]) -> str:
        out = []
        for m in messages:
            role = m.get("role", "?")
            content = m.get("content")
            if isinstance(content, str):
                out.append(f"[{role}] {content[:1500]}")
            elif isinstance(content, list):
                for b in content:
                    if not isinstance(b, dict):
                        continue
                    t = b.get("type")
                    if t == "text":
                        out.append(f"[{role}:text] {b.get('text','')[:1500]}")
                    elif t == "thinking":
                        out.append(f"[{role}:thinking] {b.get('thinking','')[:600]}")
                    elif t == "tool_use":
                        out.append(f"[{role}:tool_use:{b.get('name')}] "
                                   f"{str(b.get('input'))[:600]}")
                    elif t == "tool_result":
                        c = b.get("content")
                        c = c if isinstance(c, str) else str(c)
                        out.append(f"[{role}:tool_result] {c[:800]}")
            else:
                out.append(f"[{role}] {str(content)[:500]}")
        return "\n".join(out)
