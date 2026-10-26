"""① 的核心用户体验判据：「本会话在跑时，发送键按下去有没有反应」。

这是用户报告的**直接症状**在最贴近的层面上的复现：
  「为什么我的skill在运行的过程中，我的对话框没有解放出来」

区分「输入框被禁用」（上一轮已修）与「输入框可用但发送被静默吞掉」（本轮新发现）：
  D1 运行中 #input **可编辑**            —— 反例：disabled，连字都打不进去
  D2 运行中按下发送 → **必须给出反馈**   —— 反例：什么都不发生（无提示、无排队、无报错）
  D3 提示必须说明「怎么继续」            —— 反例：只说不能发，不说怎么办

D2 的判据刻意做成「要么发出去、要么有用户可见的反馈」：
这样它既能抓住「静默吞掉」，也不会把「排队」这种**更好**的设计判为失败。

跑法：
    ./start_web.sh restart
    /home/chenyuxi/anaconda3/bin/python tools/concurrency/ui_composer_probe.py
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
SID = "composer_probe"


def ok(name, cond, extra=""):
    log.append(("PASS" if cond else "FAIL") + " " + name)
    print(("  [PASS] " if cond else "  [缺陷] ") + name, flush=True)
    if extra:
        print("         " + extra, flush=True)


st.delete(SID)
st.append(SID, {"role": "user", "content": "第一条提问 " + SID})
st.append(SID, {"role": "assistant",
                "content": [{"type": "text", "text": "先前的回答 " + SID}]})

with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page(viewport={"width": 1400, "height": 900})
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto(B, wait_until="networkidle")
    pg.wait_for_selector("#sessionList .sess")
    pg.eval_on_selector(f'#sessionList .sess[data-id="{SID}"]',
                        "e=>e.scrollIntoView({block:'center'})")
    time.sleep(0.3)
    pg.eval_on_selector(f'#sessionList .sess[data-id="{SID}"]', "e=>e.click()")
    time.sleep(0.8)

    # 把本会话置为「正在跑」（用真实运行态 API，不伪造内部状态）
    pg.evaluate("(sid)=>{ markRunning(sid,{running:true,state:'running'}); }", SID)
    time.sleep(0.4)

    print("\nD1  运行中能否继续打字（反例：#input 被 disable）", flush=True)
    pg.fill("#input", "这条能不能打进去")
    typed = pg.eval_on_selector("#input", "e=>e.value")
    dis = pg.eval_on_selector("#input", "e=>e.disabled")
    ok("D1 运行中 #input 可编辑（未被 disable）",
       (not dis) and typed == "这条能不能打进去",
       f"#input.disabled={dis} 读回值={typed!r}")

    print("\nD2  运行中的会话按发送键 —— 按钮的真实语义（反例：以为是发送、实际是取消）", flush=True)
    # ⚠️ 判据的演化（诚实记录）：
    #   第一版问「有没有反馈」——**没有区分度**：状态条里的秒表每帧都在变，恒真。
    #   第二版抠掉秒表后再问「有没有反馈」——还是恒真，因为 onPrimary() 在 busy 时会
    #     走 stopRun()，状态条**真的**会变成「正在取消…」。
    #   所以真正要问的**不是**「有没有反应」，而是「这个反应是不是发送」：
    #     用户按下去以为要发消息，实际把正在跑的管线取消了 —— 这才是缺陷。
    pg.evaluate("()=>{ window.__cancelled = false; "
                "const of=window.fetch; window.fetch=function(u,o){"
                "  if(String(u).includes('/api/cancel')) window.__cancelled=true;"
                "  return of.apply(this,arguments); }; }")
    time.sleep(0.5)
    pg.fill("#input", "这条能不能打进去")
    pg.click("#sendBtn")
    time.sleep(1.5)
    sem = pg.evaluate("""()=>({
        cancelled: window.__cancelled,
        state: (getRun(SID)||{}).state,
        inputVal: document.querySelector('#input').value,
        btnTitle: document.querySelector('#sendBtn').title,
    })""")
    # 「按发送 = 取消」是**允许**的设计，但必须：①按钮自己说清楚 ②用户打的字不能白打
    ok("D2 运行中按发送键不会**静默**吞掉消息（要么发出去、要么有明确提示）",
       sem["cancelled"] or sem["inputVal"] != "这条能不能打进去",
       f"触发了 /api/cancel={sem['cancelled']} 会话状态={sem['state']} "
       f"按钮 title={sem['btnTitle']!r} 输入框={sem['inputVal']!r}")
    ok("D2b 若该键在运行中变成「取消」，提示必须足够明确（不靠颜色/图标猜）",
       (not sem["cancelled"]) or ("中断" in sem["btnTitle"] or "取消" in sem["btnTitle"]
                                 or "停止" in sem["btnTitle"]),
       f"按钮 title={sem['btnTitle']!r}"
       + ("：明确的文字提示" if any(k in sem['btnTitle'] for k in ('中断','取消','停止'))
          else "：**没有**文字说明——用户只能从 ➤→■ 和变红去猜"))

    print("\nD5  一个会话在跑时，**另一个**会话能不能正常发消息（用户的核心诉求）", flush=True)
    SID2 = "composer_probe_b"
    st.append(SID2, {"role": "user", "content": "B 的提问"})
    st.append(SID2, {"role": "assistant",
                     "content": [{"type": "text", "text": "B 的回答"}]})
    pg.evaluate("()=>{ renderSessions(); }")
    time.sleep(1.0)
    # 走真实路径切到 B（SID 仍在 running 态）
    pg.eval_on_selector(f'#sessionList .sess[data-id="{SID2}"]',
                        "e=>e.scrollIntoView({block:'center'})")
    time.sleep(0.3)
    pg.eval_on_selector(f'#sessionList .sess[data-id="{SID2}"]', "e=>e.click()")
    time.sleep(1.0)
    at_b = pg.evaluate("()=>({sid:SID, busy:state.busy, "
                       "title:document.querySelector('#sendBtn').title, "
                       "stopVisible:getComputedStyle(document.querySelector('#stopBtn')).display})")
    ok("D5a 切到空闲会话后，按钮恢复为「发送」（不是仍显示取消）",
       at_b["sid"] == SID2 and at_b["busy"] is False,
       f"当前会话={at_b['sid']} state.busy={at_b['busy']} 按钮 title={at_b['title']!r} "
       f"停止键显示={at_b['stopVisible']}")
    pg.fill("#input", "B 会话应当能发出去")
    pg.click("#sendBtn")
    time.sleep(1.5)
    bstate = pg.evaluate("""()=>({
        cancelled: window.__cancelled,
        sent: document.querySelector('#chat').innerText.includes('B 会话应当能发出去'),
        inputCleared: !document.querySelector('#input').value,
    })""")
    ok("D5b [核心] 别的会话在跑时，本会话的消息**真的发出去了**",
       bstate["sent"] or (bstate["inputCleared"] and not bstate["cancelled"]),
       f"聊天区出现该消息={bstate['sent']} 输入框已清空={bstate['inputCleared']} "
       f"（截图窗口内未触发 cancel={not bstate['cancelled']}）")
    st.delete(SID2)

    ok("D4 全程无 JS 运行时错误", not errs, f"pageerror={errs[:3]}")

    # ---- 负控：证明 D2/D5 的判据确实能判为不合格 ----
    # 构造「按下去什么都不发生」：把发送按钮换成没有监听器的克隆体，再按一次。
    # 若此时判据仍说「没问题」，说明判据没有区分度。
    print("\n负控  「静默吞掉」的判据能否被抓到", flush=True)
    # ⚠️ 负控必须**自证隔离**：上一版用「聊天区文字是否变化」当信号，
    #    而 D5b 刚往聊天区发过一条消息，渲染是异步的 → 差异来自 D5b 的余波，
    #    于是负控假失败。改用**本负控专属的标志词**，只看它有没有进聊天区。
    NEG_MARK = "NEGCTLMARK-9f3a"
    pg.evaluate("""()=>{
        const b = document.querySelector('#sendBtn');
        const c = b.cloneNode(true);          // 克隆体没有事件监听 → 点了没反应
        b.parentNode.replaceChild(c, b);
    }""")
    pg.evaluate("()=>{ window.__cancelled = false; }")
    pg.fill("#input", f"负控 {NEG_MARK} 这次应当什么都不发生")
    time.sleep(0.5)
    pg.click("#sendBtn")            # 点没有监听器的克隆体
    time.sleep(1.2)
    neg = pg.evaluate("""(mark)=>({
        markInChat: document.querySelector('#chat').innerText.includes(mark),
        inputVal: document.querySelector('#input').value,
        cancelled: window.__cancelled,
    })""", NEG_MARK)
    would_fail = not (neg["markInChat"] or neg["cancelled"])
    ok("负控：摘掉发送处理器后，判据确实判为「静默吞掉」",
       would_fail,
       f"标志词进了聊天区={neg['markInChat']} 触发取消={neg['cancelled']} "
       f"输入框仍为={neg['inputVal']!r} → 判为静默吞掉={would_fail}"
       "（若这里为 False，则判据无区分度，必须换判据）")

    # ---- 正控：同一个判据在**正常**按钮上必须判为「没吞掉」----
    # 只有负控会退化成「永远判不合格」的废判据；正控证明它两个方向都能动。
    POS_MARK = "POSCTLMARK-4b7e"
    pg.evaluate("""()=>{ location.reload(); }""")
    pg.wait_for_selector("#sessionList .sess")
    time.sleep(1.0)
    pg.eval_on_selector(f'#sessionList .sess[data-id="{SID}"]',
                        "e=>e.scrollIntoView({block:'center'})")
    time.sleep(0.3)
    pg.eval_on_selector(f'#sessionList .sess[data-id="{SID}"]', "e=>e.click()")
    time.sleep(1.0)
    pg.evaluate("()=>{ window.__cancelled=false; "
                "const of=window.fetch; window.fetch=function(u,o){"
                "  if(String(u).includes('/api/cancel')) window.__cancelled=true;"
                "  return of.apply(this,arguments); }; }")
    pg.fill("#input", f"正控 {POS_MARK} 这条应当真的发出去")
    time.sleep(0.3)
    pg.click("#sendBtn")
    time.sleep(1.5)
    pos = pg.evaluate("""(mark)=>({
        markInChat: document.querySelector('#chat').innerText.includes(mark),
        cancelled: window.__cancelled,
    })""", POS_MARK)
    ok("正控：正常按钮下，同一判据判为「没吞掉」（证明判据两个方向都能动）",
       pos["markInChat"] or pos["cancelled"],
       f"标志词进了聊天区={pos['markInChat']} 触发取消={pos['cancelled']} "
       f"→ 判为「没吞掉」={pos['markInChat'] or pos['cancelled']}")
    b.close()

st.delete(SID)
print("\n---- summary ----")
for l in log:
    print(l)
failed = sum(1 for l in log if l.startswith("FAIL"))
print("FAILED:", failed)
sys.exit(1 if failed else 0)
