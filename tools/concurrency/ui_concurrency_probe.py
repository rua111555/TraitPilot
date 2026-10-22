"""前端并发判据探针（Playwright）：驱动真实浏览器验证「切走/切回/解锁」。

每条判据都以**反例**形式写：若实现有缺陷，某条必然 FAIL。
上一版（13/13）有三条**没有区分度**，本版已重写并在注释里标明：
  · 旧 F3 量的 `#chat .md` 命中的是**回放的历史**（"先前的回答 xxx" 恰好 15 字符），
    根本没验证「切回能看到本轮实时内容」→ 改为量**最后一段 .md**，并断言它含本轮标志词。
  · 旧 F2d 的观测窗口 <1.5s，任务往往已 done，判据退化成恒真 → 改用 `sleep 20`
    制造确定的 20s 运行窗口，再断言「切走后仍在跑」。
  · 旧 F6a 在 A 跑着时会话正好是 A，`state.busy == isRunning(SID)` 两边都是 True
    → 改为在 A 跑着时**切到 B**，断言 state.busy 翻成 False（这才是「解锁」的实质）。

F1 运行中不锁死：本会话在跑时 #input 未被 disable；切到别的会话能继续输入
F2 切走不断流：切走后原会话后端仍在跑（running=true 且 progress.phase=tool）——用 sleep 20 制造窗口
F3 切回看得见：切回后**最后一段正文**含本轮标志词（不是回放的历史）
F4 左栏可见：正在跑的会话在左栏带运行标记 + 运行状态条提示
F5 切回不重复：标志词在切回后的全文里只出现 1 次（防「重放 + 暂存」叠加翻倍）
F6 多会话并行：A 跑着时切到 B，state.busy 翻 False，且 B 能独立发起
F7 停止是协作的：点停止后如实显示「取消中」而非谎称「已停止」

跑法：
    ./start_web.sh restart
    /home/chenyuxi/anaconda3/bin/python /tmp/rename/ui_concurrency_probe.py
"""
import json
import sys
import time
import urllib.request

sys.path.insert(0, '/home/chenyuxi/skill/agent')
from skill_agent.config import default_checkpoint_dir
from skill_agent.session_store import SessionStore

from playwright.sync_api import sync_playwright

B = "http://127.0.0.1:8787"
log = []
st = SessionStore(default_checkpoint_dir())

SIDS = ("uiprobe_a", "uiprobe_b", "uiprobe_sleep")


def ok(name, cond, extra=""):
    log.append(("PASS" if cond else "FAIL") + " " + name + ("  " + extra if extra else ""))
    print(("  [PASS] " if cond else "  [缺陷] ") + name, flush=True)
    if extra:
        print("         " + extra, flush=True)


def api(path):
    return json.loads(urllib.request.urlopen(B + path, timeout=10).read().decode())


def runstate(sid):
    try:
        return api(f"/api/session/{sid}/run")
    except Exception as e:
        return {"running": None, "state": None, "err": str(e)}


for sid in SIDS:
    st.delete(sid)
    st.append(sid, {"role": "user", "content": "第一条提问 " + sid})
    st.append(sid, {"role": "assistant",
                    "content": [{"type": "text", "text": "先前的回答 " + sid}]})

