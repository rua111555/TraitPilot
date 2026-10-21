"""左栏会话标题=E2E：验证每个对话以「用户第一句话」命名（用户报案：标题像乱码 id）。"""
import sys, time
sys.path.insert(0, '/home/chenyuxi/skill/agent')
from skill_agent.config import default_checkpoint_dir
from skill_agent.session_store import SessionStore

st = SessionStore(default_checkpoint_dir())
S = {
    "uititle_a": [                                   # 普通多轮
        {"role": "user", "content": "pig_geno 库里有哪些表？能做什么分析？"},
        {"role": "assistant", "content": [{"type": "text", "text": "有 sample / phenotype。"}]},
        {"role": "user", "content": "那 IMF 怎么看？"},
        {"role": "assistant", "content": [{"type": "text", "text": "IMF 在 phenotype 表。"}]}],
    "uititle_b": [                                   # 首轮是工具轮
        {"role": "user", "content": "帮我写个下载脚本"},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "Write",
                                           "input": {"file_path": "/tmp/d.sh"}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t2", "name": "Bash",
                                           "input": {"command": "bash /tmp/d.sh"}}]}],
    "uititle_c": [                                   # 只问一句、模型从未回复
        {"role": "user", "content": "只问了一句就没回复"}],
    "uititle_d": [                                   # 第一句是斜杠命令（不该当标题）
        {"role": "user", "content": "/help"},
        {"role": "assistant", "content": [{"type": "text", "text": "命令列表…"}]},
        {"role": "user", "content": "那我现在想问数据库的事"},
        {"role": "assistant", "content": [{"type": "text", "text": "好的。"}]}],
}
for sid, msgs in S.items():
    st.delete(sid)
    for m in msgs:
        st.append(sid, m)

from playwright.sync_api import sync_playwright
B = "http://127.0.0.1:8787"
log = []
def ok(name, cond, extra=""):
    log.append(("{PASS}" if cond else "{FAIL}") + " " + name + ("  " + extra if extra else ""))
    print(log[-1], flush=True)

with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page(viewport={"width": 1400, "height": 950})
    pg.on("pageerror", lambda e: ok("无 JS 运行时错误", False, str(e)))
    pg.goto(B, wait_until="networkidle")
    pg.wait_for_selector("#sessionList .sess")
    time.sleep(2.5)                      # 等左栏标题/预览补齐

    def ttl(sid):
        n = pg.query_selector(f'#sessionList .sess[data-id="{sid}"] .nm')
        return n.inner_text() if n else None

    ok("① 标题=用户第一句话", ttl("uititle_a") == "pig_geno 库里有哪些表？能做什么分析？",
       repr(ttl("uititle_a")))
    ok("② 首轮工具轮的会话也取第一句", ttl("uititle_b") == "帮我写个下载脚本",
       repr(ttl("uititle_b")))
    ok("③ 只问未答的会话也有标题", ttl("uititle_c") == "只问了一句就没回复",
       repr(ttl("uititle_c")))
    ok("④ 斜杠命令被跳过，取真正的提问", ttl("uititle_d") == "那我现在想问数据库的事",
       repr(ttl("uititle_d")))
    ok("⑤ 不再把会话 id 当标题显示",
       all("-" not in (ttl(s) or "") for s in S), str([ttl(s) for s in S]))

    # 点进去 → 顶部提示条也用标题
    pg.eval_on_selector('#sessionList .sess[data-id="uititle_a"]',
                        "e=>e.scrollIntoView({block:'center'})")
    time.sleep(0.4)
    pg.query_selector('#sessionList .sess[data-id="uititle_a"]').click()
    time.sleep(1.2)
    html = pg.inner_html("#chat")
    ok("⑥ 顶部提示条显示标题", "pig_geno 库里有哪些表" in html, pg.inner_text("#chat")[:100])
    ok("⑥ 历史内容仍可回放", "IMF 在 phenotype 表" in pg.inner_text("#chat"))

    b.close()

for sid in S:
    st.delete(sid)
print("\n---- summary ----")
for l in log:
    print(l)
print("FAILED:", sum(1 for l in log if l.startswith("{FAIL}")))
sys.exit(1 if any(l.startswith("{FAIL}") for l in log) else 0)
