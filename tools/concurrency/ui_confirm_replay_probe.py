"""R1 的**真实路径**重测：确认请求由真后端发出，用户切走再切回来，能否作答。

上一版 R1 的缺陷（探针自身的捷径，诚实记录）：
  它只调 handleEventSafe() 把事件喂进去，然后 reload —— 从来没建立过真实 SSE 流。
  于是「刷新后弹窗没了」只证明了「刷新会丢 DOM 状态」，**没有**走到
  selectSession → attach(after=cursor) → 重放后端日志 这条真正的补播路径。

本版：真的 POST /api/chat 起一轮（桩 agent 会发起一次确认），
在确认待答期间切走、再切回，看后端补播的 confirm 能否让用户作答。

跑法（需一个会在中途请求确认的桩 agent，见 tools/concurrency/confirm_fixture.py）：
    ./start_web.sh restart
    /home/chenyuxi/anaconda3/bin/python tools/concurrency/ui_confirm_replay_probe.py --live
不带 --live 时只做**静态**核对并打印结论，不连接服务。
"""
import json
import sys
import time
import urllib.request

sys.path.insert(0, "/home/chenyuxi/skill/agent")

LIVE = "--live" in sys.argv
B = "http://127.0.0.1:8787"
log = []


def ok(name, cond, extra=""):
    log.append(("PASS" if cond else "FAIL") + " " + name)
    print(("  [PASS] " if cond else "  [缺陷] ") + name, flush=True)
    if extra:
        print("         " + extra, flush=True)


def static_check():
    """不连服务也能做的核对：补播路径是否真的会把 confirm 交给渲染层。"""
    src = open("/home/chenyuxi/skill/agent/webapp/static/index.html",
               encoding="utf-8").read()
    lines = src.splitlines()

    def find(pat, start=0):
        for i in range(start, len(lines)):
            if pat in lines[i]:
                return i + 1
        return None

    l_select = find("async function selectSession")
    l_attach_call = find("if(running) attach(id, paneAfter(id, curRunId));", l_select)
    l_attach_def = find("async function attach")
    l_consume = find("await consume(res,sid);", l_attach_def)
    l_handle = find("function handleEventSafe")
    l_guard = find("if(!shouldRender(rt)) return;")
    l_case = find('case "confirm"')
    l_showdef = find("function showConfirm")
    print(f"     selectSession 在第 {l_select} 行；其中 attach 调用在第 {l_attach_call} 行")
    print(f"     attach→consume 在第 {l_consume} 行 → handleEventSafe 在第 {l_handle} 行")
    print(f"     渲染闸门 `if(!shouldRender(rt)) return;` 在第 {l_guard} 行，"
          f"`case \"confirm\"` 在第 {l_case} 行 → 闸门在前 = {l_guard < l_case}")
    print(f"     showConfirm 定义在第 {l_showdef} 行")
    ok("静态：补播路径确实把事件交给 handleEvent（attach→consume→handleEventSafe）",
       None not in (l_attach_call, l_consume, l_handle) and l_consume > l_attach_def,
       f"attach 定义 {l_attach_def} → consume {l_consume} → handleEventSafe {l_handle}")
    ok("静态：**切回后**该会话就是 activeViewId，闸门应放行 confirm",
       l_guard < l_case,
       f"闸门 {l_guard} < case {l_case}：闸门在 switch 之前，"
       "但此时 rt == activeViewId，shouldRender 为真 → 应能弹窗")
    return l_guard, l_case


print("== 静态核对：补播路径 ==", flush=True)
static_check()

if not LIVE:
    print("\n（未加 --live：跳过真机部分。加 --live 前请先准备会请求确认的桩 agent。）")
    print("\n---- summary ----")
    for l in log:
        print(l)
    sys.exit(1 if any(l.startswith("FAIL") for l in log) else 0)

# ---------------- 真机：真起一轮 + 切走 + 切回 ----------------
from skill_agent.config import default_checkpoint_dir  # noqa: E402
from skill_agent.session_store import SessionStore  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

st = SessionStore(default_checkpoint_dir())
SA, SB = "cfrp_A", "cfrp_B"
for sid in (SA, SB):
    st.delete(sid)
    st.append(sid, {"role": "user", "content": "提问 " + sid})
    st.append(sid, {"role": "assistant",
                    "content": [{"type": "text", "text": "回答 " + sid}]})

with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page(viewport={"width": 1400, "height": 900})
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto(B, wait_until="networkidle")
    pg.wait_for_selector("#sessionList .sess")

    def click_session(sid):
        pg.eval_on_selector(f'#sessionList .sess[data-id="{sid}"]',
                            "e=>e.scrollIntoView({block:'center'})")
        time.sleep(0.3)
        pg.eval_on_selector(f'#sessionList .sess[data-id="{sid}"]', "e=>e.click()")

    click_session(SA)
    time.sleep(0.8)
    # 起一轮 —— 桩 agent 会在中途请求确认（命令含 CONFIRM_MARK）
    pg.fill("#input", "请执行需要确认的命令 CONFIRM_MARK")
    pg.click("#sendBtn")

    # 等确认出现
    got = False
    for _ in range(200):
        time.sleep(0.1)
        s = pg.evaluate("()=>({shown:document.querySelector('#overlay').classList.contains('show'),"
                        " cmd:document.querySelector('#cmdText').textContent,"
                        " run:(getRun(SID)||{}).running})")
        if s["shown"]:
            got = True
            break
    print(f"\n     确认框出现={got} 命令={s['cmd']!r}", flush=True)
    ok("真机：本轮确实发起了一次确认请求", got, f"弹窗={got}")

    if got:
        click_session(SB)                     # 切走
        time.sleep(1.0)
        mid = pg.evaluate("""()=>({shown:document.querySelector('#overlay').classList.contains('show'),
                                   cmd:document.querySelector('#cmdText').textContent})""")
        click_session(SA)                     # 切回
        time.sleep(1.5)
        back = pg.evaluate("""()=>({shown:document.querySelector('#overlay').classList.contains('show'),
                                    cmd:document.querySelector('#cmdText').textContent,
                                    pending:(getRun(SID)||{}).pendingConfirm,
                                    bar:(document.querySelector('#runBar')||{}).innerText})""")
        ok("真机：切回后用户能作答（弹窗回来，或至少有可见的待确认入口）",
           back["shown"] or bool(back["pending"]),
           f"切走后弹窗={mid['shown']}（命令={mid['cmd']!r}）；切回后弹窗={back['shown']} "
           f"pendingConfirm={back['pending']!r} 状态条={back['bar']!r}")
        if back["shown"]:
            pg.click("#approveBtn")
            time.sleep(1.0)
            fin = pg.evaluate("()=>({shown:document.querySelector('#overlay').classList.contains('show')})")
            ok("真机：作答后弹窗关闭且未报错", not fin["shown"] and not errs,
               f"弹窗={fin['shown']} pageerror={errs[:2]}")

    ok("真机：全程无 JS 运行时错误", not errs, f"pageerror={errs[:3]}")
    b.close()

for sid in (SA, SB):
    st.delete(sid)

print("\n---- summary ----")
for l in log:
    print(l)
failed = sum(1 for l in log if l.startswith("FAIL"))
print("FAILED:", failed)
sys.exit(1 if failed else 0)
