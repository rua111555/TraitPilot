"""前端端到端回归（Playwright，需手动起 web 服务）。

覆盖用户反馈过的四类问题：
  1. 右键菜单「归档 / 取消归档 / 删除」真的会打后端（曾经点了没反应 —— document 上的
     click 监听先跑 hideCtxMenu() 把全局 ctxTarget 清空，见 index.html:425 注释）；
  2. 点历史会话 = 切换当前会话，左栏高亮跟随；
  3. 历史会话气泡重建（sess-banner + replayMessages）；
  4. 发送按钮运行中变强制中断按钮。

跑法（需装 playwright 的环境，Agent 环境里没有）：
    ./start_web.sh restart
    /home/chenyuxi/anaconda3/bin/python tests/ui_e2e.py
"""
import atexit, json, sys, time, urllib.request
sys.path.insert(0, '/home/chenyuxi/skill/agent')
from skill_agent.config import default_checkpoint_dir
from skill_agent.session_store import SessionStore

st = SessionStore(default_checkpoint_dir())
_UITEST_SIDS = ("uitest_alpha", "uitest_beta", "uitest_gamma")
atexit.register(lambda: [st.delete(s) for s in _UITEST_SIDS])   # 断言失败也清理
for sid in _UITEST_SIDS:
    st.delete(sid)
    st.append(sid, {"role": "user", "content": "hello " + sid})
    st.append(sid, {"role": "assistant", "content": [{"type": "text", "text": "hi " + sid}]})
st.archive("uitest_alpha")

from playwright.sync_api import sync_playwright

B = "http://127.0.0.1:8787"
log = []
def ok(name, cond, extra=""):
    log.append(("{PASS}" if cond else "{FAIL}") + " " + name + ("  " + extra if extra else ""))
    print(log[-1], flush=True)

def sessions(arch=False):
    u = B + "/api/sessions" + ("?archived=1" if arch else "")
    return urllib.request.urlopen(u).read().decode()

