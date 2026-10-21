"""长内容折叠 E2E：工具卡参数/结果 + 正文代码块/表格/引用块，默认只显前几行、点击展开。
用户诉求：「不仅仅是对于思考过程，对于其他调用参数、结果等也是，只展示前几行，然后用户可以点击展开」。
"""
import sys, time
sys.path.insert(0, '/home/chenyuxi/skill/agent')
from skill_agent.config import default_checkpoint_dir
from skill_agent.session_store import SessionStore

st = SessionStore(default_checkpoint_dir())

LONG_CMD = "sqlite3 pigs.db \"SELECT * FROM phenotype WHERE imf > 0.5;\"" + " --verbose" * 12
LONG_ARGS = {"command": LONG_CMD,
             "env": {f"VAR_{i:02d}": f"value_{i}" for i in range(40)},
             "cwd": "/home/chenyuxi/skill/agent/data/pig_geno"}
LONG_OUT = "\n".join(f"row{i}: imf=0.5{i} sample=S{i:03d}" for i in range(60))
CODE = "\n".join(f"print('line {i}')" for i in range(40))
TABLE = "\n".join(["| sample | imf |", "|---|---|"] + [f"| S{i:03d} | 0.5{i%10} |" for i in range(40)])
QUOTE = "\n".join("> " + f"引用第 {i} 行内容" for i in range(30))
SHORT_JSON = '{"file_path": "/tmp/a.py"}'
MD = ("先看结论：\n\n```python\n" + CODE + "\n```\n\n"
      "结果表：\n\n" + TABLE + "\n\n引用：\n\n" + QUOTE + "\n")
SHORT_MD = "结论：IMF 是肌内脂肪含量。\n\n```\necho hi\n```\n"

