# -*- coding: utf-8 -*-
"""前端 md() 的流式中间态：表头先到、分隔行未到，旧实现会死循环 → 标签页 OOM。

事故证据（2026-11-10）：Chrome 崩溃 dump 的异常码是 0xE0000008（OOM），
dump 里含 127.0.0.1:8787；服务端 web.log 无任何 5xx/traceback。
根因：md() 对以 `|` 开头的行，若不是合法表格（流式中途），没有任何分支消费，
while 不推进、每轮 out.push("<p></p>") → 内存涨到渲染进程被杀。
"""
import json
import os
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HTML = os.path.join(ROOT, "webapp", "static", "index.html")
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(
    NODE is None,
    reason="需要 node 执行前端 md()（零构建前端；无 node 的环境跳过）",
)


def _extract(src, name):
    start = src.index("function " + name + "(")
    i = src.index("{", start)
    depth = 0
    for j in range(i, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[start:j + 1]
    raise AssertionError("unbalanced function: " + name)


def test_partial_table_does_not_hang_or_flood(tmp_path):
    src = open(HTML, encoding="utf-8").read()
    js = _extract(src, "escapeHtml") + "\n" + _extract(src, "md")
    # 注入硬上限：旧实现（没有 `|` 兜底）会在这里抛 LOOP，而不是把 200k 个 <p></p> 塞满
    js = js.replace(
        "while(i<lines.length){",
        "while(i<lines.length){ if(++globalThis.__g>200000) throw new Error('LOOP');",
    )
    cases = ["| 阶段 | 判定 |", "| a | b |\n\n", "前文\n| a | b |"]
    script = (
        js
        + "\nconst cases=" + json.dumps(cases) + ";\n"
        + """
for (const t of cases) {
  const o = md(t);
  if (o.includes("<p></p>")) throw new Error("空段落泛滥");
  if (!o.includes("<p>")) throw new Error("半截表格没有退化成段落");
}
const full = md("| a | b |\\n|---|---|\\n| 1 | 2 |");
if (!full.includes("<table>")) throw new Error("完整表格被破坏");
console.log("OK");
"""
    )
    p = tmp_path / "md_case.js"
    p.write_text(script, encoding="utf-8")
    r = subprocess.run([NODE, str(p)], capture_output=True, text=True, timeout=20)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "OK" in r.stdout