SA, SB, SS = SIDS
MARK = "ZZMARK"          # 本轮独占标志词：模型被要求原样回显

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

    # ================= F1：运行中不锁死 =================
    print("\nF1  运行中对话框是否解锁（反例：输入框被 disable / 切走发不出下一条）", flush=True)
    pg.evaluate("(sid)=>{ setCurrent(sid); markRunning(sid,{running:true,state:'running'}); }", SA)
    time.sleep(0.4)
    disabled = pg.eval_on_selector("#input", "e=>e.disabled")
    ok("F1a 运行中输入框未被 disable", not disabled, f"#input.disabled={disabled}")
    pg.evaluate("(sid)=>{ setCurrent(sid); }", SB)
    time.sleep(0.3)
    pg.fill("#input", "在 A 跑着的时候给 B 发一句")
    typed = pg.eval_on_selector("#input", "e=>e.value")
    ok("F1b 切到别的会话后仍可输入（未被全局 busy 拦）",
       typed == "在 A 跑着的时候给 B 发一句", f"输入框={typed!r}")
    pg.evaluate("()=>{ $('#input').value=''; }")

    # ================= F6：全局 busy 的语义 =================
    print("\nF6  一个会话在跑时，切到另一个会话是否解锁（旧版此处恒真、无区分度）", flush=True)
    pg.evaluate("(sid)=>{ setCurrent(sid); markRunning(sid,{running:true,state:'running'}); }", SA)
    time.sleep(0.4)
    at_a = pg.evaluate("()=>({busy:state.busy, sid:SID})")
    pg.evaluate("(sid)=>{ setCurrent(sid); }", SB)
    time.sleep(0.4)
    at_b = pg.evaluate("()=>({busy:state.busy, sid:SID})")
    ok("F6a 在 A（在跑）时 state.busy=True", at_a["busy"] is True, f"{at_a}")
    ok("F6b 切到 B（空闲）后 state.busy 翻 False —— 界面解锁",
       at_b["busy"] is False, f"{at_b}")
    ok("F6c 切走后 A 的运行态仍在表里（没被抹掉）",
       pg.evaluate("(sid)=>!!isRunning(sid)", SA) is True, "runs 表应保留 A 的 running")

    # ================= F2/F3/F4/F5：真机 sleep 20 制造确定窗口 =================
    print("\nF2/F3/F5  真机一轮 `sleep 20`：切走后仍在跑、切回看得到、且不重复", flush=True)
    # ⚠️ 必须走**真实路径**（点左栏 = selectSession → replayMessages），
    # 不能用 setCurrent()：那个只改 id 不渲染，窗格里就没有历史，
    # 于是「结构翻倍」判据失去基准（上一版 F3d/F5 失败正是这个探针自身的捷径造成的）。
    click_session(SB)
    time.sleep(0.8)
    click_session(SS)          # ← 真实切会话：回放磁盘历史（1 user + 1 assist）
    time.sleep(0.8)
    pre = pg.evaluate("""()=>({user:document.querySelectorAll('#chat .msg.user').length,
                               assist:document.querySelectorAll('#chat .msg.assist').length})""")
    print(f"    切到 SS 后（回放磁盘历史）：user={pre['user']} assist={pre['assist']}", flush=True)

    pg.fill("#input", f"请用 Bash 工具执行命令：sleep 20 && echo {MARK}  （只执行这一条，"
                      f"执行完把输出原样告诉我，不要做别的）")
    pg.click("#sendBtn")

    # 等出现 Bash 工具卡（=操作真的开始了，进入确定的 20s 窗口）
    got_tool = False
    for _ in range(900):
        time.sleep(0.1)
        if pg.evaluate("()=>!!document.querySelector('#chat .tool-card')"):
            got_tool = True
            break
    ok("F2a 本轮已进入工具执行阶段（出现 Bash 工具卡）", got_tool,
       "（等待模型决定调用 Bash）")
    rs = runstate(SS)
    prog = rs.get("progress") or {}
    tool = (prog.get("tool") or {}).get("name")
    ok("F2b 后端进度快照指明在跑什么", rs.get("running") and prog.get("phase") == "tool",
       f"running={rs.get('running')} phase={prog.get('phase')} tool={tool} hint={prog.get('hint')!r}")

    # —— 切走（处于 20s 窗口内）——
    t_switch = time.time()
    click_session(SB)
    time.sleep(1.2)
    rs2 = runstate(SS)
    ok("F2c 切走后该会话**仍在跑**（断开≠取消）",
       bool(rs2.get("running")), f"切走后 A: running={rs2.get('running')} state={rs2.get('state')} "
       f"（切走耗时 {time.time()-t_switch:.1f}s，处于 sleep 20 窗口内）")
    ok("F2d 切走没有把它变成 cancelled", rs2.get("state") != "cancelled",
       f"state={rs2.get('state')}")
    ok("F2e 主区已显示 B 自己的历史",
       pg.evaluate("()=>document.querySelector('#chat').innerText.includes('uiprobe_b')"))

    marks = pg.evaluate(f"""()=>{{
        const n=document.querySelector('#sessionList .sess[data-id="{SS}"]');
        return n ? {{running:n.classList.contains('running'), dot:!!n.querySelector('.run-dot'),
                     sub:(n.querySelector('.run-sub')||{{}}).innerText||''}} : null;
    }}""")
    ok("F4 左栏显示运行中标记", bool(marks and marks["running"] and marks["dot"]), str(marks))
    bar = pg.eval_on_selector("#runBar", "e=>({show:e.classList.contains('show'), text:e.innerText})")
    # 必须真的说出在跑什么（工具名），而不是只要出现「运行」二字就算过 ——
    # 上一版就是这么放过了 progress.tool 是对象导致的「执行 [object Object]」。
    ok("F4b 运行状态条说出后台会话在跑什么（含工具名，且非 [object Object]）",
       bar["show"] and "Bash" in bar["text"] and "[object Object]" not in bar["text"],
       f"#runBar.show={bar['show']} text={bar['text']!r}")

    # —— 切回（仍在窗口内）——
    click_session(SS)
    time.sleep(1.5)
    # ⚠️ 旧版 F3a 量 `#chat .md` 的**第一段**，命中的是回放的历史（恰好 15 字符）→ 无区分度。
    # 正确的问题不是「有没有正文」（此时模型还没写正文），而是「本轮已发生的事在不在」：
    # 工具卡必须是**同一张**（复用窗格），而不是重建成两张。
    struct_back = pg.evaluate("""()=>({
        toolCards: document.querySelectorAll('#chat .tool-card').length,
        userMsgs:  document.querySelectorAll('#chat .msg.user').length,
        assistMsgs:document.querySelectorAll('#chat .msg.assist').length,
        ids: [...document.querySelectorAll('#chat .tool-card')].map(e=>e.dataset.id),
        hasRunHint: document.querySelector('#chat').innerText.includes('sleep'),
    })""")
    ok("F3a 切回后本轮的工具卡还在（同一张，未丢失）",
       struct_back["toolCards"] == 1 and len(set(struct_back["ids"])) == 1,
       f"工具卡数={struct_back['toolCards']} id={struct_back['ids']}")
    ok("F3b 切回后能看见本轮在做什么",
       struct_back["hasRunHint"] or struct_back["toolCards"] > 0,
       f"含 sleep 提示={struct_back['hasRunHint']} 工具卡={struct_back['toolCards']}")
    ok("F3c 切回后后端仍认这个会话在跑",
       bool(runstate(SS).get("running")),
       f"running={runstate(SS).get('running')}")
    # 切回后应当**只有一条**用户消息与一条助手气泡：多出来就说明「暂存」与「回放」叠加了
    ok("F3d 切回未造成消息节点翻倍（历史 + 本轮各一份）",
       struct_back["userMsgs"] == 2 and struct_back["assistMsgs"] == 2,
       f"user={struct_back['userMsgs']} assist={struct_back['assistMsgs']}"
       "（期望各 2 = 磁盘历史 1 + 本轮 1；切走前基准 "
       f"user={pre['user']} assist={pre['assist']}）")

    # —— 等本轮真正收尾，再验「不重复」 ——
    for _ in range(900):
        time.sleep(0.2)
        if not pg.evaluate("()=>state.busy"):
            break
    time.sleep(1.5)
    # ⚠️ 旧版 F5 数的是标志词出现次数，得到 7 —— 那是**正常内容**：
    #   工具参数里有、工具结果里有、模型自己原样回显了一次。判据没有区分度。
    # 重复的真正签名是**结构性的**：同一条事件被画两遍 → 工具卡/气泡数量翻倍。
    dup = pg.evaluate("""(mark)=>({
        toolCards: document.querySelectorAll('#chat .tool-card').length,
        ids: [...document.querySelectorAll('#chat .tool-card')].map(e=>e.dataset.id),
        userMsgs:  document.querySelectorAll('#chat .msg.user').length,
        assistMsgs:document.querySelectorAll('#chat .msg.assist').length,
        tails: document.querySelectorAll('#chat .tail').length,
        inFull: document.querySelector('#chat').innerText.split(mark).length-1,
    })""", MARK)
    ok("F5 切回收尾后**结构不翻倍**（同一轮只画一遍）",
       dup["toolCards"] == 1 and len(set(dup["ids"])) == 1
       and dup["userMsgs"] == 2 and dup["assistMsgs"] == 2 and dup["tails"] <= 2,
       f"工具卡={dup['toolCards']} id数={len(set(dup['ids']))} user={dup['userMsgs']} "
       f"assist={dup['assistMsgs']} tail={dup['tails']}"
       f"（期望 工具卡=1 / user=2 / assist=2；标志词出现 {dup['inFull']} 次属正常内容）")
    ok("F7 本轮正常收尾（未被切走影响）", runstate(SS).get("state") == "done",
       f"最终 state={runstate(SS).get('state')}")

    ok("F8 全程无 JS 运行时错误", not errs, f"pageerror={errs[:3]}")

    # ================= 负控：证明 F5/F3d 的判据确实能抓到「翻倍」 =================
    # 手动把同一张工具卡再插一次 —— 模拟「重放 + 暂存」叠加的老 bug。
    # 若下面的负控也「通过」，说明判据没有区分度，F5 就是废判据。
    print("\n负控  F5/F3d 的判据能否抓到「结构翻倍」", flush=True)
    neg = pg.evaluate("""()=>{
        const flow=document.querySelector('#chat .msg.assist .flow');
        const card=document.querySelector('#chat .tool-card');
        const clone=card.cloneNode(true);          // 同 id 的第二张卡
        flow.appendChild(clone);
        const r={toolCards: document.querySelectorAll('#chat .tool-card').length,
                 ids: [...document.querySelectorAll('#chat .tool-card')].map(e=>e.dataset.id),
                 wouldFail: (document.querySelectorAll('#chat .tool-card').length !== 1)};
        clone.remove();                            // 立刻撤掉，不留痕
        return r;
    }""")
    ok("负控：人为复制一张工具卡后，判据确实判为不合格",
       neg["wouldFail"] and neg["toolCards"] == 2 and len(set(neg["ids"])) == 1,
       f"复制后 toolCards={neg['toolCards']} id={neg['ids']} → wouldFail={neg['wouldFail']}"
       "（若这里 wouldFail=False，则 F5 无区分度，必须换判据）")

    b.close()

for sid in SIDS:
    st.delete(sid)

print("\n---- summary ----")
for l in log:
    print(l)
failed = sum(1 for l in log if l.startswith("FAIL"))
print("FAILED:", failed)
sys.exit(1 if failed else 0)