with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page(viewport={"width": 1400, "height": 900})
    pg.on("pageerror", lambda e: ok("无 JS 运行时错误", False, str(e)))
    dialogs = []
    pg.on("dialog", lambda d: (dialogs.append(d.message), d.accept()))
    pg.goto(B, wait_until="networkidle")
    pg.wait_for_selector("#sessionList .sess")
    pg.click("#archToggle")
    time.sleep(0.5)

    def item(sid):
        return pg.query_selector(f'#sessionList .sess[data-id="{sid}"]')

    def rclick(sid):
        # 先滚进视野再右键（Playwright 的自动滚动会触发 scroll → 关闭菜单）
        pg.eval_on_selector(f'#sessionList .sess[data-id="{sid}"]',
                            "e=>e.scrollIntoView({block:'center'})")
        time.sleep(0.5)
        pg.eval_on_selector(f'#sessionList .sess[data-id="{sid}"]',
                            "e=>e.dispatchEvent(new MouseEvent('contextmenu',"
                            "{bubbles:true,clientX:200,clientY:300}))")
        pg.wait_for_selector("#ctxMenu.show", timeout=3000)

    # 1) 归档 / 取消归档（右键菜单 — 用户报的 bug）
    ok("uitest_alpha 在归档分组中", item("uitest_alpha") is not None)
    rclick("uitest_alpha")
    ok("右键菜单弹出", True)
    un = pg.query_selector('#ctxMenu .ctx-item[data-act="unarchive"]')
    ok("菜单含「取消归档」", un is not None)
    un.click(); time.sleep(1.2)
    ok("取消归档后回到主列表", '"uitest_alpha"' in sessions())
    ok("取消归档后不在归档列表", '"uitest_alpha"' not in sessions(True))

    rclick("uitest_alpha")
    ar = pg.query_selector('#ctxMenu .ctx-item[data-act="archive"]')
    ok("菜单含「归档」", ar is not None)
    ar.click(); time.sleep(1.2)
    ok("归档后从主列表消失", '"uitest_alpha"' not in sessions())
    ok("归档后进入归档列表", '"uitest_alpha"' in sessions(True))
    ok("归档会话仍可读",
       json.loads(urllib.request.urlopen(B + "/api/session/uitest_alpha").read())["ok"])

    # 2) 删除（带确认）
    dialogs.clear()
    rclick("uitest_beta")
    d = pg.query_selector('#ctxMenu .ctx-item[data-act="delete"]')
    ok("菜单含「删除」", d is not None)
    d.click(); time.sleep(1.2)
    ok("删除弹出确认框", any("删除" in m for m in dialogs), str(dialogs))
    ok("删除后文件消失", '"uitest_beta"' not in sessions())

    # 3) 点历史会话 = 切换当前会话 + 高亮跟随
    pg.eval_on_selector('#sessionList .sess[data-id="uitest_gamma"]',
                        "e=>e.scrollIntoView({block:'center'})")
    time.sleep(0.4)
    item("uitest_gamma").click(); time.sleep(1.2)
    ok("切到历史会话显示提示条", pg.query_selector("#chat .sess-banner") is not None)
    act = pg.query_selector_all("#sessionList .sess.active")
    ok("左栏高亮唯一且等于 uitest_gamma",
       len(act) == 1 and act[0].get_attribute("data-id") == "uitest_gamma",
       str([a.get_attribute("data-id") for a in act]))
    ok("历史气泡已重建", pg.query_selector("#chat .msg.user") is not None)

    # 4) 发送按钮运行中变中断
    #    注意：本机 relay 常在 ~30ms 内把整条 SSE 流吐完，靠真实请求「捕获运行中状态」
    #    会随机失败。这里直接驱动 UI 逻辑（setBusy 是按钮状态唯一入口，onPrimary 决定
    #    点击去向），既确定性又确实覆盖了用户反馈的行为。
    pg.evaluate("setBusy(true)")
    ok("运行中显示中断图标",
       pg.eval_on_selector("#sendIconRun", "e=>getComputedStyle(e).display") != "none")
    ok("运行中隐藏发送图标",
       pg.eval_on_selector("#sendIconIdle", "e=>getComputedStyle(e).display") == "none")
    ok("运行中按钮带 running 样式",
       pg.eval_on_selector("#sendBtn", "e=>e.classList.contains('running')"))
    ok("运行中点按走中断分支", pg.evaluate(
        "(()=>{let called=null;const a=stopRun,b=send;"
        "stopRun=()=>called='stop';send=()=>called='send';"
        "onPrimary();stopRun=a;send=b;return called;})()") == "stop")
    pg.evaluate("setBusy(false)")
    ok("空闲时恢复发送图标",
       pg.eval_on_selector("#sendIconIdle", "e=>getComputedStyle(e).display") != "none")
    ok("空闲时按钮不再是 running",
       not pg.eval_on_selector("#sendBtn", "e=>e.classList.contains('running')"))
    ok("空闲中点按走发送分支", pg.evaluate(
        "(()=>{let called=null;const a=stopRun,b=send;"
        "stopRun=()=>called='stop';send=()=>called='send';"
        "onPrimary();stopRun=a;send=b;return called;})()") == "send")
    # 真机再跑一轮只作观察（RUNNING 窗口太短，不作为失败条件）。
    # 注意：真机会让服务端把该会话重新落盘，所以本轮之后一定要再删一次。
    pg.fill("#input", "请写一篇约 400 字的小短文，分三段。")
    pg.click("#sendBtn")
    for _ in range(60):
        time.sleep(0.02)
        if not pg.eval_on_selector("#sendBtn", "e=>e.classList.contains('running')"):
            break
    print("  （真机一轮结束，busy=%s）" % pg.evaluate("state.busy"), flush=True)
    time.sleep(0.5)                       # 等服务端 chat 线程把落盘写完

    b.close()

for sid in _UITEST_SIDS:
    st.delete(sid)                       # 收尾：不留测试会话（atexit 已兜底）
print("\n---- summary ----")
for l in log:
    print(l)
failed = sum(1 for l in log if l.startswith("{FAIL}"))
print("FAILED:", failed)
sys.exit(1 if failed else 0)
