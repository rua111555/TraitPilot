"""H4 的两条**独立**路径判据（在我的 C1/C2 之外）：

  R1 页面刷新后，正在跑的会话的待确认能否**重新出现**
     —— 反例：刷新即丢；用户此后永远无法作答，只能等 300s 超时被自动拒绝
  R2 切到别的会话后，A 的确认框若还挂着，作答是否**错发给 B**
     —— 反例：对话框停留在屏幕上但归属已经错乱

这两条与 C1（事件被 shouldRender 挡掉）是**不同**的代码路径：
刷新走的是「磁盘/快照重建」，切会话走的是「窗格暂存」，都不经过那条早期 return。

跑法：
    ./start_web.sh restart
    /home/chenyuxi/anaconda3/bin/python tools/concurrency/ui_confirm_lifecycle_probe.py
"""
import json
import sys
import time

sys.path.insert(0, "/home/chenyuxi/skill/agent")
from skill_agent.config import default_checkpoint_dir  # noqa: E402
from skill_agent.session_store import SessionStore  # noqa: E402

from playwright.sync_api import sync_playwright  # noqa: E402

B = "http://127.0.0.1:8787"
log = []
st = SessionStore(default_checkpoint_dir())
SA, SB = "cfly_A", "cfly_B"


def ok(name, cond, extra=""):
    log.append(("PASS" if cond else "FAIL") + " " + name)
    print(("  [PASS] " if cond else "  [缺陷] ") + name, flush=True)
    if extra:
        print("         " + extra, flush=True)


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

    # ============ R1：确认框出现后刷新页面 ============
    print("\nR1  确认框弹出后刷新页面 —— 用户还能不能作答（反例：刷新即丢）", flush=True)
    pg.evaluate("""(frame)=>{ handleEventSafe(frame, 'cfly_A'); }""",
                "event: confirm\ndata: " + json.dumps(
                    {"type": "confirm", "uid": "u_reload", "command": "rm -rf /data/tmp/x",
                     "reason": "ask-all", "seq": 9}, ensure_ascii=False) + "\n\n")
    time.sleep(0.5)
    before = pg.evaluate("""()=>({shown:document.querySelector('#overlay').classList.contains('show'),
                                  cmd:document.querySelector('#cmdText').textContent})""")
    print(f"     刷新前：弹窗={before['shown']} 命令={before['cmd']!r}", flush=True)

    pg.reload(wait_until="networkidle")
    pg.wait_for_selector("#sessionList .sess")
    time.sleep(1.5)
    after = pg.evaluate("""()=>({shown:document.querySelector('#overlay').classList.contains('show'),
                                 cmd:document.querySelector('#cmdText').textContent,
                                 pending: (typeof getRun==='function') ? (getRun(SID)||{}).pendingConfirm : null,
                                 hasAnyUI: !!document.querySelector('#overlay.show')})""")
    ok("R1 刷新后待确认仍可作答（弹窗回来，或其状态可见）",
       after["shown"] or bool(after["pending"]),
       f"刷新前弹窗={before['shown']}；刷新后弹窗={after['shown']} "
       f"run.pendingConfirm={after['pending']!r} "
       f"→ 刷新后用户无法作答={not (after['shown'] or after['pending'])}")

    # ============ R2：A 的确认框挂着时切到 B ============
    print("\nR2  A 的确认框挂着时切到 B —— 作答是否错发给 B（反例：界面与归属错乱）", flush=True)
    pg.evaluate("""()=>{
        window.__posted=[];
        const of=window.fetch;
        window.fetch=function(u,o){
            try{ if(String(u).includes('/api/confirm') && o && o.body)
                   window.__posted.push(JSON.parse(o.body)); }catch(e){}
            return of.apply(this,arguments); };
    }""")
    pg.evaluate("""(frame)=>{ handleEventSafe(frame, 'cfly_A'); }""",
                "event: confirm\ndata: " + json.dumps(
                    {"type": "confirm", "uid": "u_A1", "command": "plink --file cohort",
                     "reason": "ask-all", "seq": 10}, ensure_ascii=False) + "\n\n")
    time.sleep(0.5)
    shown_a = pg.evaluate("()=>document.querySelector('#overlay').classList.contains('show')")
    click_session(SB)                      # 切到别的会话
    time.sleep(0.8)
    mid = pg.evaluate("""()=>({shown:document.querySelector('#overlay').classList.contains('show'),
                               cmd:document.querySelector('#cmdText').textContent, sid:SID})""")
    print(f"     A 弹窗={shown_a} → 切到 B 后：弹窗={mid['shown']} 命令={mid['cmd']!r} "
          f"当前会话={mid['sid']}", flush=True)
    vis = mid["shown"]
    if vis:
        pg.click("#approveBtn")
        time.sleep(1.0)
    else:
        # 弹窗随切换消失 → 改用直接触发（模拟用户不在 UI 上作答的等价后果）
        pg.evaluate("""()=>{ const ev=state.pendingConfirm; if(ev) answerConfirm(true); }""")
        time.sleep(0.8)
    posted = pg.evaluate("()=>window.__posted")
    owner = posted[0].get("session") if posted else None
    ok("R2 作答提交给发问方 A（不是当前查看的 B）",
       owner == SA,
       f"切到 B 后弹窗仍可见={mid['shown']}；实际提交 session={owner!r}"
       f"（发问方 A={SA!r}，当前查看={SB!r}）")

    ok("R3 全程无 JS 运行时错误", not errs, f"pageerror={errs[:3]}")
    b.close()

for sid in (SA, SB):
    st.delete(sid)

print("\n---- summary ----")
for l in log:
    print(l)
failed = sum(1 for l in log if l.startswith("FAIL"))
print("FAILED:", failed)
sys.exit(1 if failed else 0)
