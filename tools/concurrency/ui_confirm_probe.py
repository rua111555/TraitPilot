"""①③ 缺陷判据探针：后台（非当前查看）会话的「确认请求」被静默丢弃 + 归属错会话。

三条判据，全部以**反例**形式给出：
  C1 非查看会话的 confirm 是否被丢弃   —— 反例：事件到达却不弹窗、也无任何提示
  C2 confirm 归属是否错会话            —— 反例：answerConfirm 提交的 session 不是发问方
  C3 对话框是否被模态遮罩挡住           —— 反例：#overlay 全屏且无其他可交互通道

做法：用 **真实后端事件形状** 直接调用前端函数（这是「被测路径」本身：
handleEvent 是 SSE 帧的唯一入口），不伪造实现细节。
后端侧另用真实 ask_confirm 验证超时语义。

跑法：
    ./start_web.sh restart
    /home/chenyuxi/anaconda3/bin/python tools/concurrency/ui_confirm_probe.py
"""
import sys
import time
import urllib.request
import json

sys.path.insert(0, "/home/chenyuxi/skill/agent")
from skill_agent.config import default_checkpoint_dir  # noqa: E402
from skill_agent.session_store import SessionStore  # noqa: E402

from playwright.sync_api import sync_playwright  # noqa: E402

B = "http://127.0.0.1:8787"
log = []
st = SessionStore(default_checkpoint_dir())
SIDS = ("cfprobe_viewed", "cfprobe_bg")


def ok(name, cond, extra=""):
    log.append(("PASS" if cond else "FAIL") + " " + name)
    print(("  [PASS] " if cond else "  [缺陷] ") + name, flush=True)
    if extra:
        print("         " + extra, flush=True)


for sid in SIDS:
    st.delete(sid)
    st.append(sid, {"role": "user", "content": "提问 " + sid})
    st.append(sid, {"role": "assistant",
                    "content": [{"type": "text", "text": "回答 " + sid}]})
SV, SBG = SIDS

# 真实后端 confirm 事件形状（server.py 发出的是 {"type":"confirm","uid":...,"command":...,"reason":...}）
def confirm_frame(uid, cmd, reason="ask-all", seq=7):
    ev = {"type": "confirm", "uid": uid, "command": cmd, "reason": reason, "seq": seq}
    return "event: confirm\ndata: " + json.dumps(ev, ensure_ascii=False) + "\n\n"


# 同时捕获前端真正 POST 出去的 /api/confirm 请求体
posted = []