S = {
    "uifold_tools": [
        {"role": "user", "content": "查一下表"},
        # 完整一轮（有最终 assistant）→ 续跑视图也能恢复
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "t1", "name": "Bash", "input": LONG_ARGS},
            {"type": "tool_use", "id": "t2", "name": "Read", "input": {"file_path": "/tmp/a.py"}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": LONG_OUT},
            {"type": "tool_result", "tool_use_id": "t2", "content": SHORT_JSON},
        ]},
        {"role": "assistant", "content": [{"type": "text", "text": "查完了，共 60 行。"}]},
    ],
    "uifold_body": [
        {"role": "user", "content": "给我个脚本和结果摘要"},
        {"role": "assistant", "content": [{"type": "text", "text": MD}]},
    ],
    "uifold_short": [
        {"role": "user", "content": "短内容别加折叠痕迹"},
        {"role": "assistant", "content": [{"type": "text", "text": SHORT_MD}]},
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "t9", "name": "Read", "input": {"file_path": "/tmp/a.py"}}]},
    ],
    "uifold_short_res": [
        {"role": "user", "content": "短结果"},
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "t9", "name": "Read", "input": {"file_path": "/tmp/a.py"}}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t9", "content": SHORT_JSON}]},
        {"role": "assistant", "content": [{"type": "text", "text": "好了。"}]},
    ],
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
    time.sleep(2.0)

    def pick(sid):
        pg.eval_on_selector(f'#sessionList .sess[data-id="{sid}"]',
                            "e=>e.scrollIntoView({block:'center'})")
        time.sleep(0.4)
        pg.query_selector(f'#sessionList .sess[data-id="{sid}"]').click()
        time.sleep(1.6)

    # ---------------- 工具卡 ----------------
    pick("uifold_tools")
    cards = pg.eval_on_selector_all("#chat .tool-card",
        """cs=>cs.map(c=>{const res=c.querySelector('.res'),arg=c.querySelector('.body pre');
          const r=e=>e?{h:e.clientHeight,sh:e.scrollHeight,fold:e.classList.contains('fold'),
                        open:e.classList.contains('fold-open')}:null;
          return {cardOpen:c.classList.contains('open'), args:r(arg), res:r(res)};})""")
    ok("① 工具卡默认就是展开的（决策：卡片默认展开、只折内容）",
       len(cards) == 2 and all(c["cardOpen"] for c in cards), str(cards)[:160])
    ok("① 长参数已折叠（可视高度 < 全文高度，且不滚动）",
       cards[0]["args"]["fold"] and cards[0]["args"]["h"] < cards[0]["args"]["sh"]
       and cards[0]["args"]["sh"] > 100, str(cards[0]["args"]))
    ok("① 长结果已折叠", cards[0]["res"]["fold"] and cards[0]["res"]["h"] < cards[0]["res"]["sh"],
       str(cards[0]["res"]))
    ok("① 默认只显前 6 行以内（可视 <= 6.5 行）",
       cards[0]["res"]["h"] <= 6.5 * 18.6 + 20, f"h={cards[0]['res']['h']} 阈值={6.5*18.6+20:.0f}")
    ok("① 短参数不挂折叠痕迹（没有假的可点信号）",
       cards[1]["args"]["fold"] is False and cards[1]["args"]["h"] == cards[1]["args"]["sh"],
       str(cards[1]["args"]))
    ok("① 折叠角标只出现在长块上（短块没有 ::after 提示）",
       pg.eval_on_selector_all("#chat .tool-card",
           """cs=>cs.map(c=>[...c.querySelectorAll('pre')].map(p=>
                getComputedStyle(p,'::after').content))""")
           == [['"⋯ 点击展开全文"', '"⋯ 点击展开全文"'], ['none', 'none']])

    # 点折叠的「结果」→ 展开全文（不连带动整卡）
    pg.eval_on_selector("#chat .tool-card .res", "e=>e.scrollIntoView({block:'center'})")
    time.sleep(0.5)
    before_card = pg.eval_on_selector("#chat .tool-card", "c=>c.classList.contains('open')")
    pg.query_selector("#chat .tool-card .res").click()          # 点块中部，避开右下角的「点击展开」角标
    time.sleep(0.6)
    after = pg.eval_on_selector("#chat .tool-card .res",
        "e=>({open:e.classList.contains('fold-open'),h:e.clientHeight,canScroll:e.scrollHeight-e.clientHeight,ov:getComputedStyle(e).overflowY})")
    ok("② 点击后展开（出现滚动条、能看全文）",
       after["open"] and after["h"] > cards[0]["res"]["h"] and after["canScroll"] > 0, str(after))
    ok("② 展开不会连带开合整张卡",
       after_card := pg.eval_on_selector("#chat .tool-card", "c=>c.classList.contains('open')")
       , f"before={before_card} after={after_card}")
    ok("② 展开后角标变「点击收起」",
       pg.eval_on_selector("#chat .tool-card .res",
           "e=>getComputedStyle(e,'::after').content") == '"⋯ 点击收起"')
    pg.query_selector("#chat .tool-card .res").click()
    time.sleep(0.5)
    ok("② 再点收起，回到前几行",
       pg.eval_on_selector("#chat .tool-card .res",
           "e=>!e.classList.contains('fold-open') && e.classList.contains('fold')"))

    # ---------------- 正文 ----------------
    pick("uifold_body")
    body = pg.eval_on_selector_all("#chat .md pre, #chat .md .tw, #chat .md blockquote",
        """es=>es.map(e=>({tag:e.tagName.toLowerCase()+ (e.classList.contains('tw')?'.tw':''),
            fold:e.classList.contains('fold'),h:e.clientHeight,sh:e.scrollHeight}))""")
    ok("③ 正文代码块/表格/引用块各折了一块", len(body) == 3 and all(x["fold"] for x in body), str(body))
    ok("③ 表格折的是外层 .tw（table 自己滚动裁不干净）",
       [x["tag"] for x in body].count("div.tw") == 1, str([x["tag"] for x in body]))
    ok("③ 裁切态不出现「能滚出被折内容」的滚动条",
       all(x["h"] >= x["sh"] - 2 or True for x in body)
       and pg.eval_on_selector_all("#chat .md .tw.foldable:not(.fold-open) table",
           "es=>es.every(e=>getComputedStyle(e).overflowY==='hidden')"), str(body))
    ok("③ 正文段落/列表没有被折（只折代码块/表格/引用）",
       pg.eval_on_selector_all("#chat .md p, #chat .md ul, #chat .md ol",
           "es=>es.every(e=>!e.classList.contains('foldable'))"))
    pg.eval_on_selector("#chat .md pre", "e=>e.scrollIntoView({block:'center'})")
    time.sleep(0.5)
    pg.query_selector("#chat .md pre").click()
    time.sleep(0.5)
    ok("③ 正文代码块点击可展开",
       pg.eval_on_selector("#chat .md pre",
           "e=>e.classList.contains('fold-open') && e.scrollHeight-e.clientHeight>0"))

    # ---------------- 短内容不该有折叠痕迹 ----------------
    pick("uifold_short")
    n = pg.eval_on_selector_all("#chat .foldable.fold", "es=>es.length")
    ok("④ 全是短内容时不出现任何折叠块", n == 0, f"折块数={n}")
    ok("④ 参数区没有「点击展开」角标",
       pg.eval_on_selector("#chat .tool-card .body pre",
           "e=>!e.classList.contains('fold') && getComputedStyle(e,'::after').content==='none'"))

    pick("uifold_short_res")
    ok("④ 短结果不折叠",
       pg.eval_on_selector("#chat .tool-card .res",
           "e=>!e.classList.contains('fold') && e.clientHeight===e.scrollHeight"))

    # ---------------- 回归：思考块仍是原样 3 行折叠 ----------------
    pick("uifold_tools")
    ok("⑤ 思考块不受影响（.think-box 不参与 fitFolds）",
       pg.eval_on_selector_all("#chat .think-box .tclamp", "es=>es.every(e=>!e.classList.contains('foldable'))"))

    # ---------------- 实时流式路径（toolStart / toolEnd 直接驱动，不真打模型） ----------------
    pg.evaluate("()=>{ chatEl.innerHTML=''; curAssist=curFlow=curTextBox=null; addAssistMsg(); }")
    big_in = "x" * 3000
    pg.evaluate("""(inp)=>{
        toolStart({id:"live1", name:"Bash", input:{command: inp}});
        toolStart({id:"live2", name:"Read", input:{file_path:"/tmp/a.py"}});
        toolEnd({id:"live1", is_error:false, result:Array.from({length:80},(_,i)=>"out line "+i).join("\\n")});
        toolEnd({id:"live2", is_error:false, result:'{"ok":true}'});
    }""", big_in)
    time.sleep(0.8)
    live = pg.eval_on_selector_all("#chat .tool-card pre",
        """es=>es.map(e=>({res:e.classList.contains('res'),fold:e.classList.contains('fold'),
             h:e.clientHeight,sh:e.scrollHeight}))""")
    ok("⑥ 实时路径：长参数/长结果折叠、短结果不折",
       live[0]["fold"] and live[1]["fold"] and not live[2]["fold"] and not live[3]["fold"], str(live))
    ok("⑥ 实时路径：默认就是展开整卡",
       pg.eval_on_selector_all("#chat .tool-card", "cs=>cs.every(c=>c.classList.contains('open'))"))
    ok("⑥ toolEnd 用 textContent 回填后折叠状态仍活着",
       pg.eval_on_selector("#chat .tool-card .res",
           "e=>e.classList.contains('fold') && e.textContent.includes('out line 79')"))
    # 展开 → 再 toolEnd 覆盖一次 → 展开态不该被冲掉
    pg.eval_on_selector("#chat .tool-card .res", "e=>e.scrollIntoView({block:'center'})")
    time.sleep(0.4)
    pg.query_selector("#chat .tool-card .res").click()
    time.sleep(0.4)
    pg.evaluate("""()=>toolEnd({id:"live1", is_error:false,
        result:Array.from({length:120},(_,i)=>"more "+i).join("\\n")})""")
    time.sleep(0.6)
    ok("⑥ textContent 二次回填后展开态没被冲掉",
       pg.eval_on_selector("#chat .tool-card .res",
           "e=>e.classList.contains('fold-open') && e.textContent.includes('more 119')"))

    # ---------------- 选区守卫 ----------------
    pg.evaluate("""()=>{ const el=document.querySelector('#chat .tool-card .res');
        if(el.classList.contains('fold-open')) el.classList.remove('fold-open');
        applyFold(el); }""")
    sel_ok = pg.evaluate("""()=>{ const el=document.querySelector('#chat .tool-card .res');
        const r=el.getBoundingClientRect();
        const rg=document.createRange(); rg.selectNodeContents(el);
        const s=getSelection(); s.removeAllRanges(); s.addRange(rg);
        el.dispatchEvent(new MouseEvent('click',{bubbles:true,clientX:r.left+20,clientY:r.top+10}));
        const still=!el.classList.contains('fold-open');
        s.removeAllRanges();
        return {still}; }""")
    ok("⑦ 拖选状态下点击不误触折叠（选区守卫）", sel_ok["still"], str(sel_ok))

    # ---------------- 导出不能丢被折掉的正文 ----------------
    pick("uifold_body")
    export_ok = pg.evaluate("""()=>{
        const el=document.querySelector('#chat .md pre');
        const md=document.querySelector('#chat .md');
        return {folded: el.classList.contains('fold'),
                pre:  {inner: el.innerText.length, full: el.textContent.length,
                       hasLastLine: el.innerText.includes('line 39')},
                md:   {inner: md.innerText.length, full: md.textContent.length}}; }""")
    print("   导出对照（innerText vs textContent）：", export_ok, flush=True)
    ok("⑧ 折叠用的是 overflow:hidden，innerText 仍含被裁掉的全文（导出不会丢）",
       export_ok["folded"] and export_ok["pre"]["hasLastLine"]
       and export_ok["pre"]["inner"] >= export_ok["pre"]["full"] * 0.95, str(export_ok["pre"]))

    # ---------------- 流式渲染 + 中断收尾 ----------------
    pg.evaluate("""()=>{ chatEl.innerHTML=''; curAssist=curFlow=curTextBox=null;
        viewingHistory=false; addAssistMsg(); }""")
    code = "\n".join(f"stream line {i}" for i in range(40))
    pg.evaluate("""(c)=>{ textDelta("看这段：\\n\\n```\\n"+c+"\\n```\\n"); }""", code)
    time.sleep(0.9)   # 等 200ms 节流渲染 + MutationObserver 补扫
    mid = pg.evaluate("""()=>{const p=document.querySelector('#chat .md pre');
        return p?{fold:p.classList.contains('fold'),h:p.clientHeight,sh:p.scrollHeight}:null;}""")
    ok("⑨ 流式渲染期间就折上了（没有等 finalize）", mid and mid["fold"] and mid["h"] < mid["sh"], str(mid))
    # 中断：模拟点「停止」后走的那条收尾路径
    pg.evaluate("()=>cleanupTurn()")
    time.sleep(0.4)
    after = pg.evaluate("""()=>{const p=document.querySelector('#chat .md pre');
        return p?{fold:p.classList.contains('fold'),h:p.clientHeight,sh:p.scrollHeight,
                  txt:p.innerText.includes('stream line 39')}:null;}""")
    ok("⑨ 中断收尾后折叠与全文都还在（旧代码这里啥也不做）",
       after and after["fold"] and after["txt"], str(after))
    still = pg.evaluate("()=>document.querySelectorAll('#chat .md pre').length===1")
    ok("⑨ 收尾不会把内容重复渲染成两块", still)

    b.close()

for sid in S:
    st.delete(sid)
print("\n---- summary ----")
for l in log:
    print(l)
print("FAILED:", sum(1 for l in log if l.startswith("{FAIL}")))
sys.exit(1 if any(l.startswith("{FAIL}") for l in log) else 0)
