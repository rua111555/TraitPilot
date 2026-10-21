import sys, time
sys.path.insert(0, '/home/chenyuxi/skill/agent')
from skill_agent.config import default_checkpoint_dir
from skill_agent.session_store import SessionStore

st = SessionStore(default_checkpoint_dir())
S = {
    "uidbg_empty_assist": [                       # 只问一句，模型从未回复（用户报的那种）
        {"role": "user", "content": "数据库里有什么数据？"}],
    "uidbg_partial": [                            # 有完成轮 + 尾轮未完成
        {"role": "user", "content": "先看看目录"},
        {"role": "assistant", "content": [{"type": "text", "text": "目录里有 a、b 两个文件。"}]},
        {"role": "user", "content": "再跑个分析"},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "Bash",
                                           "input": {"command": "ls"}}]}],
    "uidbg_tools_only": [                         # 整段都是工具轮
        {"role": "user", "content": "查库"},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "Bash",
                                           "input": {"command": "sqlite3 x .tables"}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "pigs"}]},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t2", "name": "Read",
                                           "input": {"file_path": "/tmp/x"}}]}],
    "uidbg_complete": [                           # 完整会话（回归对照）
        {"role": "user", "content": "你好呀"},
        {"role": "assistant", "content": [{"type": "thinking", "thinking": "打个招呼"},
                                          {"type": "text", "text": "你好！有什么可以帮你？"}]}],
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
    time.sleep(2.0)                      # 等左栏预览补齐

    def pick(sid):
        pg.eval_on_selector(f'#sessionList .sess[data-id="{sid}"]',
                            "e=>e.scrollIntoView({block:'center'})")
        time.sleep(0.4)
        pg.query_selector(f'#sessionList .sess[data-id="{sid}"]').click()
        time.sleep(1.2)

    def chat_text():
        return pg.inner_text("#chat")

    # 1) 核心复现：只有一条 user、模型从未回复
    pick("uidbg_empty_assist")
    t = chat_text()
    ok("① 用户报的会话不再空白（显示了用户原话）", "数据库里有什么数据" in t, repr(t[:120]))
    ok("① 不再显示空状态", "没有可显示的内容" not in t)
    ok("① 有「尚无完成回复」提示", "还没有任何已完成的助手回复" in t, repr(t[:200]))

    # 2) 完成轮 + 尾轮未完成：全显示 + 分隔条 + 丢弃提示
    pick("uidbg_partial")
    t = chat_text()
    ok("② 已完成轮可见", "目录里有 a、b 两个文件" in t)
    ok("② 未完成轮的用户话可见", "再跑个分析" in t)
    ok("② 未完成轮的 tool_use 卡可见", "Bash" in t)
    ok("② 有「以下内容尚未完成」分隔条", "尚未完成" in t)
    ok("② 有「末尾 N 条不会发给模型」提示", "不会发给模型" in t)

    # 3) 整段工具轮
    pick("uidbg_tools_only")
    t = chat_text()
    ok("③ 工具轮会话可见（工具卡）", "Bash" in t and "Read" in t)
    # 工具结果在折叠的工具卡里，innerText 取不到 → 看 textContent 确认确实回填了
    res_txt = pg.eval_on_selector("#chat", "e=>e.textContent")
    ok("③ 工具结果已回填到工具卡", "pigs" in res_txt)
    ok("③ 不显示空状态", "没有可显示的内容" not in t)

    # 4) 完整会话回归：不应有分隔条/未完成提示
    pick("uidbg_complete")
    t = chat_text()
    ok("④ 完整会话正常显示", "你好！有什么可以帮你" in t)
    ok("④ 完整会话无「尚未完成」分隔条", "尚未完成" not in t)
    ok("④ 完整会话无丢弃提示", "不会发给模型" not in t)
    ok("④ 完整会话有思考折叠块", "思考过程" in t)

    # 5) 左栏标题/预览（标题=用户第一句话；预览只在与标题不同时才显示为副行）
    pg.click("#reloadSessBtn")
    time.sleep(2.0)
    names = pg.eval_on_selector_all("#sessionList .sess .nm", "es=>es.map(e=>e.innerText)")
    dsc = pg.eval_on_selector_all("#sessionList .sess .dsc", "es=>es.map(e=>e.innerText)")
    ok("⑤ 左栏标题=用户第一句话", "数据库里有什么数据？" in names and "先看看目录" in names, str(names[:4]))
    ok("⑤ 标题不是会话 id", not any("-" in n for n in names), str(names[:4]))
    ok("⑤ 首轮之后另有提问的会话显示预览副行", len(dsc) >= 1, str(dsc[:4]))

    b.close()

for sid in S:
    st.delete(sid)
print("\n---- summary ----")
for l in log:
    print(l)
print("FAILED:", sum(1 for l in log if l.startswith("{FAIL}")))
sys.exit(1 if any(l.startswith("{FAIL}") for l in log) else 0)