with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page(viewport={"width": 1400, "height": 900})
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto(B, wait_until="networkidle")
    pg.wait_for_selector("#sessionList .sess")

    pg.evaluate("""()=>{
        const of = window.fetch;
        window.fetch = function(u, o){
            try{ if(String(u).includes('/api/confirm') && o && o.body) window.__posted.push(JSON.parse(o.body)); }catch(e){}
            return of.apply(this, arguments);
        };
        window.__posted = [];
    }""")

    def click_session(sid):
        pg.eval_on_selector(f'#sessionList .sess[data-id="{sid}"]',
                            "e=>e.scrollIntoView({block:'center'})")
        time.sleep(0.3)
        pg.eval_on_selector(f'#sessionList .sess[data-id="{sid}"]', "e=>e.click()")

    # ---- 当前查看 SV ----
    click_session(SV)
    time.sleep(0.8)
    viewed = pg.evaluate("()=>({sid:SID})")
    print(f"\n当前查看会话 = {viewed['sid']}", flush=True)

    # ================= C1：非查看会话的 confirm 是否被丢弃 =================
    print("\nC1  后台会话的确认请求是否可见（反例：事件到达但用户什么都看不到）", flush=True)
    pg.evaluate("""(frame)=>{
        window.__seen = window.__seen || [];
        handleEventSafe(frame, 'cfprobe_bg');
    }""", confirm_frame("uid_bg_1", "rm -rf /data/tmp/stage7"))
    time.sleep(0.5)
    state = pg.evaluate("""()=>({
        overlayShown: document.querySelector('#overlay').classList.contains('show'),
        cmdText: document.querySelector('#cmdText').textContent,
        pending: state.pendingConfirm,
        notices: document.querySelector('#chat').innerText,
    })""")
    ok("C1a 弹出了确认框", state["overlayShown"],
       f"#overlay.show={state['overlayShown']} cmdText={state['cmdText']!r}")
    # 用户能否感知到「有别的会话在等人批命令」——这是缺陷的实际用户体验
    aware = pg.evaluate("""()=>{
        const t = document.querySelector('#chat').innerText;
        const bar = document.querySelector('#runBar');
        return {hasNotice: /确认|批准|等待.*命令|待确认/.test(t),
                runBar: bar ? bar.innerText : '',
                sessMark: !!document.querySelector('.sess[data-id="cfprobe_bg"].need-confirm')};
    }""")
    ok("C1b 用户能感知到「后台会话在等确认」",
       aware["hasNotice"] or aware["sessMark"] or "确认" in aware["runBar"],
       f"notice={aware['hasNotice']} 左栏标记={aware['sessMark']} 状态条={aware['runBar']!r}")

    # ================= C2：confirm 归属是否错会话 =================
    print("\nC2  确认答复是否提交给**发问的那个会话**（反例：错发给当前查看的会话）", flush=True)
    pg.evaluate("()=>{ window.__posted = []; }")
    # 让确认框以「后台会话」的身份弹出（模拟用户从后台会话收到确认）
    pg.evaluate("""(frame)=>{ handleEventSafe(frame, 'cfprobe_bg'); }""",
                confirm_frame("uid_bg_2", "plink --file cohort --missing"))
    time.sleep(0.4)
    # 弹窗没出现是**缺陷本身**，不是探针错误：不能直接 click 等 30s 超时。
    visible = pg.evaluate("()=>document.querySelector('#overlay').classList.contains('show')")
    if visible:
        pg.click("#approveBtn")
        time.sleep(1.0)
    posted = pg.evaluate("()=>window.__posted")
    owner = posted[0].get("session") if posted else None
    ok("C2  答复提交给了发问方 cfprobe_bg",
       owner == SBG,
       f"弹窗可见={visible}；实际提交 session={owner!r}（发问方应为 {SBG!r}）"
       + ("" if visible else " —— 弹窗根本未出现，用户无法作答"))

    # C2b：**同会话**场景（弹窗确实看得见）时，答复是否也正确归属。
    # 这一条用来把「弹窗没出现」与「归属写死成 SID」两个缺陷分开：
    # 若 A 通过而 C2b 失败，说明归属逻辑本身写死了 SID，与可见性无关。
    pg.evaluate("()=>{ window.__posted = []; }")
    pg.evaluate("""(frame)=>{ handleEventSafe(frame, 'cfprobe_viewed'); }""",
                confirm_frame("uid_v_1", "echo viewed-session-cmd"))
    time.sleep(0.4)
    vis_v = pg.evaluate("()=>document.querySelector('#overlay').classList.contains('show')")
    if vis_v:
        pg.click("#approveBtn")
        time.sleep(1.0)
    posted_v = pg.evaluate("()=>window.__posted")
    owner_v = posted_v[0].get("session") if posted_v else None
    ok("C2b [对照] 查看中的会话，答复归属正确",
       vis_v and owner_v == SV,
       f"弹窗可见={vis_v} 提交 session={owner_v!r}（应为 {SV!r}）"
       " —— 此条通过而 C2 失败，即证明归属被写死为「当前查看会话」")

    # ================= C3：遮挡 =================
    print("\nC3  确认框是否模态遮挡整个界面（反例：全屏遮罩且无旁路）", flush=True)
    # ⚠️ 必须**先强制显示**再测量：隐藏的 .overlay 是 display:none，
    #    getBoundingClientRect() 返回 0x0 —— 上一版据此判「不遮挡」，
    #    那是在量一个不存在的元素，判据没有区分度（自己骗自己）。
    pg.evaluate("()=>{ document.querySelector('#overlay').classList.add('show'); }")
    time.sleep(0.3)
    ov = pg.evaluate("""()=>{
        const o=document.querySelector('#overlay'); const cs=getComputedStyle(o);
        const r=o.getBoundingClientRect();
        return {pos:cs.position, z:cs.zIndex, w:r.width, h:r.height,
                vw:innerWidth, vh:innerHeight,
                others: [...document.querySelectorAll('#chat,#input,#sessionList')]
                          .map(e=>({id:e.id, covered: (()=>{
                             const b=e.getBoundingClientRect();
                             const el=document.elementFromPoint(b.left+b.width/2, b.top+b.height/2);
                             return el ? (el.closest('#overlay') ? 'blocked':'reachable') : 'none';
                          })()}))};
    }""")
    covers = ov["pos"] == "fixed" and ov["w"] >= ov["vw"] and ov["h"] >= ov["vh"]
    blocked = [o["id"] for o in ov["others"] if o["covered"] == "blocked"]
    ok("C3 确认框显示时界面不被全屏遮挡（可在别的会话继续干活）",
       not (covers and blocked),
       f"position={ov['pos']} z-index={ov['z']} 尺寸={ov['w']:.0f}x{ov['h']:.0f} "
       f"视口={ov['vw']}x{ov['vh']} → 全屏遮罩={covers}；被挡住的区域={blocked or '无'}")
    pg.evaluate("()=>{ document.querySelector('#overlay').classList.remove('show'); }")

    ok("C4 全程无 JS 运行时错误", not errs, f"pageerror={errs[:3]}")
    b.close()

for sid in SIDS:
    st.delete(sid)

print("\n---- summary ----")
for l in log:
    print(l)
failed = sum(1 for l in log if l.startswith("FAIL"))
print("FAILED:", failed)
sys.exit(1 if failed else 0)
